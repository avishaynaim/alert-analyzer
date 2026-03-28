/* ===== Logger ===== */
const log    = (...a) => console.log('[Alert]', ...a);
const logErr = (...a) => console.error('[Alert ERROR]', ...a);
const logWarn= (...a) => console.warn('[Alert WARN]', ...a);

/* ===== State ===== */
const state = {
  preset: "all",
  fromDate: null,
  toDate: null,
  globalStartDate: null,
  selectedAreas: new Set(),
  allAreas: [],
};

let hourChart  = null;
let areasChart = null;

const $ = id => document.getElementById(id);

/* ===== UI helpers ===== */
function showLoading(v, msg = "טוען נתונים...") {
  $("loadingOverlay").classList.toggle("hidden", !v);
  $("loadingMsg").textContent = msg;
}
function showEmpty(v) { $("emptyState").classList.toggle("hidden", !v); }
function showError(msg) {
  logErr(msg);
  $("errorMsg").textContent = msg;
  $("errorBanner").classList.remove("hidden");
  setTimeout(() => $("errorBanner").classList.add("hidden"), 12000);
}

/* ===== DB badge ===== */
function updateDbBadge(total) {
  const badge = $("dbBadge");
  if (!total) {
    badge.className = "badge badge-empty";
    badge.textContent = "● בסיס נתונים ריק";
    $("emptyDbBanner").classList.remove("hidden");
  } else {
    badge.className = "badge badge-live";
    badge.textContent = `● ${total.toLocaleString("he-IL")} התרעות`;
    $("emptyDbBanner").classList.add("hidden");
  }
}

/* ===== Sync bar ===== */
function showSyncBar(msg, pct = null) {
  $("syncBar").classList.remove("hidden");
  $("syncBarLabel").textContent = msg;
  $("syncBarInner").style.width = pct !== null ? pct + "%" : "0%";
  if (pct === null) $("syncBarInner").classList.add("indeterminate");
  else              $("syncBarInner").classList.remove("indeterminate");
}
function hideSyncBar() {
  setTimeout(() => $("syncBar").classList.add("hidden"), 2000);
  $("syncBarInner").style.width = "100%";
  $("syncBarInner").classList.remove("indeterminate");
}

/* ===== Sync flow ===== */
const CONSOLE_SCRIPT =
`fetch('/WarningMessages/History/AlertsHistory.json')
  .then(r => r.json())
  .then(data => {
    const json = JSON.stringify(data);
    navigator.clipboard.writeText(json).then(() => {
      alert('✅ ' + data.length + ' התרעות הועתקו!\\nחזור לאפליקציה והדבק בתיבה.');
    }).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = json;
      ta.style = 'position:fixed;top:10px;left:10px;width:90vw;height:80vh;z-index:99999;font-size:10px';
      document.body.appendChild(ta); ta.select();
      alert('סמן הכל (Ctrl+A) והעתק (Ctrl+C) מהתיבה שנפתחה.');
    });
  })
  .catch(e => alert('שגיאה: ' + e.message));`;

function showCorsModal() { $("corsModal").classList.remove("hidden"); }
function hideCorsModal()  { $("corsModal").classList.add("hidden"); }

