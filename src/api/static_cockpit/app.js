"use strict";

/* QAMC Mission Control — Stage 6 cockpit.
 *
 * Vanilla JS, no build step, no framework — same posture as the /ui
 * Stage 3-5 dashboard this sits alongside (docs/WORK.md: no framework
 * migration without architecture approval). Every panel renders only what
 * the read-only API actually returns; a fetch failure or an `error` field
 * is shown as an honest degraded/error state, never backfilled with
 * placeholder numbers. See docs/architecture/MISSION_CONTROL_API.md for
 * the endpoint contract, including the Stage 6 liquidity/direction/funnel
 * additions this view leads with.
 */

const REFRESH_MS = 20000;

/* ---------------------------------------------------------------------- */
/* Utilities                                                              */
/* ---------------------------------------------------------------------- */

async function fetchJSON(path) {
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new Error(`network error: ${err.message}`);
  }
  if (!res.ok) {
    if (res.status === 404) {
      const e = new Error(`HTTP 404`);
      e.status = 404;
      throw e;
    }
    throw new Error(`HTTP ${res.status}`);
  }
  return res.json();
}

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.title) node.title = opts.title;
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtMoneyCompact(v) {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtNum(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return v > 0 ? "pnl-pos" : v < 0 ? "pnl-neg" : "";
}

function pill(text) {
  const cls = (text || "").toString().toLowerCase().replace(/[^a-z_]/g, "");
  return el("span", { className: `pill pill-${cls}`, text: (text || "—").toString().toUpperCase() });
}

function setPanelState(panelId, state, label) {
  const panel = document.getElementById(panelId);
  const status = panel.querySelector("[data-status]");
  status.dataset.state = state;
  status.textContent = label;
}

function showMessage(bodyEl, text, isError = false) {
  bodyEl.replaceChildren(el("div", { className: `state-message${isError ? " error" : ""}`, text }));
}

function table(headers, rows) {
  const thead = el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { text: h })))]);
  const tbody = el("tbody", {}, rows);
  return el("table", {}, [thead, tbody]);
}

function dash(v) {
  return v === null || v === undefined || v === "" ? "—" : v;
}

function kv(label, value) {
  return el("div", { className: "kv-row" }, [
    el("span", { className: "kv-label", text: label }),
    el("span", { className: "kv-value", text: dash(value) }),
  ]);
}

function chainList(chain, labels) {
  if (!chain) return null;
  const items = Object.entries(labels)
    .filter(([key]) => chain[key])
    .map(([key, label]) =>
      el("li", {}, [
        el("span", { className: "chain-label", text: label }),
        el("span", { text: chain[key] }),
      ])
    );
  if (!items.length) return null;
  return el("ul", { className: "chain-list" }, items);
}

function card(titleText, children, opts = {}) {
  const head = el("div", { className: "card-head" }, [
    el("span", { className: "card-title", text: titleText }),
    ...(opts.broader ? [el("span", { className: "badge-broader", text: "Market-wide" })] : []),
  ]);
  const body = children.filter((c) => c !== null && c !== undefined);
  return el("div", { className: `card${opts.broader ? " card-broader" : ""}` }, [head, ...body]);
}

function evidenceSection(titleText, children, emptyText = "Not available for this candidate/run.") {
  const body = children.filter((c) => c !== null && c !== undefined);
  if (!body.length) {
    return el("div", { className: "evidence-section" }, [
      el("div", { className: "evidence-section-title", text: titleText }),
      el("div", { className: "state-message", text: emptyText }),
    ]);
  }
  return el("div", { className: "evidence-section" }, [
    el("div", { className: "evidence-section-title", text: titleText }),
    ...body,
  ]);
}

function tstat(label, valueText, extraClass = "") {
  return el("div", { className: "tstat" }, [
    el("div", { className: "tstat-label", text: label }),
    el("div", { className: `tstat-value ${extraClass}`.trim(), text: valueText }),
  ]);
}

/* ---------------------------------------------------------------------- */
/* Topbar                                                                 */
/* ---------------------------------------------------------------------- */

function updateModeBadge(paper) {
  const badge = document.getElementById("mode-badge");
  if (paper === true) {
    badge.className = "badge badge-paper";
    badge.textContent = "Paper";
  } else if (paper === false) {
    badge.className = "badge badge-live";
    badge.textContent = "LIVE — REAL MONEY";
  } else {
    badge.className = "badge badge-unknown";
    badge.textContent = "Mode unknown";
  }
}

function updateHealthIndicator(health) {
  const dot = document.getElementById("health-dot");
  const label = document.getElementById("health-label");
  if (!health) {
    dot.className = "health-dot health-unknown";
    label.textContent = "health unavailable";
    return;
  }
  if (!health.db_reachable) {
    dot.className = "health-dot health-down";
    label.textContent = "database unreachable";
  } else if (health.broker_reachable === false) {
    dot.className = "health-dot health-degraded";
    label.textContent = "broker unreachable";
  } else if (health.broker_reachable === null) {
    dot.className = "health-dot health-degraded";
    label.textContent = "broker not configured";
  } else {
    dot.className = "health-dot health-ok";
    label.textContent = "all systems reachable";
  }
}

function stampUpdated() {
  document.getElementById("last-updated").textContent =
    `updated ${new Date().toLocaleTimeString()}`;
}

/* ---------------------------------------------------------------------- */
/* Account + positions — top stats strip, liquidity panel, positions panel */
/* ---------------------------------------------------------------------- */

function renderTopStats(acct, positions) {
  const strip = document.getElementById("topbar-stats");
  if (acct.error) {
    strip.replaceChildren(el("div", { className: "state-message error", text: `Account unavailable: ${acct.error}` }));
    return;
  }
  const unrealized = (positions || [])
    .filter((p) => !p.is_cash_equivalent)
    .reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const liq = acct.liquidity || {};
  strip.replaceChildren(
    tstat("Equity", fmtMoney(acct.portfolio_value)),
    tstat("Day P&L", `${fmtMoney(acct.daily_pnl)} (${fmtPct(acct.daily_pnl_pct)})`, pnlClass(acct.daily_pnl)),
    tstat("Unrealized P&L", fmtMoney(unrealized), pnlClass(unrealized)),
    tstat("Deployable cash", fmtMoneyCompact(liq.deployable_cash)),
    tstat("Sweep parked", liq.sweep_enabled ? `${fmtMoneyCompact(liq.sweep_parked_value)} ${liq.sweep_symbol || ""}` : "disabled", "small"),
  );
}

