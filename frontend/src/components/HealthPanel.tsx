import { HealthResponse } from "../api/client";
import { fmtTime } from "../lib/format";
import { Panel, StateMessage } from "./ui/Panel";

export function HealthPanel({ health, error }: { health: HealthResponse | null; error: string | null }) {
  const circuit = health?.llm_circuit;
  const quotaHoldCount = circuit?.active_quota_holds?.length ?? 0;
  const circuitDegraded = Boolean(
    (circuit && !circuit.available) || circuit?.requires_operator_reset || circuit?.suspended || quotaHoldCount,
  );
  const channel = health?.alert_channel;
  const channelStatus = channel?.status ?? "unknown";
  const channelDegraded = channelStatus === "broken" || channelStatus === "stale";
  const status = error
    ? "error"
    : !health
    ? "loading"
    : circuitDegraded || channelDegraded
    ? "degraded"
    : "ok";
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
      <div className="grid grid-cols-2 lg:grid-cols-6 gap-3 text-[0.8rem]">
        <Field label="Database" value={health.db_reachable ? "reachable" : "unreachable"} />
        <Field
          label="Alert channel"
          value={
            channelStatus === "ok"
              ? `verified ${channel?.last_ok_at ? fmtTime(channel.last_ok_at) : ""}`.trim()
              : channelStatus === "broken"
              ? "BROKEN"
              : channelStatus === "stale"
              ? "unverified · stale"
              : "never verified"
          }
        />
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
      {channelDegraded && (
        <div className="state-message mt-2 text-neg">
          {channelStatus === "broken"
            ? `Alert channel BROKEN at stage "${channel?.last_stage ?? "unknown"}" — ${
                channel?.last_detail || "no detail"
              }. Every alarm on this desk goes out over Telegram, so until this is fixed silence means nothing. ${
                channel?.consecutive_failures ?? 0
              } consecutive failed check(s); last working ${
                channel?.last_ok_at ? fmtTime(channel.last_ok_at) : "never"
              }.`
            : `Alert channel unverified — the last successful check is ${
                channel?.age_hours != null ? `${channel.age_hours.toFixed(1)}h` : "an unknown time"
              } old (stale past ${channel?.stale_after_hours ?? 26}h). The checks themselves have stopped; nothing here says the alarm works.`}
        </div>
      )}
      {channelStatus === "unknown" && (
        <div className="state-message mt-2 text-warn">
          Alert channel never verified — no check has been recorded{channel?.error ? ` (${channel.error})` : ""}.
          This is not the same as healthy.
        </div>
      )}
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
