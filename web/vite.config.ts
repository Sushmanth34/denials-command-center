import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": process.env.API_URL ?? "http://127.0.0.1:5080" } },
  preview: { port: 4173, proxy: { "/api": process.env.API_URL ?? "http://127.0.0.1:5080" } },
});
