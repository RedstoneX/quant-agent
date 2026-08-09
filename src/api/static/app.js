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
/* Runs — Stage 4 per-candidate fidelity drill-down.                      */
/*                                                                        */
/* No click-through mechanism existed before Stage 4; this introduces one */
/* modal-based navigation primitive: Runs panel -> run detail (candidates */
/* + agent calls) -> candidate detail (evidence / consensus / decision    */
/* chain). Fetched on demand when opened, never auto-polled — the         */
/* existing REFRESH_MS cycle below is unchanged.                          */
/* ---------------------------------------------------------------------- */

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

/* ---- modal shell ------------------------------------------------------ */

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
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modalOverlay.hidden) closeModal();
});

/* ---- Runs panel --------------------------------------------------------*/

async function loadRuns() {
  const body = document.querySelector("#panel-runs [data-body]");
  try {
    const data = await fetchJSON("/runs?limit=20");
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
        el("td", { text: fmtTime(r.last_timestamp) }),
        el("td", { text: fmtNum(r.agent_count, 0) }),
        el("td", { text: r.decision_id ? "yes" : "—" }),
        el("td", { text: fmtMoney(r.total_cost_usd) }),
      ]);
      tr.addEventListener("click", () => openRunDetail(r.run_id));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRunDetail(r.run_id); }
      });
      return tr;
    });
    body.replaceChildren(
      table(["Run ID", "Session", "First Call", "Last Call", "Agents", "Decision", "Cost"], rows)
    );
    setPanelState("panel-runs", "ok", "ok");
  } catch (err) {
    showMessage(body, `Could not load runs: ${err.message}`, true);
    setPanelState("panel-runs", "error", "unreachable");
  }
}

/* ---- run detail modal -------------------------------------------------- */

function agentLogsTable(agentLogs) {
  if (!agentLogs.length) {
    return el("div", { className: "state-message", text: "No agent calls logged for this run." });
  }
  const rows = agentLogs.map((a) => {
    const requested = `${a.requested_provider || "—"} / ${a.requested_model || "—"}`;
    const actual = `${a.actual_provider || "—"} / ${a.model || "—"}`;
    const providerChanged = a.requested_provider && a.actual_provider && a.requested_provider !== a.actual_provider;
    return el("tr", {}, [
      el("td", { text: a.agent_name }),
      el("td", { text: requested }),
      el("td", { className: providerChanged ? "delta-changed" : "", text: actual }),
      el("td", {}, [pill(a.status || "unknown")]),
      el("td", { text: fmtMoney(a.cost_usd) }),
      el("td", { text: a.latency_s !== null && a.latency_s !== undefined ? `${fmtNum(a.latency_s)}s` : "—" }),
      el("td", { text: fmtNum(a.tokens_used, 0) }),
    ]);
  });
  return table(["Agent", "Requested provider/model", "Actual provider/model", "Status", "Cost", "Latency", "Tokens"], rows);
}

function renderRunDetail(runId, detail, candidates) {
  const wrap = el("div", {});

  const summary = el("div", { className: "stat-row" }, [
    el("div", { className: "stat" }, [
      el("div", { className: "stat-label", text: "Decision ID" }),
      el("div", { text: dash(detail.decision_id) }),
    ]),
    el("div", { className: "stat" }, [
      el("div", { className: "stat-label", text: "Total Cost" }),
      el("div", { text: fmtMoney(detail.total_cost_usd) }),
    ]),
    el("div", { className: "stat" }, [
      el("div", { className: "stat-label", text: "Trades" }),
      el("div", { text: fmtNum(detail.trades.length, 0) }),
    ]),
  ]);
  if (detail.hard_risk_block_recorded) {
    summary.appendChild(
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Deterministic gate" }),
        el("div", {}, [pill("hard_risk_block")]),
      ])
    );
  }
  wrap.appendChild(summary);

  wrap.appendChild(
    evidenceSection("Candidates considered", [
      candidates.candidates.length
        ? el(
            "div",
            {},
            candidates.candidates.map((sym) => {
              const chip = el("button", { className: "candidate-chip", text: sym });
              chip.type = "button";
              chip.addEventListener("click", () => openCandidateDetail(runId, sym, detail));
              return chip;
            })
          )
        : null,
    ])
  );

  wrap.appendChild(evidenceSection("Agent calls this run", [agentLogsTable(detail.agent_logs)]));

  return wrap;
}

