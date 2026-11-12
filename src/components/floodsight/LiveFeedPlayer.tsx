import { useEffect, useState } from "react";
import { RotateCw, VideoOff } from "lucide-react";
import { cn } from "@/lib/utils";

interface LiveFeedPlayerProps {
  feedId: string;
  overlay: boolean;
  alt: string;
  className?: string;
  imgClassName?: string;
}

/**
 * Live MJPEG video of an OpenCV feed. `overlay` switches between the clean video and the
 * OpenCV view (ROIs, water tint, level panel); the measuring runs either way.
 */
export function LiveFeedPlayer({
  feedId,
  overlay,
  alt,
  className,
  imgClassName,
}: LiveFeedPlayerProps) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [feedId, overlay]);

  const src = `/api/feeds/${encodeURIComponent(feedId)}/stream?overlay=${overlay ? 1 : 0}&r=${attempt}`;

  return (
    <div className={cn("relative h-full w-full bg-muted", className)}>
      {failed ? (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-4 text-center">
          <VideoOff className="h-8 w-8 text-muted-foreground" />
          <div>
            <p className="text-sm font-semibold">OpenCV feed service offline</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Start it with <code className="font-mono">npm run feeds</code>, then retry.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setFailed(false);
              setAttempt((n) => n + 1);
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-semibold hover:bg-accent"
          >
            <RotateCw className="h-3.5 w-3.5" />
            Retry
          </button>
        </div>
      ) : (
        <img
          key={src}
          src={src}
          alt={alt}
          draggable={false}
          className={cn("h-full w-full object-contain", imgClassName)}
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}
