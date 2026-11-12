import "dotenv/config";
import { MongoClient, ObjectId } from "mongodb";

import {
  statusFromLevel,
  type Camera,
  type FeedHistoryPoint,
  type FeedInfo,
  type FeedServiceState,
  type FloodStatus,
  type MarkerMode,
} from "./floodsight/types";

const MONGODB_URI = process.env.MONGODB_URI;
const MONGODB_DB = process.env.MONGODB_DB ?? "floodsight";
const MONGODB_COLLECTION = process.env.MONGODB_COLLECTION ?? "cameras";
const MONGODB_READINGS_COLLECTION = process.env.MONGODB_READINGS_COLLECTION ?? "flood_readings";
const MAX_READING_POINTS = 600;

if (!MONGODB_URI) {
  throw new Error("MONGODB_URI is not configured in environment variables.");
}

declare global {
  // eslint-disable-next-line no-var
  var __mongoClient: MongoClient | undefined;
  // eslint-disable-next-line no-var
  var __mongoClientPromise: Promise<MongoClient> | undefined;
}

const clientPromise: Promise<MongoClient> = (globalThis.__mongoClientPromise ??= new MongoClient(
  MONGODB_URI,
).connect());

type RawCameraDoc = {
  _id: ObjectId | string; // feed cameras use string ids ("feed-<id>")
  status?: string;
  water_level?: number;
  location?: { type: string; coordinates: [number, number] } | string;
  location_label?: string;
  timestamp?: string;
  stream_config?: {
    ipAddress: string;
    port?: number;
    username?: string;
    password?: string;
    snapshotPath?: string;
  };
  roi_config?: {
    x: number;
    y: number;
    width: number;
    height: number;
    vertices?: Array<{ x: number; y: number }>;
  };
  hsv_thresholds?: {
    h_min: number;
    h_max: number;
    s_min: number;
    s_max: number;
    v_min: number;
    v_max: number;
  };
  feed?: RawFeedInfo;
};

// Written by feed-service/mongo_sink.py (snake_case is the database contract).
type RawFeedInfo = {
  feed_id: string;
  stream_path?: string;
  frame_size?: [number, number] | null;
  calibrated?: boolean;
  marker_mode?: MarkerMode | null;
  references?: string[];
  data_valid?: boolean | null;
  flood_detected?: boolean | null;
  flood_coverage_pct?: number | null;
  confidence?: number | null;
  raw_level_m?: number | null;
  rate_m_per_min?: number | null;
  above_range?: boolean | null;
  resolution_cm_per_px?: number | null;
  anomaly_reason?: string | null;
  image_quality?: FeedInfo["imageQuality"];
  fps?: number | null;
  loop_index?: number | null;
  video_t_s?: number | null;
  processing_ms?: number | null;
  service_state?: FeedServiceState | null;
  service_message?: string | null;
};

type RawReadingDoc = {
  camera_id: string;
  timestamp: Date;
  water_level_m?: number | null;
  status?: string | null;
  flood_coverage_pct?: number | null;
};

function normalizeFloodStatus(
  status: string | undefined,
  waterLevel: number,
  trustStatus = false,
): FloodStatus {
  if (typeof status === "string") {
    const normalized = status.toUpperCase();
    if (normalized === "DANGER") return "DANGER";
    if (normalized === "ALERT") return "ALERT";
    // OpenCV feeds apply hysteresis, so their NORMAL/SAFE is kept even right at a threshold.
    if (trustStatus && (normalized === "NORMAL" || normalized === "SAFE")) return "NORMAL";
  }
  return statusFromLevel(waterLevel);
}

function mapFeed(raw: RawFeedInfo): FeedInfo {
  return {
    feedId: raw.feed_id,
    streamPath: raw.stream_path ?? `/api/feeds/${encodeURIComponent(raw.feed_id)}/stream`,
    frameSize: raw.frame_size ?? null,
    calibrated: Boolean(raw.calibrated),
    markerMode: raw.marker_mode ?? null,
    references: raw.references ?? [],
    dataValid: raw.data_valid ?? null,
    floodDetected: raw.flood_detected ?? null,
    floodCoveragePct: raw.flood_coverage_pct ?? null,
    confidence: raw.confidence ?? null,
    rawLevelM: raw.raw_level_m ?? null,
    rateMPerMin: raw.rate_m_per_min ?? null,
    aboveRange: raw.above_range ?? null,
    resolutionCmPerPx: raw.resolution_cm_per_px ?? null,
    anomalyReason: raw.anomaly_reason ?? null,
    imageQuality: raw.image_quality ?? null,
    fps: raw.fps ?? null,
    loopIndex: raw.loop_index ?? null,
    videoTimeS: raw.video_t_s ?? null,
    processingMs: raw.processing_ms ?? null,
    serviceState: raw.service_state ?? null,
    serviceMessage: raw.service_message ?? null,
  };
}

function locationLabel(doc: RawCameraDoc, index: number): string {
  if (doc.location_label) return doc.location_label;
  if (typeof doc.location === "string") return doc.location;
  if (doc.location?.type === "Point" && Array.isArray(doc.location.coordinates)) {
    const [lng, lat] = doc.location.coordinates;
    return `CCTV ${index + 1} · ${lat.toFixed(4)}, ${lng.toFixed(4)}`;
  }
  return `CCTV ${index + 1}`;
}

