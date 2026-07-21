"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const state = { data: null, timer: null, loading: false };

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

const n = (value, digits = 2) => value === null || value === undefined
  ? "—"
  : Number(value).toLocaleString("ro-RO", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const integer = (value) => Number(value || 0).toLocaleString("ro-RO");
const pct = (value) => value === null || value === undefined ? "—" : `${n(Number(value) * 100, 1)}%`;
const usd = (value) => `$${n(Number(value || 0), 4)}`;
const sub = (text) => `<span class="cell-sub">${escapeHtml(text ?? "")}</span>`;

function timeLabel(value, withDate = true) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("ro-RO", {
    ...(withDate ? { day: "2-digit", month: "short" } : {}),
    hour: "2-digit", minute: "2-digit", second: withDate ? undefined : "2-digit",
    timeZone: "Europe/Bucharest",
  }).format(d);
}

function ageLabel(value) {
  if (!value) return "fără date";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)} sec în urmă`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min în urmă`;
  if (seconds < 86400) return `${n(seconds / 3600, 1)} h în urmă`;
  return `${n(seconds / 86400, 1)} zile în urmă`;
}

function durationLabel(start, end) {
  if (!start) return "—";
  const seconds = Math.max(0, ((end ? new Date(end) : new Date()).getTime() - new Date(start).getTime()) / 1000);
  if (seconds < 60) return `${n(seconds, 1)}s`;
  return `${n(seconds / 60, 1)}m`;
}

const badge = (label, tone = "neutral") => `<span class="badge ${tone}">${escapeHtml(label)}</span>`;

function setStatusCard(id, title, main, detail, tone) {
  const el = $(id);
  el.className = `status-card ${tone}`;
  el.innerHTML = `<div class="status-top"><span>${escapeHtml(title)}</span><i class="status-dot"></i></div>
    <strong>${escapeHtml(main)}</strong><small>${escapeHtml(detail)}</small>`;
}

function setChip(id, label, value, tone) {
  const el = $(id);
  el.className = `chip ${tone}`;
  el.innerHTML = `<span class="chip-label">${escapeHtml(label)}</span><span class="chip-value">${escapeHtml(value)}</span>`;
}

/** Generic table filler: cols is an array of row->html cell functions. */
function fillTable(bodyId, rows, cols, emptyMsg, onRow) {
  const body = $(bodyId);
  if (!rows || !rows.length) {
    body.innerHTML = `<tr><td colspan="${cols.length}" class="empty-row">${escapeHtml(emptyMsg)}</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r, i) => `<tr${onRow ? ` class="clickable" data-row="${i}"` : ""}>${
    cols.map((c) => `<td>${c(r)}</td>`).join("")}</tr>`).join("");
  if (onRow) body.querySelectorAll("tr[data-row]").forEach((tr) =>
    tr.addEventListener("click", () => onRow(rows[Number(tr.dataset.row)])));
}

function setCount(id, value) { const el = $(id); if (el) el.textContent = value; }

// ---------------------------------------------------------------- tabs ------
const TABS = $$(".tab").map((t) => t.dataset.tab);

function activateTab(name) {
  // Never let an unknown name (stale/tampered localStorage, bad deep link) hide EVERY panel.
  if (!TABS.includes(name)) name = TABS[0];
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;          // roving tabindex: one stop, arrows move within
  });
  $$(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  try { localStorage.setItem("brain-tab", name); } catch { /* ignore */ }
}

$$(".tab").forEach((t, i) => {
  t.addEventListener("click", () => activateTab(t.dataset.tab));
  t.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = (i + step + TABS.length) % TABS.length;
    activateTab(TABS[next]);
    $$(".tab")[next].focus();
  });
});
$$("[data-goto]").forEach((el) => el.addEventListener("click", () => activateTab(el.dataset.goto)));

// ---------------------------------------------------------------- load ------
async function loadState() {
  if (state.loading) return;
  state.loading = true;
  $("#refresh-button").classList.add("loading");
  const symbol = $("#symbol-input").value.trim() || "GOLD";
  const run = $("#run-filter").value;
  const query = new URLSearchParams({ symbol, limit: "80" });
  if (run) query.set("run_id", run);
  try {
    const response = await fetch(`/api/state?${query}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    render(state.data);
  } catch (error) {
    const strip = $("#connection-strip");
    strip.className = "connection-strip failed";
    strip.querySelector("span").textContent = `Dashboard indisponibil: ${error.message}`;
  } finally {
    state.loading = false;
    $("#refresh-button").classList.remove("loading");
  }
}