function renderLiquidityPanel(acct, positions) {
  const body = document.querySelector("#panel-liquidity [data-body]");
  if (acct.error) {
    showMessage(body, `Account read failed: ${acct.error}`, true);
    setPanelState("panel-liquidity", "degraded", "degraded");
    return;
  }
  const liq = acct.liquidity;
  const wrap = el("div", {});

  if (liq) {
    wrap.appendChild(el("div", { className: "stat-row" }, [
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Raw cash" }),
        el("div", { className: "stat-value", text: fmtMoney(liq.raw_cash) }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: `Sweep parked${liq.sweep_symbol ? ` (${liq.sweep_symbol})` : ""}` }),
        el("div", { className: "stat-value", text: liq.sweep_enabled ? fmtMoney(liq.sweep_parked_value) : "disabled" }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Reserve floor" }),
        el("div", { className: "stat-value", text: fmtMoney(liq.reserve_usd) }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Deployable cash" }),
        el("div", { className: "stat-value", text: fmtMoney(liq.deployable_cash) }),
      ]),
    ]));
    if (liq.sweep_enabled) {
      wrap.appendChild(el("div", { className: "state-message", text:
        `${liq.sweep_symbol} is deterministic cash-equivalent sweep parking, not a Portfolio Manager investment thesis — excluded from risk exposure below.` }));
    }
  } else {
    wrap.appendChild(el("div", { className: "state-message", text: "Liquidity breakdown unavailable." }));
  }

  const longMv = (positions || []).filter((p) => p.direction === "long")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const hedgeMv = (positions || []).filter((p) => p.direction === "bearish_hedge")
    .reduce((sum, p) => sum + (p.market_value || 0), 0);
  const exposureRow = el("div", { className: "stat-row" }, [
    el("div", { className: "stat" }, [
      el("div", { className: "stat-label", text: "Long exposure" }),
      el("div", { className: "stat-value", text: fmtMoney(longMv) }),
    ]),
    el("div", { className: "stat" }, [
      el("div", { className: "stat-label", text: "Bearish-hedge exposure" }),
      el("div", { className: "stat-value", text: hedgeMv > 0 ? fmtMoney(hedgeMv) : "none" }),
    ]),
  ]);
  wrap.appendChild(exposureRow);

  body.replaceChildren(wrap);
  setPanelState("panel-liquidity", "ok", "ok");
}

function renderPositionsPanel(positions, error) {
  const body = document.querySelector("#panel-positions [data-body]");
  if (error) {
    showMessage(body, `Positions read failed: ${error}`, true);
    setPanelState("panel-positions", "degraded", "degraded");
    return;
  }
  if (!positions.length) {
    showMessage(body, "No open positions.");
    setPanelState("panel-positions", "ok", "ok");
    return;
  }
  const rows = positions.map((p) =>
    el("tr", {}, [
      el("td", { text: p.symbol }),
      el("td", {}, [pill(p.direction)]),
      el("td", { text: fmtNum(p.qty) }),
      el("td", { text: fmtMoney(p.avg_entry) }),
      el("td", { text: fmtMoney(p.current_price) }),
      el("td", { text: fmtMoney(p.market_value) }),
      el("td", { className: pnlClass(p.unrealized_pnl), text: p.is_cash_equivalent ? "—" : fmtMoney(p.unrealized_pnl) }),
      el("td", { text: p.sector || "—" }),
    ])
  );
  body.replaceChildren(
    table(["Symbol", "Direction", "Qty", "Avg Entry", "Price", "Mkt Value", "Unrealized P&L", "Sector"], rows)
  );
  setPanelState("panel-positions", "ok", "ok");
}

async function loadAccountAndPositions() {
  let acct = { error: "not loaded" };
  let positions = [];
  let positionsError = null;
  try {
    acct = await fetchJSON("/account");
    updateModeBadge(acct.paper);
  } catch (err) {
    acct = { error: err.message };
  }
  try {
    const posResp = await fetchJSON("/positions");
    positions = posResp.positions || [];
    positionsError = posResp.error;
  } catch (err) {
    positionsError = err.message;
  }
  renderTopStats(acct, positions);
  renderLiquidityPanel(acct, positions);
  renderPositionsPanel(positions, positionsError);
}

/* ---------------------------------------------------------------------- */
/* Orders / Trades                                                        */
/* ---------------------------------------------------------------------- */