async function runSync() {
  log('Sync triggered');
  const btns = [$("btnSync"), $("btnSyncBanner")].filter(Boolean);
  btns.forEach(b => { b.disabled = true; const ic = b.querySelector(".sync-icon"); if (ic) ic.style.animation = "spin 0.7s linear infinite"; });

  try {
    showSyncBar("מסנכרן מ-GitHub...", null);
    const res  = await fetch("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: "[]" });
    const data = await res.json();
    log('Sync result:', data);

    if (!data.ok) throw new Error(data.error || "Sync failed");

    showSyncBar(`✓ ${(data.added || 0).toLocaleString("he-IL")} רשומות חדשות`, 100);
    hideSyncBar();
    await loadAll();
  } catch (e) {
    logErr('Sync failed:', e);
    hideSyncBar();
    showError("שגיאת סנכרון: " + e.message);
  } finally {
    btns.forEach(b => { b.disabled = false; const ic = b.querySelector(".sync-icon"); if (ic) ic.style.animation = ""; });
  }
}

/* ===== Build query params ===== */
function buildParams() {
  const qs = new URLSearchParams();
  const now = new Date();

  if (state.preset === "24h") {
    qs.set("from_date", new Date(now - 86400000).toISOString().slice(0,10));
    qs.set("to_date",   now.toISOString().slice(0,10));
  } else if (state.preset === "day") {
    const d = now.toISOString().slice(0,10);
    qs.set("from_date", d); qs.set("to_date", d);
  } else if (state.preset === "week") {
    qs.set("from_date", new Date(now - 7*86400000).toISOString().slice(0,10));
    qs.set("to_date",   now.toISOString().slice(0,10));
  } else if (state.preset === "custom") {
    if (state.fromDate) qs.set("from_date", state.fromDate);
    if (state.toDate)   qs.set("to_date",   state.toDate);
  }

  // Global start date is a floor
  if (state.globalStartDate) {
    const cur = qs.get("from_date");
    const floor = (!cur || state.globalStartDate > cur) ? state.globalStartDate : cur;
    qs.set("from_date", floor);
  }

  if (state.selectedAreas.size) qs.set("areas", [...state.selectedAreas].join(","));

  return qs;
}

/* ===== Fetch analytics (aggregated — never raw rows) ===== */
async function fetchAnalytics() {
  const url = `/api/analytics?${buildParams()}`;
  log('Fetching analytics:', url);
  const res = await fetch(url);
  if (!res.ok) { const e = await res.json().catch(()=>{}); throw new Error((e||{}).error || `HTTP ${res.status}`); }
  const data = await res.json();
  log('Analytics:', data.total, 'alerts, peak hour:', data.peak_hour);
  return data;
}

/* ===== Bar colors ===== */
function getBarColors(buckets) {
  const max = Math.max(...buckets, 1);
  return buckets.map(v => {
    const r = v / max;
    if (r >= 0.66) return "rgba(239,68,68,0.9)";
    if (r >= 0.33) return "rgba(245,158,11,0.9)";
    return "rgba(59,130,246,0.82)";
  });
}

/* ===== Render hour chart ===== */
function renderHourChart(buckets) {
  const labels = Array.from({length:24}, (_,i) => String(i).padStart(2,"0")+":00");
  const colors = getBarColors(buckets);

  if (hourChart) {
    hourChart.data.datasets[0].data = buckets;
    hourChart.data.datasets[0].backgroundColor = colors;
    hourChart.update("active"); return;
  }

  hourChart = new Chart($("hourChart").getContext("2d"), {
    type: "bar",
    data: { labels, datasets: [{ label: "התרעות", data: buckets, backgroundColor: colors, borderRadius: 5, borderSkipped: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true,
          backgroundColor: "rgba(14,17,23,0.95)", borderColor: "#252d3d", borderWidth: 1,
          titleColor: "#e2e8f0", bodyColor: "#94a3b8", padding: 12, cornerRadius: 8,
          callbacks: { title: i => `שעה ${i[0].label}`, label: i => ` ${i.raw.toLocaleString("he-IL")} התרעות` }
        }
      },
      scales: {
        x: { grid: { color: "rgba(37,45,61,0.5)" }, ticks: { color: "#475569", font: { size: 11 } } },
        y: { grid: { color: "rgba(37,45,61,0.5)" }, ticks: { color: "#475569", precision: 0 }, beginAtZero: true }
      }
    }
  });
}

/* ===== Render areas chart ===== */
function renderAreasChart(topAreas) {
  const labels = topAreas.map(a => a.area);
  const values = topAreas.map(a => a.count);
  const n = Math.max(values.length - 1, 1);
  const colors = values.map((_, i) => {
    const t = i / n;
    if (t < 0.5) { const u=t*2; return `rgba(${Math.round(59+u*40)},${Math.round(130-u*28)},246,0.85)`; }
    const u=(t-0.5)*2;
    return `rgba(${Math.round(99+u*140)},${Math.round(102-u*34)},${Math.round(241-u*173)},0.85)`;
  });

  if (areasChart) {
    areasChart.data.labels = labels;
    areasChart.data.datasets[0].data = values;
    areasChart.data.datasets[0].backgroundColor = colors;
    areasChart.update("active"); return;
  }

  areasChart = new Chart($("areasChart").getContext("2d"), {
    type: "bar",
    data: { labels, datasets: [{ label: "התרעות", data: values, backgroundColor: colors, borderRadius: 5, borderSkipped: false }] },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true,
          backgroundColor: "rgba(14,17,23,0.95)", borderColor: "#252d3d", borderWidth: 1,
          titleColor: "#e2e8f0", bodyColor: "#94a3b8", padding: 12, cornerRadius: 8,
          callbacks: { label: i => ` ${i.raw.toLocaleString("he-IL")} התרעות` }
        }
      },
      scales: {
        x: { grid: { color: "rgba(37,45,61,0.5)" }, ticks: { color: "#475569", precision: 0 }, beginAtZero: true },
        y: { grid: { display: false }, ticks: { color: "#e2e8f0", font: { size: 12 } } }
      }
    }
  });
}

