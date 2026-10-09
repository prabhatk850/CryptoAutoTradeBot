import axios from "axios";

// Same-origin /api (proxied by next.config.mjs) unless a build-time NEXT_PUBLIC_API_URL overrides it.
export const api = axios.create({ baseURL: process.env.NEXT_PUBLIC_API_URL || "/api", timeout: 12000 });

// Retry a pure network error once; never retry 5xx (that would add load to a busy server).
api.interceptors.response.use(undefined, async (error) => {
  const cfg: any = error?.config;
  if (cfg && !error?.response && !cfg.__retried) {
    cfg.__retried = true;
    await new Promise((r) => setTimeout(r, 800));
    return api(cfg);
  }
  return Promise.reject(error);
});

const q = (symbol?: string, sep = "?") => (symbol ? `${sep}symbol=${symbol}` : "");

// Bot
export const startBot = () => api.post("/bot/start");
export const stopBot = () => api.post("/bot/stop");
export const getBotStatus = () => api.get("/bot/status");
export const runBacktest = (symbol?: string, bars = 1500) =>
  api.get(`/bot/backtest?bars=${bars}${q(symbol, "&")}`, { timeout: 90000 });
export const getPerformance = () => api.get("/bot/performance");
export const getTraining = (days = 30, limit = 60) => api.get(`/bot/training?days=${days}&limit=${limit}`);

// Trades
export const getTradeLogs = (limit = 50) => api.get(`/trades/logs?limit=${limit}`);
export const getTradeStats = () => api.get("/trades/stats");
export const getPnl = (symbol?: string) => api.get(`/trades/pnl${q(symbol)}`);
export const getOrders = (limit = 200, symbol?: string, offset = 0) =>
  api.get(`/trades/orders?limit=${limit}&offset=${offset}${q(symbol, "&")}`);
export const getAllPositions = () => api.get("/trades/positions");
export const closePosition = (symbol: string, force = false) =>
  api.post(`/trades/close?symbol=${symbol}${force ? "&force=true" : ""}`);
export const updateStopLoss = (symbol: string, price: number) =>
  api.post(`/trades/sl?symbol=${symbol}&price=${price}`);

// News — refresh=true regenerates the brief (spends AI tokens, can take minutes)
export const getNewsAll = () => api.get("/news/all");
export const getBrief = (refresh = false) =>
  api.get(`/news/brief${refresh ? "?refresh=true" : ""}`, { timeout: refresh ? 180000 : 12000 });

// Market
export const getTicker = (symbol?: string) => api.get(`/market/ticker${q(symbol)}`);
export const getCandles = (symbol?: string, resolution = 5, limit = 250) =>
  api.get(`/market/candles?resolution=${resolution}&limit=${limit}${q(symbol, "&")}`);
export const getIndicators = (symbol?: string, resolution = 5, limit = 250) =>
  api.get(`/market/indicators?resolution=${resolution}&limit=${limit}${q(symbol, "&")}`);