async function loadOrders() {
  const body = document.querySelector("#panel-orders [data-body]");
  const status = document.getElementById("orders-status").value;
  try {
    const data = await fetchJSON(`/orders?status=${encodeURIComponent(status)}`);
    if (data.error) {
      showMessage(body, `Orders read failed: ${data.error}`, true);
      setPanelState("panel-orders", "degraded", "degraded");
      return;
    }
    if (!data.orders.length) {
      showMessage(body, `No ${status} orders.`);
      setPanelState("panel-orders", "ok", "ok");
      return;
    }
    const rows = data.orders.map((o) =>
      el("tr", {}, [
        el("td", { text: o.symbol }),
        el("td", { text: (o.side || "—").toUpperCase() }),
        el("td", { text: fmtNum(o.qty) }),
        el("td", {}, [pill(o.status)]),
        el("td", { text: fmtMoney(o.limit_price ?? o.stop_price) }),
        el("td", { text: `${fmtNum(o.filled_qty)} @ ${fmtMoney(o.filled_avg_price)}` }),
        el("td", { text: fmtTime(o.submitted_at) }),
      ])
    );
    body.replaceChildren(table(["Symbol", "Side", "Qty", "Status", "Limit/Stop", "Filled", "Submitted"], rows));
    setPanelState("panel-orders", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load orders: ${err.message}`, true);
    setPanelState("panel-orders", "error", "unreachable");
  }
}

async function loadTrades() {
  const body = document.querySelector("#panel-trades [data-body]");
  try {
    const data = await fetchJSON("/trades?limit=30");
    if (!data.trades.length) {
      showMessage(body, "No trades recorded yet.");
      setPanelState("panel-trades", "ok", "ok");
      return;
    }
    const rows = data.trades.map((t) =>
      el("tr", {}, [
        el("td", { text: fmtTime(t.timestamp) }),
        el("td", { text: t.symbol }),
        el("td", {}, [pill(t.action)]),
        el("td", { text: fmtNum(t.qty) }),
        el("td", { text: fmtMoney(t.price) }),
        el("td", {}, [pill(t.fill_status || "unfilled")]),
      ])
    );
    body.replaceChildren(table(["Time", "Symbol", "Action", "Qty", "Price", "Fill"], rows));
    setPanelState("panel-trades", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load trades: ${err.message}`, true);
    setPanelState("panel-trades", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Health                                                                 */
/* ---------------------------------------------------------------------- */

function statBlock(label, value) {
  return el("div", {}, [
    el("div", { className: "stat-label", text: label }),
    el("div", { text: value }),
  ]);
}

async function loadHealth() {
  const body = document.querySelector("#panel-health [data-body]");
  try {
    const data = await fetchJSON("/health");
    updateHealthIndicator(data);
    const runs = Object.entries(data.last_run_files || {})
      .map(([mode, ts]) => `${mode}: ${ts ? fmtTime(ts) : "—"}`)
      .join("  ·  ");
    const grid = el("div", { className: "health-grid" }, [
      statBlock("Database", data.db_reachable ? "reachable" : "unreachable"),
      statBlock("Broker", data.broker_reachable === null ? "not configured" : data.broker_reachable ? "reachable" : "unreachable"),
      statBlock("Sessions logged today", (data.sessions_logged_today || []).join(", ") || "none"),
      statBlock("Session lock", data.session_lock_active === null ? "unknown" : data.session_lock_active ? "active" : "idle"),
    ]);
    const runsLine = el("div", { className: "state-message", text: runs ? `Last run files — ${runs}` : "" });
    body.replaceChildren(grid, runsLine);
    setPanelState("panel-health", "ok", "ok");
    stampUpdated();
  } catch (err) {
    showMessage(body, `Could not load health: ${err.message}`, true);
    setPanelState("panel-health", "error", "unreachable");
    updateHealthIndicator(null);
  }
}

/* ---------------------------------------------------------------------- */
/* Modal shell                                                            */
/* ---------------------------------------------------------------------- */

const modalOverlay = document.getElementById("modal-overlay");
const modalBody = document.getElementById("modal-body");
const modalBreadcrumb = document.getElementById("modal-breadcrumb");
const modalCloseBtn = document.getElementById("modal-close");

function crumbLink(text, onClick) {
  const btn = el("button", { className: "crumb-link", text });
  btn.type = "button";
  btn.addEventListener("click", onClick);
  return btn;
}

function openModal(breadcrumbNodes, bodyNode) {
  modalBreadcrumb.replaceChildren(...breadcrumbNodes);
  modalBody.replaceChildren(bodyNode);
  modalOverlay.hidden = false;
  document.body.classList.add("modal-open");
}

function closeModal() {
  modalOverlay.hidden = true;
  document.body.classList.remove("modal-open");
  modalBody.replaceChildren();
  modalBreadcrumb.replaceChildren();
}

modalCloseBtn.addEventListener("click", closeModal);
modalOverlay.addEventListener("click", (e) => { if (e.target === modalOverlay) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modalOverlay.hidden) closeModal(); });

/* ---------------------------------------------------------------------- */
/* Decision funnel + watchlist — the cockpit's headline panels.           */
/*                                                                        */
/* Both read from one GET /runs/{run_id}/funnel call per refresh so the   */
/* two panels always agree, instead of drifting from independent fetches.*/
/* ---------------------------------------------------------------------- */

const FUNNEL_STATE_LABELS = {
  executed: "EXECUTED",
  proposed_not_executed: "PROPOSED — NOT EXECUTED",
  hard_risk_block: "DETERMINISTIC GATE BLOCKED",
  no_proposal: "NO TRADE — PM STAYED NEUTRAL",
  no_candidates: "NO CANDIDATES CONSIDERED",
};

function funnelSteps(funnel) {
  const steps = [
    ["Considered", funnel.candidates_considered],
    ["PM target", funnel.reached_pm_count],
    ["Proposed", funnel.proposed_order_count],
    ["Executed", funnel.executed_count],
  ];
  const out = [];
  steps.forEach(([label, count], i) => {
    if (i > 0) out.push(el("span", { className: "funnel-arrow", text: "→" }));
    out.push(el("div", { className: "funnel-step" }, [
      el("div", { className: "funnel-step-count", text: fmtNum(count, 0) }),
      el("div", { className: "funnel-step-label", text: label }),
    ]));
  });
  return out;
}

function renderFunnelPanel(funnel) {
  const body = document.querySelector("#panel-funnel [data-body]");
  const wrap = el("div", {});

  const badge = el("span", {
    className: `funnel-state-badge state-${funnel.decision_state}`,
    text: FUNNEL_STATE_LABELS[funnel.decision_state] || funnel.decision_state,
  });
  const runLabel = el("span", { className: "dim", text:
    `run ${funnel.run_id}${funnel.session_prefix ? ` (${funnel.session_prefix})` : ""} · ${fmtTime(funnel.timestamp)}` });
  wrap.appendChild(el("div", { className: "funnel-head" }, [badge, runLabel]));
  wrap.appendChild(el("div", { className: "funnel-steps" }, funnelSteps(funnel)));

  if (funnel.bearish_hedge_considered) {
    wrap.appendChild(el("div", { className: "state-message", text: "A bearish inverse-ETF candidate was considered this run — see below." }));
  }

  const narrative = el("div", { className: "funnel-narrative" });
  if (funnel.macro_context) {
    const mc = funnel.macro_context;
    narrative.appendChild(card("Market regime", [
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Regime" }),
        el("span", { className: "kv-value" }, [pill(mc.regime)]),
      ]),
      kv("Equity outlook", mc.equity_outlook),
      kv("Confidence", mc.confidence),
      mc.summary ? el("p", { className: "card-text", text: mc.summary }) : null,
    ].filter(Boolean), { broader: true }));
  } else {
    narrative.appendChild(card("Market regime", [
      el("div", { className: "state-message", text: "No macro regime evidence recorded for this run." }),
    ]));
  }
  if (funnel.pm_reasoning && funnel.pm_reasoning.portfolio_view) {
    narrative.appendChild(card("Portfolio Manager", [
      el("p", { className: "card-text", text: funnel.pm_reasoning.portfolio_view }),
    ]));
  }
  if (funnel.risk_verdict && funnel.risk_verdict.verdict) {
    const v = funnel.risk_verdict.verdict;
    narrative.appendChild(card("AI Risk Manager", [
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Verdict" }),
        el("span", { className: "kv-value" }, [pill(v.approved ? "approved" : "rejected")]),
      ]),
      el("p", { className: "card-text", text: v.reasoning }),
    ]));
  }
  if (funnel.decision_state === "hard_risk_block") {
    narrative.appendChild(card("Deterministic gate", [
      el("p", { className: "card-text", text: "The deterministic hard-risk gate blocked every candidate this run before the AI Risk Manager was ever called." }),
    ]));
  }
  if (narrative.children.length) wrap.appendChild(narrative);

  body.replaceChildren(wrap);
  setPanelState("panel-funnel", "ok", "ok");
}