function render(data) {
  const hands = data.health.trading_hands || {};
  const db = data.health.database || {};
  const git = data.runtime.git || {};
  const latestMarket = data.latest.market || {};
  const latest = data.latest.pipeline || {};
  const quote = hands.quote || {};
  const candlePayload = hands.candles || {};
  const services = data.services || [];
  const collector = services.find((s) => s.service_name === "collector_scheduler");
  const tradingEnabled = hands.status?.trading_enabled;
  const alerts = data.alerts || [];
  const criticals = alerts.filter((a) => a.severity === "critical").length;

  // health strip
  const strip = $("#connection-strip");
  strip.className = `connection-strip ${hands.ok && db.ok ? "connected" : "failed"}`;
  strip.querySelector("span").textContent = hands.ok && db.ok
    ? `Sistem citibil · XTB ${hands.status?.environment || "—"} · actualizat ${timeLabel(data.generated_at, false)}`
    : `Atenție: ${!hands.ok ? "XTB deconectat" : ""} ${!db.ok ? "DB indisponibil" : ""}`;
  // A live session does NOT mean a working feed: show DEGRADED when quote/OHLCV are broken.
  const feedDegraded = hands.session_ok && (!hands.quote_ok || !hands.candles_ok);
  setChip("#chip-xtb", "XTB",
    !hands.session_ok ? "deconectat"
      : tradingEnabled === true ? "ORDINE ACTIVE"
      : feedDegraded ? "feed degradat" : "conectat",
    !hands.session_ok || tradingEnabled === true ? "bad" : feedDegraded ? "warn" : "ok");
  setChip("#chip-market", "Piață", data.runtime.market_open === true ? "deschisă"
    : data.runtime.market_open === false ? "închisă" : "necunoscută",
    data.runtime.market_open === true ? "ok" : "neutral");
  setChip("#chip-brain", "Brain", collector ? String(collector.status) : "oprit",
    collector?.status === "healthy" ? "ok" : collector?.status === "degraded" ? "warn" : "bad");
  setChip("#chip-db", "DB", db.ok ? (db.schema_version || "ok") : "indisponibil",
    db.ok && db.schema_current ? "ok" : "bad");
  setChip("#chip-alerts", "Alerte", criticals ? `${criticals} critice` : `${alerts.length}`,
    criticals ? "bad" : alerts.length ? "warn" : "ok");

  // detailed status cards (Prezentare)
  setStatusCard("#status-xtb", "XTB CoreAPI",
    !hands.session_ok ? "Deconectat" : feedDegraded ? "Feed degradat" : "Conectat",
    hands.status
      ? `${hands.status.environment} · ordine ${tradingEnabled === false ? "DEZACTIVATE" : tradingEnabled === true ? "ACTIVE" : "NECUNOSCUT"}`
        + (feedDegraded ? ` · ${!hands.quote_ok ? "fără quote" : ""}${!hands.quote_ok && !hands.candles_ok ? " + " : ""}${!hands.candles_ok ? "fără OHLCV" : ""}` : "")
      : (hands.error || "fără răspuns"),
    !hands.session_ok || tradingEnabled === true ? "bad" : feedDegraded ? "warn" : tradingEnabled === false ? "ok" : "warn");
  setStatusCard("#status-market", "Sesiune GOLD",
    data.runtime.market_open === true ? "Deschisă" : data.runtime.market_open === false ? "Închisă" : "Necunoscută",
    candlePayload.last_time_iso ? `bară ${ageLabel(candlePayload.last_time_iso)}` : "fără bară XTB",
    data.runtime.market_open === true ? "ok" : "neutral");
  setStatusCard("#status-brain", "Trading Brain",
    collector ? String(collector.status).toUpperCase() : "Nu rulează",
    collector ? `heartbeat ${ageLabel(collector.last_seen_at)}` : (latestMarket.bar_close ? `snapshot ${ageLabel(latestMarket.bar_close)}` : "fără telemetrie"),
    collector?.status === "healthy" ? "ok" : collector?.status === "degraded" ? "warn" : "bad");
  setStatusCard("#status-db", "PostgreSQL", db.ok ? "Disponibil" : "Indisponibil",
    db.ok ? `${db.schema_version} · ${db.latency_ms} ms` : (db.error || "eroare"),
    db.ok && db.schema_current ? "ok" : "bad");

  renderAlerts(alerts);
  renderQuote(data.selection.symbol, quote, candlePayload, latestMarket, data.runtime.provider, data.timeline);
  renderMetrics(data.summary || {}, data.selection.run_id);
  renderPipeline(latest);

  renderTimeline(data.timeline || []);
  renderOpenTrades(data.open_trades || []);
  renderQuarantined(data.quarantined_trades || []);
  renderClosedTrades(data.closed_trades || []);
  renderRuns(data.runs || [], data.selection.run_id);
  renderDatasets(data.datasets || []);
  renderPaid(data.runtime.paid_ai || {}, data.paid_spend || {}, data.paid_attempts || []);
  renderLlm(data.llm_calls || []);
  renderServices(services);
  renderPipelineRuns(data.pipeline_runs || []);
  renderReservations(data.reservations || []);
  renderDowntime(data.downtime_gaps || []);
  renderProcessed(data.processed_bars || []);
  renderDbTables(data.db_tables || [], db);
  renderConflicts(data.snapshot_conflicts || []);

  // tab counters
  setCount("#count-timeline", (data.timeline || []).length);
  setCount("#count-trades", (data.open_trades || []).length + (data.quarantined_trades || []).length);
  setCount("#count-runs", (data.runs || []).length);
  setCount("#count-paid", (data.paid_attempts || []).length);

  const gitState = git.state || (git.ok ? (git.dirty ? "dirty" : "clean") : "unknown");
  const gitLabel = { dirty: " · DIRTY", clean: " · clean", unknown: " · provenance UNKNOWN" }[gitState] || " · unknown";
  $("#footer-meta").textContent = `${git.branch || "—"}@${git.commit || "—"}${gitLabel} · DB ${db.schema_version || "—"}`;
  $("#show-latest-json").disabled = !latest.snapshot_id;
  populateRunFilter(data.runs || [], data.selection.run_id);
}

