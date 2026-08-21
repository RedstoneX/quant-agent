import { useEffect, useState } from "react";
import { api, CandidateDetailResponse, RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { Card } from "./ui/Evidence";
import { AgentFlowGraph } from "./agentflow/AgentFlowGraph";
import { buildRunGraph, buildCandidateGraph } from "./agentflow/buildGraph";
import { buildCandidateStages } from "./funnelShared";
import { useModalActions } from "../context/ModalContext";

/* The cockpit's "Decision Room" — a narrow-column condensation of what
 * used to be the full-width DecisionFunnelPanel. Same underlying data
 * (RunFunnelResponse, same buildFunnelStages derivation), just laid out
 * for a ~340px rail: single-column cards, clamped excerpt text, and a
 * link into RunDetailModal for the uncondensed version rather than
 * inlining everything here.
 *
 * The primary graph is whichever symbol is currently charted: real
 * per-specialist fan-in (buildCandidateGraph — Oralexa-style agent cards
 * with a genuine disagreement/alignment computation), the same
 * component CandidateDetailModal's drill-down uses, not a second
 * implementation. This is deliberate: the vision board's "compact
 * graphical story" — specialist stances -> PM -> Risk -> gate ->
 * execution — is meant to be visible on the primary screen for whatever
 * is charted, not only after an extra click into a modal. Falls back to
 * the run-level aggregate (always exactly 5 stage boxes, no specialist
 * fan-in) only when no candidate is selected or the per-candidate fetch
 * hasn't resolved yet. */

// Local, not shared ui/Evidence.tsx's CardText — that component has no
// line-clamp option, and adding one there would affect every other panel
// using it. Long PM/Risk narrative text is exactly what would otherwise
// make this rail as tall as the old page-dump it replaces.
function ClampText({ text }: { text: string }) {
  return <p className="text-[0.875rem] mt-1.5 leading-snug line-clamp-5">{text}</p>;
}

export function DecisionRoomPanel({
  funnel,
  symbol,
  loading,
  error,
  updatedAt,
}: {
  funnel: RunFunnelResponse | null;
  /** Whichever symbol is currently charted (App.tsx's chartSymbol) — drives
   * the real per-specialist agent graph below when it's among this run's
   * candidates. */
  symbol: string | null;
  loading: boolean;
  error: string | null;
  updatedAt?: Date | null;
}) {
  const { openRunDetail, openCandidateDetail } = useModalActions();
  // A failed poll never blanks previously-loaded funnel data — it renders
  // as "stale" instead, with a timestamp, so the reader is never shown an
  // old EXECUTED/REJECTED verdict looking exactly like a current one. Only
  // a fetch that has NEVER succeeded (no funnel data at all) renders as a
  // bare error.
  const status = error ? (funnel ? "stale" : "error") : loading ? "loading" : "ok";

  const candidateEligible = !!(funnel && symbol && funnel.candidates.some((c) => c.symbol === symbol));
  const [detail, setDetail] = useState<CandidateDetailResponse | null>(null);
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    if (!candidateEligible || !funnel || !symbol) return;
    let cancelled = false;
    api
      .candidateDetail(funnel.run_id, symbol)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setDetailKey(`${funnel.run_id}:${symbol}`);
        setDetailError(null);
      })
      .catch((err) => {
        if (!cancelled) setDetailError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [candidateEligible, funnel?.run_id, symbol]);

  const detailReady = candidateEligible && detail && funnel && symbol && detailKey === `${funnel.run_id}:${symbol}`;

  return (
    <Panel
      title={detailReady ? `Decision Room — ${symbol}` : "Decision Room — selected run"}
      status={status}
      staleSince={updatedAt}
      accent
    >
      {error && !funnel && <StateMessage text={`Could not load selected decision: ${error}`} error hero glyph="■" />}
      {!error && !funnel && loading && <StateMessage text="Loading…" hero />}
      {!error && !funnel && !loading && (
        <StateMessage
          text="No session yet today. The chain populates once QAMC's first scan of the day completes."
          hero
          glyph="○"
        />
      )}
      {funnel && (
        <div className="flex flex-col gap-3">
          {error && (
            <div className="text-warn text-[0.8125rem] bg-warn/10 border border-warn/30 rounded-md px-2.5 py-1.5">
              Showing last known decision as of {updatedAt ? updatedAt.toLocaleTimeString() : "an earlier fetch"} — a fresh
              fetch failed: {error}
            </div>
          )}
          {/* The terminal state pill itself now lives in the full-width
              DecisionStateBanner above the cockpit body — this graph is the
              "how it got there," not a second place to restate "what." */}
          {detailReady && detail && symbol ? (
            <>
              <AgentFlowGraph
                {...buildCandidateGraph(
                  detail,
                  buildCandidateStages(detail, funnel),
                  () => openCandidateDetail(funnel.run_id, symbol),
                  "vertical"
                )}
                // Tall enough for every specialist row (Tech/Earnings/each
                // News item) plus the 4-stage chain below it, stacked in a
                // single column — see buildGraph.ts's "vertical" mode. A
                // fixed height here would clip a candidate with several
                // news items or crush one with none; this sizes to what
                // this specific candidate actually has. The 165/105
                // multipliers must match buildGraph.ts's own rowSpacing
                // (specialist rows) and chain-row pitch exactly (Fix 5,
                // visual convergence plan §2.6 — verified and corrected
                // against real render, see buildGraph.ts's own comment) —
                // this is a real fitView container height, not just a
                // cosmetic value: React Flow's fitView scales the whole
                // layout uniformly to fit this box, so a mismatched
                // estimate here re-introduces exactly the void Fix 5
                // removes (an under-tall box over-zooms out to compensate,
                // an over-tall one leaves dead space around the fitted
                // graph).
                height={
                  40 +
                  Math.max(1, (detail.tech ? 1 : 0) + (detail.earnings ? 1 : 0) + detail.news_symbol.length) * 165 +
                  4 * 105 +
                  60
                }
              />
              <button
                type="button"
                onClick={() => openCandidateDetail(funnel.run_id, symbol)}
                className="text-accent underline text-[0.8125rem] text-left -mt-1.5"
              >
                Full drill-down — evidence, target &amp; modification &rarr;
              </button>
            </>
          ) : candidateEligible && !detailError ? (
            <StateMessage text={`Loading ${symbol}'s decision flow…`} />
          ) : (
            <AgentFlowGraph {...buildRunGraph(funnel)} height={360} />
          )}

          {funnel.bearish_hedge_considered && (
            <div className="state-message">A bearish inverse-ETF candidate was considered this run.</div>
          )}

          {/* Fix 4 (visual convergence plan §2.5, Finding F): this used to
              repeat HeroBand's RegimeBadge in full (pill + outlook +
              confidence + the identical summary sentence) — 100% content
              duplication on the same unscrolled screen, wasting ~110-140px
              of this column's budget on run-wide context that's already
              stated, more prominently, above the fold. HeroBand's
              RegimeBadge stays the single canonical "what does QAMC think
              the market is doing" location; this is now a one-line
              cross-reference, not a second full treatment — and renders
              nothing at all when there's no regime to reference (the
              "no evidence" case is already covered by HeroBand). */}
          {funnel.macro_context?.regime && (
            <div className="text-meta">
              Regime: <span className="font-semibold text-ink">{funnel.macro_context.regime.toUpperCase()}</span>
              {funnel.macro_context.equity_outlook ? ` · ${funnel.macro_context.equity_outlook.toUpperCase()}` : ""}
              {" — see hero band above"}
            </div>
          )}

          {/* Explicitly labeled "run-wide" — this run can cover several
              candidates under one PM/Risk pass, and this panel may now be
              titled for a specific symbol above; these cards must never
              read as if they were derived for that symbol alone. See
              docs/WORK.md's "Candidate Detail can misattribute run-wide
              PM/Risk evidence to an individual ticker" debt item. */}
          {funnel.pm_reasoning?.portfolio_view && (
            <Card title="Portfolio Manager — run-wide">
              <ClampText text={funnel.pm_reasoning.portfolio_view} />
            </Card>
          )}

          {funnel.risk_verdict?.verdict && (
            <Card title="AI Risk Manager — run-wide">
              <div className="kv-row">
                <span className="text-dim">Verdict</span>
                <div className="flex gap-1.5 flex-wrap justify-end">
                  <Pill text={funnel.risk_verdict.verdict.approved ? "approved" : "rejected"} />
                  <Pill text={funnel.risk_verdict.verdict.reason_category} />
                </div>
              </div>
              <ClampText text={funnel.risk_verdict.verdict.reasoning} />
            </Card>
          )}

          {funnel.decision_state === "hard_risk_block" && (
            <Card title="Deterministic gate">
              <ClampText text="The deterministic hard-risk gate blocked every candidate this run before the AI Risk Manager was ever called." />
            </Card>
          )}

          <button
            type="button"
            onClick={() => openRunDetail(funnel.run_id)}
            className="text-accent underline text-[0.8125rem] text-left"
          >
            Open full run detail &rarr;
          </button>
        </div>
      )}
    </Panel>
  );
}
