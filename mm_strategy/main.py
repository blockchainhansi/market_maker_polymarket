"""
Entry point for the Polymarket Top-of-Book Market Maker.

Automatically discovers BTC Up/Down 15-minute markets on Polymarket
and runs continuously, switching to the next market after each expiry.
"""

import asyncio
import signal
import sys

from config import init_config
from logger import setup_logging, get_logger
from polymarket_client import init_client
from orderbook_manager import init_orderbook_manager
from user_channel import init_user_channel
from market_discovery import MarketDiscovery
from strategy_engine import StrategyEngine


async def discover_and_set_market(cfg, log):
    """
    Discover the next BTC market and update config.
    
    Returns:
        dict: Market info with title, end_date, etc.
    """
    log.info("Discovering BTC 15-minute markets...")
    
    discovery = MarketDiscovery(cfg)
    try:
        market = await discovery.find_next_market()
        
        if not market:
            raise RuntimeError("No BTC 15-minute market found on Polymarket")
        
        # Update config with discovered market IDs
        cfg.condition_id = market["condition_id"]
        cfg.token_id_yes = market["token_id_up"]
        cfg.token_id_no = market["token_id_down"]
        cfg.active_market_end_date = market["end_date"]
        
        log.info(f"Selected market: {market['title']}")
        log.info(f"  Slug: {market['slug']}")
        log.info(f"  Condition ID: {cfg.condition_id}")
        log.info(f"  End time: {market['end_date']}")
        log.info(f"  Up (YES) token: {cfg.token_id_yes[:32]}...")
        log.info(f"  Down (NO) token: {cfg.token_id_no[:32]}...")
        
        return market
    finally:
        await discovery.close()


def prompt_sell_tokens(log, inv, cfg) -> tuple[bool, float, float]:
    """
    Prompt user whether to sell tokens on manual stop (Cmd+C).
    
    Returns (should_sell, yes_amount, no_amount).
    """
    has_yes = inv.q_yes > 0.01
    has_no = inv.q_no > 0.01
    
    if not has_yes and not has_no:
        log.info("📦 No inventory to sell")
        return False, 0.0, 0.0
    
    print("\n" + "=" * 60)
    print("📦 SESSION INVENTORY")
    print("=" * 60)
    print(f"   ΔQ (imbalance): {inv.delta_q:+.2f}")
    print("   " + "-" * 45)
    if has_yes:
        print(f"   YES tokens: {inv.q_yes:>8.2f}")
    if has_no:
        print(f"   NO tokens:  {inv.q_no:>8.2f}")
    print("   " + "-" * 45)
    print(f"   Locked profit: ${inv.locked_profit:.4f}")
    print(f"   Total spent:   ${inv.total_cost:.2f}")
    print("   " + "-" * 45)
    roc = (inv.locked_profit / inv.total_cost * 100) if inv.total_cost > 0 else 0.0
    print(f"   ROC:  {roc:>6.2f}%")
    print("=" * 60)
    
    print(f"\n💡 Will sell from CLOB balance:")
    if has_yes:
        print(f"   • {inv.q_yes:.2f} YES tokens")
    if has_no:
        print(f"   • {inv.q_no:.2f} NO tokens")
    
    print("\nOptions:")
    print("  [s] SELL tokens at market (immediate exit)")
    print("  [k] KEEP tokens for redemption at expiry")
    print()
    
    try:
        response = input("Sell or keep? [s/K]: ").strip().lower()
        should_sell = response in ('s', 'sell', 'y', 'yes')
        return should_sell, inv.q_yes if has_yes else 0.0, inv.q_no if has_no else 0.0
    except (EOFError, KeyboardInterrupt):
        return False, 0.0, 0.0


async def run_single_market(cfg, client, log, stop_event):
    """
    Run the strategy on a single market until expiry or stop signal.
    
    Returns:
        bool: True if should continue to next market, False if stopped
    """
    log.info("🧹 Cleaning up any stale orders before starting...")
    try:
        cancelled = await client.cancel_all_orders()
        if cancelled and cancelled > 0:
            log.info(f"   Cancelled {cancelled} stale orders")
        else:
            log.info("   No stale orders to cancel")
    except Exception as e:
        log.warning(f"   Could not cancel stale orders: {e}")
    
    ob_manager = init_orderbook_manager(cfg)
    engine = StrategyEngine(config=cfg, client=client, ob_manager=ob_manager)
    
    api_key, api_secret, api_passphrase = client.get_api_credentials()
    
    user_channel = init_user_channel(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        config=cfg,
        on_fill=engine.on_fill,
    )
    
    user_channel.start()
    await engine.start()
    
    log.info("Strategy engine running on current market...")
    
    try:
        while not stop_event.is_set():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    
    log.info("Stopping current market session...")
    user_channel.stop()
    
    user_stopped = stop_event.is_set()
    
    if user_stopped:
        should_sell, _, _ = prompt_sell_tokens(log, engine.state.inventory, cfg)
        await engine.stop(sell_tokens=should_sell)
    else:
        await engine.stop(sell_tokens=False)
    
    return not user_stopped


async def main():
    cfg = init_config()
    setup_logging(level=cfg.log_level)
    log = get_logger(__name__)

    log.info("=" * 60)
    log.info("Polymarket Top-of-Book Market Maker")
    log.info("=" * 60)
    log.info("Asset: BTC (15m markets)")
    log.info(f"Wallet: {cfg.wallet_address}")
    log.info(f"Gamma (skew): {cfg.gamma}")
    log.info(f"Order size: {cfg.order_size}")
    log.info(f"Refresh interval: {cfg.refresh_interval}s")

    client = await init_client(cfg)
    log.info("CLOB client initialized")

    stop_event = asyncio.Event()
    signal_count = [0]

    def _handle_signal():
        signal_count[0] += 1
        if signal_count[0] == 1:
            print("\n")
            log.info("⏹️  Stop signal received - finishing up...")
            stop_event.set()
        elif signal_count[0] >= 2:
            print("\n")
            log.info("🛑 Force quit!")
            sys.exit(1)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    market_count = 0
    while not stop_event.is_set():
        market_count += 1
        log.info(f"\n{'='*60}")
        log.info(f"Market Session #{market_count}")
        log.info(f"{'='*60}")
        
        try:
            market = await discover_and_set_market(cfg, log)
            should_continue = await run_single_market(cfg, client, log, stop_event)
            
            if not should_continue:
                break
            
        except Exception as e:
            log.error(f"Error in market session: {e}")
            if not stop_event.is_set():
                log.info("Retrying in 15 seconds...")
                await asyncio.sleep(15)

    await client.close()
    log.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
