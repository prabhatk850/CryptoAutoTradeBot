"""Python ports of LuxAlgo indicators (SuperTrend AI, Trendlines with Breaks, FVG, IFVG).

© LuxAlgo — CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/)
"""
import math
import statistics
from typing import Optional

from bot.indicators import calc_atr


def _percentile(data: list[float], p: float) -> float:
    """Pine's percentile_linear_interpolation."""
    if not data:
        return 0.0
    s = sorted(data)
    if len(s) == 1:
        return s[0]
    rank = (p / 100) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[int(lo)]
    return s[int(lo)] + (s[int(hi)] - s[int(lo)]) * (rank - lo)


def supertrend_ai(
    candles: list[dict],
    atr_len: int = 10,
    min_mult: float = 1.0,
    max_mult: float = 5.0,
    step: float = 0.5,
    perf_alpha: float = 10.0,
    from_cluster: str = "Best",
    max_iter: int = 50,
) -> Optional[dict]:
    """SuperTrend over a factor range, k-means on per-factor performance, final line from the best cluster."""
    n = len(candles)
    if n < atr_len + 2:
        return None

    hl2 = [(c["high"] + c["low"]) / 2 for c in candles]
    close = [c["close"] for c in candles]
    atr = calc_atr(candles, atr_len)

    factors = []
    i = 0
    while min_mult + i * step <= max_mult + 1e-9:
        factors.append(min_mult + i * step)
        i += 1
    nf = len(factors)

    # per-factor SuperTrend state
    upper = [hl2[0]] * nf
    lower = [hl2[0]] * nf
    output = [hl2[0]] * nf
    perf = [0.0] * nf
    trend = [0] * nf
    k_perf = 2 / (perf_alpha + 1)

    # final SuperTrend state
    f_upper = hl2[0]
    f_lower = hl2[0]
    os = 0
    perf_ama: Optional[float] = None

    # performance-index denominator: ema(|close-close[1]|, perf_alpha)
    den = 0.0
    den_init = False
    k_den = 2 / (int(perf_alpha) + 1)

    ts_arr: list[Optional[float]] = [None] * n
    os_arr = [0] * n
    perfidx_arr = [0.0] * n
    ama_arr: list[Optional[float]] = [None] * n
    target_arr: list[Optional[float]] = [None] * n
    signals = []

    for b in range(n):
        a = atr[b]
        pc = close[b - 1] if b > 0 else close[b]

        for j in range(nf):
            up = hl2[b] + a * factors[j]
            dn = hl2[b] - a * factors[j]
            if close[b] > upper[j]:
                trend[j] = 1
            elif close[b] < lower[j]:
                trend[j] = 0
            upper[j] = min(up, upper[j]) if pc < upper[j] else up
            lower[j] = max(dn, lower[j]) if pc > lower[j] else dn
            d = (close[b - 1] - output[j]) if b > 0 else 0.0
            diff = 1 if d > 0 else -1 if d < 0 else 0
            perf[j] += k_perf * ((close[b] - pc) * diff - perf[j])
            output[j] = lower[j] if trend[j] == 1 else upper[j]

        # --- K-means clustering on perf ---
        data = perf[:]
        cents = [_percentile(data, 25), _percentile(data, 50), _percentile(data, 75)]
        clusters = [[], [], []]
        for _ in range(max_iter):
            clusters = [[], [], []]
            for idx, v in enumerate(data):
                dists = [abs(v - c) for c in cents]
                clusters[dists.index(min(dists))].append(idx)
            new = []
            for ci in range(3):
                new.append(sum(data[i] for i in clusters[ci]) / len(clusters[ci]) if clusters[ci] else cents[ci])
            if new == cents:
                break
            cents = new

        frm = 2 if from_cluster == "Best" else 1 if from_cluster == "Average" else 0
        cl = clusters[frm]
        if cl:
            target = sum(factors[i] for i in cl) / len(cl)
            cluster_perf_avg = sum(data[i] for i in cl) / len(cl)
        else:
            target = target_arr[b - 1] if b > 0 and target_arr[b - 1] else factors[nf // 2]
            cluster_perf_avg = 0.0
        target_arr[b] = target

        absdiff = abs(close[b] - pc)
        if not den_init:
            den, den_init = absdiff, True
        else:
            den = k_den * absdiff + (1 - k_den) * den
        perf_idx = max(cluster_perf_avg, 0.0) / den if den > 0 else 0.0
        perfidx_arr[b] = perf_idx

        # --- final SuperTrend with chosen factor ---
        up = hl2[b] + a * target
        dn = hl2[b] - a * target
        f_upper = min(up, f_upper) if pc < f_upper else up
        f_lower = max(dn, f_lower) if pc > f_lower else dn
        prev_os = os
        if close[b] > f_upper:
            os = 1
        elif close[b] < f_lower:
            os = 0
        ts = f_lower if os == 1 else f_upper
        ts_arr[b] = ts
        os_arr[b] = os

        perf_ama = ts if perf_ama is None else perf_ama + perf_idx * (ts - perf_ama)
        ama_arr[b] = perf_ama

        if b > 0 and os != prev_os:
            signals.append({
                "time": candles[b]["time"],
                "dir": "long" if os > prev_os else "short",
                "strength": int(perf_idx * 10),
            })

    latest_flip = bool(signals and signals[-1]["time"] == candles[-1]["time"])
    return {
        "time": [c["time"] for c in candles],
        "ts": ts_arr,
        "os": os_arr,
        "perf_idx": [round(x, 4) for x in perfidx_arr],
        "ama": ama_arr,
        "signals": signals,
        "latest": {
            "dir": "long" if os_arr[-1] == 1 else "short",
            "os": os_arr[-1],
            "ts": round(ts_arr[-1], 2) if ts_arr[-1] is not None else None,
            "perf_idx": perfidx_arr[-1],
            "strength": int(perfidx_arr[-1] * 10),
            "flip": latest_flip,
            "signal": signals[-1] if latest_flip else None,
        },
    }


def fair_value_gaps(
    candles: list[dict],
    threshold_pct: float = 0.0,
    auto: bool = True,
    max_keep: int = 60,
) -> Optional[dict]:
    """Three-bar gaps (bull: low > high[2]; bear: high < low[2]); mitigated once a close crosses back."""
    n = len(candles)
    if n < 3:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    times = [int(c["time"]) for c in candles]

    fvgs = []
    signals = []
    running_sum = 0.0
    for i in range(n):
        if lows[i]:
            running_sum += (highs[i] - lows[i]) / lows[i]
        thr = (running_sum / max(i, 1)) if auto else threshold_pct / 100
        if i < 2:
            continue
        h2, l2 = highs[i - 2], lows[i - 2]
        lo, hi, c1 = lows[i], highs[i], closes[i - 1]
        bull = lo > h2 and c1 > h2 and h2 > 0 and (lo - h2) / h2 > thr
        bear = hi < l2 and c1 < l2 and hi > 0 and (l2 - hi) / hi > thr
        if bull:
            fvgs.append({"isbull": True, "top": lo, "bottom": h2, "start": times[i - 2], "det_idx": i, "mitigated": False, "mitig_time": None})
            signals.append({"time": times[i], "dir": "long"})
        elif bear:
            fvgs.append({"isbull": False, "top": l2, "bottom": hi, "start": times[i - 2], "det_idx": i, "mitigated": False, "mitig_time": None})
            signals.append({"time": times[i], "dir": "short"})

    # mitigation: bull gap filled when a later close < bottom; bear when close > top
    for f in fvgs:
        for j in range(f["det_idx"], n):
            if f["isbull"] and closes[j] < f["bottom"]:
                f["mitigated"], f["mitig_time"] = True, times[j]
                break
            if (not f["isbull"]) and closes[j] > f["top"]:
                f["mitigated"], f["mitig_time"] = True, times[j]
                break

    fvgs = fvgs[-max_keep:]
    unmitigated = [f for f in fvgs if not f["mitigated"]]
    fresh = [f for f in fvgs if f["det_idx"] == n - 1]
    new_dir = ("long" if fresh[-1]["isbull"] else "short") if fresh else None

    def _pub(f):
        return {"isbull": f["isbull"], "top": round(f["top"], 2), "bottom": round(f["bottom"], 2),
                "start": f["start"], "mitigated": f["mitigated"], "mitig_time": f["mitig_time"]}

    return {
        "unmitigated": [_pub(f) for f in unmitigated],
        "signals": signals,
        "end": times[-1],
        "latest": {"new": bool(fresh), "dir": new_dir},
    }


def _stdev(values: list[float], length: int) -> list[float]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(n):
        if i + 1 >= length:
            out[i] = statistics.pstdev(values[i - length + 1:i + 1])
    first = next((x for x in out if x is not None), 0.0)
    return [x if x is not None else first for x in out]


def trendline_breakout_navigator(
    candles: list[dict],
    length: int = 14,
    mult: float = 1.0,
    method: str = "atr",
) -> Optional[dict]:
    """Trendlines with Breaks: ATR-sloped lines from swing pivots; a close through one flips the trend."""
    n = len(candles)
    if n < 2 * length + 2:
        return None
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    times = [int(c["time"]) for c in candles]

    basis = _stdev(closes, length) if str(method).lower() == "stdev" else calc_atr(candles, length)

    # Pivot confirmed at bar i refers to bar i-length.
    ph: list[Optional[float]] = [None] * n
    pl: list[Optional[float]] = [None] * n
    for i in range(2 * length, n):
        c = i - length
        hv, lv = highs[c], lows[c]
        if all(hv > highs[j] for j in range(c - length, c + length + 1) if j != c):
            ph[i] = hv
        if all(lv < lows[j] for j in range(c - length, c + length + 1) if j != c):
            pl[i] = lv

    upper = lower = 0.0
    slope_ph = slope_pl = 0.0
    upos = dnos = 0
    trend = 0
    have_u = have_l = False
    up_line = dn_line = None
    # Anchor pivot of each active line, so the chart draws one straight ray per side.
    up_anchor_v = dn_anchor_v = None
    up_anchor_idx = dn_anchor_idx = None
    points = []
    signals = []
    trend_arr = [0] * n

    for i in range(n):
        s = (basis[i] / length * mult) if basis[i] else 0.0
        is_ph = ph[i] is not None
        is_pl = pl[i] is not None

        if is_ph:
            upper, slope_ph, have_u = ph[i], s, True
            up_anchor_v, up_anchor_idx = ph[i], i - length
        elif have_u:
            upper -= slope_ph
        if is_pl:
            lower, slope_pl, have_l = pl[i], s, True
            dn_anchor_v, dn_anchor_idx = pl[i], i - length
        elif have_l:
            lower += slope_pl

        up_line = (upper - slope_ph * length) if have_u else None
        dn_line = (lower + slope_pl * length) if have_l else None

        prev_up, prev_dn = upos, dnos
        if have_u:
            upos = 0 if is_ph else (1 if closes[i] > up_line else upos)
        if have_l:
            dnos = 0 if is_pl else (1 if closes[i] < dn_line else dnos)

        if upos > prev_up:
            trend = 1
            signals.append({"time": times[i], "dir": "long"})
        if dnos > prev_dn:
            trend = -1
            signals.append({"time": times[i], "dir": "short"})
        trend_arr[i] = trend

        # Gap at each new pivot so lines render as separate segments.
        points.append({
            "time": times[i],
            "upper": round(up_line, 2) if (up_line is not None and not is_ph) else None,
            "lower": round(dn_line, 2) if (dn_line is not None and not is_pl) else None,
            "trend": trend,
        })

    # The two currently-active lines as rays from their anchor to the latest bar.
    active = []
    for j in range(n):
        uv = (round(up_anchor_v - slope_ph * (j - up_anchor_idx), 2)
              if (have_u and up_anchor_idx is not None and j >= up_anchor_idx) else None)
        lv = (round(dn_anchor_v + slope_pl * (j - dn_anchor_idx), 2)
              if (have_l and dn_anchor_idx is not None and j >= dn_anchor_idx) else None)
        active.append({"time": times[j], "upper": uv, "lower": lv})

    latest_flip = bool(signals and signals[-1]["time"] == times[-1])
    return {
        "points": points,
        "active": active,
        "signals": signals,
        "latest": {
            "trend": trend_arr[-1],
            "dir": "long" if trend_arr[-1] == 1 else "short" if trend_arr[-1] == -1 else "neutral",
            "flip": latest_flip,
            "upper": round(up_line, 2) if up_line is not None else None,
            "lower": round(dn_line, 2) if dn_line is not None else None,
        },
    }


def inverse_fvg(
    candles: list[dict],
    atr_multi: float = 0.25,
    wick: bool = False,
    disp_num: int = 8,
) -> Optional[dict]:
    """FVGs closed through become inverted zones; a retest that breaks back through signals (bull FVG → short)."""
    n = len(candles)
    if n < 5:
        return None
    o = [c["open"] for c in candles]
    h = [c["high"] for c in candles]
    lo = [c["low"] for c in candles]
    cl = [c["close"] for c in candles]
    tm = [int(c["time"]) for c in candles]
    atr = calc_atr(candles, min(200, max(n - 1, 14)))

    BUFFER = 100
    bull_fvg: list[dict] = []
    bear_fvg: list[dict] = []
    bull_inv: list[dict] = []
    bear_inv: list[dict] = []
    signals: list[dict] = []

    def _fvg_manage(arr, inv_arr, i):
        if len(arr) >= BUFFER:
            arr.pop(0)
        c_top = max(o[i], cl[i])
        c_bot = min(o[i], cl[i])
        for idx in range(len(arr) - 1, -1, -1):
            v = arr[idx]
            if v["dir"] == 1 and c_bot < v["bot"]:
                v["x_val"] = tm[i]
                inv_arr.append(arr.pop(idx))
            elif v["dir"] == -1 and c_top > v["top"]:
                v["x_val"] = tm[i]
                inv_arr.append(arr.pop(idx))

    def _inv_manage(arr, i):
        fire = False
        if len(arr) >= BUFFER:
            arr.pop(0)
        ref_h = h[i] if wick else cl[i - 1]
        ref_l = lo[i] if wick else cl[i - 1]
        for idx in range(len(arr) - 1, -1, -1):
            v = arr[idx]
            bx_top, bx_bot, _dir, st = v["top"], v["bot"], v["dir"], v["state"]
            if st == 0 and _dir == 1:
                v["state"], v["dir"], _dir, st = 1, -1, -1, 1
            elif _dir == -1 and st == 0:
                v["state"], v["dir"], _dir, st = 1, 1, 1, 1
            if st >= 1:
                v["right"] = tm[i]
            if _dir == -1 and st == 1 and cl[i] < bx_bot and ref_h >= bx_bot and ref_h < bx_top:
                fire = True
            if _dir == 1 and st == 1 and cl[i] > bx_top and ref_l <= bx_top and ref_l > bx_bot:
                fire = True
            c_top = max(o[i], cl[i])
            c_bot = min(o[i], cl[i])
            if st >= 1 and ((_dir == -1 and c_top > bx_top) or (_dir == 1 and c_bot < bx_bot)):
                arr.pop(idx)
        return fire

    for i in range(n):
        a = atr[i] * atr_multi if atr[i] else (sum(h[k] - lo[k] for k in range(i + 1)) / (i + 1))
        if i >= 2:
            if lo[i] > h[i - 2] and cl[i - 1] > h[i - 2] and abs(lo[i] - h[i - 2]) > a:
                bull_fvg.append({"left": tm[i - 1], "top": lo[i], "bot": h[i - 2],
                                 "mid": (lo[i] + h[i - 2]) / 2, "dir": 1, "state": 0, "x_val": None})
            if h[i] < lo[i - 2] and cl[i - 1] < lo[i - 2] and abs(lo[i - 2] - h[i]) > a:
                bear_fvg.append({"left": tm[i - 1], "top": lo[i - 2], "bot": h[i],
                                 "mid": (h[i] + lo[i - 2]) / 2, "dir": -1, "state": 0, "x_val": None})
        _fvg_manage(bull_fvg, bull_inv, i)
        _fvg_manage(bear_fvg, bear_inv, i)
        if _inv_manage(bull_inv, i):   # inverted bull FVG -> bearish
            signals.append({"time": tm[i], "dir": "short"})
        if _inv_manage(bear_inv, i):   # inverted bear FVG -> bullish
            signals.append({"time": tm[i], "dir": "long"})

    zones = []
    for v in (bull_inv + bear_inv)[-disp_num * 2:]:
        zones.append({"top": round(v["top"], 2), "bottom": round(v["bot"], 2),
                      "mid": round(v["mid"], 2), "dir": v["dir"],
                      "left": v.get("left"), "x_val": v.get("x_val")})
    last_sigs = [s for s in signals if s["time"] == tm[-1]]
    return {
        "zones": zones,
        "signals": signals,
        "latest": {"new": bool(last_sigs), "dir": last_sigs[-1]["dir"] if last_sigs else None},
    }