function candidateFunnelChip(runId, c) {
  const parts = [el("span", { text: c.symbol }), pill(c.direction)];
  if (c.is_bearish_hedge) parts.push(pill("bearish_hedge"));
  if (c.executed) parts.push(pill("executed"));
  else if (c.reached_proposed_order) parts.push(pill(c.risk_modified ? "modified" : "proposed"));
  const chip = el("button", { className: "candidate-chip" }, parts);
  chip.type = "button";
  chip.addEventListener("click", () => openCandidateDetail(runId, c.symbol));
  return chip;
}

function renderWatchlistPanel(funnel) {
  const body = document.querySelector("#panel-watchlist [data-body]");
  if (!funnel.candidates.length) {
    showMessage(body, "No candidates considered in the latest run.");
    setPanelState("panel-watchlist", "ok", "ok");
    return;
  }
  const chips = funnel.candidates.map((c) => candidateFunnelChip(funnel.run_id, c));
  body.replaceChildren(el("div", {}, chips));
  setPanelState("panel-watchlist", "ok", "ok");
}

async function loadLatestFunnel() {
  try {
    const runs = await fetchJSON("/runs?limit=1");
    if (!runs.runs.length) {
      showMessage(document.querySelector("#panel-funnel [data-body]"), "No runs recorded yet.");
      showMessage(document.querySelector("#panel-watchlist [data-body]"), "No runs recorded yet.");
      setPanelState("panel-funnel", "ok", "ok");
      setPanelState("panel-watchlist", "ok", "ok");
      return;
    }
    const runId = runs.runs[0].run_id;
    const funnel = await fetchJSON(`/runs/${encodeURIComponent(runId)}/funnel`);
    renderFunnelPanel(funnel);
    renderWatchlistPanel(funnel);
  } catch (err) {
    showMessage(document.querySelector("#panel-funnel [data-body]"), `Could not load latest decision: ${err.message}`, true);
    showMessage(document.querySelector("#panel-watchlist [data-body]"), `Could not load candidates: ${err.message}`, true);
    setPanelState("panel-funnel", "error", "unreachable");
    setPanelState("panel-watchlist", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Runs panel + run detail modal (funnel-led)                             */
/* ---------------------------------------------------------------------- */

async function loadRuns() {
  const body = document.querySelector("#panel-runs [data-body]");
  try {
    const data = await fetchJSON("/runs?limit=25");
    if (!data.runs.length) {
      showMessage(body, "No runs recorded yet.");
      setPanelState("panel-runs", "ok", "ok");
      return;
    }
    const rows = data.runs.map((r) => {
      const tr = el("tr", { className: "row-clickable", attrs: { tabindex: "0" } }, [
        el("td", { text: r.run_id }),
        el("td", { text: r.session_prefix || "—" }),
        el("td", { text: fmtTime(r.first_timestamp) }),
        el("td", { text: fmtNum(r.agent_count, 0) }),
        el("td", { text: fmtMoney(r.total_cost_usd) }),
      ]);
      tr.addEventListener("click", () => openRunDetail(r.run_id));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRunDetail(r.run_id); } });
      return tr;
    });
    body.replaceChildren(table(["Run ID", "Session", "First Call", "Agents", "Cost"], rows));
    setPanelState("panel-runs", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load runs: ${err.message}`, true);
    setPanelState("panel-runs", "error", "unreachable");
  }
}

function agentLogsTable(agentLogs) {
  if (!agentLogs.length) {
    return el("div", { className: "state-message", text: "No agent calls logged for this run." });
  }
  const rows = agentLogs.map((a) => {
    const actual = `${a.actual_provider || "—"} / ${a.model || "—"}`;
    const providerChanged = a.requested_provider && a.actual_provider && a.requested_provider !== a.actual_provider;
    return el("tr", {}, [
      el("td", { text: a.agent_name }),
      el("td", { className: providerChanged ? "delta-changed" : "", text: actual }),
      el("td", {}, [pill(a.status || "unknown")]),
      el("td", { text: fmtMoney(a.cost_usd) }),
      el("td", { text: a.latency_s !== null && a.latency_s !== undefined ? `${fmtNum(a.latency_s)}s` : "—" }),
    ]);
  });
  return table(["Agent", "Provider / model", "Status", "Cost", "Latency"], rows);
}

async function openRunDetail(runId) {
  openModal(
    [el("span", { className: "crumb-current", text: `Run ${runId}` })],
    el("div", { className: "state-message", text: "Loading run…" })
  );
  try {
    const [funnel, detail] = await Promise.all([
      fetchJSON(`/runs/${encodeURIComponent(runId)}/funnel`),
      fetchJSON(`/runs/${encodeURIComponent(runId)}`),
    ]);
    modalBreadcrumb.replaceChildren(el("span", { className: "crumb-current", text: `Run ${runId}` }));
    const wrap = el("div", {});
    const badge = el("span", {
      className: `funnel-state-badge state-${funnel.decision_state}`,
      text: FUNNEL_STATE_LABELS[funnel.decision_state] || funnel.decision_state,
    });
    wrap.appendChild(el("div", { className: "funnel-head" }, [badge]));
    wrap.appendChild(el("div", { className: "funnel-steps" }, funnelSteps(funnel)));
    wrap.appendChild(
      evidenceSection("Candidates", [
        funnel.candidates.length
          ? el("div", {}, funnel.candidates.map((c) => candidateFunnelChip(runId, c)))
          : null,
      ])
    );
    wrap.appendChild(evidenceSection("Agent calls this run", [agentLogsTable(detail.agent_logs)]));
    modalBody.replaceChildren(wrap);
  } catch (err) {
    showMessage(modalBody, `Could not load run ${runId}: ${err.message}`, true);
  }
}

/* ---------------------------------------------------------------------- */
/* Candidate detail modal — full specialist evidence + decision chain     */
/* (unchanged content model from the Stage 4/5 dashboard).                */
/* ---------------------------------------------------------------------- */

function techCard(tech) {
  if (!tech) return null;
  const body = [
    el("div", { className: "kv-row" }, [
      el("span", { className: "kv-label", text: "Rating" }),
      el("span", { className: "kv-value" }, [pill(tech.rating)]),
    ]),
    kv("Conviction", tech.conviction),
    kv("Entry", tech.entry_price !== null ? fmtMoney(tech.entry_price) : null),
    kv("Reference target", tech.reference_target !== null ? fmtMoney(tech.reference_target) : null),
    kv("Stop loss", tech.stop_loss !== null ? fmtMoney(tech.stop_loss) : null),
    kv("Signal age (days)", tech.signal_age_days),
  ];
  body.push(el("p", { className: "card-text", text: tech.reasoning }));
  if (tech.thesis_invalid_if) {
    body.push(el("p", { className: "card-text dim", text: `Invalid if: ${tech.thesis_invalid_if}` }));
  }
  const chain = chainList(tech.reasoning_chain, {
    trend: "Trend", momentum: "Momentum", volatility: "Volatility",
    volume: "Volume", support_resistance: "Support/Resistance",
  });
  if (chain) body.push(chain);
  return card("Technical analysis", body);
}

function earningsRiskFlags(flags) {
  if (!flags) return [];
  if (Array.isArray(flags)) return flags;
  return [...(flags.strategic_risks || []), ...(flags.operational_risks || [])];
}

function earningsCard(earnings) {
  if (!earnings) return null;
  const impl = earnings.investment_implications;
  const body = [
    kv("Form", earnings.form_type),
    kv("Filing date", earnings.filing_date),
    el("div", { className: "kv-row" }, [
      el("span", { className: "kv-label", text: "Sentiment" }),
      el("span", { className: "kv-value" }, [pill(impl.sentiment)]),
    ]),
    kv("Conviction", impl.conviction),
  ];
  body.push(el("p", { className: "card-text", text: impl.key_thesis }));
  if (impl.bull_case && impl.bull_case !== "not disclosed") body.push(el("p", { className: "card-text", text: `Bull case: ${impl.bull_case}` }));
  if (impl.bear_case && impl.bear_case !== "not disclosed") body.push(el("p", { className: "card-text", text: `Bear case: ${impl.bear_case}` }));
  const flags = earningsRiskFlags(earnings.risk_flags);
  if (flags.length) body.push(el("ul", { className: "card-list" }, flags.map((f) => el("li", { text: f }))));
  return card("Earnings / filing analysis", body);
}

function newsSymbolCards(items) {
  if (!items || !items.length) return null;
  return el("div", { className: "card-grid" }, items.map((n) =>
    card(n.headline, [
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Sentiment" }),
        el("span", { className: "kv-value" }, [pill(n.sentiment)]),
      ]),
      kv("Conviction", n.conviction),
      el("p", { className: "card-text", text: n.impact_summary }),
    ])
  ));
}

function macroContextCard(macro) {
  if (!macro) return null;
  const body = [
    kv("Regime", macro.regime), kv("Equity outlook", macro.equity_outlook), kv("Confidence", macro.confidence),
  ];
  if (macro.summary) body.push(el("p", { className: "card-text", text: macro.summary }));
  if (macro.sector_guidance && macro.sector_guidance.length) {
    const rows = macro.sector_guidance.map((g) => el("tr", {}, [
      el("td", { text: g.sector }), el("td", {}, [pill(g.stance)]), el("td", { text: g.reason }),
    ]));
    body.push(table(["Sector", "Stance", "Reason"], rows));
  }
  return card("Macro regime context", body, { broader: true });
}

function newsContextCard(newsCtx) {
  if (!newsCtx) return null;
  const body = [
    kv("Market sentiment", newsCtx.market_sentiment), kv("Confidence", newsCtx.confidence), kv("Current regime", newsCtx.current_regime),
  ];
  if (newsCtx.pm_briefing) body.push(el("p", { className: "card-text", text: newsCtx.pm_briefing }));
  return card("News / market narrative context", body, { broader: true });
}

function consensusBlock(consensus) {
  const body = [
    el("div", { className: "kv-row" }, [
      el("span", { className: "kv-label", text: "Agreement" }),
      el("span", { className: "kv-value" }, [pill(consensus.agreement)]),
    ]),
  ];
  if (consensus.signals.length) {
    body.push(el("ul", { className: "card-list" }, consensus.signals.map((s) =>
      el("li", {}, [el("strong", { text: `${s.source}: ` }), pill(s.direction), el("span", { text: ` ${s.detail}` })])
    )));
  } else {
    body.push(el("div", { className: "state-message", text: "No independent signals available to compare." }));
  }
  return card("Consensus / disagreement", body);
}

function chainStep(marker, title, children) {
  const body = children.filter((c) => c !== null && c !== undefined);
  return el("div", { className: "chain-step" }, [
    el("div", { className: "chain-step-marker", text: marker }),
    el("div", { className: "chain-step-body" }, [el("div", { className: "card-title", text: title }), ...body]),
  ]);
}

function numsDiffer(a, b) {
  if (a === null || a === undefined || b === null || b === undefined) return false;
  return Math.abs(a - b) > 0.001;
}

function deltaCell(value, changed, fmt) {
  return el("td", { className: changed ? "delta-changed" : "", text: value === null || value === undefined ? "—" : fmt(value) });
}

function proposedVsExecuted(proposed, trade) {
  if (!proposed && !trade) return null;
  const rows = [];
  rows.push(el("tr", {}, [el("td", { text: "Action" }), el("td", { text: dash(proposed && proposed.action) }), el("td", { text: dash(trade && trade.action) })]));
  rows.push(el("tr", {}, [el("td", { text: "Size" }), el("td", { text: proposed ? `${fmtNum(proposed.allocation_pct)}% alloc` : "—" }), el("td", { text: trade && trade.qty !== null ? `${fmtNum(trade.qty)} sh` : "—" })]));
  const entryChanged = numsDiffer(proposed && proposed.entry_price, trade && trade.price);
  rows.push(el("tr", {}, [el("td", { text: "Entry / Fill price" }), deltaCell(proposed && proposed.entry_price, entryChanged, fmtMoney), deltaCell(trade && trade.price, entryChanged, fmtMoney)]));
  const stopChanged = numsDiffer(proposed && proposed.stop_loss, trade && trade.stop_loss);
  rows.push(el("tr", {}, [el("td", { text: "Stop loss" }), deltaCell(proposed && proposed.stop_loss, stopChanged, fmtMoney), deltaCell(trade && trade.stop_loss, stopChanged, fmtMoney)]));
  return el("table", { className: "delta-table" }, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "" }), el("th", { text: "Proposed (PM)" }), el("th", { text: "Executed (trade)" })])]),
    el("tbody", {}, rows),
  ]);
}

function decisionChain(detail) {
  const steps = [];
  if (detail.pm_reasoning) {
    const body = [];
    if (detail.pm_reasoning.portfolio_view) body.push(el("p", { className: "card-text", text: detail.pm_reasoning.portfolio_view }));
    steps.push({ title: "Portfolio Manager reasoning", body });
  }
  if (detail.pm_target) {
    const t = detail.pm_target;
    steps.push({ title: "Portfolio Manager target", body: [
      kv("Target weight", `${fmtNum(t.target_weight_pct)}%`), kv("Conviction", t.conviction),
      el("p", { className: "card-text", text: t.thesis }),
    ] });
  }
  if (detail.pm_proposed_order) {
    const p = detail.pm_proposed_order;
    steps.push({ title: "PM constructed order (pre-review)", body: [
      el("div", { className: "kv-row" }, [el("span", { className: "kv-label", text: "Action" }), el("span", { className: "kv-value" }, [pill(p.action)])]),
      kv("Allocation", `${fmtNum(p.allocation_pct)}%`), kv("Entry", fmtMoney(p.entry_price)),
      el("p", { className: "card-text", text: p.reasoning }),
    ] });
  }
  if (detail.risk_verdict) {
    const v = detail.risk_verdict.verdict;
    const body = [];
    if (v) {
      body.push(el("div", { className: "kv-row" }, [el("span", { className: "kv-label", text: "Verdict" }), el("span", { className: "kv-value" }, [pill(v.approved ? "approved" : "rejected")])]));
      body.push(el("p", { className: "card-text", text: v.reasoning }));
      if (v.modifications && v.modifications.length) {
        body.push(el("ul", { className: "card-list" }, v.modifications.map((m) => el("li", { text: `${m.symbol}: ${m.field} ${m.original_value} → ${m.new_value} (${m.reason})` }))));
      }
    } else {
      body.push(el("div", { className: "state-message", text: "Verdict recorded but could not be read back." }));
    }
    steps.push({ title: "AI Risk Manager verdict (run-wide)", body });
  }
  if (detail.risk_modification) {
    const m = detail.risk_modification;
    steps.push({ title: "AI Risk Manager modification (this symbol)", body: [
      kv("Field", m.field), kv("Original", fmtNum(m.original_value)), kv("Modified to", fmtNum(m.new_value)),
      el("p", { className: "card-text", text: m.reason }),
    ] });
  }
  const delta = proposedVsExecuted(detail.pm_proposed_order, detail.trade);
  if (detail.trade) {
    const t = detail.trade;
    const body = [
      el("div", { className: "kv-row" }, [el("span", { className: "kv-label", text: "Action" }), el("span", { className: "kv-value" }, [pill(t.action)])]),
      kv("Qty", t.qty !== null && t.qty !== undefined ? fmtNum(t.qty) : null), kv("Price", fmtMoney(t.price)),
      el("div", { className: "kv-row" }, [el("span", { className: "kv-label", text: "Fill status" }), el("span", { className: "kv-value" }, [pill(t.fill_status || "unfilled")])]),
    ];
    if (t.reasoning) body.push(el("p", { className: "card-text", text: t.reasoning }));
    if (delta) body.push(delta);
    steps.push({ title: "Executed trade", body });
  } else if (detail.pm_proposed_order) {
    steps.push({ title: "Executed trade", body: [
      el("div", { className: "state-message", text: detail.risk_verdict && detail.risk_verdict.verdict && detail.risk_verdict.verdict.approved === false
        ? "No trade — rejected by the AI Risk Manager before execution."
        : "No trade recorded for this candidate this run (proposed but not executed, or a HOLD)." }),
    ] });
  }
  if (!steps.length) return el("div", { className: "state-message", text: "No PM/Risk/execution chain recorded for this candidate this run." });
  return el("div", { className: "chain-sequence" }, steps.map((s, i) => chainStep(String(i + 1), s.title, s.body)));
}

function renderCandidateDetail(runId, symbol, detail) {
  const wrap = el("div", {});
  wrap.appendChild(evidenceSection("Consensus", [consensusBlock(detail.consensus)]));
  wrap.appendChild(evidenceSection("Symbol-specific evidence", [
    techCard(detail.tech), earningsCard(detail.earnings), newsSymbolCards(detail.news_symbol),
  ]));
  wrap.appendChild(evidenceSection("Broader context (not symbol-specific)", [
    macroContextCard(detail.macro_context), newsContextCard(detail.news_context),
  ]));
  wrap.appendChild(evidenceSection("Decision chain: PM → AI Risk → execution", [decisionChain(detail)]));
  return wrap;
}

async function openCandidateDetail(runId, symbol) {
  openModal(
    [crumbLink(`Run ${runId}`, () => openRunDetail(runId)), el("span", { className: "crumb-sep", text: "/" }), el("span", { className: "crumb-current", text: symbol })],
    el("div", { className: "state-message", text: `Loading ${symbol}…` })
  );
  try {
    const detail = await fetchJSON(`/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(symbol)}`);
    modalBody.replaceChildren(renderCandidateDetail(runId, symbol, detail));
  } catch (err) {
    showMessage(modalBody, `Could not load ${symbol}: ${err.message}`, true);
  }
}

/* ---------------------------------------------------------------------- */
/* Missed opportunities — latest journal day's reflection, surfaced       */
/* directly rather than requiring the operator to open the Journal panel. */
/* ---------------------------------------------------------------------- */

function jsonBlobList(jsonStr, fields) {
  if (!jsonStr) return null;
  let data;
  try { data = JSON.parse(jsonStr); } catch { return null; }
  if (!Array.isArray(data) || !data.length) return null;
  return el("ul", { className: "card-list" }, data.map((item) => {
    const parts = fields.filter((f) => item[f] !== undefined && item[f] !== null && item[f] !== "").map((f) => `${f.replace(/_/g, " ")}: ${item[f]}`);
    return el("li", { text: parts.length ? parts.join(" · ") : JSON.stringify(item) });
  }));
}

async function loadMissedOpportunities() {
  const body = document.querySelector("#panel-missed [data-body]");
  try {
    const dates = await fetchJSON("/journal/dates?limit=1");
    if (!dates.dates.length) {
      showMessage(body, "No journal data recorded yet.");
      setPanelState("panel-missed", "ok", "ok");
      return;
    }
    const date = dates.dates[0];
    const day = await fetchJSON(`/journal/${encodeURIComponent(date)}`);
    const missed = day.reflection ? jsonBlobList(day.reflection.missed_opportunities_json, ["symbol", "miss_category", "move_pct"]) : null;
    if (!missed) {
      showMessage(body, `No missed opportunities recorded in the ${date} evening review.`);
      setPanelState("panel-missed", "ok", "ok");
      return;
    }
    body.replaceChildren(el("div", { className: "dim", text: `From ${date} evening review:` }), missed);
    setPanelState("panel-missed", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load missed opportunities: ${err.message}`, true);
    setPanelState("panel-missed", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Journal (full date-picker browser) + Search — unchanged content model. */
/* ---------------------------------------------------------------------- */

function runsMiniTable(runs, onOpen) {
  if (!runs.length) return el("div", { className: "state-message", text: "No runs recorded for this day." });
  const rows = runs.map((r) => {
    const tr = el("tr", { className: "row-clickable", attrs: { tabindex: "0" } }, [
      el("td", { text: r.run_id }), el("td", { text: r.session_prefix || "—" }),
      el("td", { text: fmtTime(r.first_timestamp) }), el("td", { text: fmtMoney(r.total_cost_usd) }),
    ]);
    tr.addEventListener("click", () => onOpen(r.run_id));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(r.run_id); } });
    return tr;
  });
  return table(["Run ID", "Session", "First Call", "Cost"], rows);
}

