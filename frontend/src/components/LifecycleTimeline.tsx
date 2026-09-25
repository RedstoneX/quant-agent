import { useState } from "react";
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

/** Scalar (non-object) fields only — these render inline as before. Object-
 * valued fields (e.g. `specialist_evidence`) are handled separately by
 * `EvidenceToggle` below so they never get JSON.stringify'd onto the
 * screen. Mirrors the split WhyPanel/TechnicalDetail already made for the
 * run-detail popup's sibling surface, the "Why" tab (item 106). */
function detailsText(details: Record<string, unknown>): string | null {
  const entries = Object.entries(details).filter(
    ([, value]) => value !== null && value !== "" && value !== undefined && typeof value !== "object",
  );
  if (!entries.length) return null;
  return entries.map(([key, value]) => `${humanize(key)}: ${String(value)}`).join(" · ");
}

/** Object-valued fields on the event, e.g. `specialist_evidence`. Only
 * non-empty objects/arrays are surfaced — an empty `{}` isn't worth a
 * toggle. */
function evidenceGroups(details: Record<string, unknown>): [string, unknown][] {
  return Object.entries(details).filter(
    ([, value]) => value !== null && typeof value === "object" && Object.keys(value as object).length > 0,
  );
}

/** Renders one stored value as text a person can read. Arrays become a
 * comma-separated list, nested objects become their own indented rows.
 * Nothing is ever JSON.stringify'd onto the screen — copied from
 * WhyPanel's `RawValue`, the pattern item 106 already shipped for the
 * "Why" tab's own raw-evidence toggle. */
function RawValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-dim">not recorded</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-dim">none</span>;
    return <span className="break-words">{value.map((item) => String(item)).join(", ")}</span>;
  }
  if (typeof value === "object") {
    return (
      <div className="flex flex-col gap-0.5">
        {Object.entries(value as Record<string, unknown>).map(([key, nested]) => (
          <div key={key} className="flex gap-2">
            <span className="shrink-0 text-dim">{humanize(key)}</span>
            <RawValue value={nested} />
          </div>
        ))}
      </div>
    );
  }
  if (typeof value === "boolean") return <span>{value ? "yes" : "no"}</span>;
  return <span className="break-words">{String(value)}</span>;
}

/** Labelled toggle for the object-valued fields on one lifecycle event —
 * the run-detail popup's version of the "Why" tab's technical-detail
 * disclosure (item 106), filed separately as item 115 because this modal
 * is a different surface with its own generic renderer. */
function EvidenceToggle({ groups }: { groups: [string, unknown][] }) {
  const [open, setOpen] = useState(false);
  if (!groups.length) return null;
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-[0.68rem] font-semibold uppercase tracking-wide text-dim hover:text-accent"
      >
        {open ? "Hide" : "Show"} the evidence behind this
      </button>
      {open && (
        <div className="mt-1.5 flex flex-col gap-2 font-mono text-[0.72rem]">
          {groups.map(([key, value]) => (
            <div key={key}>
              <div className="font-sans text-[0.68rem] font-semibold uppercase tracking-wide text-dim">
                {humanize(key)}
              </div>
              <div className="mt-0.5 pl-2">
                <RawValue value={value} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function LifecycleTimeline({ events, emptyText = "No persisted lifecycle events for this scope." }: { events: PipelineEvent[]; emptyText?: string }) {
  if (!events.length) return <Text>{emptyText}</Text>;
  return (
    <div className="space-y-2">
      {events.map((event, index) => {
        const details = detailsText(event.details);
        const groups = evidenceGroups(event.details);
        return (
          <Card key={`${event.timestamp || "event"}:${event.stage}:${event.outcome}:${index}`} className="!bg-panel-alt !p-3 !ring-border">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-ink">{STAGE_LABELS[event.stage] || humanize(event.stage)}</span>
              <Badge color={eventColor(event.outcome)} size="xs">{humanize(event.outcome)}</Badge>
              {event.timestamp && <span className="ml-auto text-xs text-dim">{fmtTime(event.timestamp)}</span>}
            </div>
            {event.reason && <Text className="mt-1 text-sm text-ink">{humanize(event.reason)}</Text>}
            {details && <Text className="mt-1 text-xs leading-snug">{details}</Text>}
            <EvidenceToggle groups={groups} />
          </Card>
        );
      })}
    </div>
  );
}