/* ===== Stats cards ===== */
function updateStats(data) {
  $("statTotal").textContent    = (data.total || 0).toLocaleString("he-IL");
  $("statPeakHour").textContent = data.peak_hour !== null ? String(data.peak_hour).padStart(2,"0") + ":00" : "—";
  $("statTopArea").textContent  = data.top_areas?.[0]?.area || "—";
  const f = data.earliest?.slice(0,10), t = data.latest?.slice(0,10);
  $("statDateRange").textContent = f ? (f === t ? f : `${f} – ${t}`) : "—";
}

/* ===== Full render cycle ===== */
async function render() {
  showLoading(true);
  try {
    const data = await fetchAnalytics();
    const hasData = data.total > 0;

    updateStats(data);
    updateDbBadge(data.total);
    showEmpty(!hasData);

    $("hourChart").closest(".chart-section").style.display  = hasData ? "" : "none";
    $("areasChart").closest(".chart-section").style.display = hasData ? "" : "none";

    if (hasData) {
      renderHourChart(data.hour_buckets);
      renderAreasChart(data.top_areas);
    }
    log(`Render complete — ${data.total} alerts`);
  } catch(e) {
    logErr('Render failed:', e);
    showError("שגיאה: " + e.message);
  } finally {
    showLoading(false);
  }
}

/* ===== Areas dropdown ===== */
async function loadAreas() {
  try {
    const res = await fetch("/api/areas");
    state.allAreas = res.ok ? await res.json() : [];
    log(`Loaded ${state.allAreas.length} areas`);
  } catch { state.allAreas = []; }
}

function renderAreaDropdown(filter = "") {
  const dd = $("areaDropdown");
  const items = state.allAreas.filter(a => a.toLowerCase().includes(filter.toLowerCase())).slice(0,80);
  if (!items.length) { dd.style.display = "none"; return; }
  dd.innerHTML = items.map(area => {
    const sel = state.selectedAreas.has(area);
    return `<div class="area-option${sel?" selected":""}" data-area="${area}"><input type="checkbox"${sel?" checked":""} tabindex="-1" readonly />${area}</div>`;
  }).join("");
  dd.style.display = "block";
}

