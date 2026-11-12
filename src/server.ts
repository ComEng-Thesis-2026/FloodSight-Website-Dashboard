import "./lib/error-capture";

import { spawn } from "node:child_process";
import path from "node:path";

import { consumeLastCapturedError } from "./lib/error-capture";
import { renderErrorPage } from "./lib/error-page";
import {
  getCameraFeeds,
  updateCameraFeed,
  createCameraFeed,
  getFeedReadings,
} from "./lib/db.server";
import { proxyFeedRequest } from "./lib/feed-service.server.ts";
import {
  fetchCameraSnapshot,
  CameraAuthError,
  CameraTimeoutError,
  CameraUnreachableError,
  CameraResponseError,
} from "./lib/camera-stream.server.ts";

type ServerEntry = {
  fetch: (request: Request, env: unknown, ctx: unknown) => Promise<Response> | Response;
};

let serverEntryPromise: Promise<ServerEntry> | undefined;

async function getServerEntry(): Promise<ServerEntry> {
  if (!serverEntryPromise) {
    serverEntryPromise = import("@tanstack/react-start/server-entry").then(
      (m) => (m.default ?? m) as ServerEntry,
    );
  }
  return serverEntryPromise;
}

// h3 swallows in-handler throws into a normal 500 Response with body
// {"unhandled":true,"message":"HTTPError"} — try/catch alone never fires for those.
async function normalizeCatastrophicSsrResponse(response: Response): Promise<Response> {
  if (response.status < 500) return response;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return response;

  const body = await response.clone().text();
  if (!body.includes('"unhandled":true') || !body.includes('"message":"HTTPError"')) {
    return response;
  }

  console.error(consumeLastCapturedError() ?? new Error(`h3 swallowed SSR error: ${body}`));
  return new Response(renderErrorPage(), {
    status: 500,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

export default {
  async fetch(request: Request, env: unknown, ctx: unknown) {
    try {
      const url = new URL(request.url);

      if (url.pathname === "/api/feeds" || url.pathname.startsWith("/api/feeds/")) {
        const subPath = url.pathname.slice("/api/feeds".length).replace(/^\/+|\/+$/g, "");
        return await proxyFeedRequest(request, subPath);
      }

      if (url.pathname === "/api/cameras") {
        if (request.method === "GET") {
          const feeds = await getCameraFeeds();
          return new Response(JSON.stringify(feeds), {
            headers: { "content-type": "application/json; charset=utf-8" },
          });
        }

        if (request.method === "POST") {
          try {
            const body = await request.json();
            const camera = body?.camera;
            if (!camera || typeof camera !== "object") {
              return new Response(JSON.stringify({ error: "Invalid camera payload" }), {
                status: 400,
                headers: { "content-type": "application/json; charset=utf-8" },
              });
            }

            const created = await createCameraFeed(camera);
            return new Response(JSON.stringify(created), {
              status: 201,
              headers: { "content-type": "application/json; charset=utf-8" },
            });
          } catch (error) {
            return new Response(
              JSON.stringify({ error: error instanceof Error ? error.message : "Create failed" }),
              {
                status: 500,
                headers: { "content-type": "application/json; charset=utf-8" },
              },
            );
          }
        }
      }

      if (url.pathname.startsWith("/api/cameras/")) {
        const route = url.pathname.slice("/api/cameras/".length);
        const [cameraId, ...rest] = route.split("/");
        const subPath = rest.join("/");

        if (subPath === "readings" && request.method === "GET") {
          const minutesParam = Number(url.searchParams.get("minutes") ?? 10);
          if (!Number.isFinite(minutesParam) || minutesParam <= 0 || minutesParam > 24 * 60) {
            return new Response(
              JSON.stringify({ error: "minutes must be a number between 0 and 1440" }),
              { status: 400, headers: { "content-type": "application/json; charset=utf-8" } },
            );
          }
          try {
            const readings = await getFeedReadings(decodeURIComponent(cameraId), minutesParam);
            return new Response(JSON.stringify(readings), {
              headers: {
                "content-type": "application/json; charset=utf-8",
                "cache-control": "no-store",
              },
            });
          } catch (error) {
            return new Response(
              JSON.stringify({
                error: error instanceof Error ? error.message : "Could not read flood readings",
              }),
              { status: 500, headers: { "content-type": "application/json; charset=utf-8" } },
            );
          }
        }

        if (subPath === "snapshot/test" && request.method === "POST") {
          try {
            const streamConfig = await request.json();
            if (!streamConfig || typeof streamConfig !== "object") {
              return new Response(JSON.stringify({ error: "Invalid stream configuration" }), {
                status: 400,
                headers: { "content-type": "application/json; charset=utf-8" },
              });
            }

            const result = await fetchCameraSnapshot(streamConfig);
            return new Response(result.buffer, {
              headers: {
                "content-type": result.contentType,
                "cache-control": "no-store",
              },
            });
          } catch (error) {
            let status = 500;
            const message = error instanceof Error ? error.message : "Snapshot fetch failed";
            if (error instanceof CameraTimeoutError) status = 504;
            else if (error instanceof CameraAuthError) status = 401;
            else if (error instanceof CameraUnreachableError) status = 502;
            else if (error instanceof CameraResponseError) status = 502;

            return new Response(JSON.stringify({ error: message }), {
              status,
              headers: { "content-type": "application/json; charset=utf-8" },
            });
          }
        }

        if (subPath === "snapshot" && request.method === "GET") {
          try {
            const feeds = await getCameraFeeds();
            const camera = feeds.find((entry) => entry.id === cameraId);
            if (!camera || !camera.streamConfig?.ipAddress) {
              return new Response(JSON.stringify({ error: "Snapshot source not configured" }), {
                status: 404,
                headers: { "content-type": "application/json; charset=utf-8" },
              });
            }

            const result = await fetchCameraSnapshot(camera.streamConfig);
            return new Response(result.buffer, {
              headers: {
                "content-type": result.contentType,
                "cache-control": "no-store",
              },
            });
          } catch (error) {
            let status = 500;
            const message = error instanceof Error ? error.message : "Snapshot proxy failed";
            if (error instanceof CameraTimeoutError) status = 504;
            else if (error instanceof CameraAuthError) status = 401;
            else if (error instanceof CameraUnreachableError) status = 502;
            else if (error instanceof CameraResponseError) status = 502;

            return new Response(JSON.stringify({ error: message }), {
              status,
              headers: { "content-type": "application/json; charset=utf-8" },
            });
          }
        }

        if (subPath === "evaluate" && request.method === "POST") {
          try {
            const scriptPath = path.resolve(process.cwd(), "cameraTest.py");
            const pythonExecutable =
              process.env.PYTHON_BIN ??
              path.resolve(
                process.cwd(),
                ".venv",
                process.platform === "win32" ? "Scripts" : "bin",
                process.platform === "win32" ? "python.exe" : "python",
              );
            const pythonArgs = [scriptPath, "--camera-id", cameraId];

            const child = spawn(pythonExecutable, pythonArgs, {
              cwd: process.cwd(),
              env: process.env,
            });

            let stdout = "";
            let stderr = "";

            child.stdout.on("data", (chunk) => {
              stdout += chunk.toString();
            });

            child.stderr.on("data", (chunk) => {
              stderr += chunk.toString();
            });

            const exitCode = await new Promise<number>((resolve, reject) => {
              child.on("error", reject);
              child.on("close", (code) => resolve(code ?? 1));
            });

            const trimmed = stdout.trim();
            if (!trimmed) {
              throw new Error(stderr.trim() || "Evaluation script produced no output.");
            }

            const payload = JSON.parse(trimmed);
            if (exitCode !== 0 || payload.ok === false) {
              throw new Error(payload.error || stderr.trim() || "Evaluation failed.");
            }

            return new Response(JSON.stringify(payload), {
              headers: {
                "content-type": "application/json; charset=utf-8",
                "cache-control": "no-store",
              },
            });
          } catch (error) {
            return new Response(
              JSON.stringify({
                error: error instanceof Error ? error.message : "Evaluation failed",
              }),
              {
                status: 500,
                headers: { "content-type": "application/json; charset=utf-8" },
              },
            );
          }
        }

        if (rest.length === 0 && request.method === "PATCH") {
          try {
            const body = await request.json();
            const camera = body?.camera;
            if (!camera || typeof camera !== "object") {
              return new Response(JSON.stringify({ error: "Invalid camera payload" }), {
                status: 400,
                headers: { "content-type": "application/json; charset=utf-8" },
              });
            }

            const saved = await updateCameraFeed(cameraId, camera);
            return new Response(JSON.stringify(saved), {
              headers: { "content-type": "application/json; charset=utf-8" },
            });
          } catch (error) {
            return new Response(
              JSON.stringify({ error: error instanceof Error ? error.message : "Update failed" }),
              {
                status: 500,
                headers: { "content-type": "application/json; charset=utf-8" },
              },
            );
          }
        }
      }

      const handler = await getServerEntry();
      const response = await handler.fetch(request, env, ctx);
      return await normalizeCatastrophicSsrResponse(response);
    } catch (error) {
      console.error(error);
      return new Response(renderErrorPage(), {
        status: 500,
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
  },
};
