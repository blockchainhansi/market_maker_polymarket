# Polymarket Market Maker

A top-of-book market maker for Polymarket's BTC 15-minute binary prediction markets.

## Overview

This bot implements a market making strategy on Polymarket's BTC Up/Down 15-minute prediction markets:

1. **Discovers** the next BTC 15-minute market automatically
2. **Places bid orders** on both YES (Up) and NO (Down) sides
3. **Applies inventory skew** to balance positions
4. **Captures spreads** by accumulating token pairs
5. **Profits** when YES + NO cost less than $1.00

### How It Works

In a binary prediction market:
- **YES token**: Pays $1.00 if event occurs, $0 otherwise
- **NO token**: Pays $1.00 if event doesn't occur, $0 otherwise
- **Key property**: YES + NO = $1.00 always

If you buy 10 YES at $0.48 and 10 NO at $0.48:
- Total cost: $9.60
- Guaranteed payout: $10.00
- **Profit: $0.40** (4.2% return)

## Quick Start

### Prerequisites

- Python 3.10+
- A Polygon wallet with USDC and MATIC
- Polymarket account with the wallet connected

### Installation

```bash
cd src
pip install -r requirements.txt
```

### Configuration

```bash
# Copy the example config and edit it
cp env.example .env
nano .env
```

Add your wallet's private key to `.env`

### Run

```bash
python main.py
```

Press `Ctrl+C` to stop gracefully.

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `GAMMA` | 0.01 | Inventory skew strength (γ) |
| `ORDER_SIZE` | 10.0 | Base order size in tokens |
| `ORDER_SIZE_ETA` | 0.05 | Order size decay rate (η) |
| `REFRESH_INTERVAL` | 1.0 | Quote update interval |

## Strategy

This implementation is follows the algorithm and dynamic inventory-based bid size idea by **Fushimi et al. (2018)** , which is attached in this repo, adapted for binary prediction markets. It is similar to Avellaneda and Stoikov (2008). 

The strategy implements key concepts from optimal market making theory:

**Price Skew (γ):** Adjusts bid prices based on inventory to encourage mean reversion:
```
skew = γ × ΔQ
```

**Dynamic Order Sizing (η):** Reduces order size exponentially with inventory to manage risk:
```
size = base_size × exp(-η × |ΔQ|)
```

This ensures the market maker reduces exposure as inventory grows, a key insight from the paper.

### Join-or-Improve

For each orderbook side:
1. If spread > 1 tick: Improve best bid by 1 cent
2. If spread = 1 tick: Join at best bid
3. Apply inventory skew adjustment
4. Check profitability cap

### Inventory Management

- **ΔQ = Q_yes - Q_no** (net inventory)
- Positive ΔQ: Lower YES bid, raise NO bid
- Negative ΔQ: Raise YES bid, lower NO bid
- Dynamic order sizing reduces exposure as inventory grows

### Feature

- Profitability cap ensures pairs cost < $1.00

## File Structure

```
src/
├── main.py              # Entry point
├── config.py            # Configuration
├── strategy_engine.py   # Trading logic
├── polymarket_client.py # API wrapper
├── orderbook_manager.py # Orderbook handling
├── user_channel.py      # Fill detection
├── models.py            # Data models
├── logger.py            # Logging
├── safety.py            # Safety utilities
├── sell_positions.py    # Selling utility
└── requirements.txt     # Dependencies
```

## Output

```
📈 Market: YES 0.48/0.52 | NO 0.48/0.52
📦 Inventory: ΔQ=+2.00 | Skew=$+0.02 | YES=7.00 | NO=5.00
💰 P&L: Locked=$0.20 | Pairs=5.00 | Trades=4
📋 Bids: YES@0.46 | NO@0.50
⏱️  Expiry in 420s | Mode: QUOTING
```


## Disclaimer


1. **Regulatory Restrictions**: Polymarket and similar prediction markets are **prohibited in certain countries**. It is your sole responsibility to verify whether using such platforms is legal in your jurisdiction.

2. **No Liability**: The author of this repository assume **no responsibility or liability** for any losses, damages, legal issues, or other consequences arising from the use of this code. Use at your own risk.

3. **Not Financial Advice**: This repository is provided for **educational and entertainment purposes only**. Nothing in this code or documentation constitutes financial, investment, trading, or legal advice.

4. **No Guarantees**: There is no guarantee of profitability. Automated trading involves significant risk, including the potential loss of your entire investment.

5. **Use at Your Own Risk**: By using this code, you acknowledge that you understand the risks involved and agree to take full responsibility for your actions.