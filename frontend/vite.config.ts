import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Inside docker compose the backend is reachable as http://backend:8000;
// when running vite outside compose, set BACKEND_ORIGIN=http://localhost:8000.
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://backend:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: backendOrigin, changeOrigin: true },
    },
  },
});
