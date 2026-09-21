import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "frontend",
  plugins: [react()],
  test: {
    environment: "jsdom",
  },
  publicDir: "public",
  build: {
    outDir: "../web",
    emptyOutDir: true,
    sourcemap: false,
  },
});
