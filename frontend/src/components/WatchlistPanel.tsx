import { RunFunnelResponse } from "../api/client";
import { Panel, StateMessage } from "./ui/Panel";
import { Pill } from "./ui/Pill";
import { useModalActions } from "../context/ModalContext";

export function WatchlistPanel({
  funnel,
  loading,
  error,
  onSelectSymbol,
}: {
  funnel: RunFunnelResponse | null;
  loading: boolean;
  error: string | null;
  onSelectSymbol: (symbol: string) => void;
}) {
  const { openCandidateDetail } = useModalActions();
  const status = error ? "error" : loading ? "loading" : "ok";
  return (
    <Panel title="Candidates this run" status={status}>
      {error && <StateMessage text={`Could not load candidates: ${error}`} error />}
      {!error && funnel && funnel.candidates.length === 0 && (
        <StateMessage text="No candidates considered in the latest run." />
      )}
      {!error && funnel && funnel.candidates.length > 0 && (
        <div>
          {funnel.candidates.map((c) => (
            <button
              key={c.symbol}
              type="button"
              className="candidate-chip"
              onClick={() => {
                onSelectSymbol(c.symbol);
                openCandidateDetail(funnel.run_id, c.symbol);
              }}
            >
              <span>{c.symbol}</span>
              <Pill text={c.direction} />
              {c.is_bearish_hedge && <Pill text="bearish_hedge" />}
              {c.executed ? (
                <Pill text="executed" />
              ) : c.reached_proposed_order ? (
                <Pill text={c.risk_modified ? "modified" : "proposed"} />
              ) : null}
            </button>
          ))}
        </div>
      )}
    </Panel>
  );
}
