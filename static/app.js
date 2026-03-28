/* ===== State ===== */
const state = {
  allAlerts: [],
  filteredAlerts: [],
  selectedAreas: new Set(),
  allAreas: [],
  preset: "all",
  fromDate: null,
  toDate: null,
  globalStartDate: null,
};

let hourChart = null;
let areasChart = null;

const $ = id => document.getElementById(id);

/* ===== Util ===== */
function showLoading(v, msg = "טוען נתונים...") {
  $("loadingOverlay").classList.toggle("hidden", !v);
  $("loadingMsg").textContent = msg;
}
function showEmpty(v) { $("emptyState").classList.toggle("hidden", !v); }
function showError(msg) {
  $("errorMsg").textContent = msg;
  $("errorBanner").classList.remove("hidden");
  setTimeout(() => $("errorBanner").classList.add("hidden"), 9000);
}

/* ===== DB Status Badge ===== */
function updateDbBadge(stats) {
  const badge = $("dbBadge");
  if (!stats || !stats.total) {
    badge.className = "badge badge-empty";
    badge.textContent = "● בסיס נתונים ריק";
    $("emptyDbBanner").classList.remove("hidden");
  } else {
    badge.className = "badge badge-live";
    const fmt = n => n.toLocaleString("he-IL");
    badge.textContent = `● ${fmt(stats.total)} התרעות בבסיס`;
    $("emptyDbBanner").classList.add("hidden");
    if (stats.last_sync) {
      const d = new Date(stats.last_sync.synced_at);
      badge.title = `סנכרון אחרון: ${d.toLocaleString("he-IL")}`;
    }
  }
}

/* ===== Sync progress bar ===== */
function showSyncBar(msg, pct = null) {
  $("syncBar").classList.remove("hidden");
  $("syncBarLabel").textContent = msg;
  $("syncBarInner").style.width = pct !== null ? pct + "%" : "0%";
  if (pct === null) $("syncBarInner").classList.add("indeterminate");
  else $("syncBarInner").classList.remove("indeterminate");
}
function hideSyncBar() {
  setTimeout(() => $("syncBar").classList.add("hidden"), 1500);
  $("syncBarInner").style.width = "100%";
}

/* ===== OREF Fetch from browser ===== */
const OREF_URL = "https://www.oref.org.il/WarningMessages/History/AlertsHistory.json";
const OREF_HEADERS = {
  "Referer": "https://www.oref.org.il/heb/alerts-history",
  "Accept": "application/json, text/plain, */*",
  "X-Requested-With": "XMLHttpRequest",
};

async function fetchOrefFromBrowser() {
  showSyncBar("מתחבר לשרתי פיקוד העורף...", null);
  const resp = await fetch(OREF_URL, { headers: OREF_HEADERS, mode: "cors" });
  if (!resp.ok) throw new Error(`OREF returned ${resp.status}`);
  const text = await resp.text();
  // Strip BOM if present
  const clean = text.replace(/^\uFEFF/, "").trim();
  if (!clean) throw new Error("Empty response from OREF");
  return JSON.parse(clean);
}

async function runSync() {
  const btns = [$("btnSync"), $("btnSyncBanner")].filter(Boolean);
  btns.forEach(b => { b.disabled = true; const ic = b.querySelector(".sync-icon"); if (ic) ic.style.animation = "spin 0.7s linear infinite"; });

  try {
    showSyncBar("מנסה משיכה ישירה מהשרת...", 10);

    // 1. First try server-side fetch (works if server is in Israel)
    let res = await fetch("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: "[]" });
    let data = await res.json();

    if (data.needs_browser_sync) {
      // 2. Fallback: fetch from browser and push to server
      showSyncBar("שרת חסום — מושך דרך הדפדפן...", 25);
      const alerts = await fetchOrefFromBrowser();
      showSyncBar(`נמצאו ${alerts.length.toLocaleString("he-IL")} התרעות — שומר בבסיס...`, 65);

      res = await fetch("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(alerts),
      });
      data = await res.json();
    }

    if (!data.ok) throw new Error(data.error || "Sync failed");

    showSyncBar(`✓ סנכרון הושלם — ${data.added?.toLocaleString("he-IL")} רשומות חדשות`, 100);
    hideSyncBar();
    await loadAll();

  } catch (e) {
    hideSyncBar();
    showError("שגיאת סנכרון: " + e.message);
  } finally {
    btns.forEach(b => { b.disabled = false; const ic = b.querySelector(".sync-icon"); if (ic) ic.style.animation = ""; });
  }
}

