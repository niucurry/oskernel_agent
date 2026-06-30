import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const apiPort = Number(process.env.FRONTEND_API_PORT || 3130);
const webHost = process.env.FRONTEND_WEB_HOST || "127.0.0.1";
const webPort = Number(process.env.FRONTEND_WEB_PORT || 5173);
const apiTarget = `http://127.0.0.1:${apiPort}`;

export default defineConfig({
  plugins: [vue()],
  server: {
    host: webHost,
    port: webPort,
    proxy: {
      "/api": apiTarget,
      "/reports": apiTarget
    }
  }
});
