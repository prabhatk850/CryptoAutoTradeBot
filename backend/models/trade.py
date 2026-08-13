from datetime import datetime, timezone
from typing import Optional, Literal
from pydantic import BaseModel, Field


class TradeLog(BaseModel):
    """Every bot decision is saved as a TradeLog document in MongoDB."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    reason: str                          # human-readable why the bot decided this
    price: float
    quantity: int
    indicators: dict                     # snapshot of all indicator values at decision time
    order_id: Optional[str] = None       # Delta Exchange order ID (None if HOLD)
    order_status: Optional[str] = None   # filled / rejected / etc
    paper_trade: bool = True             # always True until you switch to live


class Portfolio(BaseModel):
    """Current paper portfolio snapshot."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    balance_usd: float
    open_positions: list[dict]
    total_pnl: float
    total_trades: int
    wins: int
    losses: int
