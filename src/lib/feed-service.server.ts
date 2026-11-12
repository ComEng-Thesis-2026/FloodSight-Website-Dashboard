import "dotenv/config";

// The OpenCV feed service (feed-service/server.py) listens on localhost only; the dashboard
// reaches it through this same-origin proxy so streams, setup edits and actions need no CORS.
const FEED_SERVICE_URL = (process.env.FEED_SERVICE_URL ?? "http://127.0.0.1:8765").replace(
  /\/+$/,
  "",
);
const ALLOWED_METHODS = new Set(["GET", "PUT", "POST"]);
const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

function jsonError(status: number, error: string): Response {
  return new Response(JSON.stringify({ error }), { status, headers: JSON_HEADERS });
}

/**
 * Forward `/api/feeds/<subPath>` to the feed service and stream the answer back unchanged
 * (MJPEG streams stay open until the viewer leaves; `request.signal` then ends the upstream).
 */
export async function proxyFeedRequest(request: Request, subPath: string): Promise<Response> {
  if (!ALLOWED_METHODS.has(request.method)) {
    return jsonError(405, `Method ${request.method} not allowed`);
  }
  if (subPath && !/^[A-Za-z0-9_-]+(\/[A-Za-z0-9_.-]+)?$/.test(subPath)) {
    return jsonError(404, "Not found");
  }

  const search = new URL(request.url).search;
  const target = `${FEED_SERVICE_URL}/feeds${subPath ? `/${subPath}` : ""}${search}`;
  const init: RequestInit = { method: request.method, signal: request.signal };
  if (request.method !== "GET") {
    init.body = await request.text();
    init.headers = { "content-type": "application/json" };
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (error) {
    if (request.signal.aborted) {
      return new Response(null, { status: 499 });
    }
    console.error(`[feed-service] ${request.method} ${target} failed:`, error);
    return jsonError(503, "OpenCV feed service is offline. Start it with `npm run feeds`.");
  }

  const headers = new Headers({ "cache-control": "no-store" });
  const contentType = upstream.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  return new Response(upstream.body, { status: upstream.status, headers });
}
