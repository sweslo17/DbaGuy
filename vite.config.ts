import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The release this build is, shown in the status bar: DBAGUY_RELEASE (CI sets it from the tag: 0.2.0-beta.1, 0.1.0),
// else `<version>-dev` for a local build or the dev server. The About panel shows the same (src-tauri/src/menu.rs).
const { version } = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")) as { version: string };
const release = process.env.DBAGUY_RELEASE || `${version}-dev`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: { __APP_RELEASE__: JSON.stringify(release) },
  clearScreen: false,
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**", "**/backend/**"] },
  },
  envPrefix: ["VITE_", "TAURI_ENV_"],
  build: { target: "safari17", sourcemap: true },
  // The pure logic only (`npm test`): test files sit beside their module and are never imported by the app, so the
  // production bundle (built from index.html) does not contain them.
  test: { include: ["src/**/*.test.ts"] },
});
