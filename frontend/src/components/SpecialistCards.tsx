import { useState } from "react";
import { CandidateDetailResponse, ConsensusSummary } from "../api/client";
import { Pill } from "./ui/Pill";
import { StateMessage } from "./ui/Panel";
import { Meter } from "./ui/Meter";

/* First-class per-specialist "agent cards" — Orallexa PerspectivePanelCard-
 * inspired (docs/DONOR_COMPONENTS.md, structural/visual reference only,
 * not vendored code): one card per analyst that actually produced
 * evidence for this candidate, with identity, directional read,
 * conviction, a reasoning snippet, and a flag for whether that read
 * agrees or disagrees with the overall consensus — replacing the old flat
 * bulleted ConsensusBlock. Never fabricates a card for a specialist with
 * no evidence, and never invents an agreement value beyond what
 * detail.consensus actually encodes. */

type Direction = "bullish" | "bearish" | "neutral";

// Mirrors src/api/routes_evidence.py::_TECH_DIRECTION exactly — tech has no
// direct sentiment field, only a rating, so this mapping is re-derived here
// rather than trusting a coincidental ordering match against consensus.signals.
const TECH_DIRECTION: Record<string, Direction> = {
  strong_buy: "bullish",
  buy: "bullish",
  neutral: "neutral",
  sell: "bearish",
  strong_sell: "bearish",
};

interface SpecialistEntry {
  key: string;
  role: string;
  subtitle?: string;
  direction: Direction;
  conviction: string | null;
  reasoning: string;
}

function buildEntries(detail: CandidateDetailResponse): SpecialistEntry[] {
  const entries: SpecialistEntry[] = [];

  if (detail.tech) {
    entries.push({
      key: "tech",
      role: "Technical Analyst",
      direction: TECH_DIRECTION[detail.tech.rating] || "neutral",
      conviction: detail.tech.conviction,
      reasoning: detail.tech.reasoning,
    });
  }

  if (detail.earnings) {
    const impl = detail.earnings.investment_implications;
    entries.push({
      key: "earnings",
      role: "Earnings Analyst",
      direction: impl.sentiment,
      conviction: impl.conviction,
      reasoning: impl.key_thesis,
    });
  }

  detail.news_symbol.forEach((n, i) => {
    entries.push({
      key: `news-${i}`,
      role: "News Analyst",
      subtitle: n.headline,
      direction: n.sentiment,
      conviction: n.conviction,
      reasoning: n.impact_summary,
    });
  });

  return entries;
}

interface Alignment {
  label: string;
  tone: "pos" | "warn" | "neg";
}

// Derives an honest per-specialist alignment read purely from directions
// already present in `entries`/`consensus` — never asserts an "aligned" or
// "diverges" claim the backend's own agreement computation doesn't support.
function computeAlignments(entries: SpecialistEntry[], consensus: ConsensusSummary): (Alignment | null)[] {
  // "insufficient_data" / "no_directional_signal" carry no defined majority
  // to agree or diverge from — showing a badge there would invent a claim.
  if (consensus.agreement !== "aligned" && consensus.agreement !== "mixed") {
    return entries.map(() => null);
  }

  const directionCounts: Record<string, number> = {};
  for (const e of entries) {
    if (e.direction !== "neutral") directionCounts[e.direction] = (directionCounts[e.direction] || 0) + 1;
  }
  const totalDirectional = Object.values(directionCounts).reduce((a, b) => a + b, 0);

  return entries.map((e) => {
    if (e.direction === "neutral") return null; // not part of the directional consensus either way
    if (consensus.agreement === "aligned") {
      return { label: "Aligned with consensus", tone: "pos" };
    }
    // mixed: >=2 distinct non-neutral directions exist among the signals.
    const sameCount = directionCounts[e.direction] || 0;
    if (sameCount <= 1) {
      return { label: "Diverges — sole view", tone: "neg" };
    }
    if (sameCount * 2 > totalDirectional) {
      return { label: `Majority view (${sameCount}/${totalDirectional})`, tone: "warn" };
    }
    return { label: `Diverges from majority (${sameCount}/${totalDirectional})`, tone: "warn" };
  });
}

const TONE_BORDER: Record<Alignment["tone"], string> = {
  pos: "border-l-4 border-l-pos",
  warn: "border-l-4 border-l-warn",
  neg: "border-l-4 border-l-neg",
};

