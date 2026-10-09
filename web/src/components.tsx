import { ReactNode, useEffect, useState } from "react";
import { BUCKET_LABEL, dueText, dueTone } from "./format";

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    fn()
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, reload: () => setTick((t) => t + 1), setData };
}

export function Loading({ error, children }: { error?: string | null; children?: ReactNode }) {
  if (error) return <p className="error">{error}</p>;
  return <p className="muted">{children ?? "Loading…"}</p>;
}

export function Due({ days }: { days: number | null | undefined }) {
  return <span className={`due ${dueTone(days)}`}>{dueText(days)}</span>;
}

const BUCKET_CLASS: Record<string, string> = { RECOVERABLE: "recoverable", UNLIKELY: "unlikely", LOST: "lost" };

export function Bucket({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return <span className={`chip ${BUCKET_CLASS[value] ?? ""}`}>{BUCKET_LABEL[value] ?? value}</span>;
}

export const BUCKET_COLOR: Record<string, string> = {
  RECOVERABLE: "var(--recoverable)",
  UNLIKELY: "var(--unlikely)",
  LOST: "var(--lost)",
  NO_LOSS: "var(--neutral)",
  RESOLVED: "var(--neutral)",
};

/** Floating tooltip that follows the pointer. */
export function useTooltip() {
  const [tip, setTip] = useState<{ x: number; y: number; content: ReactNode } | null>(null);
  const bind = (content: ReactNode) => ({
    onMouseMove: (e: React.MouseEvent) => setTip({ x: e.clientX + 14, y: e.clientY + 14, content }),
    onMouseLeave: () => setTip(null),
    onFocus: (e: React.FocusEvent) => {
      const r = (e.target as HTMLElement).getBoundingClientRect();
      setTip({ x: r.left, y: r.bottom + 8, content });
    },
    onBlur: () => setTip(null),
  });
  const node = tip ? (
    <div className="tooltip" role="tooltip" style={{ left: Math.min(tip.x, window.innerWidth - 300), top: tip.y }}>
      {tip.content}
    </div>
  ) : null;
  return { bind, node };
}
