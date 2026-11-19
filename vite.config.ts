// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - tanstackStart, viteReact, tailwindcss, tsConfigPaths, nitro (build-only using cloudflare as a default target),
//     componentTagger (dev-only), VITE_* env injection, @ path alias, React/TanStack dedupe,
//     error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
  },
  vite: {
    server: {
      watch: {
        // Python / OpenCV output that is never part of the website. The feed service writes
        // capture snapshots every few seconds into folders with thousands of files; watching
        // them kept the dev server's event loop busy and /api/feeds answered in 6-13 s.
        ignored: [
          "**/.venv/**",
          "**/.venv-1/**",
          "**/__pycache__/**",
          "**/feed-service/captures/**",
          "**/feed-service/setups/**",
          "**/feed-service/videos/**",
          "**/camera_evaluation_outputs/**",
          "**/paper_results/**",
          "**/OpenCV-FloodMonitoring-main/**",
        ],
      },
    },
  },
});