async function openRunDetail(runId) {
  openModal(
    [el("span", { className: "crumb-current", text: `Run ${runId}` })],
    el("div", { className: "state-message", text: "Loading run…" })
  );
  try {
    const [detail, candidates] = await Promise.all([
      fetchJSON(`/runs/${encodeURIComponent(runId)}`),
      fetchJSON(`/runs/${encodeURIComponent(runId)}/candidates`),
    ]);
    modalBreadcrumb.replaceChildren(el("span", { className: "crumb-current", text: `Run ${runId}` }));
    modalBody.replaceChildren(renderRunDetail(runId, detail, candidates));
  } catch (err) {
    showMessage(modalBody, `Could not load run ${runId}: ${err.message}`, true);
  }
}

/* ---- candidate detail modal -------------------------------------------- */

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
    kv("Risk/Reward", tech.risk_reward !== null && tech.risk_reward !== undefined ? `${tech.risk_reward}x` : null),
    kv("Signal age (days)", tech.signal_age_days),
    kv("ATR(14)", tech.atr_14 !== null ? fmtNum(tech.atr_14) : null),
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
    kv("Data quality", earnings.data_quality),
  ];
  body.push(el("p", { className: "card-text", text: impl.key_thesis }));
  if (impl.bull_case && impl.bull_case !== "not disclosed") {
    body.push(el("p", { className: "card-text", text: `Bull case: ${impl.bull_case}` }));
  }
  if (impl.bear_case && impl.bear_case !== "not disclosed") {
    body.push(el("p", { className: "card-text", text: `Bear case: ${impl.bear_case}` }));
  }
  if (earnings.guidance) {
    body.push(el("p", { className: "card-text", text: `Guidance: ${earnings.guidance}` }));
  }
  if (earnings.management_highlights && earnings.management_highlights.length) {
    body.push(el("ul", { className: "card-list" }, earnings.management_highlights.map((h) => el("li", { text: h }))));
  }
  const flags = earningsRiskFlags(earnings.risk_flags);
  if (flags.length) {
    body.push(el("ul", { className: "card-list" }, flags.map((f) => el("li", { text: f }))));
  }
  return card("Earnings / filing analysis", body);
}

function newsSymbolCards(items) {
  if (!items || !items.length) return null;
  return el(
    "div",
    { className: "card-grid" },
    items.map((n) =>
      card(n.headline, [
        el("div", { className: "kv-row" }, [
          el("span", { className: "kv-label", text: "Sentiment" }),
          el("span", { className: "kv-value" }, [pill(n.sentiment)]),
        ]),
        kv("Conviction", n.conviction),
        el("p", { className: "card-text", text: n.impact_summary }),
      ])
    )
  );
}

function macroContextCard(macro) {
  if (!macro) return null;
  const body = [
    kv("Regime", macro.regime),
    kv("Equity outlook", macro.equity_outlook),
    kv("Confidence", macro.confidence),
  ];
  if (macro.summary) body.push(el("p", { className: "card-text", text: macro.summary }));
  if (macro.sector_guidance && macro.sector_guidance.length) {
    const rows = macro.sector_guidance.map((g) =>
      el("tr", {}, [
        el("td", { text: g.sector }),
        el("td", {}, [pill(g.stance)]),
        el("td", { text: g.reason }),
      ])
    );
    body.push(table(["Sector", "Stance", "Reason"], rows));
  }
  return card("Macro regime context", body, { broader: true });
}

function newsContextCard(newsCtx) {
  if (!newsCtx) return null;
  const body = [
    kv("Market sentiment", newsCtx.market_sentiment),
    kv("Confidence", newsCtx.confidence),
    kv("Current regime", newsCtx.current_regime),
  ];
  if (newsCtx.pm_briefing) body.push(el("p", { className: "card-text", text: newsCtx.pm_briefing }));
  if (newsCtx.era_themes && newsCtx.era_themes.length) {
    body.push(el("ul", { className: "card-list" }, newsCtx.era_themes.map((t) => el("li", { text: t }))));
  }
  if (newsCtx.relevant_state_changes && newsCtx.relevant_state_changes.length) {
    const rows = newsCtx.relevant_state_changes.map((s) =>
      el("tr", {}, [
        el("td", { text: s.event }),
        el("td", { text: `${s.previous_state} → ${s.new_state}` }),
        el("td", { text: s.market_impact }),
      ])
    );
    body.push(el("div", { className: "state-message", text: "State changes citing this symbol:" }));
    body.push(table(["Event", "Transition", "Market impact"], rows));
  }
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
    body.push(
      el("ul", { className: "card-list" }, consensus.signals.map((s) =>
        el("li", {}, [
          el("strong", { text: `${s.source}: ` }),
          pill(s.direction),
          el("span", { text: ` ${s.detail}` }),
        ])
      ))
    );
  } else {
    body.push(el("div", { className: "state-message", text: "No independent signals available to compare." }));
  }
  return card("Consensus / disagreement", body);
}

