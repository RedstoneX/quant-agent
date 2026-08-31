import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { api, type ResearchDailyResponse, type ResearchDeskData } from "../../api/client";
import { todayEtDate } from "../../lib/format";
import { useIsDesktop } from "../../lib/useIsDesktop";
import { AgentBriefsPanel, DailyThesisPanel, DecisionDeltaPanel, ReviewPanel, SignalStackPanel, SmartMoneyPanel } from "./ResearchPanels";
import { ResearchState } from "./ResearchPrimitives";
import { buildResearchDesk } from "./buildResearchDesk";

/* Standing acceptance contract for this desk, relocated from AGENTS.md's
 * "Shipped tranche" section on 2026-08-31 (shipped; kept here as the
 * regression yardstick for this file, not open work — that file loads on
 * every session, this only matters when someone touches this desk). The
 * matching Smart Money Analyst backend contract lives in
 * src/agents/smart_money_analyst.py.
 *
 * Research/reading experience outcome:
 *
 * Desktop should have a strong designed default composition, then let the
 * operator rearrange it. Reuse Dockview so panels can move, resize, tab,
 * maximize and persist their layout. iPad should be composed for reading,
 * not squeezed from desktop.
 *
 * The writing should be compact but substantive. Short sentences. Strong
 * editing. No filler, repeated conclusions, forced jokes, fake quotes or
 * generic AI throat-clearing. Wit should come from judgment, not
 * punchlines. Quiet days should stay quiet.
 *
 * Use visual structure where it genuinely helps:
 * - signal stack — quick agreement/conflict across relevant agents;
 * - what changed — the new information since the prior useful read;
 * - tension — the most important disagreement or contradiction;
 * - why now — why the item deserves attention today;
 * - evidence strip — compact factual chips instead of prose where possible;
 * - mini chart/sparkline — only when it adds immediate market context;
 * - Read / PM / Risk — clearly separated judgment, portfolio implication
 *   and risk consequence;
 * - dry annotation — occasional, restrained, evidence-based commentary
 *   when the situation earns it.
 *
 * Do not force every device onto every card. The point is rhythm and
 * hierarchy, not decoration. One important story may be visually dominant
 * while supporting research is smaller. Balance matters more than
 * symmetry.
 *
 * Favor useful editorial synthesis such as daily market thesis, agent
 * findings, disagreement, Smart Money evidence, PM ruling, Risk response,
 * proposed-versus-executed delta, position review, after-the-bell lessons
 * and tomorrow watch. Raw structured evidence remains secondary
 * drill-down.
 *
 * No fabricated confidence, quotes, history or facts. Sparse, stale,
 * partial, no-news, no-trade and provider-error states must look
 * intentional and remain truthful.
 *
 * Acceptance — this (and the Smart Money Analyst contract above) is
 * complete when real stored QAMC data demonstrates that:
 *
 * 1. An operator can read a coherent daily story without opening logs or
 *    JSON.
 * 2. Every relevant agent has a useful, visually balanced representation
 *    of its findings, strongest evidence, meaningful changes and
 *    disagreement where supported.
 * 3. The writing is substantive without being verbose, visually
 *    scannable, and has a restrained private-desk personality rather than
 *    corporate/LLM prose.
 * 4. Signal stacks, change markers, tension, why-now context, evidence
 *    strips, mini-chart context, Read/PM/Risk separation and occasional
 *    dry annotations are used where they improve comprehension rather
 *    than mechanically everywhere.
 * 5. PM/Risk/execution are understandable as deltas: what PM wanted, what
 *    Risk changed, what deterministic code allowed/blocked, and what
 *    actually executed.
 * 6. Desktop research panels are genuinely
 *    movable/resizable/tabbable/maximizable with persisted layout and a
 *    sensible default workspace.
 * 7. iPad has a deliberately designed reading/navigation experience with
 *    no horizontal overflow or micro-text.
 * 8. Smart Money Analyst is SEC-source-backed, accession/timestamp/lag-aware,
 *    attributable, direction-validated, noise-suppressing, and reaches PM
 *    only through the accepted specialist path. Any external symbol is
 *    run-scoped, visibly admitted by deterministic evidence, and still
 *    traverses the full Technical → PM → AI Risk → deterministic gate →
 *    broker chain.
 * 9. Empty, stale, partial and provider-error states are truthful and
 *    visually composed.
 * 10. Targeted tests/build pass and rendered desktop+iPad visual
 *     acceptance passes with zero console/page errors and no horizontal
 *     overflow.
 */

type DeskSection = "brief" | "signals" | "decision" | "review";
const ResearchContext = createContext<ResearchDeskData | null>(null);
function useResearch() { const value = useContext(ResearchContext); if (!value) throw new Error("ResearchDesk context missing"); return value; }
function daysBefore(value: string, count: number) {
  const parsed = new Date(`${value}T12:00:00Z`);
  parsed.setUTCDate(parsed.getUTCDate() - count);
  return parsed.toISOString().slice(0, 10);
}

function LeadPane(_: IDockviewPanelProps) { return <div className="research-pane"><DailyThesisPanel data={useResearch()} /><SignalStackPanel data={useResearch()} /></div>; }
function AgentsPane(_: IDockviewPanelProps) { return <div className="research-pane"><AgentBriefsPanel data={useResearch()} /></div>; }
function DecisionPane(_: IDockviewPanelProps) { return <div className="research-pane"><DecisionDeltaPanel data={useResearch()} /></div>; }
function SmartMoneyPane(_: IDockviewPanelProps) { return <div className="research-pane"><SmartMoneyPanel data={useResearch()} /></div>; }
function ReviewPane(_: IDockviewPanelProps) { return <div className="research-pane"><ReviewPanel data={useResearch()} /></div>; }

