"use strict";

/* QAMC Mission Control — Stage 3 cockpit.
 *
 * Vanilla JS, no build step, no framework. Every panel renders only what
 * the read-only Stage-2 API actually returns for it; a fetch failure or an
 * `error` field on a response is shown as an honest degraded/error state,
 * never backfilled with placeholder numbers. See docs/architecture/
 * MISSION_CONTROL_API.md for the endpoint contract this reads.
 */

const REFRESH_MS = 20000;

async function fetchJSON(path) {
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new Error(`network error: ${err.message}`);
  }
  if (!res.ok) {
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

/* ---------------------------------------------------------------------- */
/* Topbar                                                                  */
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
/* Account                                                                 */
/* ---------------------------------------------------------------------- */

function sparkline(history) {
  const points = history
    .map((p) => p.equity_close)
    .filter((v) => v !== null && v !== undefined);
  if (points.length < 2) return null;
  const w = 320, h = 48, pad = 3;
  const min = Math.min(...points), max = Math.max(...points);
  const range = max - min || 1;
  const step = (w - pad * 2) / (points.length - 1);
  const coords = points.map((v, i) => {
    const x = pad + i * step;
    const y = pad + (h - pad * 2) * (1 - (v - min) / range);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const rising = points[points.length - 1] >= points[0];
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", h);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", coords.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", rising ? "var(--green)" : "var(--red)");
  line.setAttribute("stroke-width", "2");
  svg.appendChild(line);
  return svg;
}

async function loadAccount() {
  const body = document.querySelector("#panel-account [data-body]");
  try {
    const data = await fetchJSON("/account");
    updateModeBadge(data.paper);
    if (data.error) {
      showMessage(body, `Account read failed: ${data.error}`, true);
      setPanelState("panel-account", "degraded", "degraded");
      return;
    }
    const stats = el("div", { className: "stat-row" }, [
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Cash" }),
        el("div", { className: "stat-value", text: fmtMoney(data.cash) }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Portfolio Value" }),
        el("div", { className: "stat-value", text: fmtMoney(data.portfolio_value) }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Daily P&L" }),
        el("div", {
          className: `stat-value ${pnlClass(data.daily_pnl)}`,
          text: `${fmtMoney(data.daily_pnl)} (${fmtPct(data.daily_pnl_pct)})`,
        }),
      ]),
    ]);
    const wrap = el("div", {}, [stats]);
    const spark = sparkline(data.history || []);
    if (spark) {
      wrap.appendChild(el("div", { className: "stat-label", text: "Equity, recent sessions" }));
      wrap.appendChild(spark);
    } else {
      wrap.appendChild(el("div", { className: "state-message", text: "Not enough history yet for a trend line." }));
    }
    body.replaceChildren(wrap);
    setPanelState("panel-account", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load account: ${err.message}`, true);
    setPanelState("panel-account", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Positions                                                               */
/* ---------------------------------------------------------------------- */

async function loadPositions() {
  const body = document.querySelector("#panel-positions [data-body]");
  try {
    const data = await fetchJSON("/positions");
    if (data.error) {
      showMessage(body, `Positions read failed: ${data.error}`, true);
      setPanelState("panel-positions", "degraded", "degraded");
      return;
    }
    if (!data.positions.length) {
      showMessage(body, "No open positions.");
      setPanelState("panel-positions", "ok", "ok");
      return;
    }
    const rows = data.positions.map((p) =>
      el("tr", {}, [
        el("td", { text: p.symbol }),
        el("td", { text: fmtNum(p.qty) }),
        el("td", { text: fmtMoney(p.avg_entry) }),
        el("td", { text: fmtMoney(p.current_price) }),
        el("td", { text: fmtMoney(p.market_value) }),
        el("td", { className: pnlClass(p.unrealized_pnl), text: fmtMoney(p.unrealized_pnl) }),
        el("td", { text: p.sector || "—" }),
      ])
    );
    body.replaceChildren(
      table(["Symbol", "Qty", "Avg Entry", "Price", "Mkt Value", "Unrealized P&L", "Sector"], rows)
    );
    setPanelState("panel-positions", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load positions: ${err.message}`, true);
    setPanelState("panel-positions", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Orders                                                                  */
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
        el("td", { text: o.order_type || "—" }),
        el("td", {}, [pill(o.status)]),
        el("td", { text: fmtMoney(o.limit_price ?? o.stop_price) }),
        el("td", { text: `${fmtNum(o.filled_qty)} @ ${fmtMoney(o.filled_avg_price)}` }),
        el("td", { text: fmtTime(o.submitted_at) }),
      ])
    );
    body.replaceChildren(
      table(["Symbol", "Side", "Qty", "Type", "Status", "Limit/Stop", "Filled", "Submitted"], rows)
    );
    setPanelState("panel-orders", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load orders: ${err.message}`, true);
    setPanelState("panel-orders", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Trades                                                                  */
/* ---------------------------------------------------------------------- */

async function loadTrades() {
  const body = document.querySelector("#panel-trades [data-body]");
  try {
    const data = await fetchJSON("/trades?limit=50");
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
        el("td", { text: t.reasoning || "—", title: t.reasoning || "" }),
      ])
    );
    body.replaceChildren(
      table(["Time", "Symbol", "Action", "Qty", "Price", "Fill", "Reasoning"], rows)
    );
    setPanelState("panel-trades", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load trades: ${err.message}`, true);
    setPanelState("panel-trades", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Candidates                                                              */
/* ---------------------------------------------------------------------- */

async function loadCandidates() {
  const body = document.querySelector("#panel-candidates [data-body]");
  try {
    const data = await fetchJSON("/candidates?lookback_days=30");
    if (!data.candidates.length) {
      showMessage(body, "No repeated watchlist candidates in the last 30 days.");
      setPanelState("panel-candidates", "ok", "ok");
      return;
    }
    const rows = data.candidates.map((c) =>
      el("tr", {}, [
        el("td", { text: c.symbol }),
        el("td", { text: fmtNum(c.add_count, 0) }),
        el("td", { text: fmtNum(c.watch_count, 0) }),
        el("td", { text: fmtNum(c.total_flags, 0) }),
        el("td", { text: (c.dates && c.dates[0]) || "—" }),
        el("td", { text: c.latest_reason || "—", title: c.latest_reason || "" }),
      ])
    );
    body.replaceChildren(
      table(["Symbol", "Add", "Watch", "Total Flags", "Latest Date", "Latest Reason"], rows)
    );
    setPanelState("panel-candidates", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load candidates: ${err.message}`, true);
    setPanelState("panel-candidates", "error", "unreachable");
  }
}

/* ---------------------------------------------------------------------- */
/* Health                                                                  */
/* ---------------------------------------------------------------------- */

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
      statBlock("Mode", data.paper === null || data.paper === undefined ? "unknown" : data.paper ? "paper" : "LIVE"),
      statBlock("Sessions logged today", (data.sessions_logged_today || []).join(", ") || "none"),
      statBlock("Session lock", data.session_lock_active === null ? "unknown" : data.session_lock_active ? "active" : "idle"),
      statBlock("Server time", fmtTime(data.timestamp)),
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

function statBlock(label, value) {
  return el("div", {}, [
    el("div", { className: "stat-label", text: label }),
    el("div", { text: value }),
  ]);
}

/* ---------------------------------------------------------------------- */
/* Orchestration                                                           */
/* ---------------------------------------------------------------------- */

function refreshAll() {
  loadAccount();
  loadPositions();
  loadOrders();
  loadTrades();
  loadCandidates();
  loadHealth();
}

document.getElementById("orders-status").addEventListener("change", loadOrders);

refreshAll();
setInterval(() => {
  if (!document.hidden) refreshAll();
}, REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});