function renderAlerts(alerts) {
  const root = $("#alerts");
  if (!alerts.length) {
    root.innerHTML = `<div class="all-clear">✓ Nicio alertă operațională detectată</div>`;
    return;
  }
  root.innerHTML = alerts.map((a) => `<div class="alert ${escapeHtml(a.severity)}">
    <strong>${escapeHtml(a.code)}</strong><span>${escapeHtml(a.message)}</span></div>`).join("");
}

function renderQuote(symbol, quote, candles, latest, provider, timeline) {
  $("#market-title").textContent = symbol;
  $("#quote-bid").textContent = n(quote.bid, 2);
  $("#quote-ask").textContent = n(quote.ask, 2);
  const spread = quote.bid && quote.ask ? ((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2)) * 100 : null;
  $("#quote-spread").textContent = spread === null ? "—" : `${n(spread, 4)}%`;
  $("#quote-time").textContent = quote.time_iso ? `${timeLabel(quote.time_iso)} · ${ageLabel(quote.time_iso)}` : "quote indisponibil";
  $("#broker-bar").textContent = candles.last_time_iso ? `${timeLabel(candles.last_time_iso)} · ${ageLabel(candles.last_time_iso)}` : "—";
  $("#brain-bar").textContent = latest.bar_close ? `${timeLabel(latest.bar_close)} · ${ageLabel(latest.bar_close)}` : "—";
  $("#provider-name").textContent = `${provider || "—"}${latest.pipeline_version ? ` · v${latest.pipeline_version}` : ""}`;
  renderChart(timeline);
}