function tradesMiniTable(trades) {
  if (!trades.length) return el("div", { className: "state-message", text: "No trades recorded for this day." });
  const rows = trades.map((t) => el("tr", {}, [
    el("td", { text: fmtTime(t.timestamp) }), el("td", { text: t.symbol }), el("td", {}, [pill(t.action)]),
    el("td", { text: fmtNum(t.qty) }), el("td", { text: fmtMoney(t.price) }), el("td", {}, [pill(t.fill_status || "unfilled")]),
  ]));
  return table(["Time", "Symbol", "Action", "Qty", "Price", "Fill"], rows);
}

function reflectionCard(r) {
  if (!r) return el("div", { className: "state-message", text: "No evening reflection recorded for this day yet." });
  const body = [];
  if (r.tomorrow_outlook) body.push(el("p", { className: "card-text", text: `Tomorrow outlook: ${r.tomorrow_outlook}` }));
  if (r.lessons) body.push(el("p", { className: "card-text", text: `Lessons: ${r.lessons}` }));
  if (r.tomorrow_bias) body.push(kv("Tomorrow bias", r.tomorrow_bias));
  const missed = jsonBlobList(r.missed_opportunities_json, ["symbol", "miss_category", "move_pct"]);
  if (missed) body.push(el("div", { className: "chain-label", text: "Missed opportunities" }), missed);
  return card("Evening reflection", body);
}