function chainStep(marker, title, children) {
  const body = children.filter((c) => c !== null && c !== undefined);
  return el("div", { className: "chain-step" }, [
    el("div", { className: "chain-step-marker", text: marker }),
    el("div", { className: "chain-step-body" }, [
      el("div", { className: "card-title", text: title }),
      ...body,
    ]),
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
  rows.push(
    el("tr", {}, [
      el("td", { text: "Action" }),
      el("td", { text: dash(proposed && proposed.action) }),
      el("td", { text: dash(trade && trade.action) }),
    ])
  );
  rows.push(
    el("tr", {}, [
      el("td", { text: "Size" }),
      el("td", { text: proposed ? `${fmtNum(proposed.allocation_pct)}% alloc` : "—" }),
      el("td", { text: trade && trade.qty !== null ? `${fmtNum(trade.qty)} sh` : "—" }),
    ])
  );
  const entryChanged = numsDiffer(proposed && proposed.entry_price, trade && trade.price);
  rows.push(
    el("tr", {}, [
      el("td", { text: "Entry / Fill price" }),
      deltaCell(proposed && proposed.entry_price, entryChanged, fmtMoney),
      deltaCell(trade && trade.price, entryChanged, fmtMoney),
    ])
  );
  const stopChanged = numsDiffer(proposed && proposed.stop_loss, trade && trade.stop_loss);
  rows.push(
    el("tr", {}, [
      el("td", { text: "Stop loss" }),
      deltaCell(proposed && proposed.stop_loss, stopChanged, fmtMoney),
      deltaCell(trade && trade.stop_loss, stopChanged, fmtMoney),
    ])
  );
  const tpChanged = numsDiffer(proposed && proposed.take_profit, trade && trade.take_profit);
  rows.push(
    el("tr", {}, [
      el("td", { text: "Take profit" }),
      deltaCell(proposed && proposed.take_profit, tpChanged, fmtMoney),
      deltaCell(trade && trade.take_profit, tpChanged, fmtMoney),
    ])
  );
  return el("table", { className: "delta-table" }, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "" }), el("th", { text: "Proposed (PM)" }), el("th", { text: "Executed (trade)" })])]),
    el("tbody", {}, rows),
  ]);
}