function renderChart(timeline) {
  const points = [...timeline].reverse().map((row) => Number(row.price)).filter(Number.isFinite);
  const root = $("#price-chart");
  if (points.length < 2) {
    root.innerHTML = `<div class="empty-row">Nu există suficiente snapshoturi pentru grafic.</div>`;
    return;
  }
  const width = 800, height = 120, pad = 5;
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const xy = points.map((value, index) => [
    pad + index * ((width - pad * 2) / (points.length - 1)),
    height - pad - ((value - min) / span) * (height - pad * 2),
  ]);
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${pad},${height} ${line} ${width - pad},${height}`;
  root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img">
    <defs><linearGradient id="area-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#52e6d0" stop-opacity=".22"/><stop offset="1" stop-color="#52e6d0" stop-opacity="0"/></linearGradient></defs>
    <line class="chart-grid" x1="0" y1="30" x2="800" y2="30"/><line class="chart-grid" x1="0" y1="60" x2="800" y2="60"/><line class="chart-grid" x1="0" y1="90" x2="800" y2="90"/>
    <polygon class="chart-area" points="${area}"/><polyline class="chart-line" points="${line}"/>
  </svg>`;
}

function renderMetrics(summary, runId) {
  $("#metrics-scope").textContent = runId
    ? `${summary.selected_run_validity === "verified" ? "VERIFICAT" : "NEVERIFICAT"} · ${runId}`
    : "DOAR RUN-URI VERIFICATE";
  $("#metric-decisions").textContent = integer(summary.decisions);
  $("#metric-trades").textContent = integer(summary.trades_total);
  $("#metric-open").textContent = integer(summary.trades_open);
  $("#metric-win").textContent = pct(summary.win_rate);
  // Quarantined trades were NEVER reconciled -> they are not "closed" and are not in the win-rate
  // denominator. Shown separately so the number can't be mistaken for a result.
  $("#metric-win-basis").textContent = `din ${integer(summary.trades_closed)} închise`;
  $("#metric-closed").textContent = integer(summary.trades_closed);
  const q = $("#metric-quarantined");
  q.textContent = integer(summary.trades_quarantined);
  q.className = Number(summary.trades_quarantined || 0) > 0 ? "negative" : "";
  const expectancy = summary.expectancy_r;
  const expNode = $("#metric-expectancy");
  expNode.textContent = expectancy === null || expectancy === undefined ? "—" : `${Number(expectancy) >= 0 ? "+" : ""}${n(expectancy, 3)}R`;
  expNode.className = Number(expectancy) >= 0 ? "positive" : "negative";
  $("#metric-cost").textContent = usd(summary.llm_cost_usd);
  $("#metric-llm-calls").textContent = `${integer(summary.llm_calls)} apeluri auditate`;
}

function step(index, title, main, detail, tone = "neutral") {
  return `<article class="pipeline-step ${tone}"><span class="step-index">0${index}</span>
    <h3>${escapeHtml(title)}</h3><strong>${escapeHtml(main)}</strong><p>${escapeHtml(detail)}</p></article>`;
}

function renderPipeline(row) {
  const root = $("#pipeline");
  if (!row || !row.snapshot_id) {
    root.innerHTML = `<div class="empty-row">Nu există o procesare pentru filtrul selectat.</div>`;
    $("#rationale").className = "rationale";
    return;
  }
  const eligibilityTone = row.eligible === true ? "ok" : row.eligible === false ? "warn" : "neutral";
  const riskTone = row.risk_verdict === "approved" ? "ok" : row.risk_verdict === "rejected" ? "warn" : "neutral";
  const tradeTone = row.trade_status === "closed" ? (Number(row.r_multiple) > 0 ? "ok" : "bad") : row.trade_status === "open" ? "warn" : "neutral";
  root.innerHTML = [
    step(1, "Bară închisă", `${timeLabel(row.bar_close)} · ${n(row.price, 2)}`, `${row.provider || "—"}:${row.provider_symbol || "—"}`, "ok"),
    step(2, "Features MTF", row.regime || "—", `${row.confluence || "—"} · ADX ${n(row.adx_h1, 1)}`, row.snapshot_id ? "ok" : "neutral"),
    step(3, "Eligibilitate", row.eligible === true ? "ELIGIBIL" : row.eligible === false ? "BLOCAT" : "NEEVALUAT", (row.eligibility_reasons || []).join(", ") || `feed lag ${n(row.feed_lag_seconds, 0)}s`, eligibilityTone),
    step(4, "Decizie", row.decision_id ? `${row.direction} · ${n(row.confidence, 2)}` : "FĂRĂ DECIZIE", row.model || "prefilter / lipsă", row.decision_id ? "ok" : "neutral"),
    step(5, "Risk & shadow", row.risk_verdict ? row.risk_verdict.toUpperCase() : "—", row.trade_id ? `${row.trade_status} · ${row.r_multiple === null ? "R pending" : `${n(row.r_multiple, 3)}R`}` : (row.risk_reason || "fără trade"), row.trade_id ? tradeTone : riskTone),
  ].join("");
  const rationale = $("#rationale");
  if (row.rationale) {
    rationale.className = "rationale visible";
    rationale.textContent = `Raționament model: ${row.rationale}`;
  } else {
    rationale.className = "rationale";
    rationale.textContent = "";
  }
}

