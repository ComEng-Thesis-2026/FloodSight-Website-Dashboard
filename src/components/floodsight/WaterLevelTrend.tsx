import { CartesianGrid, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts";
import { Database } from "lucide-react";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { useFeedHistory } from "@/hooks/use-feed-data";
import { ALERT_LEVEL_M, DANGER_LEVEL_M } from "@/lib/floodsight/types";

const CHART_CONFIG = {
  levelCm: { label: "Water level", color: "var(--color-primary)" },
} satisfies ChartConfig;

const MINUTES = 10;

/** Water level of a feed camera over the last minutes, read back from MongoDB (flood_readings). */
export function WaterLevelTrend({ cameraId }: { cameraId: string }) {
  const { points, error } = useFeedHistory(cameraId, MINUTES);
  const data = points
    .filter((p) => p.waterLevel !== null)
    .map((p) => ({
      time: new Date(p.timestamp).getTime(),
      levelCm: Math.round((p.waterLevel ?? 0) * 1000) / 10,
      status: p.status,
    }));
  const maxCm = Math.max(60, ...data.map((d) => d.levelCm + 5));

  return (
    <div className="border-t border-border p-4">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Water level · last {MINUTES} min (cm)
        </h3>
        <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
          <Database className="h-3 w-3" />
          {error ? `MongoDB: ${error}` : `${points.length} readings stored in MongoDB`}
        </span>
      </div>
      {data.length < 2 ? (
        <p className="py-6 text-center text-xs text-muted-foreground">
          {error ? "Readings could not be loaded." : "Waiting for stored readings…"}
        </p>
      ) : (
        <ChartContainer config={CHART_CONFIG} className="aspect-auto h-40 w-full">
          <LineChart data={data} margin={{ top: 8, right: 56, bottom: 0, left: -16 }}>
            <CartesianGrid vertical={false} strokeOpacity={0.5} />
            <XAxis
              dataKey="time"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t: number) =>
                new Date(t).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })
              }
              tickLine={false}
              axisLine={false}
              minTickGap={64}
            />
            <YAxis domain={[0, maxCm]} tickLine={false} axisLine={false} width={48} />
            <ReferenceLine
              y={ALERT_LEVEL_M * 100}
              stroke="var(--color-status-alert)"
              strokeDasharray="4 4"
              label={{
                value: "Alert 30",
                position: "right",
                fill: "var(--color-muted-foreground)",
                fontSize: 10,
              }}
            />
            <ReferenceLine
              y={DANGER_LEVEL_M * 100}
              stroke="var(--color-status-danger)"
              strokeDasharray="4 4"
              label={{
                value: "Danger 50",
                position: "right",
                fill: "var(--color-muted-foreground)",
                fontSize: 10,
              }}
            />
            <ChartTooltip
              cursor={{ strokeWidth: 1 }}
              content={
                <ChartTooltipContent
                  labelFormatter={(_, payload) => {
                    const t = payload?.[0]?.payload?.time as number | undefined;
                    return t ? new Date(t).toLocaleTimeString() : "";
                  }}
                  formatter={(value, _name, item) => (
                    <span className="font-mono tabular-nums text-foreground">
                      {Number(value).toFixed(1)} cm · {String(item.payload?.status ?? "")}
                    </span>
                  )}
                />
              }
            />
            <Line
              dataKey="levelCm"
              type="monotone"
              stroke="var(--color-levelCm)"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ChartContainer>
      )}
    </div>
  );
}