async function openJournalCandidate(dayRuns, symbol) {
  if (!dayRuns.length) return;
  if (dayRuns.length === 1) return openCandidateDetail(dayRuns[0].run_id, symbol);
  openModal([el("span", { className: "crumb-current", text: symbol })], el("div", { className: "state-message", text: `Resolving which run considered ${symbol}…` }));
  try {
    const results = await Promise.all(dayRuns.map((r) =>
      fetchJSON(`/runs/${encodeURIComponent(r.run_id)}/candidates`).then((d) => ({ run: r, has: d.candidates.includes(symbol) })).catch(() => ({ run: r, has: false }))
    ));
    const matches = results.filter((x) => x.has).map((x) => x.run);
    if (matches.length === 1) return openCandidateDetail(matches[0].run_id, symbol);
    if (matches.length === 0) {
      showMessage(modalBody, `${symbol} could not be matched to a specific run.`, true);
      return;
    }
    modalBody.replaceChildren(el("div", {}, [
      el("div", { className: "state-message", text: `${symbol} was considered in more than one run this day. Pick one:` }),
      el("div", {}, matches.map((r) => {
        const chip = el("button", { className: "candidate-chip", text: r.run_id });
        chip.type = "button";
        chip.addEventListener("click", () => openCandidateDetail(r.run_id, symbol));
        return chip;
      })),
    ]));
  } catch (err) {
    showMessage(modalBody, `Could not resolve ${symbol}: ${err.message}`, true);
  }
}

