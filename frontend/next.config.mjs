/** @type {import('next').NextConfig} */
const nextConfig = {
  // Brief refresh and backtest outlive the proxy's default 30s timeout.
  experimental: { proxyTimeout: 180_000 },
  async rewrites() {
    // Server-side proxy: only port 4000 is public and there's no CORS. Baked in at `next build`.
    const backend = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
};
export default nextConfig;
