import type { ResearchDeskData, ResearchAgentBrief, ResearchMarketContext } from "../../api/client";
import { DirectionBadge, EvidenceStrip, Eyebrow, ResearchState, SeatLabel, SemanticLabel, StatusBadge, toneForDirection } from "./ResearchPrimitives";

function fmtTime(value: string | null) {
  if (!value) return "time not recorded";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function DailyThesisPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel research-lead">
    <div className="flex items-start justify-between gap-3"><div><Eyebrow>Daily read · {data.date}</Eyebrow><h1>{data.thesis || "No daily thesis was recorded."}</h1></div><StatusBadge status={data.status} /></div>
    <ResearchState status={data.status} errors={data.errors} />
    <div className="research-lead-grid">
      <div><Eyebrow><SemanticLabel kind="change">What changed</SemanticLabel></Eyebrow>{data.what_changed.length ? <ul>{data.what_changed.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="text-dim">No supported change from the prior useful read.</p>}</div>
      <div><Eyebrow tone="warn"><SemanticLabel kind="tension">Tension</SemanticLabel></Eyebrow><p>{data.tension || "No material disagreement was recorded."}</p><Eyebrow><SemanticLabel kind="now">Why now</SemanticLabel></Eyebrow><p>{data.why_now || "No time-sensitive consequence was recorded."}</p></div>
    </div>
    {data.dry_annotation && <blockquote>{data.dry_annotation}</blockquote>}
    <div className="research-asof">{data.as_of ? `As of ${fmtTime(data.as_of)}` : "No stored research timestamp."}{data.prior_as_of ? ` · prior read ${fmtTime(data.prior_as_of)}` : ""}</div>
  </section>;
}

export function SignalStackPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Signal stack</Eyebrow><h2>Agreement, with the friction left in</h2></div></div>
    {data.signal_stack.length ? <div className="signal-stack">{data.signal_stack.map((item, i) => <div className={`signal-row ${toneForDirection(item.direction)}`} key={`${item.seat}-${i}`}>
      <strong><SeatLabel seat={item.seat} /></strong><DirectionBadge direction={item.direction} /><span className="signal-copy">{item.signal || "No directional read recorded."}</span><span className="signal-relation">{item.relationship}</span><time>{fmtTime(item.timestamp)}</time>
    </div>)}</div> : <p className="text-dim">No comparable agent signals were persisted.</p>}
  </section>;
}

function MiniMarketContext({ item }: { item: ResearchMarketContext }) {
  const position = ((item.entry - item.stop) / (item.target - item.stop)) * 100;
  return <div className="research-mini-chart" aria-label={`${item.symbol} stop ${item.stop}, entry ${item.entry}, target ${item.target}`}>
    <div className="research-mini-chart-head"><strong>{item.symbol}</strong><span>setup context</span></div>
    <div className="research-mini-track"><span className="research-mini-risk" style={{ width: `${position}%` }} /><i style={{ left: `${position}%` }} /></div>
    <div className="research-mini-labels"><span>stop {item.stop}</span><b>entry {item.entry}</b><span>target {item.target}</span></div>
  </div>;
}

function AgentCard({ agent }: { agent: ResearchAgentBrief }) {
  return <article className="agent-brief">
    <header><div><Eyebrow tone="agent"><SeatLabel seat={agent.seat} /></Eyebrow><h3>{agent.headline || "No headline recorded"}</h3></div><div className="flex gap-1.5"><DirectionBadge direction={agent.direction} /><StatusBadge status={agent.status} /></div></header>
    {agent.read && <p className="agent-read">{agent.read}</p>}
    <EvidenceStrip items={agent.evidence} />
    {agent.market_context.map((item) => <MiniMarketContext item={item} key={item.symbol} />)}
    {(agent.changed || agent.tension || agent.why_now) && <div className="agent-consequence">
      {agent.changed && <div><Eyebrow><SemanticLabel kind="change">Changed</SemanticLabel></Eyebrow><p>{agent.changed}</p></div>}
      {agent.tension && <div><Eyebrow tone="warn"><SemanticLabel kind="tension">Tension</SemanticLabel></Eyebrow><p>{agent.tension}</p></div>}
      {agent.why_now && <div><Eyebrow><SemanticLabel kind="now">Why now</SemanticLabel></Eyebrow><p>{agent.why_now}</p></div>}
    </div>}
    {agent.error && <p className="text-neg">{agent.error}</p>}<time>{fmtTime(agent.timestamp)}</time>
  </article>;
}

export function AgentBriefsPanel({ data }: { data: ResearchDeskData }) {
  const specialistSeats = new Set(["technical", "news", "macro", "earnings", "smart_money"]);
  const specialists = data.agents.filter((agent) => specialistSeats.has(agent.seat));
  const available = specialists.filter((agent) => agent.status !== "unavailable");
  const useful = available.filter((agent) => agent.status !== "error");
  const failed = available.filter((agent) => agent.status === "error");
  const missing = specialists.filter((agent) => agent.status === "unavailable");
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Specialist findings</Eyebrow><h2>Edited to what matters</h2></div><span>{useful.length} useful read{useful.length === 1 ? "" : "s"}{failed.length ? ` · ${failed.length} gap${failed.length === 1 ? "" : "s"}` : ""}</span></div>
    {available.length ? <div className="agent-brief-grid">{available.map((agent) => <AgentCard agent={agent} key={agent.seat} />)}</div> : <p className="text-dim">No specialist finding was stored for this day.</p>}
    {missing.length > 0 && <div className="research-coverage"><Eyebrow>Not recorded</Eyebrow>{missing.map((agent) => <span key={agent.seat}><SeatLabel seat={agent.seat} /></span>)}</div>}
  </section>;
}

