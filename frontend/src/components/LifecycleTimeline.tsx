import { Badge, Card, Text } from "@tremor/react";
import { PipelineEvent } from "../api/client";
import { fmtTime } from "../lib/format";

const STAGE_LABELS: Record<string, string> = {
  opportunity: "Opportunity",
  specialist: "Specialists",
  portfolio_manager: "Portfolio Manager",
  risk: "AI Risk Manager",
  deterministic_gate: "Deterministic gate",
  funding: "Funding",
  order: "Order",
  protection: "Protection",
  position_management: "Position management / exit",
};

function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

function eventColor(outcome: string): "emerald" | "rose" | "amber" | "cyan" | "slate" {
  if (["failed", "rejected", "blocked", "not_placed"].includes(outcome)) return "rose";
  if (["modified", "resized", "partially_filled", "submit_unknown"].includes(outcome)) return "amber";
  if (["allowed", "approved", "submitted", "filled", "placed", "funded", "exited", "evaluated", "discovered"].includes(outcome)) return "emerald";
  if (["proposed", "attempted"].includes(outcome)) return "cyan";
  return "slate";
}

function detailsText(details: Record<string, unknown>): string | null {
  const entries = Object.entries(details).filter(([, value]) => value !== null && value !== "" && value !== undefined);
  if (!entries.length) return null;
  return entries.map(([key, value]) => `${humanize(key)}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`).join(" · ");
}

export function LifecycleTimeline({ events, emptyText = "No persisted lifecycle events for this scope." }: { events: PipelineEvent[]; emptyText?: string }) {
  if (!events.length) return <Text>{emptyText}</Text>;
  return (
    <div className="space-y-2">
      {events.map((event, index) => {
        const details = detailsText(event.details);
        return (
          <Card key={`${event.timestamp || "event"}:${event.stage}:${event.outcome}:${index}`} className="!bg-panel-alt !p-3 !ring-border">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-ink">{STAGE_LABELS[event.stage] || humanize(event.stage)}</span>
              <Badge color={eventColor(event.outcome)} size="xs">{humanize(event.outcome)}</Badge>
              {event.timestamp && <span className="ml-auto text-xs text-dim">{fmtTime(event.timestamp)}</span>}
            </div>
            {event.reason && <Text className="mt-1 text-sm text-ink">{humanize(event.reason)}</Text>}
            {details && <Text className="mt-1 text-xs leading-snug">{details}</Text>}
          </Card>
        );
      })}
    </div>
  );
}
