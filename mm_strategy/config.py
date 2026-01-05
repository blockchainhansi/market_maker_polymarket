"""
Polymarket Top-of-Book Market Maker - Configuration Module

Loads and validates all environment variables and hyperparameters.
"""

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv


@dataclass
class Config:
    """Configuration container for the market maker strategy."""
    
    # API & Network
    private_key: str
    clob_http_url: str
    clob_ws_url: str
    rpc_url: str
    chain_id: int
    
    # Market Maker Hyperparameters (Fushimi et al. model)
    gamma: float  # Inventory skew strength (γ): price adjustment per unit inventory
    order_size: float  # Base order size in tokens
    order_size_eta: float  # Order size decay rate (η): reduces size with inventory
    
    # Market Selection
    condition_id: str
    token_id_yes: str
    token_id_no: str
    
    # Operational
    log_level: str
    refresh_interval: float  # How often to refresh quotes (seconds)
    ws_reconnect_delay: int
    
    # Derived wallet address (computed after loading)
    wallet_address: str = field(default="")
    
    # Active market end date (set by discovery)
    active_market_end_date: Optional[datetime] = None
    
    def compute_skew(self, inventory: float) -> float:
        """
        Compute price skew based on current inventory.
        
        Skew = inventory * gamma
        
        If inventory > 0 (long YES), skew is positive -> lower our bids
        If inventory < 0 (long NO), skew is negative -> raise our bids
        
        Args:
            inventory: Net inventory (Q_yes - Q_no)
            
        Returns:
            float: Price adjustment in dollars
        """
        return inventory * self.gamma
    
    def get_order_size(self, inventory: float = 0.0) -> float:
        """
        Compute dynamic order size based on current inventory.
        
        Implements the Fushimi et al. optimal order sizing:
        size = base_size × exp(-η × |ΔQ|)
        
        This reduces order size exponentially as inventory grows,
        limiting risk exposure when positions accumulate.
        
        Args:
            inventory: Net inventory (Q_yes - Q_no)
            
        Returns:
            float: Adjusted order size (minimum 5.0 for Polymarket)
        """
        if self.order_size_eta <= 0:
            return self.order_size
        
        decay = math.exp(-self.order_size_eta * abs(inventory))
        size = self.order_size * decay
        return max(5.0, size)  # Polymarket minimum
    
    def get_market_expiry(self) -> datetime:
        """
        Get market expiry time.
        Uses the discovered market end date if available.

        Returns:
            datetime: When the market expires
        """
        if self.active_market_end_date:
            if self.active_market_end_date.tzinfo is None:
                return self.active_market_end_date
            return self.active_market_end_date.astimezone().replace(tzinfo=None)

        # Fallback: calculate next quarter-hour boundary
        now = datetime.now()
        minute = (now.minute // 15) * 15
        current_slot = now.replace(minute=minute, second=0, microsecond=0)
        next_expiry = current_slot + timedelta(minutes=15)
        return next_expiry
    
    def time_until_expiry(self) -> timedelta:
        """Get time remaining until market expiry."""
        return self.get_market_expiry() - datetime.now()


def load_config(env_path: Optional[str] = None) -> Config:
    """
    Load configuration from environment variables.
    
    Args:
        env_path: Optional path to .env file. If None, looks in current dir.
        
    Returns:
        Config: Validated configuration object
        
    Raises:
        ValueError: If required configuration is missing or invalid
    """
    # Load .env file
    if env_path:
        load_dotenv(env_path)
    else:
        if Path(".env").exists():
            load_dotenv(".env")
        elif Path("mm_strategy/.env").exists():
            load_dotenv("mm_strategy/.env")
        else:
            load_dotenv()
    
    def get_required(key: str) -> str:
        value = os.getenv(key)
        if not value:
            raise ValueError(f"Required environment variable {key} is not set")
        return value
    
    def get_float(key: str, default: float) -> float:
        value = os.getenv(key)
        if value is None:
            return default
        return float(value)
    
    def get_int(key: str, default: int) -> int:
        value = os.getenv(key)
        if value is None:
            return default
        return int(value)
    
    def get_str(key: str, default: str) -> str:
        return os.getenv(key, default)
    
    # Market IDs are optional - can be discovered automatically
    condition_id = get_str("CONDITION_ID", "")
    token_id_yes = get_str("TOKEN_ID_YES", "")
    token_id_no = get_str("TOKEN_ID_NO", "")
    
    # Refresh interval with minimum enforcement
    refresh_interval = get_float("REFRESH_INTERVAL", 3.0)
    if refresh_interval < 2.0:
        refresh_interval = 2.0
    
    config = Config(
        # API & Network
        private_key=get_required("PRIVATE_KEY"),
        clob_http_url=get_str("CLOB_HTTP_URL", "https://clob.polymarket.com"),
        clob_ws_url=get_str("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws"),
        rpc_url=get_str("RPC_URL", "https://polygon-rpc.com"),
        chain_id=get_int("CHAIN_ID", 137),
        
        # Market Maker Hyperparameters (Fushimi et al.)
        gamma=get_float("GAMMA", 0.01),
        order_size=get_float("ORDER_SIZE", 5.0),
        order_size_eta=get_float("ORDER_SIZE_ETA", 0.05),
        
        # Market Selection
        condition_id=condition_id,
        token_id_yes=token_id_yes,
        token_id_no=token_id_no,
        
        # Operational
        log_level=get_str("LOG_LEVEL", "INFO"),
        refresh_interval=refresh_interval,
        ws_reconnect_delay=get_int("WS_RECONNECT_DELAY", 10),
    )
    
    # Derive wallet address from private key
    try:
        from eth_account import Account
        account = Account.from_key(config.private_key)
        config.wallet_address = account.address
    except Exception as e:
        raise ValueError(f"Invalid private key: {e}")
    
    # Validate configuration
    if config.gamma < 0:
        raise ValueError(f"GAMMA must be non-negative, got {config.gamma}")
    
    if config.order_size < 5.0:
        raise ValueError(f"ORDER_SIZE must be at least 5.0 (Polymarket minimum), got {config.order_size}")
    
    return config


# Singleton config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def init_config(env_path: Optional[str] = None) -> Config:
    """Initialize the global configuration from a specific path."""
    global _config
    _config = load_config(env_path)
    return _config