const TONE_TEXT: Record<Alignment["tone"], string> = {
  pos: "text-pos",
  warn: "text-warn",
  neg: "text-neg",
};

// Visual agent identity — a colored initial badge per specialist role, so
// cards read at a glance in a dense grid rather than relying on reading
// the role label text every time. Purely cosmetic; carries no meaning
// beyond distinguishing role type.
const ROLE_BADGE: Record<string, string> = {
  "Technical Analyst": "bg-accent/15 text-accent",
  "Earnings Analyst": "bg-hedge/15 text-hedge",
  "News Analyst": "bg-warn/15 text-warn",
};

// Rough, labeled-as-such visual mapping from the LLM's own discrete
// high/medium/low conviction label to a meter fill — never a fabricated
// precise number, just a graphical read of the same three-value label
// already shown as text beside it.
const CONVICTION_PCT: Record<string, number> = { high: 92, medium: 58, low: 28 };
const DIRECTION_TONE: Record<Direction, "pos" | "neg" | "dim"> = { bullish: "pos", bearish: "neg", neutral: "dim" };

// Long specialist reasoning defaults to a clamped preview with an explicit
// expand toggle — progressive disclosure inside an already-dense card grid,
// rather than letting one verbose specialist stretch every card in the row.
function ReasoningBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > 170;
  return (
    <div>
      <p className={`text-[0.8rem] mt-1.5 leading-snug ${!expanded && long ? "line-clamp-3" : ""}`}>{text}</p>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="text-accent text-[0.7rem] font-semibold mt-1"
        >
          {expanded ? "Show less" : "Show full reasoning →"}
        </button>
      )}
    </div>
  );
}

function RoleBadge({ role }: { role: string }) {
  const cls = ROLE_BADGE[role] || "bg-dim/15 text-dim";
  return (
    <span className={`flex-shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-[0.75rem] font-extrabold ${cls}`}>
      {role.charAt(0)}
    </span>
  );
}

function ConsensusHeader({ consensus }: { consensus: ConsensusSummary }) {
  return (
    <div className="flex items-center gap-2 flex-wrap mb-2.5">
      <span className="text-dim text-[0.78rem]">Consensus</span>
      <Pill text={consensus.agreement} />
      <span className="text-dim text-[0.72rem]">
        {consensus.signals.length} independent signal{consensus.signals.length === 1 ? "" : "s"}
      </span>
    </div>
  );
}

export function SpecialistCards({ detail }: { detail: CandidateDetailResponse }) {
  const entries = buildEntries(detail);
  const alignments = computeAlignments(entries, detail.consensus);

  return (
    <div>
      <ConsensusHeader consensus={detail.consensus} />
      {entries.length ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          {entries.map((e, i) => {
            const align = alignments[i];
            return (
              <div key={e.key} className={`card ${align ? TONE_BORDER[align.tone] : ""}`}>
                <div className="flex items-start justify-between gap-2 mb-1.5 flex-wrap">
                  <div className="flex items-start gap-2 min-w-0">
                    <RoleBadge role={e.role} />
                    <div className="min-w-0">
                      <div className="font-bold text-[0.85rem]">{e.role}</div>
                      {e.subtitle && <div className="text-[0.72rem] text-dim mt-0.5">{e.subtitle}</div>}
                    </div>
                  </div>
                  <Pill text={e.direction} />
                </div>
                <div className="kv-row">
                  <span className="text-dim">Conviction</span>
                  <span>{e.conviction ? e.conviction.toString().toUpperCase() : "—"}</span>
                </div>
                {e.conviction && CONVICTION_PCT[e.conviction] !== undefined && (
                  <Meter value={CONVICTION_PCT[e.conviction]} tone={DIRECTION_TONE[e.direction]} />
                )}
                <ReasoningBlock text={e.reasoning} />
                {align && <div className={`text-[0.7rem] font-semibold mt-1.5 ${TONE_TEXT[align.tone]}`}>{align.label}</div>}
              </div>
            );
          })}
        </div>
      ) : (
        <StateMessage text="No specialist evidence recorded for this candidate this run." />
      )}
    </div>
  );
}

// Exported for reuse (e.g. a future compact variant) without re-deriving
// the mapping elsewhere.
export { TECH_DIRECTION };