function renderTimeline(rows) {
  fillTable("#timeline-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(timeLabel(r.bar_close))}</span>${sub(`snap #${r.snapshot_id}`)}`,
    (r) => `${escapeHtml(n(r.price, 2))}${sub(`spread ${n(r.spread_pct, 4)}%`)}`,
    (r) => `<span class="cell-main">${escapeHtml(r.regime || "—")}</span>${sub(r.confluence || "—")}`,
    (r) => `${r.eligible === true ? badge("eligibil", "ok") : r.eligible === false ? badge("blocat", "warn") : badge("—")}${sub((r.eligibility_reasons || []).join(", "))}`,
    (r) => `<span class="cell-main">${escapeHtml(r.direction || "—")} ${r.confidence !== null && r.confidence !== undefined ? n(r.confidence, 2) : ""}</span>${sub(r.model || "—")}`,
    (r) => `${r.risk_verdict === "approved" ? badge("aprobat", "ok") : r.risk_verdict === "rejected" ? badge("respins", "warn") : badge("—")}${sub(r.risk_reason || r.blocked_reason || "")}`,
    (r) => `${r.trade_status ? badge(r.trade_status, r.trade_status === "closed" ? "neutral" : "warn") : badge("fără trade")}${sub(r.exit_reason || r.side || "")}`,
    (r) => rCell(r.r_multiple),
  ], "Nicio bară pentru filtrul selectat.", (r) => showDialog(`Snapshot #${r.snapshot_id}`, r));
}

const rCell = (r) => {
  if (r === null || r === undefined) return "—";
  const cls = Number(r) > 0 ? "positive" : Number(r) < 0 ? "negative" : "";
  return `<span class="${cls}">${Number(r) > 0 ? "+" : ""}${n(r, 3)}</span>`;
};

function renderOpenTrades(rows) {
  setCount("#open-count", rows.length);
  fillTable("#open-trades-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(r.run_id || "—")}</span>${sub(`#${r.id} · ${r.validity}`)}`,
    (r) => badge(r.side, r.side === "buy" ? "ok" : "warn"),
    (r) => `${n(r.entry_price, 2)}${sub(`acum ${n(r.current_mid, 2)}`)}`,
    (r) => `${r.unrealized_r_gross === null || r.unrealized_r_gross === undefined ? "—" : `<span class="${Number(r.unrealized_r_gross) >= 0 ? "positive" : "negative"}">${Number(r.unrealized_r_gross) >= 0 ? "+" : ""}${n(r.unrealized_r_gross, 3)}R</span>`}${sub("indicativ, brut")}`,
    (r) => `${n(r.sl_price, 2)} / ${n(r.tp_price, 2)}${sub(`distanță ${n(r.distance_to_sl_pct, 3)}% / ${n(r.distance_to_tp_pct, 3)}%`)}`,
    (r) => `${r.timeout_at_estimate ? timeLabel(r.timeout_at_estimate) : "—"}${sub(`deschis ${ageLabel(r.opened_at)}`)}`,
  ], "Nicio poziție shadow deschisă.");
}

