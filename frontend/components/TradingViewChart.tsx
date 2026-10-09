"use client";
import { useEffect, useRef } from "react";
import { createChart, ColorType, LineStyle, TickMarkType, IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";

interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface ChartOrder {
  timestamp: string;
  symbol: string;
  side: "LONG" | "SHORT";
  price: number;
  lots: number;
  realized_pnl: number | null;
  pnl_status: "realized" | "open" | "failed";
}

export interface SuperTrendData {
  points: { time: number; ts: number | null; os: number; ama: number | null }[];
}

export interface TrendlineData {
  points: { time: number; upper: number | null; lower: number | null }[];
  // The two currently-active trendlines (one descending from the latest swing high,
  // one ascending from the latest swing low) as clean straight rays.
  active?: { time: number; upper: number | null; lower: number | null }[];
}

export interface FvgData {
  zones: { isbull: boolean; top: number; bottom: number; mid?: number; start: number }[];
  end?: number;
}

export interface IfvgData {
  zones: { dir: number; top: number; bottom: number; mid?: number; left?: number }[];
  end?: number;
}

export interface SmcData {
  trend?: string;
  swings?: { label: string; price: number; kind: "high" | "low"; time: number }[];
  structure_break?: { type: string; dir: string; level: number; time: number } | null;
  liquidity?: {
    buyside?: number[]; sellside?: number[];
    equal_highs?: number[]; equal_lows?: number[];
    prev_day_high?: number | null; prev_day_low?: number | null;
  };
  recent_sweep?: { side: string; level: number; dir: string; time: number } | null;
  order_blocks?: {
    bullish?: { low: number; high: number; time: number; mitigated: boolean }[];
    bearish?: { low: number; high: number; time: number; mitigated: boolean }[];
  };
  premium_discount?: {
    range_high: number; range_low: number; equilibrium: number;
    zone: string; pct_of_range: number; discount_ote: number[];
  } | null;
}

export interface MacdData {
  points: { time: number; macd: number; signal: number; hist: number }[];
  latest?: { macd: number; signal: number; hist: number; state: string };
}

export interface VolumeData {
  points: { time: number; value: number; up: boolean }[];
  ma?: { time: number; value: number }[];
  latest?: { volume: number; avg20: number; rel: number | null };
}

export interface EmaSeries {
  points: { time: number; value: number }[];
}

interface Props {
  candles: Candle[];
  symbol: string;
  livePrice?: number;   // real-time mark price → moves the forming candle like the exchange
  orders?: ChartOrder[];
  openEntry?: { side: string; avg: number; unrealized: number; sl?: number | null; tps?: number[] } | null;
  supertrend?: SuperTrendData | null;
  showSupertrend?: boolean;
  trendline?: TrendlineData | null;
  showTrendline?: boolean;
  fvg?: FvgData | null;
  showFvg?: boolean;
  ifvg?: IfvgData | null;
  showIfvg?: boolean;
  smc?: SmcData | null;
  showSmc?: boolean;
  macd?: MacdData | null;
  showMacd?: boolean;
  volume?: VolumeData | null;
  showVolume?: boolean;
  ema200?: EmaSeries | null;
  showEma200?: boolean;
  showEma9?: boolean;
  showEma21?: boolean;
  showTrades?: boolean;
  tool?: "none" | "trendline" | "long" | "short";
  clearSignal?: number;
  fitKey?: string;
  height?: number;
}

function toSec(t: number): number {
  return typeof t === "number" && t > 1e10 ? Math.floor(t / 1000) : t;
}

// ---- Custom rectangle primitive: draws shaded FVG/IFVG zone boxes + midlines ----
interface Zone { t1: number; t2: number; top: number; bottom: number; mid?: number; fill: string; line: string; }

class ZonesRenderer {
  constructor(private src: ZonesPrimitive) {}
  draw(target: any) {
    const { _chart: chart, _series: series, _zones: zones } = this.src;
    if (!chart || !series || !zones.length) return;
    const ts = chart.timeScale();
    target.useBitmapCoordinateSpace((scope: any) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
      const w = scope.bitmapSize.width;
      for (const z of zones) {
        let x1 = ts.timeToCoordinate(z.t1 as any);
        let x2 = ts.timeToCoordinate(z.t2 as any);
        const y1 = series.priceToCoordinate(z.top);
        const y2 = series.priceToCoordinate(z.bottom);
        if (y1 == null || y2 == null) continue;
        if (x1 == null) x1 = 0;
        if (x2 == null) x2 = w / hr;
        const left = Math.min(x1, x2) * hr, right = Math.max(x1, x2) * hr;
        const top = Math.min(y1, y2) * vr, bot = Math.max(y1, y2) * vr;
        ctx.fillStyle = z.fill;
        ctx.fillRect(left, top, right - left, bot - top);
        if (z.mid != null) {
          const ym = series.priceToCoordinate(z.mid);
          if (ym != null) {
            ctx.strokeStyle = z.line;
            ctx.setLineDash([4 * hr, 4 * hr]);
            ctx.beginPath();
            ctx.moveTo(left, ym * vr);
            ctx.lineTo(right, ym * vr);
            ctx.stroke();
            ctx.setLineDash([]);
          }
        }
      }
    });
  }
}

