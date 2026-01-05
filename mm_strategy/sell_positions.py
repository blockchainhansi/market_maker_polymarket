#!/usr/bin/env python3
"""
Sell positions on Polymarket.

Shows all your positions and lets you sell them.

Usage:
    python sell_positions.py           # Interactive - select which to sell
    python sell_positions.py --all     # Sell ALL positions
    python sell_positions.py --dry-run # Show positions without selling
"""

import asyncio
import argparse
import os
import httpx
from dotenv import load_dotenv
from eth_account import Account

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

# Polymarket Data API
DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


async def get_all_positions(wallet_address: str) -> list:
    """
    Get all positions for wallet from Polymarket Data API.
    Uses pagination to ensure all positions are retrieved.
    """
    all_data = []
    offset = 0
    limit = 1000  # Max per request
    
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            url = f"{DATA_API}/positions?user={wallet_address}&limit={limit}&offset={offset}"
            
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"❌ Error fetching positions at offset {offset}: {e}")
                break
            
            if not data:
                break
                
            all_data.extend(data)
            print(f"   Fetched {len(data)} positions (total: {len(all_data)})")
            
            if len(data) < limit:
                break
                
            offset += limit
    
    return all_data


def create_clob_client(private_key: str):
    """Create and initialize CLOB client."""
    account = Account.from_key(private_key)
    
    # Create client
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=POLYGON,
        key=private_key,
    )
    
    # Derive API key
    try:
        creds = client.derive_api_key()
        client.set_api_creds(creds)
    except Exception:
        # Try to create new API key
        creds = client.create_api_key()
        client.set_api_creds(creds)
    
    return client, account.address


async def sell_token(clob_client, token_id: str, size: float):
    """
    Sell tokens using FAK order.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    
    # Round size to 2 decimals
    clean_size = int(size * 100 + 0.5) / 100
    
    order_args = OrderArgs(
        token_id=token_id,
        side=SELL,
        price=0.01,  # Worst case price - will fill at actual bid
        size=clean_size,
    )
    
    try:
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.FAK)
        
        if resp.get("success"):
            order_id = resp.get("orderID", "")[:16]
            return True, f"Order submitted: {order_id}..."
        else:
            return False, f"Order failed: {resp}"
    except Exception as e:
        error_msg = str(e)
        if "no orders found to match" in error_msg.lower():
            return False, "No buyers - tokens kept for redemption"
        return False, f"Error: {error_msg}"


async def main():
    parser = argparse.ArgumentParser(description="Sell positions on Polymarket")
    parser.add_argument("--all", "-a", action="store_true", help="Sell all positions")
    parser.add_argument("--dry-run", action="store_true", help="Show positions without selling")
    args = parser.parse_args()
    
    # Load environment
    load_dotenv()
    
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ PRIVATE_KEY not found in .env")
        return
    
    print("=" * 70)
    print("📊 SELL POSITIONS")
    print("=" * 70)
    
    # Initialize CLOB client
    print("\n🔑 Initializing client...")
    clob_client, wallet_address = create_clob_client(private_key)
    print(f"   Wallet: {wallet_address[:10]}...{wallet_address[-6:]}")
    
    # Fetch all positions
    print("\n📦 Fetching all positions...")
    positions = await get_all_positions(wallet_address)
    
    # Filter to SELLABLE positions:
    # - Must have balance > 0
    # - Must NOT be redeemable (resolved markets can't be sold on CLOB)
    # - Should have some value (curPrice > 0)
    sellable_positions = [
        p for p in positions 
        if float(p.get("size", 0)) > 0.01 
        and not p.get("redeemable", False)
    ]
    
    # Also track resolved positions for info
    resolved_positions = [
        p for p in positions 
        if float(p.get("size", 0)) > 0.01 
        and p.get("redeemable", False)
    ]
    
    if resolved_positions:
        resolved_value = sum(float(p.get("size", 0)) * float(p.get("curPrice", 0)) for p in resolved_positions)
        print(f"\n⚠️  {len(resolved_positions)} positions are RESOLVED (can't sell, use redeem_positions.py)")
        print(f"   Resolved positions value: ${resolved_value:.2f}")
    
    active_positions = sellable_positions
    
    if not active_positions:
        print("\n✅ No sellable positions found!")
        if resolved_positions:
            print("   Use 'python3 redeem_positions.py' to redeem resolved positions")
        return
    
    print(f"\n📋 Found {len(active_positions)} sellable position(s):\n")
    
    # Display positions
    total_value = 0
    for i, pos in enumerate(active_positions):
        title = pos.get("title", "Unknown")[:50]
        outcome = pos.get("outcome", "?")
        size = float(pos.get("size", 0))
        cur_price = float(pos.get("curPrice", 0))
        value = size * cur_price
        total_value += value
        
        print(f"  [{i+1}] {title}...")
        print(f"      {outcome}: {size:.2f} tokens @ ${cur_price:.2f} = ${value:.2f}")
        print()
    
    print(f"   💰 Total estimated value: ${total_value:.2f}")
    
    if args.dry_run:
        print("\n🔍 DRY RUN - No orders will be placed")
        return
    
    # Select which to sell
    print("\n" + "=" * 70)
    
    if args.all:
        to_sell = active_positions
        print(f"Selling ALL {len(to_sell)} positions...")
    else:
        print("Enter position numbers to sell (comma-separated), 'all', or 'q' to quit:")
        response = input("> ").strip().lower()
        
        if response in ('q', 'quit', ''):
            print("❌ Cancelled")
            return
        
        if response == 'all':
            to_sell = active_positions
        else:
            try:
                indices = [int(x.strip()) - 1 for x in response.split(",")]
                to_sell = [active_positions[i] for i in indices if 0 <= i < len(active_positions)]
            except ValueError:
                print("❌ Invalid input")
                return
    
    if not to_sell:
        print("❌ No positions selected")
        return
    
    # Confirm
    print(f"\n⚠️  About to sell {len(to_sell)} position(s)")
    confirm = input("Proceed? [y/N]: ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("❌ Cancelled")
        return
    
    # Execute sells
    print("\n" + "=" * 70)
    print("🚀 EXECUTING SELLS")
    print("=" * 70)
    
    for pos in to_sell:
        title = pos.get("title", "Unknown")[:40]
        outcome = pos.get("outcome", "?")
        size = float(pos.get("size", 0))
        token_id = pos.get("asset", "")
        
        print(f"\n📤 Selling {size:.2f} {outcome} ({title}...)")
        
        success, message = await sell_token(clob_client, token_id, size)
        
        if success:
            print(f"   ✅ {message}")
        else:
            print(f"   ⚠️ {message}")
    
    print("\n✅ Done!")


if __name__ == "__main__":
    asyncio.run(main())


