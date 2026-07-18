"use strict";

const $ = (selector) => document.querySelector(selector);
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

function badge(label, tone = "neutral") {
  return `<span class="badge ${tone}">${escapeHtml(label)}</span>`;
}

function setStatusCard(id, title, main, detail, tone) {
  $(id).className = `status-card ${tone}`;
  $(id).innerHTML = `<div class="status-top"><span>${escapeHtml(title)}</span><i class="status-dot"></i></div>
    <strong>${escapeHtml(main)}</strong><small>${escapeHtml(detail)}</small>`;
}

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
  const candles = candlePayload.candles || [];
  const services = data.services || [];
  const collector = services.find((service) => service.service_name === "collector_scheduler");
  const tradingEnabled = hands.status?.trading_enabled;

  const strip = $("#connection-strip");
  strip.className = `connection-strip ${hands.ok && db.ok ? "connected" : "failed"}`;
  strip.querySelector("span").textContent = hands.ok && db.ok
    ? `Sistem citibil · XTB ${hands.status?.environment || "—"} · actualizat ${timeLabel(data.generated_at, false)}`
    : `Atenție: ${!hands.ok ? "XTB deconectat" : ""} ${!db.ok ? "DB indisponibil" : ""}`;

  setStatusCard("#status-xtb", "XTB CoreAPI", hands.ok ? "Conectat" : "Deconectat",
    hands.status ? `${hands.status.environment} · ordine ${tradingEnabled === false ? "DEZACTIVATE" : tradingEnabled === true ? "ACTIVE" : "NECUNOSCUT"}` : (hands.error || "fără răspuns"),
    !hands.ok || tradingEnabled === true ? "bad" : tradingEnabled === false ? "ok" : "warn");
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

  renderAlerts(data.alerts || []);
  renderQuote(data.selection.symbol, quote, candlePayload, latestMarket, data.runtime.provider, data.timeline);
  renderMetrics(data.summary || {}, data.selection.run_id);
  renderPipeline(latest);
  renderServices(services);
  renderPipelineRuns(data.pipeline_runs || []);
  renderTimeline(data.timeline || []);
  renderOpenTrades(data.open_trades || []);
  renderRuns(data.runs || [], data.selection.run_id);
  renderLlm(data.llm_calls || []);

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
  const expectancy = summary.expectancy_r;
  const expNode = $("#metric-expectancy");
  expNode.textContent = expectancy === null || expectancy === undefined ? "—" : `${Number(expectancy) >= 0 ? "+" : ""}${n(expectancy, 3)}R`;
  expNode.className = Number(expectancy) >= 0 ? "positive" : "negative";
  $("#metric-cost").textContent = usd(summary.llm_cost_usd);
  $("#metric-llm-calls").textContent = `${integer(summary.llm_calls)} apeluri auditate`;
}