function decisionChain(detail) {
  // Collected as {title, body} and numbered sequentially at the end (not by
  // a fixed slot 1-6) — a candidate that skips e.g. the PM target/proposed
  // order steps (HOLD, or rejected pre-construction) still gets a clean
  // 1..N sequence instead of a marker jumping straight from 1 to 4.
  const steps = [];

  if (detail.pm_reasoning) {
    const body = [];
    if (detail.pm_reasoning.portfolio_view) {
      body.push(el("p", { className: "card-text", text: detail.pm_reasoning.portfolio_view }));
    }
    const chain = chainList(detail.pm_reasoning.reasoning_chain, {
      macro_filter: "Macro filter", news_check: "News check", earnings_check: "Earnings check",
      signal_conflicts: "Signal conflicts", sizing_logic: "Sizing logic",
      portfolio_balance: "Portfolio balance", cash_target: "Cash target",
      continuity_check: "Continuity check", premortem_check: "Pre-mortem check",
    });
    if (chain) body.push(chain);
    steps.push({ title: "Portfolio Manager reasoning", body });
  }

  if (detail.pm_target) {
    const t = detail.pm_target;
    const body = [
      kv("Target weight", `${fmtNum(t.target_weight_pct)}%`),
      kv("Conviction", t.conviction),
      el("p", { className: "card-text", text: t.thesis }),
    ];
    if (t.thesis_invalid_if) body.push(el("p", { className: "card-text dim", text: `Invalid if: ${t.thesis_invalid_if}` }));
    if (t.catalyst) body.push(el("p", { className: "card-text", text: `Catalyst override: ${t.catalyst}` }));
    steps.push({ title: "Portfolio Manager target", body });
  }

  if (detail.pm_proposed_order) {
    const p = detail.pm_proposed_order;
    const body = [
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Action" }),
        el("span", { className: "kv-value" }, [pill(p.action)]),
      ]),
      kv("Allocation", `${fmtNum(p.allocation_pct)}%`),
      kv("Entry", fmtMoney(p.entry_price)),
      kv("Stop loss", fmtMoney(p.stop_loss)),
      kv("Take profit", fmtMoney(p.take_profit)),
      el("p", { className: "card-text", text: p.reasoning }),
    ];
    steps.push({ title: "PM constructed order (pre-review)", body });
  }

  if (detail.risk_verdict) {
    const v = detail.risk_verdict.verdict;
    const body = [];
    if (v) {
      body.push(
        el("div", { className: "kv-row" }, [
          el("span", { className: "kv-label", text: "Verdict" }),
          el("span", { className: "kv-value" }, [pill(v.approved ? "approved" : "rejected")]),
        ])
      );
      body.push(kv("Reason category", v.reason_category));
      body.push(kv("Scale all buys", `${fmtNum(v.scale_all_buys)}x`));
      body.push(el("p", { className: "card-text", text: v.reasoning }));
      const chain = chainList(v.reasoning_chain, {
        rr_audit: "R/R audit", signal_fidelity: "Signal fidelity",
        correlation_check: "Correlation check", event_risk: "Event risk",
        sizing_sanity: "Sizing sanity", overall: "Overall",
      });
      if (chain) body.push(chain);
      if (v.modifications && v.modifications.length) {
        body.push(
          el("ul", { className: "card-list" }, v.modifications.map((m) =>
            el("li", { text: `${m.symbol}: ${m.field} ${m.original_value} → ${m.new_value} (${m.reason})` })
          ))
        );
      }
    } else {
      body.push(el("div", { className: "state-message", text: "Verdict recorded but could not be read back." }));
    }
    steps.push({ title: "AI Risk Manager verdict (run-wide)", body });
  }

  if (detail.risk_modification) {
    const m = detail.risk_modification;
    steps.push({
      title: "AI Risk Manager modification (this symbol)",
      body: [
        kv("Field", m.field),
        kv("Original", fmtNum(m.original_value)),
        kv("Modified to", fmtNum(m.new_value)),
        el("p", { className: "card-text", text: m.reason }),
      ],
    });
  }

  const delta = proposedVsExecuted(detail.pm_proposed_order, detail.trade);
  if (detail.trade) {
    const t = detail.trade;
    const body = [
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Action" }),
        el("span", { className: "kv-value" }, [pill(t.action)]),
      ]),
      kv("Qty", t.qty !== null && t.qty !== undefined ? fmtNum(t.qty) : null),
      kv("Price", fmtMoney(t.price)),
      el("div", { className: "kv-row" }, [
        el("span", { className: "kv-label", text: "Fill status" }),
        el("span", { className: "kv-value" }, [pill(t.fill_status || "unfilled")]),
      ]),
      kv("Broker order id", t.broker_order_id),
      kv("Timestamp", fmtTime(t.timestamp)),
    ];
    if (t.reasoning) body.push(el("p", { className: "card-text", text: t.reasoning }));
    if (delta) body.push(delta);
    steps.push({ title: "Executed trade", body });
  } else if (detail.pm_proposed_order) {
    steps.push({
      title: "Executed trade",
      body: [
        el("div", { className: "state-message", text: detail.risk_verdict && detail.risk_verdict.verdict && detail.risk_verdict.verdict.approved === false
          ? "No trade — rejected by the AI Risk Manager before execution."
          : "No trade recorded for this candidate this run (proposed but not executed, or a HOLD)." }),
      ],
    });
  }

  if (!steps.length) {
    return el("div", { className: "state-message", text: "No PM/Risk/execution chain recorded for this candidate this run." });
  }
  return el(
    "div", { className: "chain-sequence" },
    steps.map((s, i) => chainStep(String(i + 1), s.title, s.body))
  );
}

