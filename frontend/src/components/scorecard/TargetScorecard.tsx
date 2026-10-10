/* Profit target scorecard. RECORD ONLY: the take-profit target is not a sell
 * rule (owner ruling 2026-10-09). Plain counts, no judgement. */

import { useState } from "react";
import { api, type TargetScorecardResponse } from "../../api/client";
import { usePoll } from "../../lib/usePoll";

export function TargetScorecard() {
  const [data, setData] = useState<TargetScorecardResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  usePoll(() => {
    api
      .targetScorecard()
      .then((d) => {
        setData(d);
        setFetchError(null);
      })
      .catch((err: Error) => setFetchError(err.message));
  }, []);

  const error = fetchError ?? data?.read_error ?? null;
  return (
    <section className="rounded-xl border border-border bg-panel p-4" data-testid="target-scorecard">
      <h2 className="m-0 mb-1 text-[length:var(--fs-subhead)] font-semibold text-ink">
        Profit target: how often price reached it (record only, not a sell rule)
      </h2>
      <p className="m-0 mb-3 text-[length:var(--fs-meta)] text-dim">
        &quot;Reached&quot; is a lower bound: price is sampled, so a brief spike can be missed. Positions
        counted &quot;not reached&quot; were only not seen to reach it.
      </p>
      {error && <p className="m-0 text-warn">Could not be read: {error}</p>}
      {data && !error && (
        <>
          <ul className="m-0 list-none p-0 text-ink">
            <li>Closed, reached target: {data.closed_reached}</li>
            <li>Closed, not seen to reach target: {data.closed_not_reached}</li>
            <li>Still open: {data.still_open}</li>
          </ul>
          <h3 className="mb-1 mt-3 text-[length:var(--fs-meta)] font-semibold text-dim">Not counted</h3>
          <ul className="m-0 list-none p-0 text-[length:var(--fs-meta)] text-dim">
            <li>Opened before the clean record began: {data.excluded.opened_before_clean_record}</li>
            <li>No entry target recorded: {data.excluded.no_entry_target}</li>
            <li>No best-move figure: {data.excluded.no_best_move_figure}</li>
            <li>Trade rows with no position: {data.excluded.trade_rows_without_position}</li>
          </ul>
        </>
      )}
    </section>
  );
}
