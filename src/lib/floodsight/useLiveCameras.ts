import { useEffect, useState } from "react";
import {
  statusFromLevel,
  type Camera,
  type FeedImageQuality,
  type FeedInfo,
  type FeedServiceState,
  type FloodStatus,
  type MarkerMode,
} from "@/lib/floodsight/types";
import { store, useStore } from "@/lib/floodsight/store";

export type DbStatus = "connecting" | "connected" | "error";

type FeedListing = {
  id: string;
  location: string;
  state: FeedServiceState;
  message: string | null;
  coordinates?: { lat: number | null; lng: number | null };
  setup?: {
    frame_size?: [number, number] | null;
    flood_roi?: [number, number][];
    calibrated?: boolean;
    marker_mode?: MarkerMode | null;
    references?: string[];
  } | null;
  reading?: {
    timestamp?: string;
    water_level_m?: number | null;
    status?: string | null;
    marker_mode?: MarkerMode | null;
    data_valid?: boolean | null;
    flood_detected?: boolean | null;
    flood_coverage_pct?: number | null;
    confidence?: number | null;
    raw_level_m?: number | null;
    rate_m_per_min?: number | null;
    above_range?: boolean | null;
    resolution_cm_per_px?: number | null;
    anomaly_reason?: string | null;
    image_quality?: FeedImageQuality | null;
    fps?: number | null;
    loop_index?: number | null;
    video_t_s?: number | null;
    processing_ms?: number | null;
  } | null;
};

function mapFeedListing(feed: FeedListing): Camera {
  const waterLevel = feed.reading?.water_level_m ?? 0;
  const status = feed.reading?.status?.toUpperCase();
  const floodStatus: FloodStatus =
    status === "DANGER"
      ? "DANGER"
      : status === "ALERT"
        ? "ALERT"
        : status === "SAFE" || status === "NORMAL"
          ? "NORMAL"
          : statusFromLevel(waterLevel);
  const vertices = feed.setup?.flood_roi?.map(([x, y]) => ({ x, y })) ?? [];
  const x = vertices.length ? Math.min(...vertices.map((point) => point.x)) : 110;
  const y = vertices.length ? Math.min(...vertices.map((point) => point.y)) : 180;
  const maxX = vertices.length ? Math.max(...vertices.map((point) => point.x)) : 430;
  const maxY = vertices.length ? Math.max(...vertices.map((point) => point.y)) : 560;

  const feedInfo: FeedInfo = {
    feedId: feed.id,
    streamPath: `/api/feeds/${encodeURIComponent(feed.id)}/stream`,
    frameSize: feed.setup?.frame_size ?? null,
    calibrated: Boolean(feed.setup?.calibrated),
    markerMode: feed.setup?.marker_mode ?? feed.reading?.marker_mode ?? null,
    references: feed.setup?.references ?? [],
    dataValid: feed.reading?.data_valid ?? null,
    floodDetected: feed.reading?.flood_detected ?? null,
    floodCoveragePct: feed.reading?.flood_coverage_pct ?? null,
    confidence: feed.reading?.confidence ?? null,
    rawLevelM: feed.reading?.raw_level_m ?? null,
    rateMPerMin: feed.reading?.rate_m_per_min ?? null,
    aboveRange: feed.reading?.above_range ?? null,
    resolutionCmPerPx: feed.reading?.resolution_cm_per_px ?? null,
    anomalyReason: feed.reading?.anomaly_reason ?? null,
    imageQuality: feed.reading?.image_quality ?? null,
    fps: feed.reading?.fps ?? null,
    loopIndex: feed.reading?.loop_index ?? null,
    videoTimeS: feed.reading?.video_t_s ?? null,
    processingMs: feed.reading?.processing_ms ?? null,
    serviceState: feed.state,
    serviceMessage: feed.message,
  };

  return {
    id: `feed-${feed.id}`,
    location: feed.location,
    coordinates: {
      lat: feed.coordinates?.lat ?? 8.4822,
      lng: feed.coordinates?.lng ?? 124.6433,
    },
    waterLevel,
    floodStatus,
    timestamp: feed.reading?.timestamp ?? new Date().toISOString(),
    snapshotUrl: `/api/feeds/${encodeURIComponent(feed.id)}/frame.jpg?overlay=0`,
    roiConfig: {
      x,
      y,
      width: maxX - x,
      height: maxY - y,
      vertices: vertices.length ? vertices : undefined,
    },
    hsvThresholds: { h_min: 0, h_max: 30, s_min: 50, s_max: 255, v_min: 40, v_max: 255 },
    feed: feedInfo,
  };
}

function mergeCameras(cameras: Camera[], liveFeeds: Camera[]): Camera[] {
  const cameraIds = new Set(cameras.map((camera) => camera.id));
  return [...cameras, ...liveFeeds.filter((camera) => !cameraIds.has(camera.id))];
}

export function useLiveCameras() {
  const persistedCameras = useStore((s) => s.cameras);
  const [cameras, setCameras] = useState<Camera[]>(persistedCameras);
  const [dbStatus, setDbStatus] = useState<DbStatus>("connecting");
  const [dbError, setDbError] = useState<string | null>(null);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState(() => new Date());

  useEffect(() => {
    let isMounted = true;

    async function refreshCameras() {
      let liveFeeds: Camera[] = [];

      try {
        const response = await fetch("/api/feeds");
        if (!response.ok) {
          throw new Error(`API returned ${response.status}`);
        }
        const data = (await response.json()) as { feeds: FeedListing[] };
        liveFeeds = data.feeds.map(mapFeedListing);
        if (!isMounted) return;
        setFeedError(null);
        setCameras((current) => mergeCameras(current, liveFeeds));
        setLastUpdate(new Date());
      } catch (error) {
        if (!isMounted) return;
        setFeedError(error instanceof Error ? error.message : String(error));
      }

      try {
        const response = await fetch("/api/cameras");
        if (!response.ok) {
          throw new Error(`API returned ${response.status}`);
        }
        const data: Camera[] = await response.json();
        if (!isMounted) return;

        const mergedCameras = mergeCameras(data, liveFeeds);
        setCameras(mergedCameras);
        store.syncCameras(mergedCameras);
        setDbStatus("connected");
        setDbError(null);
        setLastUpdate(new Date());
      } catch (error) {
        if (!isMounted) return;
        setCameras((current) => mergeCameras(current, liveFeeds));
        setDbStatus("error");
        setDbError(error instanceof Error ? error.message : String(error));
      }
    }

    refreshCameras();
    const id = setInterval(refreshCameras, 7000);
    return () => {
      isMounted = false;
      clearInterval(id);
    };
  }, []);

  return { cameras, dbStatus, dbError, feedError, lastUpdate };
}