function renderJournalDay(date, data) {
  const wrap = el("div", {});
  const pnl = data.daily_pnl;
  wrap.appendChild(el("div", { className: "stat-row" }, [
    el("div", { className: "stat" }, [el("div", { className: "stat-label", text: "Equity close" }), el("div", { className: "stat-value", text: pnl ? fmtMoney(pnl.equity_close) : "—" })]),
    el("div", { className: "stat" }, [el("div", { className: "stat-label", text: "Daily P&L" }), el("div", { className: `stat-value ${pnl ? pnlClass(pnl.daily_pnl) : ""}`, text: pnl ? `${fmtMoney(pnl.daily_pnl)} (${fmtPct(pnl.daily_return_pct)})` : "—" })]),
  ]));
  wrap.appendChild(evidenceSection("Evening reflection", [reflectionCard(data.reflection)]));
  wrap.appendChild(evidenceSection("Runs this day", [runsMiniTable(data.runs, openRunDetail)]));
  wrap.appendChild(evidenceSection("Trades this day", [tradesMiniTable(data.trades)]));
  wrap.appendChild(evidenceSection("Candidates considered", [
    data.candidates.length ? el("div", {}, data.candidates.map((sym) => {
      const chip = el("button", { className: "candidate-chip", text: sym });
      chip.type = "button";
      chip.addEventListener("click", () => openJournalCandidate(data.runs, sym));
      return chip;
    })) : null,
  ], "No candidates recorded for this day."));
  return wrap;
}