function renderServices(rows) {
  $("#services-count").textContent = rows.length;
  const body = $("#services-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="5" class="empty-row">Niciun serviciu nu a emis heartbeat.</td></tr>`;
  body.innerHTML = rows.map((row) => {
    const tone = row.status === "healthy" ? "ok" : row.status === "degraded" ? "warn" : row.status === "stopped" ? "neutral" : "bad";
    return `<tr>
      <td><span class="cell-main">${escapeHtml(row.service_name)}</span><span class="cell-sub">${escapeHtml(row.instance_id)}</span></td>
      <td>${badge(row.status, tone)}<span class="cell-sub">${escapeHtml(row.last_error || "")}</span></td>
      <td>${timeLabel(row.last_seen_at)}<span class="cell-sub">${ageLabel(row.last_seen_at)}</span></td>
      <td>${row.next_wake_at ? timeLabel(row.next_wake_at) : "—"}</td>
      <td>${escapeHtml(row.git_commit || "—")}</td>
    </tr>`;
  }).join("");
}

function renderPipelineRuns(rows) {
  $("#pipeline-runs-count").textContent = rows.length;
  const body = $("#pipeline-runs-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="5" class="empty-row">Niciun tick auditat.</td></tr>`;
  body.innerHTML = rows.slice(0, 30).map((row) => {
    const tone = row.status === "success" ? "ok" : row.status === "running" ? "warn" : row.status === "cancelled" ? "neutral" : "bad";
    return `<tr class="clickable" data-operation-id="${row.id}">
      <td>${timeLabel(row.started_at)}<span class="cell-sub">#${row.id}</span></td>
      <td><span class="cell-main">${escapeHtml(row.service_name)}</span><span class="cell-sub">${escapeHtml(row.run_kind)}</span></td>
      <td>${badge(row.status, tone)}<span class="cell-sub">${escapeHtml(row.error_type || "")}</span></td>
      <td>${integer(row.bars_processed)}</td>
      <td>${durationLabel(row.started_at, row.finished_at)}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("tr[data-operation-id]").forEach((tr) => tr.addEventListener("click", () => {
    const row = rows.find((item) => String(item.id) === tr.dataset.operationId);
    showDialog(`Execuție #${row.id}`, row);
  }));
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
  const body = $("#timeline-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="8" class="empty-row">Nicio bară pentru filtrul selectat.</td></tr>`;
  body.innerHTML = rows.map((row, index) => {
    const elig = row.eligible === true ? badge("eligibil", "ok") : row.eligible === false ? badge("blocat", "warn") : badge("—");
    const risk = row.risk_verdict === "approved" ? badge("aprobat", "ok") : row.risk_verdict === "rejected" ? badge("respins", "warn") : badge("—");
    const trade = row.trade_status ? badge(row.trade_status, row.trade_status === "closed" ? "neutral" : "warn") : badge("fără trade");
    const r = row.r_multiple === null || row.r_multiple === undefined ? "—" : `${Number(row.r_multiple) > 0 ? "+" : ""}${n(row.r_multiple, 3)}`;
    return `<tr class="clickable" data-timeline-index="${index}">
      <td><span class="cell-main">${escapeHtml(timeLabel(row.bar_close))}</span><span class="cell-sub">snap #${row.snapshot_id}</span></td>
      <td>${escapeHtml(n(row.price, 2))}<span class="cell-sub">spread ${n(row.spread_pct, 4)}%</span></td>
      <td><span class="cell-main">${escapeHtml(row.regime || "—")}</span><span class="cell-sub">${escapeHtml(row.confluence || "—")}</span></td>
      <td>${elig}<span class="cell-sub">${escapeHtml((row.eligibility_reasons || []).join(", "))}</span></td>
      <td><span class="cell-main">${escapeHtml(row.direction || "—")} ${row.confidence !== null ? n(row.confidence, 2) : ""}</span><span class="cell-sub">${escapeHtml(row.model || "—")}</span></td>
      <td>${risk}<span class="cell-sub">${escapeHtml(row.risk_reason || row.blocked_reason || "")}</span></td>
      <td>${trade}<span class="cell-sub">${escapeHtml(row.exit_reason || row.side || "")}</span></td>
      <td class="${Number(row.r_multiple) > 0 ? "positive" : Number(row.r_multiple) < 0 ? "negative" : ""}">${escapeHtml(r)}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("tr[data-timeline-index]").forEach((tr) => tr.addEventListener("click", () => {
    const row = rows[Number(tr.dataset.timelineIndex)];
    showDialog(`Snapshot #${row.snapshot_id}`, row);
  }));
}

function renderOpenTrades(rows) {
  $("#open-count").textContent = rows.length;
  const body = $("#open-trades-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="6" class="empty-row">Nicio poziție shadow deschisă.</td></tr>`;
  body.innerHTML = rows.map((row) => `<tr>
    <td><span class="cell-main">${escapeHtml(row.run_id || "—")}</span><span class="cell-sub">#${row.id} · ${escapeHtml(row.validity)}</span></td>
    <td>${badge(row.side, row.side === "buy" ? "ok" : "warn")}</td>
    <td>${n(row.entry_price, 2)}<span class="cell-sub">acum ${n(row.current_mid, 2)}</span></td>
    <td class="${Number(row.unrealized_r_gross) >= 0 ? "positive" : "negative"}">${row.unrealized_r_gross === null || row.unrealized_r_gross === undefined ? "—" : `${Number(row.unrealized_r_gross) >= 0 ? "+" : ""}${n(row.unrealized_r_gross, 3)}R`}<span class="cell-sub">indicativ, brut</span></td>
    <td>${n(row.sl_price, 2)} / ${n(row.tp_price, 2)}<span class="cell-sub">distanță ${n(row.distance_to_sl_pct, 3)}% / ${n(row.distance_to_tp_pct, 3)}%</span></td>
    <td>${row.timeout_at_estimate ? timeLabel(row.timeout_at_estimate) : "—"}<span class="cell-sub">deschis ${ageLabel(row.opened_at)}</span></td>
  </tr>`).join("");
}

function renderRuns(rows, selected) {
  $("#runs-count").textContent = rows.length;
  const body = $("#runs-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="6" class="empty-row">Niciun experiment persistat.</td></tr>`;
  body.innerHTML = rows.map((row) => `<tr class="clickable ${row.run_id === selected ? "selected" : ""}" data-run="${escapeHtml(row.run_id)}">
    <td><span class="cell-main">${escapeHtml(row.run_id)}</span><span class="cell-sub">${timeLabel(row.last_decision || row.last_trade)}</span></td>
    <td>${badge(row.validity, row.validity === "verified" ? "ok" : "warn")}<span class="cell-sub">${escapeHtml(row.run_kind)} · ${escapeHtml(row.validity_reason || "")}</span></td>
    <td>${escapeHtml(row.model || "—")}</td><td>${integer(row.decisions)}</td>
    <td>${integer(row.trades)}<span class="cell-sub">${integer(row.open_trades)} open</span></td>
    <td class="${Number(row.expectancy_r) >= 0 ? "positive" : "negative"}">${row.expectancy_r === null ? "—" : `${n(row.expectancy_r, 3)}R`}</td>
  </tr>`).join("");
  body.querySelectorAll("tr[data-run]").forEach((tr) => tr.addEventListener("click", () => {
    $("#run-filter").value = tr.dataset.run;
    loadState();
  }));
}

function renderLlm(rows) {
  const body = $("#llm-body");
  if (!rows.length) return body.innerHTML = `<tr><td colspan="8" class="empty-row">Niciun apel LLM auditat pentru simbolul selectat.</td></tr>`;
  body.innerHTML = rows.map((row) => `<tr>
    <td>${timeLabel(row.ts)}</td><td><span class="cell-main">${escapeHtml(row.requested_model)}</span><span class="cell-sub">${escapeHtml(row.effective_model || "—")}</span></td>
    <td>${row.ok ? badge("OK", "ok") : badge(row.error || "FAIL", "bad")}</td>
    <td>${integer(row.input_tokens)} / ${integer(row.output_tokens)}</td>
    <td>${integer(row.cache_read_tokens)} / ${integer(row.cache_creation_tokens)}</td>
    <td>${integer(row.retry_count)}</td><td>${integer(row.latency_ms)} ms</td><td>${usd(row.estimated_cost_usd)}</td>
  </tr>`).join("");
}

function populateRunFilter(runs, selected) {
  const select = $("#run-filter");
  const current = selected || select.value;
  select.innerHTML = `<option value="">Toate run-urile</option>` + runs.map((row) =>
    `<option value="${escapeHtml(row.run_id)}">${escapeHtml(row.run_id)}</option>`).join("");
  if ([...select.options].some((option) => option.value === current)) select.value = current;
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

loadState();
restartTimer();