class ZonesPaneView {
  constructor(private src: ZonesPrimitive) {}
  update() {}
  renderer() { return new ZonesRenderer(this.src); }
}

class ZonesPrimitive {
  _chart: any = null;
  _series: any = null;
  _zones: Zone[] = [];
  _requestUpdate: (() => void) | null = null;
  private _pv = new ZonesPaneView(this);
  attached(p: any) { this._chart = p.chart; this._series = p.series; this._requestUpdate = p.requestUpdate; }
  detached() { this._chart = null; this._series = null; }
  setZones(z: Zone[]) { this._zones = z; this._requestUpdate?.(); }
  updateAllViews() {}
  paneViews() { return [this._pv]; }
}

// EMA matching the backend's ewm(span, adjust=False).
function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const out: number[] = [];
  let prev = values[0] ?? 0;
  values.forEach((v, i) => {
    prev = i === 0 ? v : v * k + prev * (1 - k);
    out.push(prev);
  });
  return out;
}

const IST = "Asia/Kolkata";
const istTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: false,
});
const istFull = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST, day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});
const istDay = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "numeric", month: "short" });
const istMonth = new Intl.DateTimeFormat("en-IN", { timeZone: IST, month: "short" });
const istYear = new Intl.DateTimeFormat("en-IN", { timeZone: IST, year: "numeric" });

/**
 * Format an axis tick for the span it represents, not always as a clock time.
 *
 * lightweight-charts passes a TickMarkType telling us what the label stands for
 * (Year / Month / DayOfMonth / Time). Formatting everything as HH:mm made daily
 * candles — which all open at 00:00 UTC, i.e. 05:30 IST — render as an endless
 * row of identical "05:30" labels.
 */
function formatTick(t: number, tickMarkType: TickMarkType): string {
  const d = new Date(t * 1000);
  switch (tickMarkType) {
    case TickMarkType.Year: return istYear.format(d);
    case TickMarkType.Month: return istMonth.format(d);
    case TickMarkType.DayOfMonth: return istDay.format(d);
    default: return istTime.format(d);
  }
}

