import { CandidateDetailResponse, ConsensusSummary } from "../api/client";
import { CardText } from "./ui/Evidence";
import { Pill } from "./ui/Pill";
import { StateMessage } from "./ui/Panel";

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
                  <div className="min-w-0">
                    <div className="font-bold text-[0.85rem]">{e.role}</div>
                    {e.subtitle && <div className="text-[0.72rem] text-dim mt-0.5">{e.subtitle}</div>}
                  </div>
                  <Pill text={e.direction} />
                </div>
                <div className="kv-row">
                  <span className="text-dim">Conviction</span>
                  <span>{e.conviction ? e.conviction.toString().toUpperCase() : "—"}</span>
                </div>
                <CardText text={e.reasoning} />
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
