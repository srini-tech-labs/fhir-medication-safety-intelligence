/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: /v1/* is proxied to the FastAPI backend, so the browser never needs CORS.
// Deployed: set VITE_API_BASE_URL to the API Gateway URL at build time.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/v1": process.env.VITE_DEV_API ?? "http://localhost:8000" } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test/setup.ts"], css: false },
});
