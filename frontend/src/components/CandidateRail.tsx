import { useMemo, useState } from "react";
import { CandidateFunnelItem, RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { useModalActions } from "../context/ModalContext";

/* Left rail: the candidate universe collapsed into the funnel stage each
 * candidate actually reached, instead of one flat "PM: no proposal" pill
 * per ticker (the prior WatchlistPanel dumped all 80+ candidates as an
 * unbounded page-length list). Each candidate is bucketed into exactly
 * one stage — the FURTHEST stage it reached — so the five buckets below
 * partition the full candidate set with no double-counting. */

type Stage = "rejected" | "reached_pm" | "proposed" | "risk_action" | "executed";

const STAGE_META: Record<Stage, { label: string; dotClass: string; textClass: string; short: string }> = {
  rejected: { label: "Rejected by specialist", dotClass: "bg-dim", textClass: "text-dim", short: "—" },
  reached_pm: { label: "Reached PM", dotClass: "bg-accent", textClass: "text-accent", short: "PM" },
  proposed: { label: "Proposed", dotClass: "bg-warn", textClass: "text-warn", short: "proposed" },
  risk_action: { label: "Modified / blocked by risk", dotClass: "bg-neg", textClass: "text-neg", short: "risk" },
  executed: { label: "Executed", dotClass: "bg-pos", textClass: "text-pos", short: "executed" },
};

// Furthest-stage first, so the rail's default (unfiltered) ordering
// surfaces the most decision-relevant candidates before the 60+ that
// never left specialist screening.
const STAGE_ORDER: Stage[] = ["executed", "risk_action", "proposed", "reached_pm", "rejected"];

// risk_verdict is recorded per run, not per candidate (the AI Risk
// Manager evaluates a run's proposed orders as one batch — see
// DirectionalBiasPanel's identical caveat). A run-wide hard-risk block or
// rejected verdict is therefore attributed to every candidate that
// reached a proposed order in that run; risk_modified is already exact
// per-candidate. This is the same run-level-attribution precedent already
// accepted elsewhere in this codebase, not a new inference.
function candidateStage(c: CandidateFunnelItem, funnel: RunFunnelResponse): Stage {
  if (c.executed) return "executed";
  const verdict = funnel.risk_verdict?.verdict;
  const riskActed =
    c.risk_modified ||
    (c.reached_proposed_order && funnel.hard_risk_block) ||
    (c.reached_proposed_order && verdict?.approved === false);
  if (riskActed) return "risk_action";
  if (c.reached_proposed_order) return "proposed";
  if (c.reached_pm_target) return "reached_pm";
  return "rejected";
}

function DirGlyph({ direction }: { direction: CandidateFunnelItem["direction"] }) {
  const glyph = direction === "bullish" ? "▲" : direction === "bearish" ? "▼" : "•";
  const cls = direction === "bullish" ? "text-pos" : direction === "bearish" ? "text-neg" : "text-dim";
  return <span className={`${cls} font-bold w-3 inline-block text-center flex-shrink-0`}>{glyph}</span>;
}

// The funnel as bars (Considered -> Reached PM -> Proposed -> Executed),
// each width relative to "Considered" — a real graphical funnel, not just
// the four numbers FunnelSteps already prints elsewhere.
function FunnelBars({ funnel }: { funnel: RunFunnelResponse }) {
  const stages: { label: string; value: number; cls: string }[] = [
    { label: "Considered", value: funnel.candidates_considered, cls: "bg-accent" },
    { label: "Reached PM", value: funnel.reached_pm_count, cls: "bg-accent" },
    { label: "Proposed", value: funnel.proposed_order_count, cls: "bg-warn" },
    { label: "Executed", value: funnel.executed_count, cls: "bg-pos" },
  ];
  const max = stages[0].value || 1;
  return (
    <div className="flex flex-col gap-1 mb-3">
      {stages.map((s) => (
        <div key={s.label} className="flex items-center gap-2">
          <span className="text-[0.6rem] text-dim uppercase tracking-wide w-[68px] flex-shrink-0">{s.label}</span>
          <div className="flex-1 h-2.5 rounded-full bg-panel-alt overflow-hidden border border-border/60">
            <div className={`h-full rounded-full ${s.cls}`} style={{ width: `${(s.value / max) * 100}%` }} />
          </div>
          <span className="text-[0.74rem] font-extrabold tabular-nums w-5 text-right flex-shrink-0">{s.value}</span>
        </div>
      ))}
    </div>
  );
}

function StageChip({
  active,
  label,
  count,
  onClick,
  dotClass,
}: {
  active: boolean;
  label: string;
  count: number;
  onClick: () => void;
  dotClass: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1.5 px-2 py-1.5 rounded-md border text-left ${
        active ? "border-accent bg-accent/10" : "border-border bg-panel-alt hover:border-accent/50"
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotClass}`} />
      <span className="text-[0.62rem] uppercase tracking-wide text-dim leading-tight flex-1">{label}</span>
      <span className="text-[0.82rem] font-extrabold tabular-nums flex-shrink-0">{count}</span>
    </button>
  );
}

export function CandidateRail({
  funnel,
  loading,
  error,
  selectedSymbol,
  onSelectSymbol,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  selectedSymbol: string | null;
  onSelectSymbol: (symbol: string) => void;
}) {
  const { openCandidateDetail } = useModalActions();
  const [filter, setFilter] = useState<Stage | "all">("all");
  const [q, setQ] = useState("");

  const buckets = useMemo(() => {
    const map: Record<Stage, CandidateFunnelItem[]> = {
      rejected: [],
      reached_pm: [],
      proposed: [],
      risk_action: [],
      executed: [],
    };
    if (funnel) {
      for (const c of funnel.candidates) map[candidateStage(c, funnel)].push(c);
    }
    return map;
  }, [funnel]);

  const total = funnel?.candidates.length ?? 0;

  const visible = useMemo(() => {
    if (!funnel) return [];
    let list = filter === "all" ? funnel.candidates : buckets[filter];
    if (q.trim()) {
      const needle = q.trim().toUpperCase();
      list = list.filter((c) => c.symbol.includes(needle));
    }
    return [...list].sort((a, b) => {
      const sa = STAGE_ORDER.indexOf(candidateStage(a, funnel));
      const sb = STAGE_ORDER.indexOf(candidateStage(b, funnel));
      if (sa !== sb) return sa - sb;
      return a.symbol.localeCompare(b.symbol);
    });
  }, [funnel, buckets, filter, q]);

  const status = error ? "error" : loading ? "loading" : "ok";

  return (
    <Panel title="Candidates this run" subtitle={funnel && total > 0 ? `${total} shortlisted for consideration` : undefined} status={status}>
      {error && <StateMessage text={`Could not load candidates: ${error}`} error />}
      {!error && funnel && total === 0 && <StateMessage text="No candidates considered in the latest run." />}
      {!error && funnel && total > 0 && (
        <div>
          <FunnelBars funnel={funnel} />
          {funnel.bearish_hedge_considered && (
            <div className="inline-flex items-center gap-1.5 mb-2.5 px-2 py-1 rounded-md bg-hedge/10 border border-hedge/30">
              <span className="w-1.5 h-1.5 rounded-full bg-hedge flex-shrink-0" />
              <span className="text-hedge text-[0.66rem] font-bold uppercase tracking-wide">Bearish-hedge candidate in this run</span>
            </div>
          )}
          <div className="grid grid-cols-2 gap-1.5 mb-2.5">
            <StageChip active={filter === "all"} label="Shortlisted" count={total} onClick={() => setFilter("all")} dotClass="bg-accent" />
            {STAGE_ORDER.slice()
              .reverse()
              .map((s) => (
                <StageChip
                  key={s}
                  active={filter === s}
                  label={STAGE_META[s].label}
                  count={buckets[s].length}
                  onClick={() => setFilter(filter === s ? "all" : s)}
                  dotClass={STAGE_META[s].dotClass}
                />
              ))}
          </div>

          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter symbol…"
            className="w-full bg-panel-alt border border-border rounded text-[0.78rem] px-2 py-1 mb-2"
          />

          <div className="flex flex-col gap-0.5 max-h-[420px] overflow-y-auto -mx-1 px-1">
            {visible.length === 0 && <StateMessage text="No candidates match this filter." />}
            {visible.map((c) => {
              const stage = candidateStage(c, funnel);
              const meta = STAGE_META[stage];
              const active = c.symbol === selectedSymbol;
              return (
                <button
                  key={c.symbol}
                  type="button"
                  onClick={() => {
                    onSelectSymbol(c.symbol);
                    openCandidateDetail(funnel.run_id, c.symbol);
                  }}
                  className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[0.79rem] border ${
                    active ? "border-accent bg-accent/10" : "border-transparent hover:bg-panel-alt"
                  }`}
                >
                  <DirGlyph direction={c.direction} />
                  <span className="font-semibold flex-shrink-0">{c.symbol}</span>
                  {c.is_bearish_hedge && <span className="text-hedge text-[0.6rem] font-bold uppercase flex-shrink-0">hedge</span>}
                  <span className={`ml-auto text-[0.64rem] font-semibold uppercase tracking-wide flex-shrink-0 ${meta.textClass}`}>
                    {meta.short}
                  </span>
                </button>
              );
            })}
          </div>

          {buckets.risk_action.length > 0 && (
            <div className="state-message mt-2 text-[0.7rem]">
              Risk-verdict attribution is per run, not per candidate — a rejected/blocked run attributes to every
              proposed candidate within it.
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
