"""
Polymarket Top-of-Book Market Maker - Data Models

Data models for inventory state, orders, and orderbook data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any
import json
from pathlib import Path


class Side(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    """Binary market outcome."""
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    """Order lifecycle status."""
    PENDING = "PENDING"
    LIVE = "LIVE"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class StrategyMode(str, Enum):
    """Current strategy mode."""
    QUOTING = "QUOTING"  # Normal operation: placing bids on both sides
    SKEWED_YES = "SKEWED_YES"  # Long YES, only bidding on NO
    SKEWED_NO = "SKEWED_NO"  # Long NO, only bidding on YES
    STOPPED = "STOPPED"  # Strategy stopped (manual or expiry)


@dataclass
class OrderBookLevel:
    """Single level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Order book snapshot for one outcome."""
    asset_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    
    @property
    def best_bid(self) -> Optional[float]:
        """Best bid price (highest)."""
        if not self.bids:
            return None
        return max(level.price for level in self.bids)
    
    @property
    def best_ask(self) -> Optional[float]:
        """Best ask price (lowest)."""
        if not self.asks:
            return None
        return min(level.price for level in self.asks)
    
    @property
    def mid_price(self) -> Optional[float]:
        """Mid price."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2
    
    @property
    def spread(self) -> Optional[float]:
        """Bid-ask spread."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid
    
    def get_best_bid_level(self) -> Optional[OrderBookLevel]:
        """Get the best bid level."""
        if not self.bids:
            return None
        return max(self.bids, key=lambda x: x.price)
    
    def get_best_ask_level(self) -> Optional[OrderBookLevel]:
        """Get the best ask level."""
        if not self.asks:
            return None
        return min(self.asks, key=lambda x: x.price)


@dataclass
class LiveOrder:
    """Tracks a live order on the exchange."""
    order_id: str
    asset_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    @property
    def remaining_size(self) -> float:
        return self.size - self.filled_size
    
    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED)


@dataclass
class InventoryState:
    """
    Portfolio inventory state for market making.
    
    Tracks positions in both YES and NO tokens:
    - Q_yes: Quantity of YES tokens held
    - Q_no: Quantity of NO tokens held
    - ΔQ = Q_yes - Q_no: Net inventory imbalance
    
    Cost basis tracking for P&L calculation:
    - C_yes: Total cost (USD spent) to acquire YES tokens
    - C_no: Total cost (USD spent) to acquire NO tokens
    """
    # YES side
    q_yes: float = 0.0  # Quantity of YES tokens
    c_yes: float = 0.0  # Cost basis for YES (total USD spent)
    
    # NO side
    q_no: float = 0.0  # Quantity of NO tokens
    c_no: float = 0.0  # Cost basis for NO (total USD spent)
    
    # P&L tracking
    realized_pnl: float = 0.0  # Realized profit from sales
    
    # Statistics
    total_trades: int = 0
    total_volume: float = 0.0
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    @property
    def mu_yes(self) -> float:
        """VWAP (average cost) for YES position."""
        if self.q_yes == 0:
            return 0.0
        return self.c_yes / self.q_yes
    
    @property
    def mu_no(self) -> float:
        """VWAP (average cost) for NO position."""
        if self.q_no == 0:
            return 0.0
        return self.c_no / self.q_no
    
    @property
    def delta_q(self) -> float:
        """Net inventory imbalance: ΔQ = Q_yes - Q_no"""
        return self.q_yes - self.q_no
    
    @property
    def total_position(self) -> float:
        """Total tokens held (YES + NO)."""
        return self.q_yes + self.q_no
    
    @property
    def total_cost(self) -> float:
        """Total USD spent on positions."""
        return self.c_yes + self.c_no
    
    @property
    def paired_quantity(self) -> float:
        """Quantity of paired YES+NO positions (guaranteed $1 payout each)."""
        return min(self.q_yes, self.q_no)
    
    @property
    def paired_cost(self) -> float:
        """Cost basis for paired positions."""
        paired = self.paired_quantity
        if paired == 0:
            return 0.0
        # Pro-rata cost from each side
        yes_cost = self.mu_yes * paired if self.q_yes > 0 else 0
        no_cost = self.mu_no * paired if self.q_no > 0 else 0
        return yes_cost + no_cost
    
    @property
    def locked_profit(self) -> float:
        """
        Guaranteed profit from paired positions.
        
        Each paired YES+NO = $1 at expiry.
        Locked profit = paired_quantity * $1 - paired_cost
        """
        return self.paired_quantity * 1.0 - self.paired_cost
    
    @property
    def unrealized_pnl(self) -> float:
        """
        Unrealized P&L = locked profit from pairs.
        
        Note: Unpaired positions have uncertain value until expiry.
        """
        return self.locked_profit
    
    @property
    def is_balanced(self) -> bool:
        """Check if inventory is approximately balanced."""
        return abs(self.delta_q) < 0.01
    
    def record_fill(self, outcome: Outcome, side: Side, price: float, size: float):
        """
        Record a fill and update inventory.
        
        Args:
            outcome: YES or NO
            side: BUY or SELL
            price: Fill price
            size: Fill size (tokens)
        """
        self.total_trades += 1
        self.total_volume += price * size
        self.updated_at = datetime.now()
        
        if outcome == Outcome.YES:
            if side == Side.BUY:
                # Buying YES: increase position and cost
                self.c_yes += price * size
                self.q_yes += size
            else:  # SELL
                # Selling YES: reduce position, realize P&L
                if self.q_yes > 0:
                    avg_cost = self.c_yes / self.q_yes
                    sell_amount = min(size, self.q_yes)
                    # P&L = (sell_price - avg_cost) * amount
                    self.realized_pnl += (price - avg_cost) * sell_amount
                    self.c_yes -= avg_cost * sell_amount
                    self.q_yes = max(0, self.q_yes - size)
        else:  # NO
            if side == Side.BUY:
                # Buying NO: increase position and cost
                self.c_no += price * size
                self.q_no += size
            else:  # SELL
                # Selling NO: reduce position, realize P&L
                if self.q_no > 0:
                    avg_cost = self.c_no / self.q_no
                    sell_amount = min(size, self.q_no)
                    self.realized_pnl += (price - avg_cost) * sell_amount
                    self.c_no -= avg_cost * sell_amount
                    self.q_no = max(0, self.q_no - size)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for persistence."""
        return {
            "q_yes": self.q_yes,
            "c_yes": self.c_yes,
            "q_no": self.q_no,
            "c_no": self.c_no,
            "realized_pnl": self.realized_pnl,
            "total_trades": self.total_trades,
            "total_volume": self.total_volume,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InventoryState":
        """Deserialize from dictionary."""
        return cls(
            q_yes=data.get("q_yes", 0.0),
            c_yes=data.get("c_yes", 0.0),
            q_no=data.get("q_no", 0.0),
            c_no=data.get("c_no", 0.0),
            realized_pnl=data.get("realized_pnl", 0.0),
            total_trades=data.get("total_trades", 0),
            total_volume=data.get("total_volume", 0.0),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if "updated_at" in data else datetime.now(),
        )
    
    def save(self, filepath: str):
        """Save state to JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, filepath: str) -> "InventoryState":
        """Load state from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return cls()
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass
class StrategyState:
    """Overall strategy state."""
    mode: StrategyMode = StrategyMode.STOPPED
    inventory: InventoryState = field(default_factory=InventoryState)
    
    # Active bid orders (we only place BUY orders as market maker)
    bid_order_yes: Optional[LiveOrder] = None
    bid_order_no: Optional[LiveOrder] = None
    
    # Order books (updated continuously)
    orderbook_yes: Optional[OrderBook] = None
    orderbook_no: Optional[OrderBook] = None
    
    # Last quote prices (for detecting when to update)
    last_bid_price_yes: Optional[float] = None
    last_bid_price_no: Optional[float] = None
    
    # Timing
    started_at: Optional[datetime] = None
    last_quote_update: Optional[datetime] = None
    
    # Market info
    market_expiry: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for persistence."""
        return {
            "mode": self.mode.value,
            "inventory": self.inventory.to_dict(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "market_expiry": self.market_expiry.isoformat() if self.market_expiry else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategyState":
        """Deserialize from dictionary."""
        state = cls()
        state.mode = StrategyMode(data.get("mode", "STOPPED"))
        state.inventory = InventoryState.from_dict(data.get("inventory", {}))
        if data.get("started_at"):
            state.started_at = datetime.fromisoformat(data["started_at"])
        if data.get("market_expiry"):
            state.market_expiry = datetime.fromisoformat(data["market_expiry"])
        return state
    
    def save(self, filepath: str):
        """Save state to JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, filepath: str) -> "StrategyState":
        """Load state from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return cls()
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)
