import { useEffect, useState } from "react";
import type { FeedHistoryPoint, FeedReadingResponse } from "@/lib/floodsight/types";

const READING_INTERVAL_MS = 1000;
const HISTORY_INTERVAL_MS = 5000;

/** Latest OpenCV reading of one feed, live from the feed service (refreshed every second). */
export function useFeedReading(feedId: string | undefined) {
  const [data, setData] = useState<FeedReadingResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!feedId) {
      setData(null);
      return;
    }
    let isMounted = true;
    const controller = new AbortController();

    async function refresh() {
      try {
        const response = await fetch(`/api/feeds/${encodeURIComponent(feedId!)}/reading`, {
          signal: controller.signal,
        });
        const body = await response.json();
        if (!isMounted) return;
        if (!response.ok)
          throw new Error(body?.error ?? `Feed service returned ${response.status}`);
        setData(body as FeedReadingResponse);
        setError(null);
      } catch (err) {
        if (!isMounted || controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    }

    refresh();
    const id = setInterval(refresh, READING_INTERVAL_MS);
    return () => {
      isMounted = false;
      controller.abort();
      clearInterval(id);
    };
  }, [feedId]);

  return { data, error };
}

/** Stored readings of one camera from MongoDB (flood_readings), for the trend chart. */
export function useFeedHistory(cameraId: string | undefined, minutes = 10) {
  const [points, setPoints] = useState<FeedHistoryPoint[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!cameraId) {
      setPoints([]);
      return;
    }
    let isMounted = true;

    async function refresh() {
      try {
        const response = await fetch(
          `/api/cameras/${encodeURIComponent(cameraId!)}/readings?minutes=${minutes}`,
        );
        const body = await response.json();
        if (!isMounted) return;
        if (!response.ok) throw new Error(body?.error ?? `API returned ${response.status}`);
        setPoints(body as FeedHistoryPoint[]);
        setError(null);
      } catch (err) {
        if (!isMounted) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    }

    refresh();
    const id = setInterval(refresh, HISTORY_INTERVAL_MS);
    return () => {
      isMounted = false;
      clearInterval(id);
    };
  }, [cameraId, minutes]);

  return { points, error };
}
