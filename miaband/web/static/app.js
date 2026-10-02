"use strict";
// miA-band dashboard: reads /api, never computes metrics itself.

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const state = { date: null, days: 28, range: null, charts: {} };

const fmtHM = (m) => (m == null ? "–" : `${Math.floor(m / 60)}h${String(Math.round(m % 60)).padStart(2, "0")}`);
const fmtClock = (m) => (m == null ? "–" : `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(Math.round(m % 60)).padStart(2, "0")}`);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (v, d = 0) => (v == null ? "–" : Number(v).toFixed(d));
const signed = (v, d = 0) => (v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d));

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
}

// ---------------------------------------------------------------- day cards
function metric(m, name) { return m[name] || { value: null, status: "missing", reason: null }; }
function okVal(m, name) { const x = metric(m, name); return x.status === "ok" ? x.value : null; }

function kv(el, pairs) {
  el.innerHTML = pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderDay(d) {
  const m = d.metrics;
  // Recovery
  const rec = metric(m, "recovery.score");
  const band = $("rec-band");
  if (rec.status === "ok") {
    $("rec-score").textContent = fmt(rec.value);
    const b = rec.value <= 33 ? ["low", "--critical"] : rec.value >= 67 ? ["high", "--good"] : ["moderate", "--warning"];
    band.textContent = b[0];
    band.style.setProperty("--dot", css(b[1]));
    $("rec-reason").textContent = "score = 50 + component points";
    const parts = [["Resting HR", okVal(m, "recovery.pts_rhr"), okVal(m, "recovery.z_rhr")],
                   ["Sleep", okVal(m, "recovery.pts_sleep"), okVal(m, "recovery.z_sleep")]];
    $("rec-parts").innerHTML = parts.map(([name, pts, z]) => {
      const w = Math.min(Math.abs(pts || 0), 50);
      const left = pts >= 0 ? 50 : 50 - w;
      const color = pts >= 0 ? css("--pos") : css("--neg");
      return `<div class="part" title="z ${signed(z, 2)} vs your 28-day baseline">
        <span>${name}</span>
        <span class="track"><span class="fill" style="left:${left}%;width:${w}%;background:${color}"></span></span>
        <span class="val">${signed(pts)} pts</span></div>`;
    }).join("");
  } else {
    $("rec-score").textContent = "–";
    band.textContent = rec.status.replace("_", " ");
    band.style.setProperty("--dot", "transparent");
    $("rec-parts").innerHTML = "";
    $("rec-reason").textContent = rec.reason || "";
  }
  // Readiness
  const rd = metric(m, "readiness.rule");
  $("readiness").textContent = rd.reason || "–";
  // Sleep
  const tst = metric(m, "sleep.tst_min");
  $("sleep-tst").textContent = tst.status === "ok" ? fmtHM(tst.value) : "–";
  const stages = metric(m, "sleep.rem_min").status === "ok"
    ? `${fmt(okVal(m, "sleep.deep_min"))} / ${fmt(okVal(m, "sleep.light_min"))} / ${fmt(okVal(m, "sleep.rem_min"))} min`
    : "not available";
  kv($("sleep-kv"), tst.status === "ok" ? [
    ["Bed – wake", `${fmtClock(okVal(m, "sleep.onset_clock"))} – ${fmtClock(okVal(m, "sleep.wake_clock"))}`],
    ["Baseline", `${fmtHM(okVal(m, "tst.base28"))} ± ${fmt(okVal(m, "tst.spread28"))} min`],
    ["SME", `${fmt((okVal(m, "sleep.sme") || 0) * 100)} %`],
    ["Deep / light / REM", stages],
    ["Nap", `${fmt(okVal(m, "sleep.nap_min"))} min`],
    ["SRI 7 d", fmt(okVal(m, "sleep.sri_7d"))],
  ] : [["", tst.reason || "no data"]]);
  // RHR
  const rhr = metric(m, "phys.rhr");
  $("rhr").textContent = rhr.status === "ok" ? fmt(rhr.value, 1) : "–";
  const z = metric(m, "rhr.z");
  kv($("rhr-kv"), rhr.status === "ok" ? [
    ["Baseline", `${fmt(okVal(m, "rhr.base28"), 1)} ± ${fmt(okVal(m, "rhr.spread28"), 1)}`],
    ["z", z.status === "ok" ? signed(z.value, 2) + (okVal(m, "rhr.flag") ? " (flagged)" : "") : z.status.replace("_", " ")],
    ["7-day mean", fmt(okVal(m, "rhr.mean7"), 1)],
  ] : [["", rhr.reason || "no data"]]);
  // Load
  const load = metric(m, "strain.load");
  $("load").textContent = load.status === "ok" ? fmt(load.value) : "–";
  const acwr = metric(m, "acwr.ewma");
  const n = metric(m, "strain.n_workouts");
  kv($("load-kv"), [
    ["Strain", load.status === "ok" ? `${fmt(okVal(m, "strain.strain"), 1)} / 21` : "–"],
    ["Sessions", `${fmt(n.value)}${n.reason ? " (" + n.reason + ")" : ""}`],
    ["ACWR", acwr.status === "ok" ? fmt(acwr.value, 2) : (acwr.reason || acwr.status)],
    ...(load.status !== "ok" || load.reason ? [["Note", load.reason || ""]] : []),
  ]);
}

// ---------------------------------------------------------------- charts
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.animation = false;

function baseOptions(yTitle, extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, spanGaps: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: {} },
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: css("--text-muted"), maxRotation: 0, autoSkipPadding: 16 } },
      y: { title: { display: !!yTitle, text: yTitle, color: css("--text-muted") },
           grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--text-muted") }, ...extra },
    },
  };
}