const COMPONENTS = { lead: LeadPane, agents: AgentsPane, decision: DecisionPane, smart_money: SmartMoneyPane, review: ReviewPane };
export const RESEARCH_STORAGE_KEY = "qamc.dockview.research.v2";

export function buildResearchDefaultLayout(api: DockviewApi) {
  api.addPanel({ id: "agents", component: "agents", title: "Agent Findings" });
  api.addPanel({ id: "lead", component: "lead", title: "Daily Brief", position: { referencePanel: "agents", direction: "left" }, initialWidth: 440 });
  api.addPanel({ id: "smart_money", component: "smart_money", title: "Smart Money", position: { referencePanel: "agents", direction: "within" }, inactive: true });
  api.addPanel({ id: "decision", component: "decision", title: "Decision & Review", position: { referencePanel: "agents", direction: "right" }, initialWidth: 420 });
  api.addPanel({ id: "review", component: "review", title: "After the Bell", position: { referencePanel: "decision", direction: "within" }, inactive: true });
  api.getPanel("agents")?.api.setActive();
}

function DesktopResearchWorkspace({ data }: { data: ResearchDeskData }) {
  const apiRef = useRef<DockviewApi | null>(null);
  const [maximized, setMaximized] = useState(false);
  const reset = useCallback(() => {
    localStorage.removeItem(RESEARCH_STORAGE_KEY);
    const dockApi = apiRef.current;
    if (!dockApi) return;
    dockApi.activePanel?.api.exitMaximized();
    [...dockApi.panels].forEach((panel) => dockApi.removePanel(panel));
    buildResearchDefaultLayout(dockApi);
    setMaximized(false);
  }, []);
  const toggleMaximize = useCallback(() => {
    const dockApi = apiRef.current;
    const panel = dockApi?.activePanel;
    if (!dockApi || !panel) return;
    if (panel.api.isMaximized()) panel.api.exitMaximized(); else panel.api.maximize();
    setMaximized(panel.api.isMaximized());
    try { localStorage.setItem(RESEARCH_STORAGE_KEY, JSON.stringify(dockApi.toJSON())); } catch { /* read-side best effort */ }
  }, []);
  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    try { const saved = localStorage.getItem(RESEARCH_STORAGE_KEY); if (saved) event.api.fromJSON(JSON.parse(saved)); }
    catch { localStorage.removeItem(RESEARCH_STORAGE_KEY); }
    if (!event.api.panels.length) buildResearchDefaultLayout(event.api);
    const syncMaximized = () => setMaximized(Boolean(event.api.activePanel?.api.isMaximized()));
    event.api.onDidLayoutChange(() => { try { localStorage.setItem(RESEARCH_STORAGE_KEY, JSON.stringify(event.api.toJSON())); } catch { /* read-side best effort */ } syncMaximized(); });
    event.api.onDidActivePanelChange(syncMaximized);
    syncMaximized();
  }, []);
  return <ResearchContext.Provider value={data}><div className="research-workspace">
    <div className="research-workspace-bar"><span>Research workspace · move, resize, tab or maximize panels</span><div><button type="button" onClick={toggleMaximize}>{maximized ? "Restore workspace" : "Maximize active panel"}</button><button type="button" onClick={reset}>Reset layout</button></div></div>
    <div className="h-[max(620px,calc(100vh-210px))] overflow-hidden rounded-lg border border-border"><DockviewReact className="dockview-theme-qamc" components={COMPONENTS} onReady={onReady} /></div>
  </div></ResearchContext.Provider>;
}

function IPadResearchDesk({ data }: { data: ResearchDeskData }) {
  const [section, setSection] = useState<DeskSection>("brief");
  return <div className="ipad-research"><nav aria-label="Research sections">{(["brief", "signals", "decision", "review"] as const).map((item) => <button type="button" key={item} onClick={() => setSection(item)} className={section === item ? "active" : ""}>{item}</button>)}</nav>
    <main>
      {section === "brief" && <><DailyThesisPanel data={data} /><AgentBriefsPanel data={data} /></>}
      {section === "signals" && <><SignalStackPanel data={data} /><SmartMoneyPanel data={data} /></>}
      {section === "decision" && <DecisionDeltaPanel data={data} />}
      {section === "review" && <ReviewPanel data={data} />}
    </main>
  </div>;
}

export function ResearchDesk() {
  const isDesktop = useIsDesktop();
  const [date, setDate] = useState(todayEtDate());
  const [data, setData] = useState<ResearchDailyResponse | null>(null);
  const [priorData, setPriorData] = useState<ResearchDailyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true; setLoading(true); setError(null); setData(null); setPriorData(null);
    (async () => {
      const result = await api.researchDaily(date);
      let prior: ResearchDailyResponse | null = null;
      for (let offset = 1; offset <= 7 && !prior; offset += 1) {
        try {
          const candidate = await api.researchDaily(daysBefore(date, offset));
          if (candidate.date !== date && ["complete", "partial"].includes(candidate.state) && (candidate.runs.length || candidate.reflection || candidate.daily_pnl)) prior = candidate;
        } catch { continue; }
      }
      if (active) { setData(result); setPriorData(prior); }
    })().catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Research request failed"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [date]);
  return <div className="research-desk-shell">
    <header className="research-date-bar"><div><span>Research Intelligence Desk</span><small>Stored evidence, edited for one operator. Read-only.</small></div><label>Date <input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label></header>
    {loading && !data ? <div className="research-loading">Loading the desk…</div> : error && !data ? <ResearchState status="error" errors={[error]} /> : data ? (isDesktop ? <DesktopResearchWorkspace data={buildResearchDesk(data, priorData)} /> : <IPadResearchDesk data={buildResearchDesk(data, priorData)} />) : null}
  </div>;
}