function mapCamera(doc: RawCameraDoc, index: number): Camera {
  const waterLevel = typeof doc.water_level === "number" ? doc.water_level : 0;
  const location = locationLabel(doc, index);

  const coordinates =
    typeof doc.location === "object" &&
    doc.location?.type === "Point" &&
    Array.isArray(doc.location.coordinates)
      ? { lat: doc.location.coordinates[1], lng: doc.location.coordinates[0] }
      : { lat: 8.4822, lng: 124.6433 };

  const roiConfig = doc.roi_config ?? {
    x: 110,
    y: 180,
    width: 320,
    height: 380,
    vertices: [
      { x: 110, y: 180 },
      { x: 430, y: 180 },
      { x: 430, y: 560 },
      { x: 110, y: 560 },
    ],
  };

  const cameraId = doc._id.toString();
  const hasStreamConfig = Boolean(doc.stream_config?.ipAddress);
  const feed = doc.feed?.feed_id ? mapFeed(doc.feed) : undefined;
  const snapshotUrl = feed
    ? `/api/feeds/${encodeURIComponent(feed.feedId)}/frame.jpg?overlay=0`
    : hasStreamConfig
      ? `/api/cameras/${encodeURIComponent(cameraId)}/snapshot`
      : `https://picsum.photos/seed/${cameraId.slice(-8)}/640/400`;

  return {
    id: cameraId,
    location,
    coordinates,
    waterLevel,
    floodStatus: normalizeFloodStatus(doc.status, waterLevel, Boolean(feed)),
    timestamp: doc.timestamp ?? new Date().toISOString(),
    snapshotUrl,
    streamConfig: doc.stream_config ?? undefined,
    roiConfig,
    hsvThresholds: doc.hsv_thresholds ?? {
      h_min: 0,
      h_max: 30,
      s_min: 50,
      s_max: 255,
      v_min: 40,
      v_max: 255,
    },
    feed,
  };
}

function parseDocumentId(id: string) {
  try {
    return new ObjectId(id);
  } catch {
    return id;
  }
}

async function findCameraCollection(db: ReturnType<MongoClient["db"]>) {
  const collections = await db.listCollections().toArray();

  for (const { name } of collections) {
    try {
      const collection = db.collection<RawCameraDoc>(name);
      const match = await collection.findOne({
        $or: [
          { water_level: { $exists: true } },
          { status: { $exists: true } },
          { "location.type": "Point" },
        ],
      });
      if (match) return collection;
    } catch {
      continue;
    }
  }
  return null;
}

export async function getCameraFeeds(): Promise<Camera[]> {
  const client = await clientPromise;
  const db = client.db(MONGODB_DB);
  let collection = db.collection<RawCameraDoc>(MONGODB_COLLECTION);

  let docs = await collection.find({}).toArray();
  if (!docs.length) {
    const fallback = await findCameraCollection(db);
    if (fallback) {
      collection = fallback;
      docs = await collection.find({}).toArray();
    }
  }

  return docs.map(mapCamera);
}

function buildCameraSetPayload(camera: Camera): Record<string, unknown> {
  const roiConfig = camera.roiConfig ?? {
    x: 110,
    y: 180,
    width: 320,
    height: 380,
    vertices: [
      { x: 110, y: 180 },
      { x: 430, y: 180 },
      { x: 430, y: 560 },
      { x: 110, y: 560 },
    ],
  };

  const setPayload: Record<string, unknown> = {
    location: {
      type: "Point",
      coordinates: [camera.coordinates.lng, camera.coordinates.lat],
    },
    location_label: camera.location,
    water_level: camera.waterLevel,
    status: camera.floodStatus,
    timestamp: camera.timestamp,
    roi_config: roiConfig,
    hsv_thresholds: camera.hsvThresholds,
  };

  if (camera.streamConfig !== undefined) {
    setPayload.stream_config = camera.streamConfig;
  }

  // Feed cameras: the feed service owns the live fields (level, status, ROI, feed metrics);
  // a Camera Config save only changes the name and coordinates.
  if (camera.feed) {
    delete setPayload.water_level;
    delete setPayload.status;
    delete setPayload.timestamp;
    delete setPayload.roi_config;
    delete setPayload.stream_config;
  }

  return setPayload;
}

export async function updateCameraFeed(id: string, camera: Camera): Promise<Camera> {
  const client = await clientPromise;
  const db = client.db(MONGODB_DB);
  const collection = db.collection<RawCameraDoc>(MONGODB_COLLECTION);

  await collection.updateOne(
    { _id: parseDocumentId(id) },
    { $set: buildCameraSetPayload(camera) },
    { upsert: true },
  );

  return camera;
}

export async function createCameraFeed(camera: Camera): Promise<Camera> {
  const client = await clientPromise;
  const db = client.db(MONGODB_DB);
  const collection = db.collection<RawCameraDoc>(MONGODB_COLLECTION);

  const doc = buildCameraSetPayload(camera) as RawCameraDoc;
  const result = await collection.insertOne(doc);

  return mapCamera({ ...doc, _id: result.insertedId }, 0);
}

export async function getFeedReadings(
  cameraId: string,
  minutes: number,
): Promise<FeedHistoryPoint[]> {
  const client = await clientPromise;
  const collection = client.db(MONGODB_DB).collection<RawReadingDoc>(MONGODB_READINGS_COLLECTION);
  const since = new Date(Date.now() - minutes * 60_000);

  const docs = await collection
    .find(
      { camera_id: cameraId, timestamp: { $gte: since } },
      { projection: { _id: 0, timestamp: 1, water_level_m: 1, status: 1, flood_coverage_pct: 1 } },
    )
    .sort({ timestamp: -1 })
    .limit(MAX_READING_POINTS)
    .toArray();

  return docs.reverse().map((doc) => ({
    timestamp: doc.timestamp.toISOString(),
    waterLevel: typeof doc.water_level_m === "number" ? doc.water_level_m : null,
    status: doc.status ?? null,
    floodCoveragePct: typeof doc.flood_coverage_pct === "number" ? doc.flood_coverage_pct : null,
  }));
}
