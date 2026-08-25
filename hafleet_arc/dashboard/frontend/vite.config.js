import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const apiTarget = process.env.DASHBOARD_API_URL || "http://127.0.0.1:3200";
const port = Number.parseInt(process.env.VITE_PORT || "5173", 10);
const dashboardFrontendDir = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  publicDir: resolve(dashboardFrontendDir, "../static"),
  server: {
    host: "127.0.0.1",
    port,
    fs: {
      allow: [resolve(dashboardFrontendDir, "..")],
    },
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port,
  },
});
