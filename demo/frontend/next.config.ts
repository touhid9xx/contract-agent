import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The demo talks to the mocked FastAPI service running on :8000.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.DEMO_API_URL ?? "http://127.0.0.1:8000"}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
