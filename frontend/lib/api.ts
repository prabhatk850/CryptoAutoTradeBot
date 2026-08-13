import axios from "axios";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const api = axios.create({ baseURL: BASE, timeout: 12000 });

// Retry ONLY true network errors (backend unreachable / timeout) once, briefly.
// Do NOT retry 5xx — that would amplify load when the server is busy.
api.interceptors.response.use(undefined, async (error) => {
  const cfg: any = error?.config;
  const networkError = !error?.response;  // no HTTP response at all
  if (cfg && networkError && !cfg.__retried) {
    cfg.__retried = true;
    await new Promise((r) => setTimeout(r, 800));
    return api(cfg);
  }
  return Promise.reject(error);
});

// Bot controls
export const startBot  = () => api.post("/bot/start");
export const stopBot   = () => api.post("/bot/stop");
export const getBotStatus = () => api.get("/bot/status");
export const setBotSymbol = (symbol: string) => api.post(`/bot/symbol?symbol=${symbol}`);

// Trade data
export const getTradeLogs = (limit = 50) => api.get(`/trades/logs?limit=${limit}`);
export const getTradeStats = () => api.get("/trades/stats");
export const getPnl = (symbol?: string) => api.get(`/trades/pnl${symbol ? `?symbol=${symbol}` : ""}`);
export const getOrders = (limit = 200, symbol?: string, offset = 0) =>
  api.get(`/trades/orders?limit=${limit}&offset=${offset}${symbol ? `&symbol=${symbol}` : ""}`);
export const getAllPositions = () => api.get("/trades/positions");
export const closePosition = (symbol: string) => api.post(`/trades/close?symbol=${symbol}`);
export const updateStopLoss = (symbol: string, price: number) =>
  api.post(`/trades/sl?symbol=${symbol}&price=${price}`);
export const runBacktest = (symbol?: string, bars = 1500) =>
  api.get(`/bot/backtest?bars=${bars}${symbol ? `&symbol=${symbol}` : ""}`, { timeout: 90000 });
export const getPerformance = () => api.get("/bot/performance");

// News + economic calendar
export const getNewsAll = () => api.get("/news/all");

// Market
export const getTicker  = (symbol?: string) => api.get(`/market/ticker${symbol ? `?symbol=${symbol}` : ""}`);
export const getCandles = (symbol?: string, resolution = 5, limit = 250) =>
  api.get(`/market/candles?resolution=${resolution}&limit=${limit}${symbol ? `&symbol=${symbol}` : ""}`);
export const getIndicators = (symbol?: string, resolution = 5, limit = 250) =>
  api.get(`/market/indicators?resolution=${resolution}&limit=${limit}${symbol ? `&symbol=${symbol}` : ""}`);
export const getWallet    = () => api.get("/market/wallet");
export const getPositions = () => api.get("/market/positions");
