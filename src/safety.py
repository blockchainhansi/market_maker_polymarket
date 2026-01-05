"""
Safety and risk management utilities for the Polymarket Top-of-Book Market Maker.

Provides utility functions for time tracking.
"""

from datetime import datetime, timedelta
from typing import Optional

from config import Config


def seconds_until_expiry(config: Config) -> float:
    """Get seconds remaining until market expiry."""
    return config.time_until_expiry().total_seconds()
