import { useState } from "react";
import { HoldingWhyResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";

/* "Why do we hold this" for whichever symbol is currently charted.
 *
 * Owner's own proposal, approved 2026-09-18: clicking a symbol ANYWHERE
 * in the cockpit already charts it; it now also fills this tab, whether
 * or not the tab happens to be open, so the answer is already sitting
 * there when he switches to it. The fetch therefore does NOT live in this
 * component — it lives in App.tsx beside the chart symbol itself (see
 * `holdingWhy` there) and arrives here as props. A fetch inside this
 * component would only run while the tab was mounted and visible, which
 * is exactly the behaviour he asked us not to build.
 *
 * Every string rendered below is written by the BACKEND
 * (src/api/holding_why.py, merged in PR #472). This component chooses
 * what to show and in what order; it never re-words, re-derives or
 * fills in a value. That division is deliberate: the wording rules — no
 * jargon, dates written out, "$720 million" not 720543738.73, a missing
 * field SAYING it is missing rather than reading as zero — are unit
 * tested there (tests/test_holding_why.py) and would be untestable if
 * half of them lived in JSX.
 *
 * The one rule this file owns: the machine layer. Accession numbers, run
 * identifiers, broker-eligibility flags and internal enum values are
 * collapsed and hidden by default, and even when expanded they are
 * rendered as labelled rows — never as a raw payload. The owner's
 * original complaint about this view was a verbatim insider-filing
 * payload rendered on screen as, in his words, "gobbledygook".
 */

/** Section heading + body. `body` is already-plain backend prose. */
function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-border pt-2.5">
      <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-dim">{label}</div>
      <div className="mt-1 font-sans text-[0.84rem] leading-relaxed text-ink">{children}</div>
    </div>
  );
}

/** `first_transaction_date` -> `First transaction date`. Turns the stored
 * key into something readable WITHOUT claiming to explain it — these rows
 * only ever appear inside the collapsed technical section, where the
 * owner has deliberately asked to see the machine's own field names. */
function humanizeKey(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Renders one stored value as text a person can read. Arrays become a
 * comma-separated list, nested objects become their own indented rows.
 * Nothing is ever JSON.stringify'd onto the screen: a raw payload dump is
 * the specific thing this whole view exists to replace. */
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
            <span className="shrink-0 text-dim">{humanizeKey(key)}</span>
            <RawValue value={nested} />
          </div>
        ))}
      </div>
    );
  }
  if (typeof value === "boolean") return <span>{value ? "yes" : "no"}</span>;
  return <span className="break-words">{String(value)}</span>;
}

function TechnicalDetail({ raw }: { raw: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const groups = Object.entries(raw).filter(
    ([, value]) => value && typeof value === "object" && Object.keys(value as object).length > 0,
  );
  if (!groups.length) return null;
  return (
    <div className="border-t border-border pt-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-[0.68rem] font-semibold uppercase tracking-wide text-dim hover:text-accent"
      >
        {open ? "Hide" : "Show"} the technical detail behind this
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2.5 font-mono text-[0.72rem]">
          <p className="font-sans text-[0.78rem] leading-relaxed text-dim">
            Filing reference numbers and the desk's own internal record identifiers. Nothing here
            changes the reasoning above — it is here so a number on screen can always be traced back
            to the record it came from.
          </p>
          {groups.map(([group, value]) => (
            <div key={group}>
              <div className="font-sans text-[0.72rem] font-semibold uppercase tracking-wide text-dim">
                {humanizeKey(group)}
              </div>
              <div className="mt-1 pl-2">
                <RawValue value={value} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function WhyPanel({
  symbol,
  why,
  error,
  loading,
  fit,
}: {
  /** The symbol currently charted — what this tab is answering about. */
  symbol: string | null;
  why: HoldingWhyResponse | null;
  /** Already plain-language: App.tsx turns a 404 into the "no recorded
   * entry" sentence rather than surfacing a status code. */
  error: string | null;
  loading: boolean;
  fit?: boolean;
}) {
  const readable = why?.readable;
  const subject = why?.company_name ? `${why.company_name} (${why.symbol})` : why?.symbol || symbol;

  return (
    <Panel
      fit={fit}
      title="Why"
      subtitle={
        symbol
          ? `Why the desk holds ${subject}. Select any symbol in the cockpit to switch this over.`
          : "Select any symbol in the cockpit — a holding, a position, a trade or an order — and its reasoning appears here."
      }
      status={error ? "degraded" : loading ? "loading" : why ? "ok" : undefined}
    >
      {!symbol && <StateMessage hero glyph="○" text="No symbol selected yet." />}
      {symbol && loading && !why && <StateMessage text={`Reading the record for ${symbol}…`} />}
      {symbol && error && !why && <StateMessage text={error} />}
      {symbol && !loading && !error && !why && (
        <StateMessage text={`Nothing is recorded for ${symbol}.`} />
      )}

      {why && readable && (
        <div className="flex flex-col gap-2.5">
          {/* The one sentence, with the real numbers in it. Deliberately
              the largest text in the panel: it is the answer, and
              everything below it is the supporting detail. */}
          <p className="font-sans text-[0.98rem] font-semibold leading-snug text-ink">{why.lede}</p>

          {/* Optional-chained deliberately: the type says this is always
              present, but a browser holding a newer bundle than the API
              (or the reverse) must degrade to a missing line, not a blank
              panel. */}
          {readable.purchase?.plain && (
            <Detail label="What we bought, and when">{readable.purchase.plain}</Detail>
          )}

          {readable.why && <Detail label="Why we opened it">{readable.why}</Detail>}

          {readable.primary_driver && (
            <Detail label={`What mainly drove it — ${readable.primary_driver.toLowerCase()}`}>
              {readable.primary_driver_detail || readable.raised_by}
            </Detail>
          )}

          {readable.raised_by && readable.primary_driver_detail && (
            <Detail label="Which seat raised it">{readable.raised_by}</Detail>
          )}

          {readable.fundamental_reason && (
            <Detail label="The fundamental reason">{readable.fundamental_reason}</Detail>
          )}

          {readable.supporting.length > 0 && (
            <Detail label="What else supported it">
              <ul className="flex flex-col gap-1.5">
                {readable.supporting.map((item) => (
                  <li key={item.seat}>
                    <span className="font-semibold">{item.seat}: </span>
                    {item.reason}
                  </li>
                ))}
              </ul>
            </Detail>
          )}

          <Detail label="How long we meant to hold it">
            <span>{readable.horizon.plain}</span>
            <span className="mt-1 block text-dim">{readable.horizon.note}</span>
          </Detail>

          <Detail label="The profit target we recorded">
            <span>{readable.take_profit.plain}</span>
            <span className="mt-1 block text-dim">{readable.take_profit.note}</span>
          </Detail>

          <Detail label="What would prove us wrong">{readable.invalidation}</Detail>

          {readable.since_entry.length > 0 && (
            <Detail label="What has happened since we bought it">
              <ul className="flex flex-col gap-1.5">
                {readable.since_entry.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </Detail>
          )}

          {why.not_recorded.length > 0 && (
            <Detail label="What the desk did not record">
              {/* Named in words rather than left as blank fields above —
                  a silent gap reads as "there was nothing to say", which
                  is a different claim from "we never wrote it down". */}
              The desk has no record of {why.not_recorded.join(", ")}.
            </Detail>
          )}

          <TechnicalDetail raw={why.raw_evidence} />
        </div>
      )}
    </Panel>
  );
}