export default function TradingViewChart({
  candles, symbol, livePrice = 0, orders = [], openEntry = null,
  supertrend = null, showSupertrend = false,
  trendline = null, showTrendline = false,
  fvg = null, showFvg = false,
  ifvg = null, showIfvg = false,
  smc = null, showSmc = false,
  macd = null, showMacd = false,
  volume = null, showVolume = false,
  ema200 = null, showEma200 = false,
  showEma9 = false, showEma21 = false, showTrades = true,
  tool = "none", clearSignal = 0, fitKey = "", height = 460,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const ema9Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema21Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const ema200Ref = useRef<ISeriesApi<"Line"> | null>(null);
  const stBullRef = useRef<ISeriesApi<"Line"> | null>(null);
  const stBearRef = useRef<ISeriesApi<"Line"> | null>(null);
  const stAmaRef = useRef<ISeriesApi<"Line"> | null>(null);
  const tlBullRef = useRef<ISeriesApi<"Line"> | null>(null);
  const tlBearRef = useRef<ISeriesApi<"Line"> | null>(null);
  const zonesPrimRef = useRef<ZonesPrimitive | null>(null);
  const smcZonesPrimRef = useRef<ZonesPrimitive | null>(null);
  const smcLinesRef = useRef<any[]>([]);
  const smcMarkerSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdHistRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const macdLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const macdSignalRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volHistRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const volMaRef = useRef<ISeriesApi<"Line"> | null>(null);
  const trendsRef = useRef<ISeriesApi<"Line">[]>([]);
  const priceLinesRef = useRef<any[]>([]);
  const toolRef = useRef(tool);
  const pointsRef = useRef<{ time: UTCTimestamp; value: number }[]>([]);
  const needsFitRef = useRef(true);
  const liveBarRef = useRef<{ time: number; open: number; high: number; low: number; close: number } | null>(null);

  // ---- create chart ONCE (recreate only when symbol/timeframe/height change) ----
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: "#0d1117" }, textColor: "#9ca3af" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      width: containerRef.current.clientWidth,
      height,
      localization: { timeFormatter: (t: any) => istFull.format(new Date((t as number) * 1000)) },
      timeScale: {
        timeVisible: true, secondsVisible: false,
        tickMarkFormatter: (t: any, tickMarkType: TickMarkType) => formatTick(t as number, tickMarkType),
      },
      crosshair: { mode: 0 },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#00C896", downColor: "#ef4444",
      borderUpColor: "#00C896", borderDownColor: "#ef4444",
      wickUpColor: "#00C896", wickDownColor: "#ef4444",
    });
    chartRef.current = chart;
    candleRef.current = series;

    // FVG/IFVG shaded-box primitive
    try {
      const prim = new ZonesPrimitive();
      (series as any).attachPrimitive?.(prim);
      zonesPrimRef.current = prim;
      // separate primitive for SMC order-block + premium/discount shading
      const smcPrim = new ZonesPrimitive();
      (series as any).attachPrimitive?.(smcPrim);
      smcZonesPrimRef.current = smcPrim;
    } catch { /* primitive API unavailable */ }

    // invisible line series that hosts SMC swing/break markers so they never
    // clobber the trade markers on the candle series
    smcMarkerSeriesRef.current = chart.addLineSeries({
      color: "rgba(0,0,0,0)", lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
    });

    // trendline drawing: collect two clicks
    const onClick = (param: any) => {
      if (toolRef.current !== "trendline" || !param.point || param.time == null) return;
      const price = series.coordinateToPrice(param.point.y);
      if (price == null) return;
      pointsRef.current.push({ time: param.time as UTCTimestamp, value: price });
      if (pointsRef.current.length === 2) {
        const pts = [...pointsRef.current].sort((a, b) => (a.time as number) - (b.time as number));
        const line = chart.addLineSeries({ color: "#e5e7eb", lineWidth: 2, lineStyle: LineStyle.Solid, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
        line.setData(pts as any);
        trendsRef.current.push(line);
        pointsRef.current = [];
      }
    };
    chart.subscribeClick(onClick);

    const onResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      chart.unsubscribeClick(onClick);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      ema9Ref.current = null;
      ema21Ref.current = null;
      ema200Ref.current = null;
      stBullRef.current = null;
      stBearRef.current = null;
      stAmaRef.current = null;
      tlBullRef.current = null;
      tlBearRef.current = null;
      zonesPrimRef.current = null;
      macdHistRef.current = null;
      macdLineRef.current = null;
      macdSignalRef.current = null;
      volHistRef.current = null;
      volMaRef.current = null;
      trendsRef.current = [];
      priceLinesRef.current = [];
      pointsRef.current = [];
    };
  }, [symbol, height]);

  useEffect(() => { toolRef.current = tool; }, [tool]);

  // refit the view when symbol/timeframe changes (not on every 30s data refresh)
  useEffect(() => { needsFitRef.current = true; }, [fitKey]);

  // ---- clear drawings on signal ----
  useEffect(() => {
    if (!chartRef.current) return;
    trendsRef.current.forEach((s) => chartRef.current?.removeSeries(s));
    trendsRef.current = [];
    pointsRef.current = [];
  }, [clearSignal]);

  // ---- update candle + indicator data (no chart recreation → keeps zoom & drawings) ----
  useEffect(() => {
    const chart = chartRef.current, series = candleRef.current;
    if (!chart || !series || candles.length === 0) return;

    // Sanitize: drop any candle with a null/NaN OHLC or bad time, sort ascending,
    // and dedupe timestamps (Lightweight Charts crashes on null values or dup times).
    const num = (v: any) => (typeof v === "number" ? v : Number(v));
    const cleaned = candles
      .map((c) => ({
        time: toSec(num(c.time)) as UTCTimestamp,
        open: num(c.open), high: num(c.high), low: num(c.low), close: num(c.close),
      }))
      .filter((c) =>
        Number.isFinite(c.time) &&
        Number.isFinite(c.open) && Number.isFinite(c.high) &&
        Number.isFinite(c.low) && Number.isFinite(c.close))
      .sort((a, b) => (a.time as number) - (b.time as number));

    const data: typeof cleaned = [];
    for (const c of cleaned) {
      if (data.length && data[data.length - 1].time === c.time) data[data.length - 1] = c;
      else data.push(c);
    }
    if (data.length === 0) return;

    series.setData(data);
    if (needsFitRef.current) {
      chart.timeScale().fitContent();
      needsFitRef.current = false;
    }

    const closes = data.map((d) => d.close);
    const times = data.map((d) => d.time);

    const emaData = (period: number) =>
      ema(closes, period)
        .map((v, i) => ({ time: times[i], value: v }))
        .filter((p) => Number.isFinite(p.value as number));

    // EMA 9
    if (showEma9) {
      if (!ema9Ref.current) ema9Ref.current = chart.addLineSeries({ color: "#f59e0b", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      ema9Ref.current.setData(emaData(9));
    } else if (ema9Ref.current) {
      chart.removeSeries(ema9Ref.current); ema9Ref.current = null;
    }
    // EMA 21
    if (showEma21) {
      if (!ema21Ref.current) ema21Ref.current = chart.addLineSeries({ color: "#a78bfa", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      ema21Ref.current.setData(emaData(21));
    } else if (ema21Ref.current) {
      chart.removeSeries(ema21Ref.current); ema21Ref.current = null;
    }
  }, [candles, showEma9, showEma21]);

  // ---- REAL-TIME: move the forming candle with the live mark price (like Delta) ----
  // Instead of rebuilding the whole array, we `update()` only the last bar every time a
  // fresh price arrives (~1s). Accumulate high/low within the current time bucket; when
  // the bucket rolls over, start a new forming candle. Runs after setData so a 30s
  // historical refresh is immediately re-topped with the live bar (no flicker).
  useEffect(() => {
    const series = candleRef.current;
    if (!series || !livePrice || livePrice <= 0 || candles.length === 0) return;
    const num = (v: any) => (typeof v === "number" ? v : Number(v));
    const last = candles[candles.length - 1];
    const lastT = toSec(num(last.time));
    const prevT = candles.length >= 2 ? toSec(num(candles[candles.length - 2].time)) : lastT - 60;
    const step = Math.max(1, lastT - prevT);                       // candle width in seconds
    const bucket = Math.max(lastT, Math.floor(Date.now() / 1000 / step) * step);

    let bar = liveBarRef.current;
    if (!bar || bar.time !== bucket) {
      // new bucket → open a fresh candle; same bucket as the last historical → seed from it
      bar = bucket > lastT
        ? { time: bucket, open: livePrice, high: livePrice, low: livePrice, close: livePrice }
        : { time: lastT, open: num(last.open), high: Math.max(num(last.high), livePrice),
            low: Math.min(num(last.low), livePrice), close: livePrice };
    } else {
      bar = { ...bar, high: Math.max(bar.high, livePrice), low: Math.min(bar.low, livePrice), close: livePrice };
    }
    liveBarRef.current = bar;
    try { series.update(bar as any); } catch { /* series not ready yet */ }
  }, [livePrice, candles]);

  // ---- EMA 200 (server-computed over full history so it's warmed up) ----
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!showEma200 || !ema200?.points?.length) {
      if (ema200Ref.current) { chart.removeSeries(ema200Ref.current); ema200Ref.current = null; }
      return;
    }
    if (!ema200Ref.current)
      ema200Ref.current = chart.addLineSeries({ color: "#e5e7eb", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    const data = ema200.points
      .map((p) => ({ time: toSec(p.time) as UTCTimestamp, value: p.value }))
      .filter((p) => Number.isFinite(p.value));
    ema200Ref.current.setData(data);
  }, [ema200, showEma200]);

  // ---- SuperTrend AI overlay (colored trailing stop + adaptive MA) ----
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const clear = () => {
      [stBullRef, stBearRef, stAmaRef].forEach((r) => {
        if (r.current) { chart.removeSeries(r.current); r.current = null; }
      });
    };
    const pts = supertrend?.points;
    if (!showSupertrend || !pts || pts.length === 0) { clear(); return; }

    if (!stBullRef.current) stBullRef.current = chart.addLineSeries({ color: "#00C896", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    if (!stBearRef.current) stBearRef.current = chart.addLineSeries({ color: "#ef4444", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    if (!stAmaRef.current) stAmaRef.current = chart.addLineSeries({ color: "rgba(120,160,255,0.45)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });

    const bull: any[] = [], bear: any[] = [], ama: any[] = [];
    let lastT = -1;
    for (const p of pts) {
      const t = (typeof p.time === "number" ? p.time : Number(p.time)) as UTCTimestamp;
      if (!Number.isFinite(t as number) || (t as number) <= lastT) continue; // ascending unique
      lastT = t as number;
      bull.push(p.ts != null && p.os === 1 ? { time: t, value: p.ts } : { time: t });
      bear.push(p.ts != null && p.os === 0 ? { time: t, value: p.ts } : { time: t });
      ama.push(p.ama != null ? { time: t, value: p.ama } : { time: t });
    }
    stBullRef.current.setData(bull);
    stBearRef.current.setData(bear);
    stAmaRef.current.setData(ama);
  }, [supertrend, showSupertrend]);

  // ---- Trendline Breakout Navigator overlay (active trendline level, colored by trend) ----
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const clear = () => {
      [tlBullRef, tlBearRef].forEach((r) => {
        if (r.current) { chart.removeSeries(r.current); r.current = null; }
      });
    };
    // Use the two active rays (clean straight lines). Fall back to points only if absent.
    const pts = trendline?.active ?? trendline?.points;
    if (!showTrendline || !pts || pts.length === 0) { clear(); return; }

    // Match LuxAlgo: upper (down-trendline / resistance) = teal, lower (up-trendline / support) = red.
    if (!tlBearRef.current) tlBearRef.current = chart.addLineSeries({ color: "#14b8a6", lineWidth: 2, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    if (!tlBullRef.current) tlBullRef.current = chart.addLineSeries({ color: "#ef4444", lineWidth: 2, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });

    const lower: any[] = [], upper: any[] = [];
    let lastT = -1;
    for (const p of pts) {
      const t = (typeof p.time === "number" ? p.time : Number(p.time)) as UTCTimestamp;
      if (!Number.isFinite(t as number) || (t as number) <= lastT) continue;
      lastT = t as number;
      // whitespace point (time only) where the line isn't active, so each side
      // renders as a single continuous diagonal ray with no connecting jumps.
      lower.push(p.lower != null ? { time: t, value: p.lower } : { time: t });
      upper.push(p.upper != null ? { time: t, value: p.upper } : { time: t });
    }
    tlBearRef.current.setData(upper);   // teal — resistance (descending from swing high)
    tlBullRef.current.setData(lower);   // red — support (ascending from swing low)
  }, [trendline, showTrendline]);

  // ---- FVG + IFVG shaded zone boxes (only zones already filtered to >=0.6% by backend) ----
  useEffect(() => {
    const prim = zonesPrimRef.current;
    if (!prim) return;
    const zones: Zone[] = [];
    const lastT = candles.length ? toSec(Number(candles[candles.length - 1].time)) : 0;
    if (showFvg && fvg?.zones) {
      const end = fvg.end ?? lastT;
      for (const z of fvg.zones) {
        if (!Number.isFinite(z.top) || !Number.isFinite(z.bottom)) continue;
        zones.push({
          t1: Number(z.start), t2: end, top: z.top, bottom: z.bottom, mid: z.mid,
          fill: z.isbull ? "rgba(8,153,129,0.16)" : "rgba(242,54,69,0.16)",
          line: "rgba(120,123,134,0.55)",
        });
      }
    }
    if (showIfvg && ifvg?.zones) {
      const end = ifvg.end ?? lastT;
      for (const z of ifvg.zones) {
        if (!Number.isFinite(z.top) || !Number.isFinite(z.bottom)) continue;
        zones.push({
          t1: Number(z.left ?? end), t2: end, top: z.top, bottom: z.bottom, mid: z.mid,
          fill: z.dir === 1 ? "rgba(8,153,129,0.22)" : "rgba(242,54,69,0.22)",
          line: "rgba(120,123,134,0.7)",
        });
      }
    }
    prim.setZones(zones);
  }, [fvg, ifvg, showFvg, showIfvg, candles]);

  // ---- SMC overlays: order-block zones, premium/discount shading, liquidity &
  //      structure lines, swing (HH/HL/LH/LL) + sweep markers ----
  useEffect(() => {
    const series = candleRef.current;
    const prim = smcZonesPrimRef.current;
    const mk = smcMarkerSeriesRef.current;
    if (!series) return;

    // clear previous SMC drawings
    smcLinesRef.current.forEach((pl) => series.removePriceLine(pl));
    smcLinesRef.current = [];
    prim?.setZones([]);
    mk?.setMarkers([]);

    if (!showSmc || !smc || candles.length === 0) { mk?.setData([]); return; }

    const times = candles.map((c) => toSec(Number(c.time))).filter((t) => Number.isFinite(t)).sort((a, b) => a - b);
    if (!times.length) return;
    const firstT = times[0], lastT = times[times.length - 1];
    const snap = (t: number) => { let best = times[0]; for (const ct of times) { if (ct <= t) best = ct; else break; } return best; };

    // anchor the (invisible) marker series to the candle time range
    mk?.setData(candles.map((c) => ({ time: toSec(Number(c.time)) as UTCTimestamp, value: c.close })) as any);

    // --- zones: premium/discount shading + order blocks ---
    const zones: Zone[] = [];
    const pd = smc.premium_discount;
    if (pd && Number.isFinite(pd.range_high) && Number.isFinite(pd.range_low)) {
      zones.push({ t1: firstT, t2: lastT, top: pd.range_high, bottom: pd.equilibrium, fill: "rgba(242,54,69,0.05)", line: "" });
      zones.push({ t1: firstT, t2: lastT, top: pd.equilibrium, bottom: pd.range_low, fill: "rgba(8,153,129,0.05)", line: "" });
    }
    const pushOB = (o: any, bull: boolean) => {
      if (!o || !Number.isFinite(o.high) || !Number.isFinite(o.low)) return;
      const faded = o.mitigated ? 0.07 : 0.16;
      zones.push({
        t1: Math.max(toSec(Number(o.time)), firstT), t2: lastT, top: o.high, bottom: o.low,
        fill: bull ? `rgba(8,153,129,${faded})` : `rgba(242,54,69,${faded})`,
        line: bull ? "rgba(8,153,129,0.55)" : "rgba(242,54,69,0.55)",
      });
    };
    (smc.order_blocks?.bullish || []).forEach((o) => pushOB(o, true));
    (smc.order_blocks?.bearish || []).forEach((o) => pushOB(o, false));
    prim?.setZones(zones);

    // --- horizontal lines: liquidity, equilibrium, structure break ---
    const addLine = (price?: number | null, color = "#8b8fa3", title = "", style: LineStyle = LineStyle.Dashed, width = 1) => {
      if (price == null || !Number.isFinite(price)) return;
      smcLinesRef.current.push(series.createPriceLine({ price, color, lineWidth: width as any, lineStyle: style, axisLabelVisible: true, title }));
    };
    if (pd) addLine(pd.equilibrium, "#3b82f6", "EQ 50%", LineStyle.Dotted);
    (smc.liquidity?.buyside || []).slice(0, 2).forEach((lv) => addLine(lv, "#f59e0b", "BSL"));   // buy-side liquidity (above)
    (smc.liquidity?.sellside || []).slice(0, 2).forEach((lv) => addLine(lv, "#22d3ee", "SSL"));  // sell-side liquidity (below)
    addLine(smc.liquidity?.prev_day_high, "#8b8fa3", "PDH", LineStyle.Dotted);
    addLine(smc.liquidity?.prev_day_low, "#8b8fa3", "PDL", LineStyle.Dotted);
    if (smc.structure_break) {
      const sb = smc.structure_break;
      addLine(sb.level, sb.dir === "bullish" ? "#00C896" : "#ef4444",
        `${sb.type} ${sb.dir === "bullish" ? "↑" : "↓"}`, LineStyle.Solid, 2);
    }

    // --- markers: swing labels + latest liquidity sweep ---
    const markers: any[] = [];
    (smc.swings || []).forEach((s) => {
      const t = toSec(Number(s.time));
      if (!Number.isFinite(t) || t < firstT || t > lastT) return;
      const bull = s.label === "HH" || s.label === "HL";
      const bear = s.label === "LH" || s.label === "LL";
      markers.push({
        time: snap(t) as UTCTimestamp,
        position: s.kind === "high" ? "aboveBar" : "belowBar",
        color: bull ? "#00C896" : bear ? "#ef4444" : "#9ca3af",
        shape: "circle", text: s.label,
      });
    });
    if (smc.recent_sweep) {
      const t = toSec(Number(smc.recent_sweep.time));
      if (Number.isFinite(t) && t >= firstT && t <= lastT) {
        const buy = smc.recent_sweep.side === "buyside";
        markers.push({
          time: snap(t) as UTCTimestamp, position: buy ? "aboveBar" : "belowBar",
          color: "#eab308", shape: buy ? "arrowDown" : "arrowUp", text: "sweep",
        });
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    mk?.setMarkers(markers);
  }, [smc, showSmc, candles]);

  // ---- MACD sub-pane (histogram + MACD/signal lines) on a bottom price scale ----
  useEffect(() => {
    const chart = chartRef.current, candle = candleRef.current;
    if (!chart || !candle) return;
    const MSCALE = "macd";
    const removeMacd = () => {
      [macdHistRef, macdLineRef, macdSignalRef].forEach((r) => {
        if (r.current) { try { chart.removeSeries(r.current); } catch { /* already gone */ } r.current = null; }
      });
      try { chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } }); } catch { /* noop */ }
    };
    if (!showMacd || !macd?.points?.length) { removeMacd(); return; }

    // reserve the bottom ~26% of the chart for the MACD pane
    try { chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.05, bottom: 0.28 } }); } catch { /* noop */ }
    if (!macdHistRef.current) {
      macdHistRef.current = chart.addHistogramSeries({ priceScaleId: MSCALE, priceLineVisible: false, lastValueVisible: false });
      macdHistRef.current.createPriceLine({ price: 0, color: "rgba(120,123,134,0.5)", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false });
    }
    if (!macdLineRef.current)
      macdLineRef.current = chart.addLineSeries({ priceScaleId: MSCALE, color: "#2962ff", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    if (!macdSignalRef.current)
      macdSignalRef.current = chart.addLineSeries({ priceScaleId: MSCALE, color: "#ff6d00", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    try { chart.priceScale(MSCALE).applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } }); } catch { /* noop */ }

    const pts = [...macd.points].filter((p) => Number.isFinite(p.hist)).sort((a, b) => a.time - b.time);
    let prev = 0;
    const histData = pts.map((p) => {
      const rising = p.hist > prev; prev = p.hist;
      // match the Pine script's four-state histogram coloring
      const color = p.hist >= 0 ? (rising ? "#26a69a" : "#b2dfdb") : (rising ? "#ffcdd2" : "#ff5252");
      return { time: toSec(p.time) as UTCTimestamp, value: p.hist, color };
    });
    macdHistRef.current.setData(histData);
    macdLineRef.current.setData(pts.map((p) => ({ time: toSec(p.time) as UTCTimestamp, value: p.macd })));
    macdSignalRef.current.setData(pts.map((p) => ({ time: toSec(p.time) as UTCTimestamp, value: p.signal })));
  }, [macd, showMacd]);

  // ---- Volume: direction-colored histogram + 20-period MA, overlaid at the bottom ----
  useEffect(() => {
    const chart = chartRef.current, candle = candleRef.current;
    if (!chart || !candle) return;
    const VSCALE = "volume";
    const remove = () => {
      [volHistRef, volMaRef].forEach((r) => {
        if (r.current) { try { chart.removeSeries(r.current); } catch { /* gone */ } r.current = null; }
      });
    };
    if (!showVolume || !volume?.points?.length) { remove(); return; }

    if (!volHistRef.current)
      volHistRef.current = chart.addHistogramSeries({ priceScaleId: VSCALE, priceLineVisible: false, lastValueVisible: false });
    if (!volMaRef.current)
      volMaRef.current = chart.addLineSeries({ priceScaleId: VSCALE, color: "#e0b040", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    // overlay at the bottom; if the MACD pane is open, float just above it
    const margins = showMacd ? { top: 0.5, bottom: 0.3 } : { top: 0.82, bottom: 0 };
    try { chart.priceScale(VSCALE).applyOptions({ scaleMargins: margins }); } catch { /* noop */ }

    const pts = [...volume.points].sort((a, b) => a.time - b.time);
    volHistRef.current.setData(pts.map((p) => ({
      time: toSec(p.time) as UTCTimestamp, value: p.value,
      color: p.up ? "rgba(38,166,154,0.5)" : "rgba(239,83,80,0.5)",
    })));
    volMaRef.current.setData((volume.ma || []).map((m) => ({ time: toSec(m.time) as UTCTimestamp, value: m.value })));
  }, [volume, showVolume, showMacd]);

  // ---- trade markers + open-position price line ----
  useEffect(() => {
    const chart = chartRef.current, series = candleRef.current;
    if (!chart || !series || candles.length === 0) return;

    // clear existing price lines
    priceLinesRef.current.forEach((pl) => series.removePriceLine(pl));
    priceLinesRef.current = [];

    if (!showTrades) { series.setMarkers([]); return; }

    const candleTimes = candles
      .map((c) => toSec(typeof c.time === "number" ? c.time : Number(c.time)))
      .filter((t) => Number.isFinite(t))
      .sort((a, b) => a - b);
    if (candleTimes.length === 0) { series.setMarkers([]); return; }
    const firstT = candleTimes[0], lastT = candleTimes[candleTimes.length - 1];
    const snap = (t: number) => {
      let best = candleTimes[0];
      for (const ct of candleTimes) { if (ct <= t) best = ct; else break; }
      return best;
    };

    const markers = (orders || [])
      .filter((o) => o && o.symbol === symbol && o.timestamp && Number.isFinite(o.price))
      .map((o) => {
        const t = Math.floor(new Date(o.timestamp).getTime() / 1000);
        if (!Number.isFinite(t) || t < firstT - 86400 || t > lastT + 86400) return null;
        const isLong = o.side === "LONG";
        const closed = o.pnl_status === "realized" && o.realized_pnl != null;
        return {
          time: snap(t) as UTCTimestamp,
          position: isLong ? "belowBar" : "aboveBar",
          color: closed ? (o.realized_pnl! >= 0 ? "#00C896" : "#ef4444") : (isLong ? "#00C896" : "#ef4444"),
          shape: isLong ? "arrowUp" : "arrowDown",
          text: `${o.side} ${o.price}${closed ? ` (${o.realized_pnl! >= 0 ? "+" : ""}${o.realized_pnl})` : ""}`,
        } as any;
      })
      .filter(Boolean)
      .sort((a: any, b: any) => a.time - b.time);

    series.setMarkers(markers);

    // open-position entry / SL / TP lines (SL & TP are live Delta orders)
    if (openEntry && Number.isFinite(openEntry.avg) && openEntry.avg > 0) {
      priceLinesRef.current.push(series.createPriceLine({
        price: openEntry.avg,
        color: openEntry.side === "LONG" ? "#00C896" : "#ef4444",
        lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
        title: `${openEntry.side} entry`,
      }));
      if (Number.isFinite(openEntry.sl as number)) {
        priceLinesRef.current.push(series.createPriceLine({
          price: openEntry.sl as number, color: "#ef4444",
          lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "SL",
        }));
      }
      (openEntry.tps || []).forEach((tp, i) => {
        if (!Number.isFinite(tp)) return;
        priceLinesRef.current.push(series.createPriceLine({
          price: tp, color: "#00C896",
          lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
          title: `TP${i + 1}`,
        }));
      });
    }
  }, [orders, openEntry, showTrades, candles, symbol]);

  return (
    <div className="relative">
      <div ref={containerRef} className="w-full rounded-lg overflow-hidden" style={{ height }} />
      {tool === "trendline" && (
        <div className="absolute top-2 left-1/2 -translate-x-1/2 text-[11px] text-gray-300 bg-[#161b22] border border-[#30363d] rounded px-2 py-1 pointer-events-none">
          Trendline: click two points on the chart
        </div>
      )}
    </div>
  );
}