export function DecisionDeltaPanel({ data }: { data: ResearchDeskData }) {
  const labels: Record<string, string> = { read: "Read", portfolio_manager: "PM", ai_risk: "Risk", deterministic_gate: "Gate", execution: "Actual" };
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Read / PM / Risk</Eyebrow><h2>Proposal → consequence</h2></div>{data.decision_run_id && <span title={data.decision_run_id}>Latest stored chain</span>}</div>
    {data.decision_chain.length ? <div className="decision-delta">{data.decision_chain.map((step, i) => <div className="decision-step" key={`${step.stage}-${i}`}>
      <div className="decision-index">{i + 1}</div><div><Eyebrow>{labels[step.stage] || step.stage}</Eyebrow><div className="flex items-center gap-2"><h3>{step.summary || "No summary recorded"}</h3><span className="research-badge border-border text-dim">{step.status}</span></div>{step.detail && <p>{step.detail}</p>}<time>{fmtTime(step.timestamp)}</time></div>
    </div>)}</div> : <p className="text-dim">No PM/Risk/gate/execution chain was stored. Nothing is inferred.</p>}
  </section>;
}

export function SmartMoneyPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Smart Money</Eyebrow><h2>Knowable then, useful now?</h2></div></div>
    {data.smart_money.length ? <div className="smart-money-list">{data.smart_money.map((item) => <article key={item.id} className="smart-money-card">
      <header><div><Eyebrow><SeatLabel seat="smart_money" /> · {item.stream}{item.symbol ? ` · ${item.symbol}` : ""}</Eyebrow><h3>{item.headline}</h3></div><div className="flex flex-wrap justify-end gap-1.5"><DirectionBadge direction={item.direction} /><span className="research-badge border-agent/40 text-agent">{item.classification}</span><span className="research-badge border-border text-dim">{item.freshness}</span>{item.admitted_this_run && <span className="research-badge border-pos/40 text-pos">admitted this run</span>}</div></header>
      <p>{item.summary}</p><div className="smart-money-times"><span><b>Happened</b>{fmtTime(item.event_timestamp)}</span><span><b>Knowable</b>{fmtTime(item.knowable_timestamp)}</span><span><b>Lag</b>{item.lag_days == null ? "not established" : `${item.lag_days}d`}</span></div>
      {item.materiality && <p><b>Why it survived the noise filter:</b> {item.materiality}</p>}
      {item.admitted_this_run && <div className="research-admission"><strong>Run-scoped admission</strong><span>{item.admission_detail || "Deterministic admission evidence stored."}</span><small>Still required: Technical → PM → AI Risk → deterministic gate → broker.</small></div>}
      <footer>Source: {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.source_name}</a> : item.source_name}{item.source_detail ? ` · ${item.source_detail}` : ""}</footer>
    </article>)}</div> : <p className="text-dim">No material smart-money pattern survived the filter. A lone stale filing is not promoted into a signal.</p>}
  </section>;
}

export function ReviewPanel({ data }: { data: ResearchDeskData }) {
  const review = data.reviews;
  const hasReview = Boolean(review && (review.daily_result || review.position_reviewer || review.evening_review || review.meta_reflection || review.lesson_learned || review.suggested_actions.length || review.tomorrow_watch.length));
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>After the bell</Eyebrow><h2>Review, learning, tomorrow</h2></div></div>
    {!review || !hasReview ? <p className="text-dim">No review or reflection was recorded yet.</p> : <div className="review-grid">
      <div><Eyebrow>Daily result</Eyebrow><p>{review.daily_result || "No daily P&L result recorded."}</p></div>
      <div><Eyebrow><SeatLabel seat="position_reviewer" /></Eyebrow><p>{review.position_reviewer || "No position review recorded."}</p></div>
      <div><Eyebrow><SeatLabel seat="evening_review" /></Eyebrow><p>{review.evening_review || "No evening review recorded."}</p></div>
      <div><Eyebrow><SeatLabel seat="meta_reflection" /></Eyebrow><p>{review.meta_reflection || "No meta-reflection recorded."}</p></div>
      <div><Eyebrow>Lesson learned</Eyebrow><p>{review.lesson_learned || "No lesson was recorded."}</p></div>
      <div><Eyebrow>Suggested actions</Eyebrow>{review.suggested_actions.length ? <ul>{review.suggested_actions.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>No actions recorded.</p>}</div>
      <div><Eyebrow>Tomorrow watch</Eyebrow>{review.tomorrow_watch.length ? <ul>{review.tomorrow_watch.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>No watch items recorded.</p>}</div>
    </div>}
  </section>;
}
