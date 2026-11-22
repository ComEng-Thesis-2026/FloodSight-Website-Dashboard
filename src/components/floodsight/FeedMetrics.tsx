import { AlertTriangle, CheckCircle2, Settings2 } from "lucide-react";
import type { FeedPayload, FeedReadingResponse } from "@/lib/floodsight/types";
import { cn } from "@/lib/utils";

const MODE_LABEL: Record<string, string> = {
  physical: "Physical marker",
  converting: "Converting to virtual",
  virtual: "Virtual marker",
};

function fmt(value: number | null | undefined, digits: number, unit = ""): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(digits)}${unit}`
    : "–";
}

function statusLabel(status: FeedPayload["status"] | undefined): string {
  if (status === "SAFE") return "NORMAL";
  if (status === "NOT_CALIBRATED") return "SETUP NEEDED";
  return status ?? "–";
}

/** OpenCV readings of one feed, live from the feed service (also stored in MongoDB every second). */
interface FeedMetricsProps {
  data: FeedReadingResponse | null;
  error: string | null;
}

export function FeedMetrics({ data, error }: FeedMetricsProps) {
  const reading = data?.reading ?? null;
  const setup = data?.setup ?? null;
  const offline = Boolean(error) || (data !== null && data.state !== "running");
  const iq = reading?.image_quality;

  const metrics: Array<{ label: string; value: string }> = [
    {
      label: "Water level",
      value: fmt(reading?.water_level_m != null ? reading.water_level_m * 100 : null, 1, " cm"),
    },
    { label: "OpenCV status", value: statusLabel(reading?.status) },
    { label: "Road condition", value: reading?.road_condition ?? "–" },
    { label: "Marker band reached", value: reading?.marker_color ?? "–" },
    { label: "Road flood coverage", value: fmt(reading?.flood_coverage_pct, 0, " %") },
    {
      label: "Confidence",
      value: fmt(reading?.confidence != null ? reading.confidence * 100 : null, 0, " %"),
    },
    {
      label: "Rate",
      value: fmt(
        reading?.rate_m_per_min != null ? reading.rate_m_per_min * 100 : null,
        1,
        " cm/min",
      ),
    },
    { label: "Resolution", value: fmt(reading?.resolution_cm_per_px, 1, " cm/px") },
    {
      label: "Image quality",
      value: iq ? `${iq.mode ?? "–"} ${fmt(iq.score, 2)} · ${iq.lighting ?? "–"}` : "–",
    },
    {
      label: "Marker",
      value: reading
        ? (MODE_LABEL[reading.marker_mode] ?? reading.marker_mode) +
          (reading.level_source === "flood_roi" ? " · from flood ROI" : "")
        : "–",
    },
    {
      label: "Processing",
      value: reading
        ? `${fmt(reading.fps, 1, " fps")} · ${fmt(reading.processing_ms, 0, " ms")}`
        : "–",
    },
  ];

  let notice: { tone: "ok" | "warn" | "setup"; text: string };
  if (offline) {
    notice = {
      tone: "warn",
      text: error ?? `Feed ${data?.state ?? "offline"}${data?.message ? `: ${data.message}` : ""}`,
    };
  } else if (setup && !setup.calibrated) {
    notice = { tone: "setup", text: "Setup needed: place the ROIs on the Camera Config page." };
  } else if (setup?.conversion_hint) {
    notice = { tone: "setup", text: setup.conversion_hint };
  } else if (reading && (reading.data_valid === false || reading.anomaly)) {
    notice = { tone: "warn", text: reading.anomaly_reason ?? "No reliable data - level held." };
  } else {
    notice = {
      tone: "ok",
      text: reading ? "Measuring - data valid" : "Waiting for the first reading…",
    };
  }
  const NoticeIcon =
    notice.tone === "ok" ? CheckCircle2 : notice.tone === "setup" ? Settings2 : AlertTriangle;

  return (
    <div className="border-t border-border p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          OpenCV metrics
        </h3>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1",
            notice.tone === "ok" &&
              "bg-[var(--color-status-normal)]/10 ring-[var(--color-status-normal)]/30",
            notice.tone === "warn" &&
              "bg-[var(--color-status-danger)]/10 ring-[var(--color-status-danger)]/30",
            notice.tone === "setup" &&
              "bg-[var(--color-status-alert)]/15 ring-[var(--color-status-alert)]/40",
          )}
        >
          <NoticeIcon className="h-3.5 w-3.5" />
          {notice.text}
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
        {metrics.map((m) => (
          <div key={m.label} className="min-w-0">
            <dt className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {m.label}
            </dt>
            <dd className="truncate text-sm font-semibold tabular-nums">{m.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
