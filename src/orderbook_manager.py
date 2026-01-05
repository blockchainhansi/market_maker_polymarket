"""
Polymarket Top-of-Book Market Maker - Order Book Manager

Manages orderbook data fetching and caching for the strategy engine.
"""

import asyncio
from typing import Optional, Callable, Set, Dict, Any
from datetime import datetime

from config import Config, get_config
from models import OrderBook, OrderBookLevel, Outcome
from logger import get_logger

logger = get_logger(__name__)


class OrderBookManager:
    """
    Orderbook manager for the market maker.
    
    Features:
    - Caches latest order book state
    - Tracks order IDs for fill detection
    - Provides orderbook data to strategy engine
    """
    
    def __init__(
        self, 
        config: Optional[Config] = None,
        on_update: Optional[Callable[[Outcome, OrderBook], None]] = None,
        on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
    ):
        self.config = config or get_config()
        self.on_update = on_update
        self.on_fill = on_fill
        
        # Order book cache
        self._book_yes: Optional[OrderBook] = None
        self._book_no: Optional[OrderBook] = None
        
        # Track order IDs for fill detection
        self._tracked_order_ids: Set[str] = set()
        
        # Stats
        self._fetch_count = 0
        self._last_update: Optional[datetime] = None
    
    @property
    def book_yes(self) -> Optional[OrderBook]:
        return self._book_yes
    
    @property
    def book_no(self) -> Optional[OrderBook]:
        return self._book_no
    
    @property
    def has_data(self) -> bool:
        """Check if we have order book data for both sides."""
        return self._book_yes is not None and self._book_no is not None
    
    def set_callback(self, callback: Callable[[Outcome, OrderBook], None]):
        """Set the callback for order book updates."""
        self.on_update = callback
    
    def set_fill_callback(self, callback: Callable[[str, Outcome, float, float], None]):
        """Set the callback for fill detection."""
        self.on_fill = callback
    
    def track_order(self, order_id: str):
        """Start tracking an order ID for fill detection."""
        self._tracked_order_ids.add(order_id)
    
    def untrack_order(self, order_id: str):
        """Stop tracking an order ID."""
        self._tracked_order_ids.discard(order_id)
    
    async def start(self):
        """Start the orderbook manager."""
        logger.info("Starting OrderBookManager...")
        logger.info(f"   YES token: {self.config.token_id_yes[:32]}...")
        logger.info(f"   NO token: {self.config.token_id_no[:32]}...")
    
    async def stop(self):
        """Stop the orderbook manager."""
        logger.info("OrderBookManager stopped")
    
    def update_book(self, outcome: Outcome, book: OrderBook):
        """Update the cached orderbook."""
        if outcome == Outcome.YES:
            self._book_yes = book
        else:
            self._book_no = book
        
        self._fetch_count += 1
        self._last_update = datetime.now()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics."""
        return {
            "has_data": self.has_data,
            "fetch_count": self._fetch_count,
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "tracked_orders": len(self._tracked_order_ids),
            "yes_book": {
                "best_bid": self._book_yes.best_bid if self._book_yes else None,
                "best_ask": self._book_yes.best_ask if self._book_yes else None,
            } if self._book_yes else None,
            "no_book": {
                "best_bid": self._book_no.best_bid if self._book_no else None,
                "best_ask": self._book_no.best_ask if self._book_no else None,
            } if self._book_no else None,
        }


# Singleton instance
_manager: Optional[OrderBookManager] = None


def get_orderbook_manager() -> OrderBookManager:
    """Get the global OrderBookManager instance."""
    global _manager
    if _manager is None:
        _manager = OrderBookManager()
    return _manager


def init_orderbook_manager(
    config: Optional[Config] = None,
    on_update: Optional[Callable[[Outcome, OrderBook], None]] = None,
    on_fill: Optional[Callable[[str, Outcome, float, float], None]] = None,
) -> OrderBookManager:
    """Initialize the global OrderBookManager."""
    global _manager
    _manager = OrderBookManager(config, on_update, on_fill)
    return _manager
