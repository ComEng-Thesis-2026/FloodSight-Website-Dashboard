export type Role = "admin" | "authority" | "viewer";
export type FloodStatus = "NORMAL" | "ALERT" | "DANGER";

export interface StreamConfig {
  ipAddress: string;
  port?: number;
  username?: string;
  password?: string;
  snapshotPath?: string;
}

export interface RoiPoint {
  x: number;
  y: number;
}

export interface CameraEvaluation {
  status?: FloodStatus;
  water_level?: number;
  water_ratio?: number;
  pixel_detection_accuracy?: number;
  classification_accuracy?: number;
  processed_image_path?: string;
  captured_at?: string;
  source?: string;
}

export interface Camera {
  id: string;
  location: string;
  coordinates: { lat: number; lng: number };
  waterLevel: number; // meters
  floodStatus: FloodStatus;
  timestamp: string;
  snapshotUrl: string;
  streamConfig?: StreamConfig;
  roiConfig: {
    x: number;
    y: number;
    width: number;
    height: number;
    vertices?: RoiPoint[];
  };
  hsvThresholds: {
    h_min: number;
    h_max: number;
    s_min: number;
    s_max: number;
    v_min: number;
    v_max: number;
  };
  last_evaluation?: CameraEvaluation;
  feed?: FeedInfo;
}

export type MarkerMode = "physical" | "converting" | "virtual";
export type FeedServiceState = "starting" | "running" | "missing_video" | "error" | "stopped";

export interface FeedImageQuality {
  score?: number | null;
  mode?: string | null;
  lighting?: string | null;
  conditions?: string[];
}

/** OpenCV video feed attached to a camera (written by feed-service, see feed-service/README.md). */
export interface FeedInfo {
  feedId: string;
  streamPath: string;
  frameSize: [number, number] | null;
  calibrated: boolean;
  markerMode: MarkerMode | null;
  references: string[];
  dataValid: boolean | null;
  floodDetected: boolean | null;
  floodCoveragePct: number | null;
  confidence: number | null;
  rawLevelM: number | null;
  rateMPerMin: number | null;
  aboveRange: boolean | null;
  resolutionCmPerPx: number | null;
  anomalyReason: string | null;
  imageQuality: FeedImageQuality | null;
  fps: number | null;
  loopIndex: number | null;
  videoTimeS: number | null;
  processingMs: number | null;
  serviceState: FeedServiceState | null;
  serviceMessage: string | null;
}

/** Latest OpenCV payload, straight from the feed service (snake_case: external contract). */
export interface FeedPayload {
  camera_id: string;
  timestamp: string;
  calibrated: boolean;
  marker_mode: MarkerMode;
  status: "SAFE" | "ALERT" | "DANGER" | "NOT_CALIBRATED";
  water_level_m: number | null;
  raw_level_m?: number;
  data_valid?: boolean;
  data_held_s?: number;
  rate_m_per_min?: number;
  above_range?: boolean;
  resolution_cm_per_px?: number | null;
  flood_detected?: boolean;
  flood_coverage_pct?: number;
  /** Feed service: DRY, WET (road changed, no flood water) or FLOODED. */
  road_condition?: "DRY" | "WET" | "FLOODED" | null;
  /** Feed service: "flood_roi" when the virtual marker's level comes from the flood ROI. */
  level_source?: "flood_roi" | "marker";
  /** Feed service: colour of the remembered physical-marker band the water reaches. */
  marker_color?: "GREEN" | "YELLOW" | "RED";
  anomaly?: boolean;
  anomaly_reason?: string | null;
  obstruction_at_marker?: boolean;
  confidence?: number;
  fps?: number;
  video_t_s?: number;
  paused?: boolean;
  loop_index?: number;
  processing_ms?: number;
  image_quality?: FeedImageQuality & { problem?: string | null };
}

export interface FeedSetup {
  frame_size: [number, number];
  flood_roi: [number, number][];
  marker_roi: [number, number][];
  ref_height_m: number;
  marker_mode: MarkerMode;
  marker_locked: boolean;
  virtual_marker: boolean;
  references: string[];
  calibrated: boolean;
  capturing: boolean;
  conversion_hint: string | null;
  /** The clip is frozen on one frame (still measured, so a dry reference can be captured). */
  paused?: boolean;
  /** The physical marker as the virtual marker remembers it (null = not remembered). */
  marker_memory?: {
    bands: { color: "GREEN" | "YELLOW" | "RED"; from_m: number; to_m: number }[];
    upside_down: boolean;
    source: string;
    learned_at: string;
    px_per_m: number;
    cm_per_px: number;
  } | null;
}

export interface FeedReadingResponse {
  id: string;
  state: FeedServiceState;
  message: string | null;
  reading: FeedPayload | null;
  setup: FeedSetup | null;
}

/** One stored reading from MongoDB (flood_readings), for the trend chart. */
export interface FeedHistoryPoint {
  timestamp: string;
  waterLevel: number | null;
  status: string | null;
  floodCoveragePct: number | null;
}

export interface User {
  id: string;
  name: string;
  email: string;
  role: Role;
  avatarUrl?: string;
  photoURL?: string;
  addedAt: string;
}

// Same bands as the OpenCV program (OpenCV-FloodMonitoring-main/floodmon/bands.py, 1 m marker).
export const ALERT_LEVEL_M = 0.3;
export const DANGER_LEVEL_M = 0.5;

export function statusFromLevel(level: number): FloodStatus {
  if (level >= DANGER_LEVEL_M) return "DANGER";
  if (level >= ALERT_LEVEL_M) return "ALERT";
  return "NORMAL";
}
