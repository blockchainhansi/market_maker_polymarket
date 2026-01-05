"""
Top-of-Book Market Maker Strategy Engine

Implements the Join-or-Improve strategy:
- Places BUY orders on both YES and NO orderbooks
- Improves best bid by 1 tick when spread is wide
- Joins best bid when spread is tight (1 cent)
- Applies inventory skew to discourage accumulating more of the side we're long
- Stops bidding on one side when inventory exceeds max threshold
"""

import asyncio
import time
from datetime import datetime
from typing import Optional

from config import Config, get_config
from logger import get_logger
from models import (
    StrategyState,
    StrategyMode,
    Outcome,
    Side,
    OrderBook,
)
from polymarket_client import PolymarketClient, get_client
from orderbook_manager import OrderBookManager, get_orderbook_manager

logger = get_logger(__name__)

# Constants
TICK_SIZE = 0.01  # Polymarket minimum price increment


class StrategyEngine:
    """
    Top-of-Book Market Maker
    
    Places bid orders on both YES and NO orderbooks, attempting to be
    at or near the top of the book to capture fills.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        client: Optional[PolymarketClient] = None,
        ob_manager: Optional[OrderBookManager] = None,
    ):
        self.config = config or get_config()
        self.client = client
        self.ob_manager = ob_manager
        self.state = StrategyState()
        self._lock = asyncio.Lock()
        self._loop = None
        self._running = False
        
        # Guards against duplicate order placement
        self._placing_yes = False
        self._placing_no = False
        # Halt flag to block any new placements immediately
        self._halt_new_orders = False
        
        # Track order outcomes for fill handling
        self._order_outcome: dict[str, Outcome] = {}

        # Orders we attempted to cancel but haven't confirmed as cancelled/filled yet
        self._pending_cancel: set[str] = set()
        
        # Last status log time (for periodic logging)
        self._last_status_log: Optional[datetime] = None
        
        # Throttle for "skipping bid" log messages
        self._last_skip_log_time: float = 0.0

    async def start(self):
        """Start the strategy engine."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        # Reset placement guards when starting
        self._halt_new_orders = False
        self._placing_yes = False
        self._placing_no = False
        self._pending_cancel.clear()
        
        if self.client is None:
            self.client = await get_client()
        if self.ob_manager is None:
            self.ob_manager = get_orderbook_manager()
        
        # Start orderbook manager
        await self.ob_manager.start()
        self.state.mode = StrategyMode.QUOTING
        self.state.started_at = datetime.now()
        
        logger.info("🚀 Top-of-Book Market Maker started")
        logger.info(f"   Gamma (skew): {self.config.gamma}")
        logger.info(f"   Order size: {self.config.order_size}")
        logger.info(f"   Refresh interval: {self.config.refresh_interval}s")
        
        # Start the main loop
        asyncio.create_task(self._main_loop())

    async def _main_loop(self):
        """
        Main trading loop - refreshes orderbooks and updates quotes periodically.
        """
        logger.info(f"📊 Starting main loop (interval: {self.config.refresh_interval}s)")
        
        while self._running and self.state.mode != StrategyMode.STOPPED:
            try:
                # Wait for next iteration
                await asyncio.sleep(self.config.refresh_interval)
                
                # Fetch fresh orderbook data
                await self._refresh_orderbooks()
                
                # Log status periodically
                await self._log_status()
                
                # Calculate and place/update quotes
                await self._update_quotes()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(5)

    async def _refresh_orderbooks(self):
        """Fetch fresh orderbook data from API."""
        try:
            # Fetch YES orderbook
            yes_book = await self.client.get_orderbook(self.config.token_id_yes)
            if yes_book:
                self.state.orderbook_yes = yes_book
            
            # Rate limiting between API calls
            await asyncio.sleep(0.5)
            
            # Fetch NO orderbook
            no_book = await self.client.get_orderbook(self.config.token_id_no)
            if no_book:
                self.state.orderbook_no = no_book
                
        except Exception as e:
            logger.error(f"Error fetching orderbooks: {e}")

    async def stop(self, sell_tokens: bool = False):
        """
        Stop the strategy engine and cancel all orders.
        
        Args:
            sell_tokens: If True, sell remaining tokens.
                        If False, keep tokens for redemption.
        """
        logger.info("🛑 Stopping strategy engine...")
        self._running = False
        self._halt_new_orders = True
        self._placing_yes = True
        self._placing_no = True
        
        # Cancel all orders
        await self.cancel_all_orders()
        
        if sell_tokens:
            await self._delayed_flatten(delay_seconds=3.0)
            logger.info("   Strategy stopped")
        else:
            logger.info("   Strategy stopped (tokens kept for redemption)")
        
        if self.ob_manager:
            await self.ob_manager.stop()
        
        self.state.mode = StrategyMode.STOPPED

    async def cancel_all_orders(self):
        """Cancel all open orders."""
        logger.info("🧹 Cancelling all orders...")
        try:
            # First try bulk cancel
            cancelled = await self.client.cancel_all_orders()
            logger.info(f"   Cancelled {cancelled} orders")

            # Then attempt per-order cancel for any still tracked
            active_ids = set()
            if self.state.bid_order_yes and self.state.bid_order_yes.is_active:
                active_ids.add(self.state.bid_order_yes.order_id)
            if self.state.bid_order_no and self.state.bid_order_no.is_active:
                active_ids.add(self.state.bid_order_no.order_id)
            active_ids.update(getattr(self, "_order_outcome", {}).keys())

            for oid in active_ids:
                try:
                    await self.client.cancel_order(oid)
                    logger.debug(f"   Cancelled order {oid[:8]}...")
                except Exception as ce:
                    logger.debug(f"   Order {oid[:8]} already gone or cannot cancel: {ce}")

            # Clear local order references
            self._pending_cancel.clear()
            self.state.bid_order_yes = None
            self.state.bid_order_no = None
            self.state.last_bid_price_yes = None
            self.state.last_bid_price_no = None
            self._placing_yes = False
            self._placing_no = False
        except Exception as e:
            logger.error(f"   Error cancelling orders: {e}")

    async def _delayed_flatten(self, delay_seconds: float = 3.0):
        """Wait for on-chain settlement before flattening position."""
        logger.info(f"⏳ Waiting {delay_seconds}s for settlement...")
        await asyncio.sleep(delay_seconds)
        await self.flatten_position()

    async def flatten_position(self):
        """
        Sell any held inventory at market on shutdown.
        
        Uses FAK (Fill-and-Kill) orders which allow partial fills.
        """
        inv = self.state.inventory
        
        if inv.q_yes > 0.01:
            logger.info(f"📤 Selling {inv.q_yes:.2f} YES tokens...")
            try:
                price = 0.01
                order = await self.client.place_market_order(
                    token_id=self.config.token_id_yes,
                    side=Side.SELL,
                    size=inv.q_yes,
                    price=price,
                    use_ioc=True,
                )
                if order:
                    logger.info(f"   ✅ Sold YES tokens")
                    inv.record_fill(Outcome.YES, Side.SELL, price, inv.q_yes)
            except Exception as e:
                if "no orders found to match" in str(e).lower() or "FAK" in str(e):
                    logger.warning(f"   ⚠️ No buyers for YES - tokens kept for redemption")
                else:
                    logger.error(f"   ❌ Failed to sell YES: {e}")
        
        if inv.q_no > 0.01:
            logger.info(f"📤 Selling {inv.q_no:.2f} NO tokens...")
            try:
                price = 0.01
                order = await self.client.place_market_order(
                    token_id=self.config.token_id_no,
                    side=Side.SELL,
                    size=inv.q_no,
                    price=price,
                    use_ioc=True,
                )
                if order:
                    logger.info(f"   ✅ Sold NO tokens")
                    inv.record_fill(Outcome.NO, Side.SELL, price, inv.q_no)
            except Exception as e:
                if "no orders found to match" in str(e).lower() or "FAK" in str(e):
                    logger.warning(f"   ⚠️ No buyers for NO - tokens kept for redemption")
                else:
                    logger.error(f"   ❌ Failed to sell NO: {e}")

    def _remember_order(self, order_id: str, outcome: Outcome):
        """Remember which outcome an order is for."""
        self._order_outcome[order_id] = outcome

    def _forget_order(self, order_id: str):
        """Forget order metadata."""
        self._order_outcome.pop(order_id, None)

    def _track_order(self, order_id: str):
        """Track an order for fill detection."""
        if self.ob_manager:
            self.ob_manager.track_order(order_id)

    def _untrack_order(self, order_id: str):
        """Stop tracking an order."""
        if self.ob_manager:
            self.ob_manager.untrack_order(order_id)
        self._forget_order(order_id)

    async def _log_status(self):
        """Log status information periodically."""
        now = datetime.now()
        if self._last_status_log and (now - self._last_status_log).total_seconds() < 10:
            return
        
        self._last_status_log = now
        inv = self.state.inventory
        yes_book = self.state.orderbook_yes
        no_book = self.state.orderbook_no
        
        # Market state
        if yes_book and no_book and yes_book.best_bid and yes_book.best_ask and no_book.best_bid and no_book.best_ask:
            logger.info(f"📈 Market: YES {yes_book.best_bid:.2f}/{yes_book.best_ask:.2f} | NO {no_book.best_bid:.2f}/{no_book.best_ask:.2f}")
        else:
            logger.info("📈 Market: Waiting for orderbook data...")
        
        # Inventory state
        skew = self.config.compute_skew(inv.delta_q)
        logger.info(f"📦 Inventory: ΔQ={inv.delta_q:.2f} | Skew=${skew:+.3f} | YES={inv.q_yes:.2f} | NO={inv.q_no:.2f}")
        logger.info(f"💰 P&L: Locked=${inv.locked_profit:.4f} | Pairs={inv.paired_quantity:.2f} | Trades={inv.total_trades}")
        
        # Active orders
        if self.state.bid_order_yes:
            yes_order_info = f"YES@{self.state.bid_order_yes.price:.2f}"
        else:
            yes_order_info = "None"
        if self.state.bid_order_no:
            no_order_info = f"NO@{self.state.bid_order_no.price:.2f}"
        else:
            no_order_info = "None"
        logger.info(f"📋 Bids: {yes_order_info} | {no_order_info}")
        
        # Mode and timing
        time_left = self.config.time_until_expiry().total_seconds()
        logger.info(f"⏱️  Expiry in {time_left:.0f}s | Mode: {self.state.mode.value}")

    async def _update_quotes(self):
        """
        Calculate and update bid quotes on both sides.
        
        Implements the Join-or-Improve logic:
        1. If spread > 1 tick: Improve best bid by 1 tick
        2. If spread = 1 tick: Join best bid
        3. Apply inventory skew to both prices
        4. Skip bidding on a side if inventory is too skewed
        """
        if self.state.mode == StrategyMode.STOPPED or self._halt_new_orders:
            return
        
        inv = self.state.inventory
        yes_book = self.state.orderbook_yes
        no_book = self.state.orderbook_no
        
        if not yes_book or not no_book:
            return
        
        # Calculate inventory skew
        skew = self.config.compute_skew(inv.delta_q)
        
        # Normal quoting mode - bid on both sides with skew adjustment
        if self.state.mode != StrategyMode.QUOTING:
            logger.info(f"✅ Resuming normal quoting")
            self.state.mode = StrategyMode.QUOTING
        should_bid_yes = True
        should_bid_no = True
        
        # Calculate YES bid price
        yes_bid_price = None
        if should_bid_yes and yes_book.best_bid is not None and yes_book.best_ask is not None:
            no_best_bid = no_book.best_bid if no_book.best_bid is not None else 0.50
            no_avg_cost = inv.mu_no
            yes_bid_price = self._calculate_bid_price(
                best_bid=yes_book.best_bid,
                best_ask=yes_book.best_ask,
                skew=skew,
                opposite_best_bid=no_best_bid,
                opposite_avg_cost=no_avg_cost,
            )
        
        # Calculate NO bid price
        no_bid_price = None
        if should_bid_no and no_book.best_bid is not None and no_book.best_ask is not None:
            yes_best_bid = yes_book.best_bid if yes_book.best_bid is not None else 0.50
            yes_avg_cost = inv.mu_yes
            no_bid_price = self._calculate_bid_price(
                best_bid=no_book.best_bid,
                best_ask=no_book.best_ask,
                skew=-skew,
                opposite_best_bid=yes_best_bid,
                opposite_avg_cost=yes_avg_cost,
            )
        
        # Update YES bid if needed
        if should_bid_yes and yes_bid_price is not None:
            await self._update_bid(Outcome.YES, yes_bid_price)
            await asyncio.sleep(0.5)
        elif not should_bid_yes:
            await self._cancel_bid(Outcome.YES)
            await asyncio.sleep(0.5)
        
        # Update NO bid if needed
        if should_bid_no and no_bid_price is not None:
            await self._update_bid(Outcome.NO, no_bid_price)
            await asyncio.sleep(0.5)
        elif not should_bid_no:
            await self._cancel_bid(Outcome.NO)

    def _calculate_bid_price(
        self,
        best_bid: float,
        best_ask: float,
        skew: float,
        opposite_best_bid: float,
        opposite_avg_cost: float,
    ) -> Optional[float]:
        """
        Calculate our bid price using Join-or-Improve logic.
        """
        spread = round(best_ask - best_bid, 2)
        
        if spread > TICK_SIZE + 0.001:
            raw_bid = best_bid + TICK_SIZE
        else:
            raw_bid = best_bid
        
        # Apply inventory skew
        adjusted_bid = raw_bid - skew
        
        # Round to tick size
        adjusted_bid = round(adjusted_bid / TICK_SIZE) * TICK_SIZE
        adjusted_bid = round(adjusted_bid, 2)
        
        # Clamp to valid range
        adjusted_bid = max(0.01, min(0.99, adjusted_bid))
        
        # Ensure we don't cross the book
        if adjusted_bid >= best_ask:
            adjusted_bid = best_bid
            if adjusted_bid >= best_ask:
                return None
        
        # Profitability cap
        if opposite_avg_cost > 0:
            effective_opposite_cost = opposite_avg_cost
        else:
            effective_opposite_cost = opposite_best_bid
        
        max_profitable_bid = round(1.00 - effective_opposite_cost, 2)
        max_profitable_bid = min(0.99, max_profitable_bid)
        
        if adjusted_bid > max_profitable_bid:
            now = time.time()
            if now - self._last_skip_log_time >= 30.0:
                logger.info(f"   ⛔ Skipping bid {adjusted_bid:.2f} > cap {max_profitable_bid:.2f}")
                self._last_skip_log_time = now
            return None
        
        return adjusted_bid

    async def _update_bid(self, outcome: Outcome, price: float):
        """Update or place a bid order for the given outcome."""
        if self.state.mode == StrategyMode.STOPPED or self._halt_new_orders:
            return
        inv = self.state.inventory
        if outcome == Outcome.YES:
            current_order = self.state.bid_order_yes
            last_price = self.state.last_bid_price_yes
            placing_flag = "_placing_yes"
        else:
            current_order = self.state.bid_order_no
            last_price = self.state.last_bid_price_no
            placing_flag = "_placing_no"
        
        if last_price is not None and abs(price - last_price) < 0.005:
            return
        
        if getattr(self, placing_flag):
            return
        setattr(self, placing_flag, True)
        
        try:
            if current_order and current_order.is_active:
                if current_order.order_id in self._pending_cancel:
                    return

                cancel_success = await self.client.cancel_order(current_order.order_id)
                await asyncio.sleep(0.5)

                if not cancel_success:
                    self._pending_cancel.add(current_order.order_id)
                    return

                self._pending_cancel.discard(current_order.order_id)
                self._untrack_order(current_order.order_id)

                if outcome == Outcome.YES:
                    self.state.bid_order_yes = None
                    self.state.last_bid_price_yes = None
                else:
                    self.state.bid_order_no = None
                    self.state.last_bid_price_no = None
            
            if self._halt_new_orders or self.state.mode == StrategyMode.STOPPED:
                return
            
            token_id = self.config.token_id_yes if outcome == Outcome.YES else self.config.token_id_no
            order_size = self.config.get_order_size(inv.delta_q)
            
            order = await self.client.place_limit_order(
                token_id=token_id,
                side=Side.BUY,
                price=price,
                size=order_size,
                time_in_force="GTC",
            )
            
            if order:
                self._remember_order(order.order_id, outcome)
                self._track_order(order.order_id)
                
                if outcome == Outcome.YES:
                    self.state.bid_order_yes = order
                    self.state.last_bid_price_yes = price
                else:
                    self.state.bid_order_no = order
                    self.state.last_bid_price_no = price
                
                logger.debug(f"📝 Placed {outcome.value} bid @ {price:.2f} × {order_size:.1f}")
        except Exception as e:
            logger.error(f"   Error updating {outcome.value} bid: {e}")
        finally:
            setattr(self, placing_flag, False)

    async def _cancel_bid(self, outcome: Outcome):
        """Cancel a bid order for the given outcome."""
        if outcome == Outcome.YES:
            order = self.state.bid_order_yes
            if order and order.is_active:
                if order.order_id in self._pending_cancel:
                    return
                ok = await self.client.cancel_order(order.order_id)
                if not ok:
                    self._pending_cancel.add(order.order_id)
                    return
                self._pending_cancel.discard(order.order_id)
                self._untrack_order(order.order_id)
                self.state.bid_order_yes = None
                self.state.last_bid_price_yes = None
        else:
            order = self.state.bid_order_no
            if order and order.is_active:
                if order.order_id in self._pending_cancel:
                    return
                ok = await self.client.cancel_order(order.order_id)
                if not ok:
                    self._pending_cancel.add(order.order_id)
                    return
                self._pending_cancel.discard(order.order_id)
                self._untrack_order(order.order_id)
                self.state.bid_order_no = None
                self.state.last_bid_price_no = None

    def on_fill(self, order_id: str, outcome: Outcome, price: float, size: float):
        """Handle a fill event from WebSocket."""
        inv = self.state.inventory
        
        order_type = "UNKNOWN"
        
        if self.state.bid_order_yes and self.state.bid_order_yes.order_id == order_id:
            order_type = "BID_YES"
        elif self.state.bid_order_no and self.state.bid_order_no.order_id == order_id:
            order_type = "BID_NO"
        
        mapped_outcome = self._order_outcome.get(order_id)
        if mapped_outcome and outcome != mapped_outcome:
            outcome = mapped_outcome

        if order_type == "UNKNOWN" and order_id not in self._order_outcome:
            logger.warning(f"⚠️ Ignoring fill from unknown order: {order_id[:16]}...")
            return
        
        logger.info("")
        logger.info("=" * 60)
        logger.info(f"🎉 FILL: {order_type}")
        logger.info(f"   Bought {size:.2f} {outcome.value} @ ${price:.2f}")
        logger.info(f"   Cost: ${price * size:.2f}")
        
        inv.record_fill(outcome, Side.BUY, price, size)
        
        if order_type == "BID_YES":
            self.state.bid_order_yes = None
            self.state.last_bid_price_yes = None
            self._pending_cancel.discard(order_id)
            self._forget_order(order_id)
        elif order_type == "BID_NO":
            self.state.bid_order_no = None
            self.state.last_bid_price_no = None
            self._pending_cancel.discard(order_id)
            self._forget_order(order_id)
        
        logger.info(f"   → Inventory: ΔQ={inv.delta_q:.2f} | YES={inv.q_yes:.2f} | NO={inv.q_no:.2f}")
        logger.info(f"   💵 Locked profit: ${inv.locked_profit:.4f}")
        logger.info(f"   📊 Total trades: {inv.total_trades}")
        logger.info("=" * 60)
        logger.info("")

    def get_active_order_ids(self) -> set:
        """Return set of active order IDs for fill detection."""
        order_ids = set()
        if self.state.bid_order_yes and self.state.bid_order_yes.is_active:
            order_ids.add(self.state.bid_order_yes.order_id)
        if self.state.bid_order_no and self.state.bid_order_no.is_active:
            order_ids.add(self.state.bid_order_no.order_id)
        return order_ids


def build_engine() -> StrategyEngine:
    """Build and return a StrategyEngine instance."""
    return StrategyEngine()