async function loadJournalDay(date) {
  const body = document.querySelector("#panel-journal [data-body]");
  if (!date) { showMessage(body, "Pick a date above."); setPanelState("panel-journal", "ok", "ok"); return; }
  showMessage(body, `Loading ${date}…`);
  try {
    const res = await fetch(`/journal/${encodeURIComponent(date)}`, { headers: { Accept: "application/json" } });
    if (res.status === 404) { showMessage(body, `No journal data recorded for ${date}.`); setPanelState("panel-journal", "ok", "ok"); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    body.replaceChildren(renderJournalDay(date, data));
    setPanelState("panel-journal", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load journal for ${date}: ${err.message}`, true);
    setPanelState("panel-journal", "error", "unreachable");
  }
}

async function loadJournalDates() {
  const select = document.getElementById("journal-date");
  try {
    const data = await fetchJSON("/journal/dates?limit=60");
    if (!data.dates.length) {
      select.replaceChildren(el("option", { text: "No journal days yet", attrs: { value: "" } }));
      showMessage(document.querySelector("#panel-journal [data-body]"), "No journal data recorded yet.");
      setPanelState("panel-journal", "ok", "ok");
      return;
    }
    select.replaceChildren(...data.dates.map((d, i) => el("option", { text: d, attrs: { value: d, ...(i === 0 ? { selected: "selected" } : {}) } })));
    await loadJournalDay(data.dates[0]);
  } catch (err) {
    select.replaceChildren(el("option", { text: "Unavailable", attrs: { value: "" } }));
    showMessage(document.querySelector("#panel-journal [data-body]"), `Could not load journal dates: ${err.message}`, true);
    setPanelState("panel-journal", "error", "unreachable");
  }
}

document.getElementById("journal-date").addEventListener("change", (e) => loadJournalDay(e.target.value));

function searchHitsTable(hits, kind) {
  if (!hits.length) return null;
  if (kind === "trade") {
    const rows = hits.map((h) => {
      const tr = el("tr", { className: h.run_id ? "row-clickable" : "", attrs: { tabindex: h.run_id ? "0" : "-1" } }, [
        el("td", { text: fmtTime(h.timestamp) }), el("td", { text: h.symbol }), el("td", {}, [pill(h.action)]), el("td", { text: h.reasoning || "—", title: h.reasoning || "" }),
      ]);
      if (h.run_id) tr.addEventListener("click", () => openRunDetail(h.run_id));
      return tr;
    });
    return table(["Time", "Symbol", "Action", "Reasoning"], rows);
  }
  const rows = hits.map((h) => {
    const tr = el("tr", { className: h.run_id ? "row-clickable" : "", attrs: { tabindex: h.run_id ? "0" : "-1" } }, [
      el("td", { text: fmtTime(h.timestamp) }), el("td", { text: h.agent_name }), el("td", { text: h.model || "—" }), el("td", { text: h.output_summary || "—", title: h.output_summary || "" }),
    ]);
    if (h.run_id) tr.addEventListener("click", () => openRunDetail(h.run_id));
    return tr;
  });
  return table(["Time", "Agent", "Model", "Summary"], rows);
}

async function runSearch() {
  const body = document.querySelector("#panel-search [data-body]");
  const q = document.getElementById("search-input").value.trim();
  if (!q) { showMessage(body, "Type a search term above."); setPanelState("panel-search", "ok", "ok"); return; }
  showMessage(body, `Searching for "${q}"…`);
  try {
    const data = await fetchJSON(`/search?q=${encodeURIComponent(q)}&limit=50`);
    const tradesTable = searchHitsTable(data.trades, "trade");
    const agentTable = searchHitsTable(data.agent_logs, "agent_log");
    if (!tradesTable && !agentTable) { showMessage(body, `No matches for "${q}".`); setPanelState("panel-search", "ok", "ok"); return; }
    body.replaceChildren(
      evidenceSection(`Trade hits (${data.trades.length})`, [tradesTable], `No trades matched "${q}".`),
      evidenceSection(`Agent-call hits (${data.agent_logs.length})`, [agentTable], `No agent calls matched "${q}".`)
    );
    setPanelState("panel-search", "ok", "ok");
  } catch (err) {
    showMessage(body, `Search failed: ${err.message}`, true);
    setPanelState("panel-search", "error", "unreachable");
  }
}

document.getElementById("search-btn").addEventListener("click", runSearch);
document.getElementById("search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

/* ---------------------------------------------------------------------- */
/* Orchestration                                                          */
/* ---------------------------------------------------------------------- */

function refreshAll() {
  loadAccountAndPositions();
  loadOrders();
  loadTrades();
  loadHealth();
  loadLatestFunnel();
  loadMissedOpportunities();
}

document.getElementById("orders-status").addEventListener("change", loadOrders);

refreshAll();
loadRuns();
loadJournalDates();
setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(); });
