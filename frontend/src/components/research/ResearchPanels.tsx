import type { ResearchDeskData, ResearchAgentBrief } from "../../api/client";
import { DirectionBadge, EvidenceStrip, Eyebrow, ResearchState, seatLabel, StatusBadge, toneForDirection } from "./ResearchPrimitives";

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
      <div><Eyebrow>What changed</Eyebrow>{data.what_changed.length ? <ul>{data.what_changed.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="text-dim">No supported change from the prior useful read.</p>}</div>
      <div><Eyebrow tone="warn">Tension</Eyebrow><p>{data.tension || "No material disagreement was recorded."}</p><Eyebrow>Why now</Eyebrow><p>{data.why_now || "No time-sensitive consequence was recorded."}</p></div>
    </div>
    {data.dry_annotation && <blockquote>{data.dry_annotation}</blockquote>}
    <div className="research-asof">As of {fmtTime(data.as_of)}{data.prior_as_of ? ` · prior read ${fmtTime(data.prior_as_of)}` : ""}</div>
  </section>;
}

export function SignalStackPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Signal stack</Eyebrow><h2>Agreement, with the friction left in</h2></div></div>
    {data.signal_stack.length ? <div className="signal-stack">{data.signal_stack.map((item, i) => <div className={`signal-row ${toneForDirection(item.direction)}`} key={`${item.seat}-${i}`}>
      <strong>{seatLabel(item.seat)}</strong><DirectionBadge direction={item.direction} /><span className="signal-copy">{item.signal || "No directional read recorded."}</span><span className="signal-relation">{item.relationship}</span><time>{fmtTime(item.timestamp)}</time>
    </div>)}</div> : <p className="text-dim">No comparable agent signals were persisted.</p>}
  </section>;
}

function AgentCard({ agent }: { agent: ResearchAgentBrief }) {
  return <article className="agent-brief">
    <header><div><Eyebrow tone="agent">{seatLabel(agent.seat)}</Eyebrow><h3>{agent.headline || "No headline recorded"}</h3></div><div className="flex gap-1.5"><DirectionBadge direction={agent.direction} /><StatusBadge status={agent.status} /></div></header>
    <p className="agent-read">{agent.read || "No substantive read was stored for this seat."}</p>
    <EvidenceStrip items={agent.evidence} />
    {(agent.changed || agent.tension || agent.why_now) && <div className="agent-consequence">
      {agent.changed && <div><Eyebrow>Changed</Eyebrow><p>{agent.changed}</p></div>}
      {agent.tension && <div><Eyebrow tone="warn">Tension</Eyebrow><p>{agent.tension}</p></div>}
      {agent.why_now && <div><Eyebrow>Why now</Eyebrow><p>{agent.why_now}</p></div>}
    </div>}
    {agent.error && <p className="text-neg">{agent.error}</p>}<time>{fmtTime(agent.timestamp)}</time>
  </article>;
}

export function AgentBriefsPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Agent findings</Eyebrow><h2>Every seat, edited to what matters</h2></div><span>{data.agents.length} reads</span></div>
    {data.agents.length ? <div className="agent-brief-grid">{data.agents.map((agent, i) => <AgentCard agent={agent} key={`${agent.seat}-${i}`} />)}</div> : <p className="text-dim">No agent findings were stored for this day.</p>}
  </section>;
}

export function DecisionDeltaPanel({ data }: { data: ResearchDeskData }) {
  const labels: Record<string, string> = { read: "Read", portfolio_manager: "PM", ai_risk: "Risk", deterministic_gate: "Gate", execution: "Actual" };
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Read / PM / Risk</Eyebrow><h2>Proposal → consequence</h2></div></div>
    {data.decision_chain.length ? <div className="decision-delta">{data.decision_chain.map((step, i) => <div className="decision-step" key={`${step.stage}-${i}`}>
      <div className="decision-index">{i + 1}</div><div><Eyebrow>{labels[step.stage] || step.stage}</Eyebrow><div className="flex items-center gap-2"><h3>{step.summary || "No summary recorded"}</h3><span className="research-badge border-border text-dim">{step.status}</span></div>{step.detail && <p>{step.detail}</p>}<time>{fmtTime(step.timestamp)}</time></div>
    </div>)}</div> : <p className="text-dim">No PM/Risk/gate/execution chain was stored. Nothing is inferred.</p>}
  </section>;
}

export function SmartMoneyPanel({ data }: { data: ResearchDeskData }) {
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>Smart Money</Eyebrow><h2>Knowable then, useful now?</h2></div></div>
    {data.smart_money.length ? <div className="smart-money-list">{data.smart_money.map((item) => <article key={item.id} className="smart-money-card">
      <header><div><Eyebrow>{item.stream}{item.symbol ? ` · ${item.symbol}` : ""}</Eyebrow><h3>{item.headline}</h3></div><div className="flex gap-1.5"><span className="research-badge border-agent/40 text-agent">{item.classification}</span><span className="research-badge border-border text-dim">{item.freshness}</span></div></header>
      <p>{item.summary}</p><div className="smart-money-times"><span><b>Happened</b>{fmtTime(item.event_timestamp)}</span><span><b>Knowable</b>{fmtTime(item.knowable_timestamp)}</span><span><b>Lag</b>{item.lag_days == null ? "not established" : `${item.lag_days}d`}</span></div>
      {item.materiality && <p><b>Why it survived the noise filter:</b> {item.materiality}</p>}
      <footer>Source: {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.source_name}</a> : item.source_name}{item.source_detail ? ` · ${item.source_detail}` : ""}</footer>
    </article>)}</div> : <p className="text-dim">No material smart-money pattern survived the filter. A lone stale filing is not promoted into a signal.</p>}
  </section>;
}

export function ReviewPanel({ data }: { data: ResearchDeskData }) {
  const review = data.reviews;
  return <section className="research-panel"><div className="research-panel-title"><div><Eyebrow>After the bell</Eyebrow><h2>Review, learning, tomorrow</h2></div></div>
    {!review ? <p className="text-dim">No review or reflection was recorded yet.</p> : <div className="review-grid">
      <div><Eyebrow>Position Reviewer</Eyebrow><p>{review.position_reviewer || "No position review recorded."}</p></div>
      <div><Eyebrow>Evening Review</Eyebrow><p>{review.evening_review || "No evening review recorded."}</p></div>
      <div><Eyebrow>Meta-Reflection</Eyebrow><p>{review.meta_reflection || "No meta-reflection recorded."}</p></div>
      <div><Eyebrow>Tomorrow watch</Eyebrow>{review.tomorrow_watch.length ? <ul>{review.tomorrow_watch.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>No watch items recorded.</p>}</div>
    </div>}
  </section>;
}
