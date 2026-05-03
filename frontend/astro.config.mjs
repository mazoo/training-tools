import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";

export default defineConfig({
  devToolbar: {
    enabled: false,
  },
  integrations: [tailwind()],
  vite: {
    server: {
      proxy: {
        "/api": {
          target: "http://localhost:8000",
          changeOrigin: true,
        },
        "/auth": {
          target: "http://localhost:8000",
          changeOrigin: true,
        },
      },
    },
  },
});