function renderCandidateDetail(runId, symbol, detail, runDetail) {
  const wrap = el("div", {});

  wrap.appendChild(
    el("div", { className: "stat-row" }, [
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Decision ID" }),
        el("div", { text: dash(detail.decision_id) }),
      ]),
    ])
  );

  wrap.appendChild(evidenceSection("Consensus", [consensusBlock(detail.consensus)]));

  wrap.appendChild(
    evidenceSection("Symbol-specific evidence", [
      techCard(detail.tech),
      earningsCard(detail.earnings),
      newsSymbolCards(detail.news_symbol),
    ])
  );

  wrap.appendChild(
    evidenceSection("Broader context (not symbol-specific)", [
      macroContextCard(detail.macro_context),
      newsContextCard(detail.news_context),
    ])
  );

  wrap.appendChild(evidenceSection("Decision chain: PM → AI Risk → execution", [decisionChain(detail)]));

  if (runDetail) {
    wrap.appendChild(evidenceSection("Agent calls this run", [agentLogsTable(runDetail.agent_logs)]));
  }

  return wrap;
}

async function openCandidateDetail(runId, symbol, runDetail) {
  openModal(
    [
      crumbLink(`Run ${runId}`, () => openRunDetail(runId)),
      el("span", { className: "crumb-sep", text: "/" }),
      el("span", { className: "crumb-current", text: symbol }),
    ],
    el("div", { className: "state-message", text: `Loading ${symbol}…` })
  );
  try {
    const detail = await fetchJSON(`/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(symbol)}`);
    modalBody.replaceChildren(renderCandidateDetail(runId, symbol, detail, runDetail));
  } catch (err) {
    showMessage(modalBody, `Could not load ${symbol}: ${err.message}`, true);
  }
}

/* ---------------------------------------------------------------------- */
/* Journal — Stage 5 prior-day browsing.                                  */
/*                                                                        */
/* Reuses the Stage 4 run-detail/candidate-detail modal rather than       */
/* duplicating it. Loaded on demand (date picker), never auto-polled.     */
/* ---------------------------------------------------------------------- */

function runsMiniTable(runs, onOpen) {
  if (!runs.length) {
    return el("div", { className: "state-message", text: "No runs recorded for this day." });
  }
  const rows = runs.map((r) => {
    const tr = el("tr", { className: "row-clickable", attrs: { tabindex: "0" } }, [
      el("td", { text: r.run_id }),
      el("td", { text: r.session_prefix || "—" }),
      el("td", { text: fmtTime(r.first_timestamp) }),
      el("td", { text: fmtNum(r.agent_count, 0) }),
      el("td", { text: fmtMoney(r.total_cost_usd) }),
    ]);
    tr.addEventListener("click", () => onOpen(r.run_id));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(r.run_id); }
    });
    return tr;
  });
  return table(["Run ID", "Session", "First Call", "Agents", "Cost"], rows);
}

