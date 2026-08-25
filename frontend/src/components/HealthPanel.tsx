import { HealthResponse } from "../api/client";
import { fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";

export function HealthPanel({ health, error }: { health: HealthResponse | null; error: string | null }) {
  const circuit = health?.llm_circuit;
  const quotaHoldCount = circuit?.active_quota_holds?.length ?? 0;
  const circuitDegraded = Boolean(
    (circuit && !circuit.available) || circuit?.requires_operator_reset || circuit?.suspended || quotaHoldCount,
  );
  const status = error ? "error" : !health ? "loading" : circuitDegraded ? "degraded" : "ok";
  if (error) {
    return (
      <Panel title="System health" status="error">
        <StateMessage text={`Could not load health: ${error}`} error />
      </Panel>
    );
  }
  if (!health) {
    return (
      <Panel title="System health" status="loading">
        <StateMessage text="Loading…" />
      </Panel>
    );
  }
  const runs = Object.entries(health.last_run_files)
    .map(([mode, ts]) => `${mode}: ${ts ? fmtTime(ts) : "—"}`)
    .join("  ·  ");
  const circuitValue = !circuit?.available
    ? "unavailable"
    : circuit.requires_operator_reset
    ? "hard stop · operator reset"
    : circuit.suspended
    ? "daily quota hold · auto-rearm"
    : quotaHoldCount
    ? `${quotaHoldCount} scoped hold${quotaHoldCount === 1 ? "" : "s"}`
    : circuit.recent_recovery
    ? "rearmed · checks passed"
    : "ready";
  return (
    <Panel title="System health" status={status}>
      {/* lg:, not sm: — this panel is half-width in the main 2-column
          layout; see LiquidityPanel.tsx for the full explanation of why a
          viewport-width breakpoint alone is wrong here. */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 text-[0.8rem]">
        <Field label="Database" value={health.db_reachable ? "reachable" : "unreachable"} />
        <Field
          label="Broker"
          value={health.broker_reachable === null ? "not configured" : health.broker_reachable ? "reachable" : "unreachable"}
        />
        <Field label="Sessions logged today" value={health.sessions_logged_today.join(", ") || "none"} />
        <Field
          label="Session lock"
          value={health.session_lock_active === null ? "unknown" : health.session_lock_active ? "active" : "idle"}
        />
        <Field label="Paid analysis" value={circuitValue} />
      </div>
      {circuit?.trigger && circuitDegraded && (
        <div className={`state-message mt-2 ${circuit.requires_operator_reset ? "text-neg" : "text-warn"}`}>
          {circuit.trigger}
        </div>
      )}
      {circuit?.recent_recovery && !circuitDegraded && (
        <div className="state-message mt-2 text-pos">
          Last recovery — {circuit.recent_recovery.release_reason}
        </div>
      )}
      {runs && <div className="state-message mt-2">Last run files — {runs}</div>}
    </Panel>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[0.68rem] text-dim uppercase tracking-wide">{label}</div>
      <div>{value}</div>
    </div>
  );
}
