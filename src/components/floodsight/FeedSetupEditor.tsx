import { useEffect, useRef, useState } from "react";
import {
  Camera as CameraIcon,
  ImageDown,
  Lock,
  Minus,
  Palette,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Save,
  ScanEye,
  SkipBack,
  Unlock,
} from "lucide-react";
import { toast } from "sonner";
import { LiveFeedPlayer } from "@/components/floodsight/LiveFeedPlayer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useFeedReading } from "@/hooks/use-feed-data";
import type { FeedSetup } from "@/lib/floodsight/types";
import { cn } from "@/lib/utils";

type Point = [number, number];
type FeedAction =
  | "capture_reference"
  | "virtual_marker"
  | "virtual_unlock"
  | "remember_marker"
  | "save"
  | "restart"
  | "pause"
  | "resume";
type DragTarget = { region: "flood" | "marker"; index: number } | null;

interface ActionResult {
  ok?: boolean;
  message?: string;
  needs_confirm?: boolean;
  error?: string;
  problems?: string[];
}

const MODE_LABEL: Record<FeedSetup["marker_mode"], string> = {
  physical: "Physical Marker",
  converting: "Converting - Marker Locked",
  virtual: "Virtual Marker",
};

const BAND_DOT: Record<string, string> = {
  GREEN: "bg-green-500",
  YELLOW: "bg-yellow-400",
  RED: "bg-red-500",
};

/** The physical marker's colour bands as the virtual marker remembers them (bottom to top). */
function RememberedBands({ memory }: { memory: NonNullable<FeedSetup["marker_memory"]> }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      Remembered:
      {memory.bands.map((b) => (
        <span key={`${b.color}${b.from_m}`} className="inline-flex items-center gap-1">
          <span className={cn("h-2 w-2 rounded-full", BAND_DOT[b.color] ?? "bg-muted")} />
          {b.from_m.toFixed(2)}–{b.to_m.toFixed(2)} m
        </span>
      ))}
      {memory.upside_down ? <span>(flipped)</span> : null}
    </span>
  );
}

function clonePoints(points: Point[] | undefined): Point[] {
  return (points ?? []).map(([x, y]) => [x, y]);
}

function samePoints(a: Point[], b: Point[]): boolean {
  return a.length === b.length && a.every((p, i) => p[0] === b[i][0] && p[1] === b[i][1]);
}

/**
 * Admin editor for one OpenCV feed: the same setup as the stand-alone program's window
 * (flood ROI polygon, 4-corner marker ROI, reference height, dry reference 'b',
 * virtual marker 'x' / Shift+X, save 's'), applied live by the feed service.
 */
