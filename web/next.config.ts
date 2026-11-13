import type { NextConfig } from "next";

const api = process.env.API_INTERNAL_URL || "http://127.0.0.1:8091";

const nextConfig: NextConfig = {
  // Static export is what the single Render process serves. `next dev` keeps the proxy.
  ...(process.env.NEXT_OUTPUT === "export" ? { output: "export" as const } : {}),
  ...(process.env.NEXT_OUTPUT === "export"
    ? {}
    : {
        async rewrites() {
          return [
            { source: "/api/:path*", destination: `${api}/api/:path*` },
            { source: "/health", destination: `${api}/health` },
            { source: "/webhooks/:path*", destination: `${api}/webhooks/:path*` },
          ];
        },
      }),
};

export default nextConfig;