function line(label, data, color, more = {}) {
  return { type: "line", label, data, borderColor: color, backgroundColor: color, borderWidth: 2,
           pointRadius: 0, pointHoverRadius: 5, tension: 0, ...more };
}

function constLine(label, n, y) {
  return line(label, Array(n).fill(y), css("--reference"), { borderWidth: 1, borderDash: [4, 4], pointHoverRadius: 0 });
}

function gapsNote(id, s) {
  const reasons = {};
  s.status.forEach((st, i) => { if (st && st !== "ok") reasons[s.reason[i] || st] = (reasons[s.reason[i] || st] || 0) + 1; });
  const missing = s.status.filter((st) => st == null).length;
  const parts = Object.entries(reasons).map(([r, c]) => `${c} d: ${r}`);
  if (missing) parts.push(`${missing} d: no data`);
  $(id).textContent = parts.length ? "Gaps — " + parts.join(" · ") : "";
}

function draw(id, config) {
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart($(id), config);
}

function okSeries(s) { return s.value.map((v, i) => (s.status[i] === "ok" ? v : null)); }

function renderCharts(res) {
  const labels = res.dates.map((d) => d.slice(5));
  const S = res.series;
  const n = labels.length;
  const blue = css("--series-1");
  const sel = res.dates.indexOf(state.date);
  const highlight = (data) => data.map((_, i) => (i === sel ? 5 : 0));

  const rec = okSeries(S["recovery.score"]);
  draw("c-recovery", { data: { labels, datasets: [
      line("Recovery", rec, blue, { pointRadius: highlight(rec) }),
      constLine("33", n, 33), constLine("67", n, 67)] },
    options: { ...baseOptions("", { min: 0, max: 100 }),
      plugins: { legend: { display: false }, tooltip: { filter: (c) => c.datasetIndex === 0 } } } });
  gapsNote("g-recovery", S["recovery.score"]);

  const rhr = okSeries(S["phys.rhr"]);
  const base = okSeries(S["rhr.base28"]);
  const spread = okSeries(S["rhr.spread28"]);
  const hi = base.map((b, i) => (b == null || spread[i] == null ? null : b + spread[i]));
  const lo = base.map((b, i) => (b == null || spread[i] == null ? null : b - spread[i]));
  const flags = S["rhr.flag"].value.map((f, i) => (f === 1 && S["rhr.flag"].status[i] === "ok" ? 6 : 0));
  draw("c-rhr", { data: { labels, datasets: [
      line("Resting HR", rhr, blue, { pointRadius: rhr.map((_, i) => Math.max(flags[i], i === sel ? 5 : 0)),
        pointBackgroundColor: flags.map((f) => (f ? css("--serious") : blue)) }),
      line("baseline + spread", hi, "transparent", { pointHoverRadius: 0 }),
      line("baseline − spread", lo, "transparent", { pointHoverRadius: 0, fill: "-1", backgroundColor: css("--band") })] },
    options: { ...baseOptions("bpm"), plugins: { legend: { display: false },
      tooltip: { filter: (c) => c.datasetIndex === 0,
        callbacks: { afterLabel: (c) => (flags[c.dataIndex] ? "flagged: above baseline" : "") } } } } });
  gapsNote("g-rhr", S["phys.rhr"]);

  const tst = okSeries(S["sleep.tst_min"]).map((v) => (v == null ? null : v / 60));
  const tbase = okSeries(S["tst.base28"]).map((v) => (v == null ? null : v / 60));
  draw("c-sleep", { data: { labels, datasets: [
      { type: "bar", label: "Sleep", data: tst, backgroundColor: blue, borderRadius: { topLeft: 4, topRight: 4 },
        borderSkipped: "bottom", maxBarThickness: 18 },
      line("28-day baseline", tbase, css("--reference"), { borderWidth: 2, pointHoverRadius: 0 })] },
    options: { ...baseOptions("h", { beginAtZero: true }),
      plugins: { legend: { display: true, labels: { color: css("--text-secondary"), boxWidth: 12 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.raw == null ? "–" : fmtHM(c.raw * 60)}` } } } } });
  gapsNote("g-sleep", S["sleep.tst_min"]);

  const load = okSeries(S["strain.load"]);
  draw("c-load", { data: { labels, datasets: [
      { type: "bar", label: "Load", data: load, backgroundColor: blue, borderRadius: { topLeft: 4, topRight: 4 },
        borderSkipped: "bottom", maxBarThickness: 18 }] },
    options: { ...baseOptions("TRIMP", { beginAtZero: true }), plugins: { legend: { display: false },
      tooltip: { callbacks: { afterLabel: (c) => `strain ${fmt(S["strain.strain"].value[c.dataIndex], 1)} / 21` +
        (S["strain.load"].reason[c.dataIndex] ? `\n${S["strain.load"].reason[c.dataIndex]}` : "") } } } } });
  gapsNote("g-load", S["strain.load"]);

  const acwr = okSeries(S["acwr.ewma"]);
  draw("c-acwr", { data: { labels, datasets: [
      line("ACWR", acwr, blue, { pointRadius: highlight(acwr) }),
      line("1.3", Array(n).fill(1.3), "transparent", { pointHoverRadius: 0 }),
      line("0.8", Array(n).fill(0.8), "transparent", { pointHoverRadius: 0, fill: "-1", backgroundColor: css("--band") })] },
    options: { ...baseOptions("", { suggestedMin: 0.4, suggestedMax: 1.8 }),
      plugins: { legend: { display: false }, tooltip: { filter: (c) => c.datasetIndex === 0 } } } });
  gapsNote("g-acwr", S["acwr.ewma"]);

  const sri = okSeries(S["sleep.sri_7d"]);
  draw("c-sri", { data: { labels, datasets: [line("SRI", sri, blue, { pointRadius: highlight(sri) })] },
    options: { ...baseOptions("", { suggestedMin: 40, max: 100 }) } });
  gapsNote("g-sri", S["sleep.sri_7d"]);

  renderTable(res);
}

function renderTable(res) {
  const S = res.series;
  const cols = [["Recovery", "recovery.score", 0], ["RHR", "phys.rhr", 1], ["Sleep", "sleep.tst_min", "hm"],
                ["SRI", "sleep.sri_7d", 0], ["Load", "strain.load", 0], ["ACWR", "acwr.ewma", 2]];
  const head = `<tr><th>Date</th>${cols.map((c) => `<th>${c[0]}</th>`).join("")}<th>Why missing</th></tr>`;
  const rows = res.dates.map((d, i) => {
    const why = new Set();
    const cells = cols.map(([, k, p]) => {
      const s = S[k];
      if (s.status[i] !== "ok") { if (s.reason[i]) why.add(s.reason[i]); return "<td>–</td>"; }
      return `<td>${p === "hm" ? fmtHM(s.value[i]) : fmt(s.value[i], p)}</td>`;
    }).join("");
    return `<tr><td>${d}</td>${cells}<td class="reason">${esc([...why].join("; "))}</td></tr>`;
  }).reverse().join("");
  $("table").innerHTML = head + rows;
}

// ---------------------------------------------------------------- wiring
async function refresh() {
  $("date").value = state.date;
  const [day, series] = await Promise.all([
    api(`/api/day/${state.date}`).catch(() => ({ metrics: {} })),
    api(`/api/series?end=${state.date}&days=${state.days}`)]);
  renderDay(day);
  renderCharts(series);
  history.replaceState(null, "", `?date=${state.date}&days=${state.days}`);
}

function shift(delta) {
  const d = new Date(state.date + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + delta);
  const iso = d.toISOString().slice(0, 10);
  if (iso >= state.range.min && iso <= state.range.max) { state.date = iso; refresh(); }
}

async function init() {
  try {
    state.range = await api("/api/range");
  } catch (e) {
    $("notice").textContent = String(e.message || e);
    return;
  }
  const q = new URLSearchParams(location.search);
  state.date = q.get("date") || state.range.default;
  state.days = Number(q.get("days")) || 28;
  Object.assign($("date"), { min: state.range.min, max: state.range.max });
  document.querySelectorAll(".seg button").forEach((b) => {
    b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days));
    b.addEventListener("click", () => {
      state.days = Number(b.dataset.days);
      document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      refresh();
    });
  });
  $("date").addEventListener("change", (e) => { if (e.target.value) { state.date = e.target.value; refresh(); } });
  $("prev").addEventListener("click", () => shift(-1));
  $("next").addEventListener("click", () => shift(1));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);
  refresh();
}

init();
