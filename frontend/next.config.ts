import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The browser talks to the same origin; the Next server proxies /api/* to the
  // FastAPI backend so cookies stay first-party and CORS is a non-issue.
  async rewrites() {
    const api = process.env.API_PROXY_URL ?? "http://127.0.0.1:8000";

    return [
      {
        source: "/api/:path*",
        destination: `${api}/api/:path*`,
      },
      // The probes are unversioned and outside the API prefix (`app/api/v1/
      // health.py`): an orchestrator must not track an API version to ask
      // whether the process is alive. `components/health-status.tsx` reads
      // `/health`, so it needs its own rule — `/api/:path*` does not match it.
      { source: "/health", destination: `${api}/health` },
      { source: "/health/ready", destination: `${api}/health/ready` },
    ];
  },
};

export default nextConfig;
