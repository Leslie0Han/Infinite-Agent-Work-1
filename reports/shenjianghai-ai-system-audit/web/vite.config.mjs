import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { cloudflare } from "@cloudflare/vite-plugin";
import { sites } from "./build/sites-vite-plugin.js";

export default defineConfig({
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [
    react(),
    sites(),
    cloudflare({
      viteEnvironment: { name: "server" },
      config: {
        main: "./worker/index.js",
        compatibility_date: "2026-05-22",
        assets: { not_found_handling: "single-page-application" },
      },
    }),
  ],
});
