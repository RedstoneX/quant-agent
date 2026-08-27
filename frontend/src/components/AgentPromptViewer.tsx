import { useMemo, useState } from "react";
import { AgentLogItem } from "../api/client";

/**
 * "What this agent actually read" — the verbatim prompt behind a decision.
 *
 * `agent_logs.input_message` has stored the complete assembled prompt all
 * along, and `/runs/{run_id}` has served it; nothing rendered it. For the
 * Portfolio Manager that prompt IS the briefing the operator otherwise has to
 * reconstruct from its sources: the seven-evening narrative, the fourteen-day
 * recurring missed themes, repeat loss patterns, the last risk verdicts, the
 * PM's own recent decisions, and its realized win-rate calibration. Reading
 * the evening reflection tells you what the desk concluded. This tells you
 * what it was looking at when it concluded it — which is the only way to
 * tell a bad decision apart from a decision made on bad input.
 *
 * Size is the design constraint: production PM prompts run 13KB-190KB, so
 * this stays collapsed until asked for, caps its own height, and never
 * reflows the modal around it.
 */

const PREVIEW_CHARS = 400;

function ByteSize({ chars }: { chars: number }) {
  const label = chars >= 1024 ? `${(chars / 1024).toFixed(1)}KB` : `${chars} chars`;
  return <span className="text-dim tabular-nums">{label}</span>;
}

function TextBlock({ text }: { text: string }) {
  return (
    <pre className="mt-2 max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-panel-alt p-3 text-[0.78rem] leading-relaxed font-mono">
      {text}
    </pre>
  );
}

function Section({
  label, text, defaultOpen = false,
}: { label: string; text: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  // The head of the prompt is the account-status block, which is the most
  // useful 400 characters to show without expanding.
  const preview = useMemo(
    () => text.slice(0, PREVIEW_CHARS).replace(/\s+/g, " ").trim(),
    [text],
  );
  return (
    <div className="mb-3 last:mb-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-left text-[0.85rem] font-bold hover:text-accent"
      >
        <span aria-hidden className="text-dim">{open ? "▾" : "▸"}</span>
        <span>{label}</span>
        <ByteSize chars={text.length} />
      </button>
      {open ? (
        <TextBlock text={text} />
      ) : (
        <p className="mt-1 pl-5 text-[0.78rem] text-dim line-clamp-2">
          {preview}
          {text.length > PREVIEW_CHARS ? "…" : ""}
        </p>
      )}
    </div>
  );
}

export function AgentPromptViewer({ log }: { log: AgentLogItem }) {
  const prompt = log.input_message ?? "";
  const response = log.full_response ?? "";

  if (!prompt && !response) {
    return (
      <div className="state-message">
        No prompt or response was persisted for this call. Rows written before
        the field existed, and any call that failed before the prompt was
        assembled, are legitimately empty.
      </div>
    );
  }

  return (
    <div>
      {prompt ? (
        <Section label="Prompt the agent received" text={prompt} defaultOpen />
      ) : null}
      {response ? (
        <Section label="Raw response" text={response} />
      ) : null}
    </div>
  );
}