function renderQuarantined(rows) {
  setCount("#quarantined-count", rows.length);
  fillTable("#quarantined-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(r.run_id || "—")}</span>${sub(`#${r.id}`)}`,
    (r) => badge(r.side, r.side === "buy" ? "ok" : "warn"),
    (r) => n(r.entry_price, 2),
    (r) => badge(r.data_provider || "—", "warn"),
    (r) => `<span class="cell-sub wrap">${escapeHtml(r.quarantine_reason || "")}</span>`,
    (r) => `${timeLabel(r.quarantined_at)}${sub(ageLabel(r.quarantined_at))}`,
  ], "Niciun trade în carantină.");
}

function renderClosedTrades(rows) {
  setCount("#closed-count", rows.length);
  fillTable("#closed-trades-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(r.run_id || "—")}</span>${sub(`#${r.id}`)}`,
    (r) => badge(r.side, r.side === "buy" ? "ok" : "warn"),
    (r) => `${n(r.entry_price, 2)} → ${n(r.exit_price, 2)}`,
    (r) => `${badge(r.status, r.status === "expired" ? "neutral" : "ok")}${sub(r.exit_reason || "")}${r.ambiguous ? sub("ambiguu") : ""}`,
    (r) => rCell(r.r_multiple),
    (r) => `${timeLabel(r.closed_at)}${sub(ageLabel(r.closed_at))}`,
  ], "Niciun trade închis pentru filtrul selectat.");
}

function renderRuns(rows, selected) {
  setCount("#runs-count", rows.length);
  const body = $("#runs-body");
  if (!rows.length) { body.innerHTML = `<tr><td colspan="6" class="empty-row">Niciun experiment persistat.</td></tr>`; return; }
  body.innerHTML = rows.map((row) => `<tr class="clickable ${row.run_id === selected ? "selected" : ""}" data-run="${escapeHtml(row.run_id)}">
    <td><span class="cell-main">${escapeHtml(row.run_id)}</span>${sub(timeLabel(row.last_decision || row.last_trade))}</td>
    <td>${badge(row.validity, row.validity === "verified" ? "ok" : "warn")}${sub(`${row.run_kind} · ${row.validity_reason || ""}`)}</td>
    <td>${escapeHtml(row.model || "—")}</td><td>${integer(row.decisions)}</td>
    <td>${integer(row.trades)}${sub(`${integer(row.open_trades)} open`)}</td>
    <td>${rCell(row.expectancy_r)}</td></tr>`).join("");
  body.querySelectorAll("tr[data-run]").forEach((tr) => tr.addEventListener("click", () => {
    $("#run-filter").value = tr.dataset.run;
    loadState();
  }));
}

function renderDatasets(rows) {
  setCount("#datasets-count", rows.length);
  fillTable("#datasets-body", rows, [
    (r) => `<span class="cell-main mono">${escapeHtml(r.dataset_id)}</span>${sub(`v${r.pipeline_version || "?"}`)}`,
    (r) => `<span class="cell-main">${escapeHtml(r.provider || "—")}</span>${sub(r.source || r.provider_symbol || "")}`,
    (r) => escapeHtml((r.timeframes || []).join(", ")),
    (r) => escapeHtml(Object.values(r.bar_counts || {}).reduce((a, b) => a + Number(b), 0) || "—"),
    (r) => `${timeLabel(r.first_bar)}${sub(`→ ${timeLabel(r.last_bar)}`)}`,
    (r) => `${timeLabel(r.frozen_at)}${sub(ageLabel(r.frozen_at))}`,
  ], "Niciun dataset înghețat.", (r) => showDialog(`Dataset ${r.dataset_id}`, r));
}