function tradesMiniTable(trades) {
  if (!trades.length) {
    return el("div", { className: "state-message", text: "No trades recorded for this day." });
  }
  const rows = trades.map((t) =>
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
  return table(["Time", "Symbol", "Action", "Qty", "Price", "Fill", "Reasoning"], rows);
}

function jsonBlobList(jsonStr, fields) {
  if (!jsonStr) return null;
  let data;
  try {
    data = JSON.parse(jsonStr);
  } catch {
    return el("div", { className: "state-message error", text: "Could not parse recorded data for this field." });
  }
  if (!Array.isArray(data) || !data.length) return null;
  return el(
    "ul", { className: "card-list" },
    data.map((item) => {
      const parts = fields
        .filter((f) => item[f] !== undefined && item[f] !== null && item[f] !== "")
        .map((f) => `${f.replace(/_/g, " ")}: ${item[f]}`);
      return el("li", { text: parts.length ? parts.join(" · ") : JSON.stringify(item) });
    })
  );
}

function reflectionCard(r) {
  if (!r) {
    return el("div", { className: "state-message", text: "No evening reflection recorded for this day yet." });
  }
  const body = [];
  if (r.tomorrow_outlook) body.push(el("p", { className: "card-text", text: `Tomorrow outlook: ${r.tomorrow_outlook}` }));
  if (r.lessons) body.push(el("p", { className: "card-text", text: `Lessons: ${r.lessons}` }));
  body.push(kv("Risk rating", r.risk_rating));
  if (r.tomorrow_bias) body.push(kv("Tomorrow bias", r.tomorrow_bias));
  if (r.tomorrow_conviction) body.push(kv("Tomorrow conviction", r.tomorrow_conviction));
  if (r.tomorrow_key_risks) body.push(el("p", { className: "card-text dim", text: `Key risks: ${r.tomorrow_key_risks}` }));
  if (r.suggested_actions) body.push(el("p", { className: "card-text dim", text: `Suggested actions: ${r.suggested_actions}` }));
  if (r.sell_decisions_assessment) body.push(el("p", { className: "card-text dim", text: `Sell decisions: ${r.sell_decisions_assessment}` }));
  const sellGrades = jsonBlobList(r.sell_grades_json, ["symbol", "grade", "reason"]);
  if (sellGrades) {
    body.push(el("div", { className: "chain-label", text: "Sell grades" }), sellGrades);
  }
  const buyGrades = jsonBlobList(r.buy_grades_json, ["symbol", "grade", "reason"]);
  if (buyGrades) {
    body.push(el("div", { className: "chain-label", text: "Buy grades" }), buyGrades);
  }
  const missed = jsonBlobList(r.missed_opportunities_json, ["symbol", "miss_category", "move_pct"]);
  if (missed) {
    body.push(el("div", { className: "chain-label", text: "Missed opportunities" }), missed);
  }
  return card("Evening reflection", body);
}

/** A journal day's `candidates` are a flat symbol list with no run_id
 * attached (unlike Stage 4's per-run candidates). Rather than silently
 * guessing which of the day's runs to attach a click to, resolve it: skip
 * straight through when there's exactly one run that day, otherwise check
 * each run's actual candidate list and only auto-open on an unambiguous
 * single match — a genuine multi-run match asks the operator to pick. */
async function openJournalCandidate(dayRuns, symbol) {
  if (!dayRuns.length) return;
  if (dayRuns.length === 1) {
    return openCandidateDetail(dayRuns[0].run_id, symbol);
  }
  openModal(
    [el("span", { className: "crumb-current", text: symbol })],
    el("div", { className: "state-message", text: `Resolving which run considered ${symbol}…` })
  );
  try {
    const results = await Promise.all(
      dayRuns.map((r) =>
        fetchJSON(`/runs/${encodeURIComponent(r.run_id)}/candidates`)
          .then((d) => ({ run: r, has: d.candidates.includes(symbol) }))
          .catch(() => ({ run: r, has: false }))
      )
    );
    const matches = results.filter((x) => x.has).map((x) => x.run);
    if (matches.length === 1) {
      return openCandidateDetail(matches[0].run_id, symbol);
    }
    if (matches.length === 0) {
      showMessage(modalBody, `${symbol} could not be matched to a specific run — the day's runs may have changed since this journal snapshot was read.`, true);
      return;
    }
    modalBreadcrumb.replaceChildren(el("span", { className: "crumb-current", text: `${symbol} — pick a run` }));
    modalBody.replaceChildren(
      el("div", {}, [
        el("div", { className: "state-message", text: `${symbol} was considered in more than one run this day. Pick which one:` }),
        el(
          "div", {},
          matches.map((r) => {
            const chip = el("button", { className: "candidate-chip", text: r.run_id });
            chip.type = "button";
            chip.addEventListener("click", () => openCandidateDetail(r.run_id, symbol));
            return chip;
          })
        ),
      ])
    );
  } catch (err) {
    showMessage(modalBody, `Could not resolve ${symbol}: ${err.message}`, true);
  }
}

function renderJournalDay(date, data) {
  const wrap = el("div", {});

  const pnl = data.daily_pnl;
  wrap.appendChild(
    el("div", { className: "stat-row" }, [
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Equity close" }),
        el("div", { className: "stat-value", text: pnl ? fmtMoney(pnl.equity_close) : "—" }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Daily P&L" }),
        el("div", {
          className: `stat-value ${pnl ? pnlClass(pnl.daily_pnl) : ""}`,
          text: pnl ? `${fmtMoney(pnl.daily_pnl)} (${fmtPct(pnl.daily_return_pct)})` : "—",
        }),
      ]),
      el("div", { className: "stat" }, [
        el("div", { className: "stat-label", text: "Total value" }),
        el("div", { className: "stat-value", text: pnl ? fmtMoney(pnl.total_value) : "—" }),
      ]),
    ])
  );
  if (!pnl) {
    wrap.appendChild(el("div", { className: "state-message", text: "No equity snapshot recorded for this day." }));
  }

  wrap.appendChild(evidenceSection("Evening reflection", [reflectionCard(data.reflection)]));

  wrap.appendChild(evidenceSection("Runs this day", [runsMiniTable(data.runs, openRunDetail)]));

  wrap.appendChild(evidenceSection("Trades this day", [tradesMiniTable(data.trades)]));

  wrap.appendChild(
    evidenceSection(
      "Candidates considered",
      [
        data.candidates.length
          ? el(
              "div", {},
              data.candidates.map((sym) => {
                const chip = el("button", { className: "candidate-chip", text: sym });
                chip.type = "button";
                chip.addEventListener("click", () => openJournalCandidate(data.runs, sym));
                return chip;
              })
            )
          : null,
      ],
      "No candidates recorded for this day."
    )
  );

  return wrap;
}

