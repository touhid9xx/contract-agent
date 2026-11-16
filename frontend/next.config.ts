import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // `Dockerfile.frontend` serves this app from a container, and `standalone` is
  // what makes that one small artefact instead of a `node_modules` tree the
  // image has to reproduce: `next build` traces the modules each route actually
  // reaches and writes a `server.js` that imports only those. It changes nothing
  // about `next dev` — the `dev` script reads the same config.
  output: "standalone",
  // The browser talks to the same origin; the Next server proxies /api/* to the
  // FastAPI backend so cookies stay first-party and CORS is a non-issue.
  async rewrites() {
    // `||` and not `??`: an empty value must fall back rather than win. `next build`
    // runs with `API_PROXY_URL` set from a Docker build argument, and a
    // `--build-arg API_PROXY_URL=` would otherwise make every destination a bare
    // `/api/:path*` — a rewrite to itself, which fails with no error that names the
    // cause. This value is fixed at build time; `Dockerfile.frontend` says why.
    const api = process.env.API_PROXY_URL || "http://127.0.0.1:8000";

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

// Wires `src/i18n/request.ts` into the build. Without it the production build
// fails every prerender with "Couldn't find next-intl config file" even though
// the file exists: `NextIntlClientProvider` consults the request config while
// rendering on the server, and the plugin is what tells next-intl where to load
// it from (D10-47).
export default createNextIntlPlugin()(nextConfig);