function renderSelectedTags() {
  const c = $("selectedAreas");
  c.innerHTML = [...state.selectedAreas].map(area =>
    `<span class="tag">${area}<button class="tag-remove" data-remove="${area}">✕</button></span>`
  ).join("");
  c.querySelectorAll(".tag-remove").forEach(btn => {
    btn.addEventListener("click", () => { state.selectedAreas.delete(btn.dataset.remove); renderSelectedTags(); render(); });
  });
}

/* ===== Load status + areas then render ===== */
async function loadAll() {
  const [status] = await Promise.all([
    fetch("/api/status").then(r => r.json()).catch(() => ({})),
    loadAreas(),
  ]);
  log('Status:', status);
  await render();
}

/* ===== Init ===== */
async function init() {
  log('=== App init ===');

  await loadAll();

  // Preset buttons
  document.querySelectorAll(".btn-filter").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".btn-filter").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.preset = btn.dataset.preset;
      $("customDateGroup").style.display = state.preset === "custom" ? "flex" : "none";
      render();
    });
  });

  $("btnApplyDates").addEventListener("click", () => {
    state.fromDate = $("fromDate").value || null;
    state.toDate   = $("toDate").value   || null;
    render();
  });

  $("btnSetStartDate").addEventListener("click", () => {
    state.globalStartDate = $("globalStartDate").value || null;
    render();
  });

  $("btnSync").addEventListener("click", runSync);
  $("btnSyncBanner")?.addEventListener("click", runSync);

  $("btnCloseModal").addEventListener("click", hideCorsModal);
  $("corsModal").addEventListener("click", e => { if (e.target === $("corsModal")) hideCorsModal(); });

  $("btnCopyScript").addEventListener("click", () => {
    navigator.clipboard.writeText(CONSOLE_SCRIPT)
      .then(() => { $("btnCopyScript").textContent = "✓ הועתק!"; setTimeout(() => $("btnCopyScript").textContent = "📋 העתק קוד", 2500); })
      .catch(() => showError("לא ניתן להעתיק"));
  });

  $("btnLoadPasted").addEventListener("click", async () => {
    const raw = $("pasteArea").value.trim();
    if (!raw) { $("pasteStatus").textContent = "אנא הדבק נתונים"; return; }
    let arr;
    try { arr = JSON.parse(raw); if (!Array.isArray(arr)) throw new Error("לא מערך"); }
    catch(e) { $("pasteStatus").textContent = "JSON לא תקין: " + e.message; return; }

    $("pasteStatus").textContent = `שולח ${arr.length.toLocaleString("he-IL")} רשומות...`;
    $("btnLoadPasted").disabled = true;
    try {
      const res = await fetch("/api/sync", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(arr) });
      const result = await res.json();
      if (!result.ok) throw new Error(result.error);
      $("pasteStatus").textContent = `✓ ${result.added.toLocaleString("he-IL")} רשומות חדשות`;
      $("pasteArea").value = "";
      setTimeout(async () => { hideCorsModal(); await loadAll(); }, 1500);
    } catch(e) {
      $("pasteStatus").textContent = "שגיאה: " + e.message;
    } finally {
      $("btnLoadPasted").disabled = false;
    }
  });

  $("btnCloseError").addEventListener("click", () => $("errorBanner").classList.add("hidden"));

  const areaSearch = $("areaSearch");
  areaSearch.addEventListener("input",  () => renderAreaDropdown(areaSearch.value));
  areaSearch.addEventListener("focus",  () => renderAreaDropdown(areaSearch.value));
  document.addEventListener("click", e => { if (!e.target.closest(".multiselect-wrapper")) $("areaDropdown").style.display = "none"; });
  $("areaDropdown").addEventListener("click", e => {
    const opt = e.target.closest(".area-option"); if (!opt) return;
    const area = opt.dataset.area;
    state.selectedAreas.has(area) ? state.selectedAreas.delete(area) : state.selectedAreas.add(area);
    renderAreaDropdown(areaSearch.value); renderSelectedTags(); render();
  });

  log('=== App ready ===');
}

document.addEventListener("DOMContentLoaded", init);
