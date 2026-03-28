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
  usingUploadedData: false,
};

let hourChart = null;
let areasChart = null;

const $ = id => document.getElementById(id);

/* ===== Util ===== */
function showLoading(v) { $("loadingOverlay").classList.toggle("hidden", !v); }
function showEmpty(v) { $("emptyState").classList.toggle("hidden", !v); }

function showError(msg) {
  $("errorMsg").textContent = msg;
  $("errorBanner").classList.remove("hidden");
  setTimeout(() => $("errorBanner").classList.add("hidden"), 8000);
}

/* ===== API ===== */
async function fetchAlerts(params = {}) {
  showLoading(true);
  try {
    const qs = new URLSearchParams();
    if (params.preset && params.preset !== "all") qs.set("preset", params.preset);
    if (params.from_date) qs.set("from_date", params.from_date);
    if (params.to_date) qs.set("to_date", params.to_date);

    const res = await fetch(`/api/alerts${qs.toString() ? "?" + qs : ""}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return await res.json();
  } catch (e) {
    showError("שגיאה בטעינת נתונים: " + e.message);
    return [];
  } finally {
    showLoading(false);
  }
}

async function fetchAreas() {
  try {
    const res = await fetch("/api/areas");
    return res.ok ? res.json() : [];
  } catch { return []; }
}

async function postUploadData(arr) {
  const res = await fetch("/api/upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(arr),
  });
  return res.json();
}

async function deleteUploadData() {
  await fetch("/api/upload", { method: "DELETE" });
}

/* ===== Filter logic ===== */
function applyFilters() {
  let alerts = [...state.allAlerts];

  // Global start date
  if (state.globalStartDate) {
    alerts = alerts.filter(a => a.date && a.date >= state.globalStartDate);
  }

  // Time preset / custom range (client-side cut)
  const now = new Date();
  let cutFrom = null, cutTo = null;
  if (state.preset === "24h") {
    cutFrom = new Date(now - 24 * 3600 * 1000);
  } else if (state.preset === "day") {
    cutFrom = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  } else if (state.preset === "week") {
    cutFrom = new Date(now - 7 * 24 * 3600 * 1000);
  } else if (state.preset === "custom") {
    if (state.fromDate) cutFrom = new Date(state.fromDate);
    if (state.toDate) { cutTo = new Date(state.toDate); cutTo.setHours(23, 59, 59); }
  }

  if (cutFrom || cutTo) {
    alerts = alerts.filter(a => {
      if (!a.timestamp) return true;
      const t = new Date(a.timestamp);
      if (cutFrom && t < cutFrom) return false;
      if (cutTo && t > cutTo) return false;
      return true;
    });
  }

  // Area filter
  if (state.selectedAreas.size > 0) {
    alerts = alerts.filter(a => state.selectedAreas.has(a.data));
  }

  state.filteredAlerts = alerts;
}

/* ===== Hour buckets ===== */
function computeHourBuckets(alerts) {
  const b = new Array(24).fill(0);
  alerts.forEach(a => { if (a.hour >= 0 && a.hour <= 23) b[a.hour]++; });
  return b;
}

function getBarColors(buckets) {
  const max = Math.max(...buckets, 1);
  return buckets.map(v => {
    const r = v / max;
    if (r >= 0.66) return "rgba(239,68,68,0.88)";
    if (r >= 0.33) return "rgba(245,158,11,0.88)";
    return "rgba(59,130,246,0.8)";
  });
}

/* ===== Charts ===== */
const chartDefaults = {
  color: "#64748b",
  borderColor: "#252d3d",
  font: { family: "'Segoe UI', system-ui, sans-serif" },
};

function renderHourChart(alerts) {
  const buckets = computeHourBuckets(alerts);
  const labels = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0") + ":00");
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
        hoverBackgroundColor: colors.map(c => c.replace(/[\d.]+\)$/, "1)")),
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 500, easing: "easeOutQuart" },
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
          grid: { color: "rgba(37,45,61,0.5)", drawBorder: false },
          ticks: { color: "#475569", font: { size: 11 } },
        },
        y: {
          grid: { color: "rgba(37,45,61,0.5)", drawBorder: false },
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

  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 15);
  const labels = sorted.map(e => e[0]);
  const values = sorted.map(e => e[1]);

  // Build gradient colors from blue → red based on rank
  const barColors = values.map((_, i) => {
    const t = i / Math.max(values.length - 1, 1);
    // blue → indigo → red
    if (t < 0.5) {
      const u = t * 2;
      return `rgba(${Math.round(59 + u*(99-59))},${Math.round(130 + u*(102-130))},${Math.round(246 + u*(241-246))},0.85)`;
    } else {
      const u = (t - 0.5) * 2;
      return `rgba(${Math.round(99 + u*(239-99))},${Math.round(102 + u*(68-102))},${Math.round(241 + u*(68-241))},0.85)`;
    }
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
      animation: { duration: 500, easing: "easeOutQuart" },
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
          grid: { color: "rgba(37,45,61,0.5)", drawBorder: false },
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
    ["statPeakHour","statTopArea","statDateRange"].forEach(id => $( id).textContent = "—");
    return;
  }

  const buckets = computeHourBuckets(alerts);
  const peak = buckets.indexOf(Math.max(...buckets));
  $("statPeakHour").textContent = `${String(peak).padStart(2,"0")}:00`;

  const areaCounts = {};
  alerts.forEach(a => { const k = (a.data||"?").trim(); areaCounts[k] = (areaCounts[k]||0)+1; });
  const top = Object.entries(areaCounts).sort((a,b)=>b[1]-a[1])[0];
  $("statTopArea").textContent = top ? top[0] : "—";

  const dates = alerts.map(a=>a.date).filter(Boolean).sort();
  if (dates.length) {
    const f = dates[0], t = dates[dates.length-1];
    $("statDateRange").textContent = f === t ? f : `${f} – ${t}`;
  }
}

/* ===== Full render ===== */
function render() {
  applyFilters();
  const alerts = state.filteredAlerts;
  updateStats(alerts);

  const hasData = alerts.length > 0;
  showEmpty(!hasData);
  $("hourChart").closest(".chart-section").style.display = hasData ? "" : "none";
  $("areasChart").closest(".chart-section").style.display = hasData ? "" : "none";

  if (hasData) {
    renderHourChart(alerts);
    renderAreasChart(alerts);
  }
}

/* ===== Area dropdown ===== */
function renderAreaDropdown(filter = "") {
  const dropdown = $("areaDropdown");
  const lower = filter.toLowerCase();
  const items = state.allAreas.filter(a => a.toLowerCase().includes(lower)).slice(0, 80);

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
    `<span class="tag" data-tag="${area}">${area}<button class="tag-remove" data-remove="${area}" title="הסר">✕</button></span>`
  ).join("");
  c.querySelectorAll(".tag-remove").forEach(btn => {
    btn.addEventListener("click", () => {
      state.selectedAreas.delete(btn.dataset.remove);
      renderSelectedTags();
      render();
    });
  });
}

/* ===== Data source badge ===== */
function setDataSourceBadge(uploaded) {
  const badge = $("dataSourceBadge");
  badge.className = "badge " + (uploaded ? "badge-offline" : "badge-live");
  badge.textContent = uploaded ? "● נתונים ידניים" : "● נתונים חיים";
}

/* ===== Upload handlers ===== */
function handleJSON(raw) {
  try {
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) throw new Error("Expected JSON array");
    return arr;
  } catch (e) {
    showError("JSON לא תקין: " + e.message);
    return null;
  }
}

async function loadUploadedData(arr) {
  showLoading(true);
  try {
    const r = await postUploadData(arr);
    if (r.error) throw new Error(r.error);
    state.usingUploadedData = true;
    setDataSourceBadge(true);
    await loadData();
  } catch (e) {
    showError(e.message);
  } finally {
    showLoading(false);
  }
}

/* ===== Load all data ===== */
async function loadData() {
  const data = await fetchAlerts({});
  state.allAlerts = data;
  state.allAreas = [...new Set(data.map(a=>(a.data||"").trim()).filter(Boolean))].sort();
  render();
}

/* ===== Init ===== */
async function init() {
  await loadData();

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
    state.toDate = $("toDate").value || null;
    render();
  });

  $("btnSetStartDate").addEventListener("click", () => {
    state.globalStartDate = $("globalStartDate").value || null;
    state.usingUploadedData = false;
    deleteUploadData();
    setDataSourceBadge(false);
    loadData();
  });

  $("btnRefresh").addEventListener("click", () => {
    if (!state.usingUploadedData) loadData();
    else render(); // just re-render uploaded data
  });

  // Upload panel toggle
  $("btnUploadToggle").addEventListener("click", () => {
    $("uploadPanel").classList.toggle("hidden");
  });

  // Paste / file load
  $("btnPasteLoad").addEventListener("click", async () => {
    const raw = $("pasteArea").value.trim();
    if (!raw) { showError("אנא הדבק JSON תחילה"); return; }
    const arr = handleJSON(raw);
    if (arr) await loadUploadedData(arr);
  });

  $("btnClearUpload").addEventListener("click", async () => {
    await deleteUploadData();
    state.usingUploadedData = false;
    setDataSourceBadge(false);
    $("pasteArea").value = "";
    $("uploadPanel").classList.add("hidden");
    await loadData();
  });

  $("fileInput").addEventListener("change", async e => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    const arr = handleJSON(text);
    if (arr) await loadUploadedData(arr);
    e.target.value = "";
  });

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