function renderPaid(config, spend, attempts) {
  const budgetCard = (id, title, spent, budget) => {
    const over = budget > 0 && spent > budget;
    setStatusCard(id, title, usd(spent),
      budget > 0 ? `din $${n(budget, 2)}` : "buget 0 (nimic permis)",
      over ? "bad" : budget > 0 && spent > 0 ? "warn" : "ok");
  };
  budgetCard("#budget-run", "Spend run", Number(spend.run || 0), Number(config.budget_run || 0));
  budgetCard("#budget-day", "Spend azi (UTC)", Number(spend.day || 0), Number(config.budget_day || 0));
  budgetCard("#budget-month", "Spend lună (UTC)", Number(spend.month || 0), Number(config.budget_month || 0));
  setStatusCard("#budget-orphans", "Paid AI",
    config.enabled ? "ACTIV" : "OPRIT",
    `${integer(spend.orphans || 0)} orfani · ${integer(spend.unreconciled || 0)} nereconciliate · model ${config.model || "—"}`,
    config.enabled ? (Number(spend.orphans || 0) ? "bad" : "warn") : "ok");

  setCount("#paid-count", attempts.length);
  fillTable("#paid-body", attempts, [
    (r) => `${timeLabel(r.started_at)}${sub(ageLabel(r.started_at))}`,
    (r) => `<span class="cell-main">${escapeHtml(r.run_id || "—")}</span>${sub(r.context || "")}`,
    (r) => escapeHtml(r.model || "—"),
    (r) => badge(r.status, r.status === "completed" ? "ok" : r.status === "started" ? "warn"
      : r.status === "timeout" || r.status === "unknown" ? "warn" : "bad"),
    (r) => `${integer(r.input_tokens)} / ${integer(r.output_tokens)}`,
    (r) => r.actual_cost_usd != null ? usd(r.actual_cost_usd) : `${sub(`est ${usd(r.est_cost_usd)}`)}`,
    (r) => r.reconciled_console ? badge("da", "ok") : r.status === "completed" ? badge("de reconciliat", "warn") : "—",
  ], "Niciun apel plătit înregistrat.", (r) => showDialog(`Paid attempt #${r.id}`, r));
}

function renderLlm(rows) {
  fillTable("#llm-body", rows, [
    (r) => timeLabel(r.ts),
    (r) => `<span class="cell-main">${escapeHtml(r.requested_model)}</span>${sub(r.effective_model || "—")}`,
    (r) => r.ok ? badge("OK", "ok") : badge(r.error || "FAIL", "bad"),
    (r) => `${integer(r.input_tokens)} / ${integer(r.output_tokens)}`,
    (r) => `${integer(r.cache_read_tokens)} / ${integer(r.cache_creation_tokens)}`,
    (r) => integer(r.retry_count),
    (r) => `${integer(r.latency_ms)} ms`,
    (r) => usd(r.estimated_cost_usd),
  ], "Niciun apel LLM auditat pentru simbolul selectat.");
}

function renderServices(rows) {
  setCount("#services-count", rows.length);
  fillTable("#services-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(r.service_name)}</span>${sub(r.instance_id)}`,
    (r) => `${badge(r.status, r.status === "healthy" ? "ok" : r.status === "degraded" ? "warn" : r.status === "stopped" ? "neutral" : "bad")}${sub(r.last_error || "")}`,
    (r) => `${timeLabel(r.last_seen_at)}${sub(ageLabel(r.last_seen_at))}`,
    (r) => r.next_wake_at ? timeLabel(r.next_wake_at) : "—",
    (r) => escapeHtml(r.git_commit || "—"),
  ], "Niciun serviciu nu a emis heartbeat.");
}

function renderPipelineRuns(rows) {
  setCount("#pipeline-runs-count", rows.length);
  fillTable("#pipeline-runs-body", rows.slice(0, 40), [
    (r) => `${timeLabel(r.started_at)}${sub(`#${r.id}`)}`,
    (r) => `<span class="cell-main">${escapeHtml(r.service_name)}</span>${sub(r.run_kind)}`,
    (r) => `${badge(r.status, r.status === "success" ? "ok" : r.status === "running" ? "warn" : r.status === "cancelled" ? "neutral" : "bad")}${sub(r.error_type || "")}`,
    (r) => integer(r.bars_processed),
    (r) => durationLabel(r.started_at, r.finished_at),
  ], "Niciun tick auditat.", (r) => showDialog(`Execuție #${r.id}`, r));
}

function renderReservations(rows) {
  setCount("#reservations-count", rows.length);
  fillTable("#reservations-body", rows, [
    (r) => escapeHtml(r.run_id || "—"),
    (r) => badge(r.status, r.status === "in_progress" ? "warn" : "neutral"),
    (r) => escapeHtml(r.worker || "—"),
    (r) => timeLabel(r.reserved_at),
    (r) => `${timeLabel(r.lease_expires_at)}${sub(ageLabel(r.lease_expires_at))}`,
  ], "Nicio rezervare activă.");
}

