import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { DockviewReact, type DockviewApi, type DockviewReadyEvent, type IDockviewPanelProps } from "dockview-react";
import "dockview-react/dist/styles/dockview.css";
import { api, type ResearchDailyResponse, type ResearchDeskData } from "../../api/client";
import { todayEtDate } from "../../lib/format";
import { useIsDesktop } from "../../lib/useIsDesktop";
import { AgentBriefsPanel, DailyThesisPanel, DecisionDeltaPanel, ReviewPanel, SignalStackPanel, SmartMoneyPanel } from "./ResearchPanels";
import { ResearchState } from "./ResearchPrimitives";
import { buildResearchDesk } from "./buildResearchDesk";

type DeskSection = "brief" | "signals" | "decision" | "review";
const ResearchContext = createContext<ResearchDeskData | null>(null);
function useResearch() { const value = useContext(ResearchContext); if (!value) throw new Error("ResearchDesk context missing"); return value; }

function LeadPane(_: IDockviewPanelProps) { return <div className="research-pane"><DailyThesisPanel data={useResearch()} /><SignalStackPanel data={useResearch()} /></div>; }
function AgentsPane(_: IDockviewPanelProps) { return <div className="research-pane"><AgentBriefsPanel data={useResearch()} /></div>; }
function DecisionPane(_: IDockviewPanelProps) { return <div className="research-pane"><DecisionDeltaPanel data={useResearch()} /><ReviewPanel data={useResearch()} /></div>; }
function SmartMoneyPane(_: IDockviewPanelProps) { return <div className="research-pane"><SmartMoneyPanel data={useResearch()} /></div>; }
function ReviewPane(_: IDockviewPanelProps) { return <div className="research-pane"><ReviewPanel data={useResearch()} /></div>; }

const COMPONENTS = { lead: LeadPane, agents: AgentsPane, decision: DecisionPane, smart_money: SmartMoneyPane, review: ReviewPane };
export const RESEARCH_STORAGE_KEY = "qamc.dockview.research.v1";

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
  const reset = useCallback(() => {
    localStorage.removeItem(RESEARCH_STORAGE_KEY);
    const dockApi = apiRef.current;
    if (!dockApi) return;
    [...dockApi.panels].forEach((panel) => dockApi.removePanel(panel));
    buildResearchDefaultLayout(dockApi);
  }, []);
  const onReady = useCallback((event: DockviewReadyEvent) => {
    apiRef.current = event.api;
    try { const saved = localStorage.getItem(RESEARCH_STORAGE_KEY); if (saved) event.api.fromJSON(JSON.parse(saved)); }
    catch { localStorage.removeItem(RESEARCH_STORAGE_KEY); }
    if (!event.api.panels.length) buildResearchDefaultLayout(event.api);
    event.api.onDidLayoutChange(() => { try { localStorage.setItem(RESEARCH_STORAGE_KEY, JSON.stringify(event.api.toJSON())); } catch { /* read-side best effort */ } });
  }, []);
  return <ResearchContext.Provider value={data}><div className="research-workspace">
    <div className="research-workspace-bar"><span>Research workspace · move, resize, tab or maximize panels</span><button type="button" onClick={reset}>Reset layout</button></div>
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true; setLoading(true); setError(null);
    api.researchDaily(date).then((result) => { if (active) setData(result); }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "Research request failed"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [date]);
  return <div className="research-desk-shell">
    <header className="research-date-bar"><div><span>Research Intelligence Desk</span><small>Stored evidence, edited for one operator. Read-only.</small></div><label>Date <input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label></header>
    {loading && !data ? <div className="research-loading">Loading the desk…</div> : error && !data ? <ResearchState status="error" errors={[error]} /> : data ? (isDesktop ? <DesktopResearchWorkspace data={buildResearchDesk(data)} /> : <IPadResearchDesk data={buildResearchDesk(data)} />) : null}
  </div>;
}
