"""Top-of-book bid/ask volume skew — a seconds-scale signal, shadow-only."""
from __future__ import annotations
from typing import Optional

from config import settings


def imbalance(book: dict, levels: Optional[int] = None) -> Optional[dict]:
    """imb in -1..1 (> 0 = bid-heavy) with a BUY/SELL/NEUTRAL signal, or None for an unusable book."""
    levels = levels or settings.ob_imbalance_levels
    bids, asks = (book or {}).get("buy") or [], (book or {}).get("sell") or []
    if not bids or not asks:
        return None
    try:
        bid_vol = sum(float(b["size"]) for b in bids[:levels])
        ask_vol = sum(float(a["size"]) for a in asks[:levels])
        top_bid, top_ask = float(bids[0]["price"]), float(asks[0]["price"])
    except (KeyError, TypeError, ValueError):
        return None
    total = bid_vol + ask_vol
    if total <= 0 or top_bid <= 0:
        return None
    imb = (bid_vol - ask_vol) / total
    thr = settings.ob_imbalance_threshold
    signal = "BUY" if imb > thr else "SELL" if imb < -thr else "NEUTRAL"
    return {
        "imb": round(imb, 3), "signal": signal,
        "top_bid": top_bid, "top_ask": top_ask,
        "spread_pct": round((top_ask - top_bid) / top_bid * 100, 3),
        "bid_vol": round(bid_vol, 4), "ask_vol": round(ask_vol, 4),
    }
