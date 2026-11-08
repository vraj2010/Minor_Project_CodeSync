import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.CODESYNC_API ?? "http://127.0.0.1:8000";

// In development the page and the API share an origin through this proxy, so a
// local engine needs no CORS settings.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/v1": api, "/health": api },
  },
  build: { chunkSizeWarningLimit: 4000 },
});