/* ===== API ===== */
async function fetchAlerts(from_date, to_date, areas) {
  const qs = new URLSearchParams();
  // globalStartDate is the floor — use whichever is later
  const effectiveFrom = from_date && state.globalStartDate
    ? (from_date > state.globalStartDate ? from_date : state.globalStartDate)
    : (from_date || state.globalStartDate || null);
  if (effectiveFrom) qs.set("from_date", effectiveFrom);
  if (to_date) qs.set("to_date", to_date);
  if (areas && areas.length) qs.set("areas", areas.join(","));

  const res = await fetch(`/api/alerts?${qs}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ===== Filter logic ===== */
function getDateRange() {
  const now = new Date();
  if (state.preset === "24h") {
    return [
      new Date(now - 86400000).toISOString().slice(0,10),
      now.toISOString().slice(0,10),
    ];
  }
  if (state.preset === "day") {
    const d = now.toISOString().slice(0,10);
    return [d, d];
  }
  if (state.preset === "week") {
    return [
      new Date(now - 7 * 86400000).toISOString().slice(0,10),
      now.toISOString().slice(0,10),
    ];
  }
  if (state.preset === "custom") {
    return [state.fromDate, state.toDate];
  }
  return [state.globalStartDate || null, null];
}

/* ===== Compute hour buckets ===== */
function computeHourBuckets(alerts) {
  const b = new Array(24).fill(0);
  alerts.forEach(a => { if (a.hour >= 0 && a.hour <= 23) b[a.hour]++; });
  return b;
}

function getBarColors(buckets) {
  const max = Math.max(...buckets, 1);
  return buckets.map(v => {
    const r = v / max;
    if (r >= 0.66) return "rgba(239,68,68,0.9)";
    if (r >= 0.33) return "rgba(245,158,11,0.9)";
    return "rgba(59,130,246,0.82)";
  });
}

/* ===== Charts ===== */
function renderHourChart(alerts) {
  const buckets = computeHourBuckets(alerts);
  const labels = Array.from({ length: 24 }, (_, i) => String(i).padStart(2,"0") + ":00");
  const colors = getBarColors(buckets);

  if (hourChart) {
    hourChart.data.datasets[0].data = buckets;
    hourChart.data.datasets[0].backgroundColor = colors;
    hourChart.update("active");
    return;
  }

  const ctx = $("hourChart").getContext("2d");
  hourChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "התרעות",
        data: buckets,
        backgroundColor: colors,
        borderRadius: 5,
        borderSkipped: false,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true,
          backgroundColor: "rgba(14,17,23,0.95)",
          borderColor: "#252d3d",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#94a3b8",
          padding: 12,
          cornerRadius: 8,
          callbacks: {
            title: items => `שעה ${items[0].label}`,
            label: item => ` ${item.raw.toLocaleString("he-IL")} התרעות`,
          }
        }
      },
      scales: {
        x: {
          grid: { color: "rgba(37,45,61,0.5)" },
          ticks: { color: "#475569", font: { size: 11 } },
        },
        y: {
          grid: { color: "rgba(37,45,61,0.5)" },
          ticks: { color: "#475569", precision: 0 },
          beginAtZero: true,
        }
      }
    }
  });
}

function renderAreasChart(alerts) {
  const counts = {};
  alerts.forEach(a => {
    const k = (a.data || "לא ידוע").trim();
    counts[k] = (counts[k] || 0) + 1;
  });

  const sorted = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,15);
  const labels = sorted.map(e => e[0]);
  const values = sorted.map(e => e[1]);
  const n = Math.max(values.length - 1, 1);
  const barColors = values.map((_, i) => {
    const t = i / n;
    if (t < 0.5) {
      const u = t * 2;
      return `rgba(${Math.round(59+u*40)},${Math.round(130-u*28)},${Math.round(246-u*5)},0.85)`;
    }
    const u = (t-0.5)*2;
    return `rgba(${Math.round(99+u*140)},${Math.round(102-u*34)},${Math.round(241-u*173)},0.85)`;
  });

  if (areasChart) {
    areasChart.data.labels = labels;
    areasChart.data.datasets[0].data = values;
    areasChart.data.datasets[0].backgroundColor = barColors;
    areasChart.update("active");
    return;
  }

  const ctx = $("areasChart").getContext("2d");
  areasChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "התרעות",
        data: values,
        backgroundColor: barColors,
        borderRadius: 5,
        borderSkipped: false,
      }]
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: { display: false },
        tooltip: {
          rtl: true,
          backgroundColor: "rgba(14,17,23,0.95)",
          borderColor: "#252d3d",
          borderWidth: 1,
          titleColor: "#e2e8f0",
          bodyColor: "#94a3b8",
          padding: 12,
          cornerRadius: 8,
          callbacks: { label: item => ` ${item.raw.toLocaleString("he-IL")} התרעות` }
        }
      },
      scales: {
        x: {
          grid: { color: "rgba(37,45,61,0.5)" },
          ticks: { color: "#475569", precision: 0 },
          beginAtZero: true,
        },
        y: {
          grid: { display: false },
          ticks: { color: "#e2e8f0", font: { size: 12 } },
        }
      }
    }
  });
}

/* ===== Stats ===== */
function updateStats(alerts) {
  $("statTotal").textContent = alerts.length.toLocaleString("he-IL");
  if (!alerts.length) {
    ["statPeakHour","statTopArea","statDateRange"].forEach(id => $(id).textContent = "—");
    return;
  }

  const buckets = computeHourBuckets(alerts);
  const peak = buckets.indexOf(Math.max(...buckets));
  $("statPeakHour").textContent = `${String(peak).padStart(2,"0")}:00`;

  const areaCounts = {};
  alerts.forEach(a => { const k=(a.data||"?").trim(); areaCounts[k]=(areaCounts[k]||0)+1; });
  const top = Object.entries(areaCounts).sort((a,b)=>b[1]-a[1])[0];
  $("statTopArea").textContent = top ? top[0] : "—";

  const dates = alerts.map(a=>a.date).filter(Boolean).sort();
  if (dates.length) {
    const f=dates[0], t=dates[dates.length-1];
    $("statDateRange").textContent = f===t ? f : `${f} – ${t}`;
  }
}

/* ===== Full render ===== */
async function render() {
  showLoading(true);
  try {
    const [from, to] = getDateRange();
    const areas = state.selectedAreas.size ? [...state.selectedAreas] : null;
    const alerts = await fetchAlerts(from, to, areas);

    state.allAlerts = alerts;
    state.filteredAlerts = alerts;

    const hasData = alerts.length > 0;
    showEmpty(!hasData);
    $("hourChart").closest(".chart-section").style.display = hasData ? "" : "none";
    $("areasChart").closest(".chart-section").style.display = hasData ? "" : "none";

    updateStats(alerts);
    if (hasData) {
      renderHourChart(alerts);
      renderAreasChart(alerts);
    }
  } catch(e) {
    showError("שגיאה בטעינת נתונים: " + e.message);
  } finally {
    showLoading(false);
  }
}

/* ===== Area dropdown ===== */
async function loadAreas() {
  try {
    const res = await fetch("/api/areas");
    state.allAreas = res.ok ? await res.json() : [];
  } catch { state.allAreas = []; }
}

function renderAreaDropdown(filter = "") {
  const dropdown = $("areaDropdown");
  const lower = filter.toLowerCase();
  const items = state.allAreas.filter(a=>a.toLowerCase().includes(lower)).slice(0,80);
  if (!items.length) { dropdown.style.display = "none"; return; }
  dropdown.innerHTML = items.map(area => {
    const sel = state.selectedAreas.has(area);
    return `<div class="area-option${sel?" selected":""}" data-area="${area}">
      <input type="checkbox"${sel?" checked":""} tabindex="-1" readonly />${area}
    </div>`;
  }).join("");
  dropdown.style.display = "block";
}

function renderSelectedTags() {
  const c = $("selectedAreas");
  c.innerHTML = [...state.selectedAreas].map(area =>
    `<span class="tag">${area}<button class="tag-remove" data-remove="${area}">✕</button></span>`
  ).join("");
  c.querySelectorAll(".tag-remove").forEach(btn => {
    btn.addEventListener("click", () => {
      state.selectedAreas.delete(btn.dataset.remove);
      renderSelectedTags();
      render();
    });
  });
}

/* ===== Load everything ===== */
async function loadAll() {
  const [statusRes, areasRes] = await Promise.all([
    fetch("/api/status").then(r=>r.json()).catch(()=>({})),
    loadAreas(),
  ]);
  updateDbBadge(statusRes.db);
  await render();
}

/* ===== Init ===== */
async function init() {
  await loadAll();

  // Preset buttons
  document.querySelectorAll(".btn-filter").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".btn-filter").forEach(b=>b.classList.remove("active"));
      btn.classList.add("active");
      state.preset = btn.dataset.preset;
      $("customDateGroup").style.display = state.preset==="custom" ? "flex" : "none";
      render();
    });
  });

  $("btnApplyDates").addEventListener("click", () => {
    state.fromDate = $("fromDate").value || null;
    state.toDate = $("toDate").value || null;
    render();
  });

  $("btnSetStartDate").addEventListener("click", () => {
    state.globalStartDate = $("globalStartDate").value || null;
    render();
  });

  $("btnSync").addEventListener("click", runSync);
  $("btnSyncBanner")?.addEventListener("click", runSync);

  // Area search
  const areaSearch = $("areaSearch");
  areaSearch.addEventListener("input", () => renderAreaDropdown(areaSearch.value));
  areaSearch.addEventListener("focus", () => renderAreaDropdown(areaSearch.value));
  document.addEventListener("click", e => {
    if (!e.target.closest(".multiselect-wrapper")) $("areaDropdown").style.display = "none";
  });
  $("areaDropdown").addEventListener("click", e => {
    const opt = e.target.closest(".area-option");
    if (!opt) return;
    const area = opt.dataset.area;
    state.selectedAreas.has(area) ? state.selectedAreas.delete(area) : state.selectedAreas.add(area);
    renderAreaDropdown(areaSearch.value);
    renderSelectedTags();
    render();
  });
}

document.addEventListener("DOMContentLoaded", init);