async function loadJournalDay(date) {
  const body = document.querySelector("#panel-journal [data-body]");
  if (!date) {
    showMessage(body, "Pick a date above.");
    setPanelState("panel-journal", "ok", "ok");
    return;
  }
  showMessage(body, `Loading ${date}…`);
  try {
    const res = await fetch(`/journal/${encodeURIComponent(date)}`, { headers: { Accept: "application/json" } });
    if (res.status === 404) {
      showMessage(body, `No journal data recorded for ${date}.`);
      setPanelState("panel-journal", "ok", "ok");
      return;
    }
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
    select.replaceChildren(
      ...data.dates.map((d, i) => el("option", { text: d, attrs: { value: d, ...(i === 0 ? { selected: "selected" } : {}) } }))
    );
    await loadJournalDay(data.dates[0]);
  } catch (err) {
    select.replaceChildren(el("option", { text: "Unavailable", attrs: { value: "" } }));
    showMessage(document.querySelector("#panel-journal [data-body]"), `Could not load journal dates: ${err.message}`, true);
    setPanelState("panel-journal", "error", "unreachable");
  }
}

document.getElementById("journal-date").addEventListener("change", (e) => loadJournalDay(e.target.value));

/* ---------------------------------------------------------------------- */
/* Search — Stage 5 forensic search over trades + agent_logs.             */
/* ---------------------------------------------------------------------- */

function searchHitsTable(hits, kind) {
  if (!hits.length) return null;
  if (kind === "trade") {
    const rows = hits.map((h) => {
      const tr = el("tr", { className: h.run_id ? "row-clickable" : "", attrs: { tabindex: h.run_id ? "0" : "-1" } }, [
        el("td", { text: fmtTime(h.timestamp) }),
        el("td", { text: h.symbol }),
        el("td", {}, [pill(h.action)]),
        el("td", { text: h.run_id || "—" }),
        el("td", { text: h.reasoning || "—", title: h.reasoning || "" }),
      ]);
      if (h.run_id) {
        tr.addEventListener("click", () => openRunDetail(h.run_id));
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openRunDetail(h.run_id); });
      }
      return tr;
    });
    return table(["Time", "Symbol", "Action", "Run", "Reasoning"], rows);
  }
  const rows = hits.map((h) => {
    const tr = el("tr", { className: h.run_id ? "row-clickable" : "", attrs: { tabindex: h.run_id ? "0" : "-1" } }, [
      el("td", { text: fmtTime(h.timestamp) }),
      el("td", { text: h.agent_name }),
      el("td", { text: h.model || "—" }),
      el("td", { text: h.run_id || "—" }),
      el("td", { text: h.output_summary || "—", title: h.output_summary || "" }),
    ]);
    if (h.run_id) {
      tr.addEventListener("click", () => openRunDetail(h.run_id));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openRunDetail(h.run_id); });
    }
    return tr;
  });
  return table(["Time", "Agent", "Model", "Run", "Summary"], rows);
}

async function runSearch() {
  const body = document.querySelector("#panel-search [data-body]");
  const q = document.getElementById("search-input").value.trim();
  if (!q) {
    showMessage(body, "Type a search term above.");
    setPanelState("panel-search", "ok", "ok");
    return;
  }
  showMessage(body, `Searching for "${q}"…`);
  try {
    const data = await fetchJSON(`/search?q=${encodeURIComponent(q)}&limit=50`);
    const tradesTable = searchHitsTable(data.trades, "trade");
    const agentTable = searchHitsTable(data.agent_logs, "agent_log");
    if (!tradesTable && !agentTable) {
      showMessage(body, `No matches for "${q}".`);
      setPanelState("panel-search", "ok", "ok");
      return;
    }
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
document.getElementById("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});

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
loadRuns();
loadJournalDates();
setInterval(() => {
  if (!document.hidden) refreshAll();
}, REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});