export function FeedSetupEditor({ feedId }: { feedId: string }) {
  const { data, error } = useFeedReading(feedId);
  const setup = data?.setup ?? null;
  const running = data?.state === "running";

  const [flood, setFlood] = useState<Point[]>([]);
  const [marker, setMarker] = useState<Point[]>([]);
  const [refHeight, setRefHeight] = useState("1.00");
  const [dirty, setDirty] = useState(false);
  const [drag, setDrag] = useState<DragTarget>(null);
  const [overlay, setOverlay] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [lastMessage, setLastMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Follow the service's setup until the admin starts editing.
  useEffect(() => {
    if (!setup || dirty) return;
    if (!samePoints(flood, setup.flood_roi)) setFlood(clonePoints(setup.flood_roi));
    if (!samePoints(marker, setup.marker_roi)) setMarker(clonePoints(setup.marker_roi));
    setRefHeight(setup.ref_height_m.toFixed(2));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setup, dirty]);

  const [frameW, frameH] = setup?.frame_size ?? [960, 540];
  const handleR = Math.max(frameW, frameH) / 110;
  const locked = Boolean(setup?.marker_locked);
  const paused = Boolean(setup?.paused);

  function framePoint(clientX: number, clientY: number): Point {
    const rect = svgRef.current!.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * frameW;
    const y = ((clientY - rect.top) / rect.height) * frameH;
    return [
      Math.round(Math.min(Math.max(x, 0), frameW) * 10) / 10,
      Math.round(Math.min(Math.max(y, 0), frameH) * 10) / 10,
    ];
  }

  function onPointerMove(event: React.PointerEvent<SVGSVGElement>) {
    if (!drag) return;
    const point = framePoint(event.clientX, event.clientY);
    const update = (points: Point[]) => points.map((p, i) => (i === drag.index ? point : p));
    if (drag.region === "flood") setFlood(update);
    else setMarker(update);
  }

  function beginDrag(event: React.PointerEvent, target: DragTarget) {
    event.preventDefault();
    svgRef.current?.setPointerCapture(event.pointerId);
    setDrag(target);
    setDirty(true);
  }

  function addFloodPoint() {
    if (flood.length < 3) return;
    let best = 0;
    let bestLen = -1;
    flood.forEach((p, i) => {
      const q = flood[(i + 1) % flood.length];
      const len = Math.hypot(q[0] - p[0], q[1] - p[1]);
      if (len > bestLen) {
        best = i;
        bestLen = len;
      }
    });
    const p = flood[best];
    const q = flood[(best + 1) % flood.length];
    const mid: Point = [Math.round((p[0] + q[0]) / 2), Math.round((p[1] + q[1]) / 2)];
    setFlood([...flood.slice(0, best + 1), mid, ...flood.slice(best + 1)]);
    setDirty(true);
  }

  function removeFloodPoint() {
    if (flood.length <= 3) return;
    setFlood(flood.slice(0, -1));
    setDirty(true);
  }

  function resetDraft() {
    setDirty(false);
    setLastMessage(null);
  }

  async function call(label: string, url: string, init: RequestInit): Promise<ActionResult | null> {
    setBusy(label);
    try {
      const response = await fetch(url, {
        ...init,
        headers: { "Content-Type": "application/json" },
      });
      const body = (await response.json()) as ActionResult;
      const text =
        body.message ?? (body.problems?.length ? body.problems.join("; ") : body.error) ?? "Done";
      const ok = response.ok && body.ok !== false;
      setLastMessage({ ok, text });
      if (ok) toast.success(label, { description: text });
      else if (body.needs_confirm) toast.warning(label, { description: text });
      else toast.error(label, { description: text });
      return body;
    } catch (err) {
      const text = err instanceof Error ? err.message : String(err);
      setLastMessage({ ok: false, text });
      toast.error(label, { description: text });
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function applySetup() {
    const height = Number(refHeight);
    const body: Record<string, unknown> = { flood_roi: flood };
    if (!locked) {
      body.marker_roi = marker;
      body.ref_height_m = height;
    }
    const result = await call("Apply setup", `/api/feeds/${encodeURIComponent(feedId)}/setup`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    if (result?.ok) setDirty(false);
  }

  function runAction(action: FeedAction, label: string) {
    return call(label, `/api/feeds/${encodeURIComponent(feedId)}/actions`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
  }

  const virtualLabel = !setup
    ? "Virtual Marker"
    : setup.marker_mode === "physical"
      ? "Lock Virtual Marker (1/2)"
      : setup.marker_mode === "converting"
        ? "Marker Removed - Convert (2/2)"
        : "Virtual Marker Active";

  return (
    <div className="overflow-hidden rounded-2xl border border-border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <Chip tone={running ? "ok" : "warn"}>
            {data?.state
              ? data.state.charAt(0).toUpperCase() + data.state.slice(1)
              : error
                ? "Offline"
                : "Loading"}
          </Chip>
          {setup ? (
            <>
              <Chip tone={setup.calibrated ? "ok" : "warn"}>
                {setup.calibrated ? "Calibrated" : "Setup Needed"}
              </Chip>
              <Chip>{MODE_LABEL[setup.marker_mode]}</Chip>
              <Chip tone={setup.references.length ? "ok" : "warn"}>
                Dry Ref:{" "}
                {setup.references.length ? setup.references.join(", ").toUpperCase() : "None"}
              </Chip>
              {setup.marker_memory ? (
                <Chip tone="ok">
                  <RememberedBands memory={setup.marker_memory} />
                </Chip>
              ) : locked ? (
                <Chip tone="warn">Marker Colors Not Remembered</Chip>
              ) : null}
              {setup.capturing ? <Chip tone="warn">Capturing Reference…</Chip> : null}
              {paused ? <Chip tone="warn">Paused</Chip> : null}
            </>
          ) : null}
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
          <ScanEye className="h-4 w-4 text-muted-foreground" />
          OpenCV Overlay
          <Switch
            checked={overlay}
            onCheckedChange={setOverlay}
            aria-label="Show the OpenCV overlay"
          />
        </label>
      </div>

      <div className="relative w-full bg-muted" style={{ aspectRatio: `${frameW} / ${frameH}` }}>
        <LiveFeedPlayer
          feedId={feedId}
          overlay={overlay}
          alt="Feed being configured"
          className="absolute inset-0"
          imgClassName="object-fill"
        />
        {setup && running ? (
          <svg
            ref={svgRef}
            viewBox={`0 0 ${frameW} ${frameH}`}
            preserveAspectRatio="none"
            className="absolute inset-0 h-full w-full touch-none"
            onPointerMove={onPointerMove}
            onPointerUp={() => setDrag(null)}
            onPointerCancel={() => setDrag(null)}
          >
            {flood.length >= 3 ? (
              <polygon
                points={flood.map((p) => p.join(",")).join(" ")}
                className="fill-roi-flood/15 stroke-roi-flood"
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
              />
            ) : null}
            {marker.length === 4 ? (
              <polygon
                points={marker.map((p) => p.join(",")).join(" ")}
                className="fill-roi-marker/15 stroke-roi-marker"
                strokeWidth={2}
                strokeDasharray={locked ? "6 4" : undefined}
                vectorEffect="non-scaling-stroke"
              />
            ) : null}
            {flood.map((p, i) => (
              <circle
                key={`f${i}`}
                cx={p[0]}
                cy={p[1]}
                r={handleR}
                className="cursor-move fill-roi-flood stroke-background"
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
                onPointerDown={(e) => beginDrag(e, { region: "flood", index: i })}
              >
                <title>{`Flood ROI point ${i + 1}`}</title>
              </circle>
            ))}
            {locked
              ? null
              : marker.map((p, i) => (
                  <rect
                    key={`m${i}`}
                    x={p[0] - handleR * 0.8}
                    y={p[1] - handleR * 0.8}
                    width={handleR * 1.6}
                    height={handleR * 1.6}
                    className="cursor-move fill-roi-marker stroke-background"
                    strokeWidth={2}
                    vectorEffect="non-scaling-stroke"
                    onPointerDown={(e) => beginDrag(e, { region: "marker", index: i })}
                  >
                    <title>{`Marker corner ${i + 1}`}</title>
                  </rect>
                ))}
          </svg>
        ) : null}
      </div>

      <div className="space-y-4 border-t border-border p-4">
        <p className="text-xs text-muted-foreground">
          Drag the <span className="font-semibold text-roi-flood">Flood ROI</span> (Road Area) and
          the <span className="font-semibold text-roi-marker">Marker ROI</span> (Bottom Edge = Road,
          0 m; Top Edge = Reference Height), Then Apply. Restart the Video, Pause It on the Dry
          Scene, Then Capture the Dry Reference.
        </p>

        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">
              Reference Height (m)
            </span>
            <Input
              type="number"
              min={0.05}
              max={5}
              step={0.01}
              value={refHeight}
              disabled={locked}
              onChange={(e) => {
                setRefHeight(e.target.value);
                setDirty(true);
              }}
              className="w-28"
            />
          </label>
          <div className="flex gap-1.5">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={addFloodPoint}
              title="Add a flood ROI point"
            >
              <Plus className="h-3.5 w-3.5" />
              Point
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={removeFloodPoint}
              disabled={flood.length <= 3}
              title="Remove the last flood ROI point"
            >
              <Minus className="h-3.5 w-3.5" />
              Point
            </Button>
          </div>
          <div className="ml-auto flex gap-1.5">
            <Button type="button" variant="ghost" size="sm" onClick={resetDraft} disabled={!dirty}>
              <RotateCcw className="h-3.5 w-3.5" />
              Reset
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={applySetup}
              disabled={!running || !dirty || busy !== null}
            >
              Apply ROI
            </Button>
          </div>
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <Button
            type="button"
            variant={paused ? "default" : "secondary"}
            onClick={() =>
              paused ? runAction("resume", "Play video") : runAction("pause", "Pause video")
            }
            disabled={!running || busy !== null}
            aria-pressed={paused}
            title={
              paused
                ? "Continue playing the clip"
                : "Like 'p': freeze the clip on the current frame (it is still measured)"
            }
          >
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            {paused ? "Play Video" : "Pause Video"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => runAction("restart", "Restart video")}
            disabled={!running || busy !== null}
            title="Jump back to the dry scene at the start of the clip (stays paused if paused)"
          >
            <SkipBack className="h-4 w-4" />
            Restart Video
          </Button>
          <Button
            type="button"
            variant="secondary"
            className="sm:col-span-2"
            onClick={() => runAction("capture_reference", "Capture dry reference")}
            disabled={!running || busy !== null}
            title="Like 'b': the road must be dry and still; press again within 5 s to capture anyway"
          >
            <CameraIcon className="h-4 w-4" />
            Capture Dry Reference
          </Button>
          <Button
            type="button"
            variant="default"
            className="sm:col-span-2"
            onClick={() => runAction("save_screenshot", "Save screenshot with matrices")}
            disabled={!running || busy !== null}
            title="Save the clean and annotated screenshots in the project's Snapshots folder and store their matrices in MongoDB"
          >
            <ImageDown className="h-4 w-4" />
            Save Screenshot With Matrices
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => runAction("virtual_marker", "Virtual marker")}
            disabled={!running || busy !== null || setup?.marker_mode === "virtual"}
            title="Like 'x': lock the marker geometry, then (after removing the physical marker) convert"
          >
            <Lock className="h-4 w-4" />
            {virtualLabel}
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => runAction("virtual_unlock", "Unlock marker")}
            disabled={!running || busy !== null || !locked}
            title="Like Shift+X: unlock the marker so it can be moved again"
          >
            <Unlock className="h-4 w-4" />
            Unlock Marker
          </Button>
          <Button
            type="button"
            variant="secondary"
            className="sm:col-span-2"
            onClick={() => runAction("remember_marker", "Remember physical marker")}
            disabled={!running || busy !== null}
            title="While the physical marker is in view (pause on the dry scene): remember its green / yellow / red bands for the virtual marker. Locking does this too."
          >
            <Palette className="h-4 w-4" />
            Remember Physical Marker
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          The Screenshot Includes the Water Level, Timestamp, Flood ROI, Virtual Marker Level, Flood
          Status, and OpenCV Measurement Matrices. The Clean and Annotated Images are Saved under{" "}
          <span className="font-medium text-foreground">Snapshots/&lt;Feed Id&gt;/</span>, and Their
          Matrix Data Is Stored in the MongoDB <span className="font-mono">camera_metrics</span>{" "}
          collection.
        </p>
        <Button
          type="button"
          className="w-full"
          onClick={() => runAction("save", "Save setup")}
          disabled={!running || busy !== null}
        >
          <Save className="h-4 w-4" />
          Save OpenCV Setup
        </Button>

        {setup?.conversion_hint ? (
          <p className="rounded-lg border border-border bg-muted/50 p-2.5 text-xs">
            {setup.conversion_hint}
          </p>
        ) : null}
        {lastMessage ? (
          <p
            className={cn("text-xs", lastMessage.ok ? "text-muted-foreground" : "text-destructive")}
          >
            {lastMessage.text}
          </p>
        ) : null}
        {!running ? (
          <p className="text-xs text-destructive">
            {error
              ? error
              : `Feed ${data?.state ?? "offline"}${data?.message ? `: ${data.message}` : ""}.`}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function Chip({ children, tone }: { children: React.ReactNode; tone?: "ok" | "warn" }) {
  return (
    <span
      className={cn(
        "rounded-full border px-2 py-0.5 font-medium",
        tone === "ok" &&
          "border-[var(--color-status-normal)]/40 bg-[var(--color-status-normal)]/10",
        tone === "warn" &&
          "border-[var(--color-status-alert)]/50 bg-[var(--color-status-alert)]/15",
        !tone && "border-border bg-background",
      )}
    >
      {children}
    </span>
  );
}