function renderDowntime(rows) {
  setCount("#downtime-count", rows.length);
  fillTable("#downtime-body", rows, [
    (r) => `<span class="cell-main">${escapeHtml(timeLabel(r.resumed_bar_close))}</span>${sub(`de la ${timeLabel(r.prev_bar_close)}`)}`,
    (r) => `<span class="cell-main">${integer(r.missed_bars)}</span>`,
    (r) => badge(r.policy || "—", "neutral"),
    (r) => `${escapeHtml(r.prev_run_id || "—")}${sub(`→ ${r.run_id || "—"}`)}`,
    (r) => `${timeLabel(r.detected_at)}${sub(ageLabel(r.detected_at))}`,
  ], "Niciun gol de downtime înregistrat.");
}

function renderProcessed(rows) {
  setCount("#processed-count", rows.length);
  fillTable("#processed-body", rows, [
    (r) => escapeHtml(timeLabel(r.bar_close)),
    (r) => escapeHtml(r.provider || "—"),
    (r) => escapeHtml(r.run_id || "—"),
    (r) => badge(r.outcome, r.outcome === "decided" ? "ok" : r.ok ? "neutral" : "bad"),
    (r) => r.ok ? badge("da", "ok") : badge("nu", "bad"),
    (r) => `${timeLabel(r.processed_at)}${sub(ageLabel(r.processed_at))}`,
  ], "Nicio bară procesată înregistrată.");
}

function renderDbTables(rows, db) {
  $("#db-schema").textContent = db.ok ? `${db.schema_version || "—"} · ${db.tables || 0} tabele` : "DB indisponibil";
  fillTable("#db-tables-body", rows, [
    (r) => `<span class="cell-main mono">${escapeHtml(r.name)}</span>`,
    (r) => integer(r.approx_rows),
  ], "Fără tabele.");
}

function renderConflicts(rows) {
  setCount("#conflicts-count", rows.length);
  fillTable("#conflicts-body", rows, [
    (r) => escapeHtml(timeLabel(r.bar_close)),
    (r) => `${escapeHtml(r.existing_provider || "—")}${sub(r.existing_pipeline_version || "")}`,
    (r) => `${escapeHtml(r.incoming_provider || "—")}${sub(r.incoming_pipeline_version || "")}`,
    (r) => `${timeLabel(r.ts)}${sub(ageLabel(r.ts))}`,
  ], "Niciun conflict de snapshot.");
}

function populateRunFilter(runs, selected) {
  const select = $("#run-filter");
  const current = selected || select.value;
  const ids = runs.map((r) => r.run_id);
  // The run list is capped (LIMIT), so an OLDER selected run may not be in it. Re-add it explicitly
  // instead of letting the filter silently fall back to "all runs" on the next refresh.
  const options = [...ids];
  if (current && !ids.includes(current)) options.unshift(current);
  select.innerHTML = `<option value="">Toate run-urile</option>` + options.map((id) =>
    `<option value="${escapeHtml(id)}">${escapeHtml(id)}${ids.includes(id) ? "" : " (selectat)"}</option>`).join("");
  if (current) select.value = current;
}

function showDialog(title, payload) {
  $("#dialog-title").textContent = title;
  $("#dialog-content").textContent = JSON.stringify(payload, null, 2);
  $("#detail-dialog").showModal();
}

function restartTimer() {
  clearInterval(state.timer);
  if ($("#auto-refresh").checked) state.timer = setInterval(loadState, 10_000);
}

$("#refresh-button").addEventListener("click", loadState);
$("#run-filter").addEventListener("change", loadState);
$("#symbol-input").addEventListener("keydown", (event) => { if (event.key === "Enter") loadState(); });
$("#auto-refresh").addEventListener("change", restartTimer);
$("#show-latest-json").addEventListener("click", () => state.data?.latest?.pipeline && showDialog("Ultima procesare", state.data.latest.pipeline));
$("#close-dialog").addEventListener("click", () => $("#detail-dialog").close());
$("#detail-dialog").addEventListener("click", (event) => { if (event.target === $("#detail-dialog")) $("#detail-dialog").close(); });

try { const saved = localStorage.getItem("brain-tab"); if (saved) activateTab(saved); } catch { /* ignore */ }
loadState();
restartTimer();
