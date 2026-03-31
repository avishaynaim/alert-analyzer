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
  pinnedCity: localStorage.getItem("pinnedCity") || null,
};

let hourChart = null;

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

/* ===== Pinned city helpers ===== */
function setPinnedCity(city) {
  state.pinnedCity = city;
  if (city) localStorage.setItem("pinnedCity", city);
  else localStorage.removeItem("pinnedCity");
  renderCityWidget();
}

function renderCityWidget() {
  const widget = $("pinnedCityWidget");
  if (!widget) return;

  if (state.pinnedCity) {
    widget.innerHTML = `
      <div class="city-widget-pinned">
        <span class="city-widget-label">השוואה:</span>
        <span class="city-widget-name">${state.pinnedCity}</span>
        <button class="city-widget-clear" id="btnClearPin">× הצג הכל</button>
        <span class="city-widget-hint">לחץ על עיר אחרת בטבלה להחלפה</span>
      </div>`;
    $("btnClearPin").addEventListener("click", () => { setPinnedCity(null); render(); });
  } else {
    const areas = state.allAreas.slice(0, 200);
    widget.innerHTML = `
      <div class="city-widget-search">
        <span class="city-widget-label">השווה עם עיר:</span>
        <div class="city-search-wrap">
          <input type="text" id="cityPinSearch" class="city-pin-input" placeholder="חפש עיר..." autocomplete="off" />
          <div id="cityPinDropdown" class="city-pin-dropdown" style="display:none">
            ${areas.map(a => `<div class="city-pin-option" data-city="${a.replace(/"/g,'&quot;')}">${a}</div>`).join("")}
          </div>
        </div>
      </div>`;
    const input = $("cityPinSearch");
    const dd = $("cityPinDropdown");
    input.addEventListener("focus", () => { filterCityDropdown(""); dd.style.display = ""; });
    input.addEventListener("input", () => filterCityDropdown(input.value));
    document.addEventListener("click", e => { if (!e.target.closest(".city-search-wrap")) dd.style.display = "none"; }, { once: false });
    dd.addEventListener("click", e => {
      const opt = e.target.closest(".city-pin-option"); if (!opt) return;
      setPinnedCity(opt.dataset.city);
      render();
    });
  }
}

function filterCityDropdown(q) {
  const dd = $("cityPinDropdown"); if (!dd) return;
  const lower = q.toLowerCase();
  const filtered = state.allAreas.filter(a => a.toLowerCase().includes(lower)).slice(0, 80);
  dd.innerHTML = filtered.map(a => `<div class="city-pin-option" data-city="${a.replace(/"/g,'&quot;')}">${a}</div>`).join("");
  dd.style.display = filtered.length ? "" : "none";
  dd.querySelectorAll(".city-pin-option").forEach(opt => {
    opt.addEventListener("click", () => { setPinnedCity(opt.dataset.city); render(); });
  });
}

async function fetchPinnedCityHours() {
  if (!state.pinnedCity) return null;
  const qs = new URLSearchParams();
  if (state.preset && state.preset !== "custom") qs.set("preset", state.preset);
  if (state.fromDate) qs.set("from_date", state.fromDate);
  if (state.toDate)   qs.set("to_date",   state.toDate);
  if (state.globalStartDate) {
    const cur = qs.get("from_date");
    const floor = (!cur || state.globalStartDate > cur) ? state.globalStartDate : cur;
    qs.set("from_date", floor);
  }
  qs.set("areas", state.pinnedCity);
  const res = await fetch(`/api/analytics?${qs}`);
  if (!res.ok) return null;
  const d = await res.json();
  return d.hour_daily_avg || d.hour_buckets || null;
}

/* ===== Render hour chart ===== */
function renderHourChart(buckets, weekBuckets, totalDays, cityBuckets) {
  const labels = Array.from({length:24}, (_,i) => String(i).padStart(2,"0")+":00");
  const colors = getBarColors(buckets);
  const avgLabel = `ממוצע יומי (${totalDays} ימים)`;
  const cityDataset = cityBuckets ? {
    label: state.pinnedCity,
    data: cityBuckets,
    type: "line",
    yAxisID: "yRight",
    borderColor: "rgba(34,197,94,0.9)",
    backgroundColor: "rgba(34,197,94,0.12)",
    borderWidth: 2,
    pointRadius: 3,
    pointBackgroundColor: "rgba(34,197,94,0.9)",
    tension: 0.3,
    fill: false,
    order: 0
  } : null;

  const yTick = v => v >= 10 ? v.toLocaleString("he-IL") : v % 1 === 0 ? v : v.toFixed(1);

  if (hourChart) {
    hourChart.data.datasets[0].data = buckets;
    hourChart.data.datasets[0].backgroundColor = colors;
    hourChart.data.datasets[0].label = avgLabel;
    hourChart.data.datasets[1].data = weekBuckets;
    if (cityDataset) {
      if (hourChart.data.datasets[2]) {
        hourChart.data.datasets[2].data = cityBuckets;
        hourChart.data.datasets[2].label = state.pinnedCity;
      } else {
        hourChart.data.datasets.push(cityDataset);
      }
    } else {
      hourChart.data.datasets.splice(2);
    }
    hourChart.options.scales.yRight.display = !!cityBuckets;
    hourChart.options.scales.yRight.title.text = state.pinnedCity || "";
    hourChart.update("active"); return;
  }

  const datasets = [
    { label: avgLabel, data: buckets, yAxisID: "y", backgroundColor: colors, borderRadius: 5, borderSkipped: false, order: 2 },
    { label: "7 ימים אחרונים", data: weekBuckets, yAxisID: "y", type: "line", borderColor: "rgba(168,85,247,0.9)", backgroundColor: "rgba(168,85,247,0.15)", borderWidth: 2, pointRadius: 3, pointBackgroundColor: "rgba(168,85,247,0.9)", tension: 0.3, fill: false, order: 1 }
  ];
  if (cityDataset) datasets.push(cityDataset);

  hourChart = new Chart($("hourChart").getContext("2d"), {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: { display: true, position: "top", labels: { color: "#94a3b8", font: { size: 12 }, boxWidth: 16, padding: 12 } },
        tooltip: {
          rtl: true,
          backgroundColor: "rgba(14,17,23,0.95)", borderColor: "#252d3d", borderWidth: 1,
          titleColor: "#e2e8f0", bodyColor: "#94a3b8", padding: 12, cornerRadius: 8,
          callbacks: {
            title: i => `שעה ${i[0].label}`,
            label: i => {
              const v = i.raw.toLocaleString("he-IL");
              if (i.datasetIndex === 1) return ` 7 ימים אחרונים: ${v} התרעות`;
              if (i.datasetIndex === 2) return ` ${state.pinnedCity}: ${v} התרעות`;
              return ` ממוצע יומי (כלל): ${v} התרעות`;
            }
          }
        }
      },
      scales: {
        x: { grid: { color: "rgba(37,45,61,0.5)" }, ticks: { color: "#475569", font: { size: 11 } } },
        y: { position: "left", grid: { color: "rgba(37,45,61,0.5)" }, ticks: { color: "#475569", callback: yTick }, beginAtZero: true, title: { display: true, text: "ממוצע התרעות / יום (כלל)", color: "#475569", font: { size: 11 } } },
        yRight: { position: "right", display: !!cityBuckets, grid: { drawOnChartArea: false }, ticks: { color: "rgba(34,197,94,0.8)", callback: yTick }, beginAtZero: true, title: { display: !!cityBuckets, text: state.pinnedCity || "", color: "rgba(34,197,94,0.8)", font: { size: 11 } } }
      }
    }
  });
}

/* ===== Alert Map ===== */
let alertMap = null;
let heatLayer = null;
let mapDotMarkers = [];

function initMap() {
  if (alertMap) return;
  alertMap = L.map("alertMap", { zoomControl: true }).setView([31.5, 34.85], 8);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: '© <a href="https://carto.com">CARTO</a> © <a href="https://openstreetmap.org">OSM</a>',
    subdomains: "abcd",
    maxZoom: 19,
  }).addTo(alertMap);
}

async function loadMap() {
  initMap();
  try {
    const res = await fetch(`/api/map?${buildParams()}`);
    const data = await res.json();
    const { points, geocoded_total } = data;

    $("mapGeoBadge").textContent = `${geocoded_total.toLocaleString("he-IL")} מיקומים ממופים`;

    // Remove old layers
    if (heatLayer) { alertMap.removeLayer(heatLayer); heatLayer = null; }
    mapDotMarkers.forEach(m => alertMap.removeLayer(m));
    mapDotMarkers = [];

    if (!points.length) return;

    const maxCount = points[0].count;

    // Heat layer — each point weighted by count
    const heatData = points.map(p => [p.lat, p.lng, p.count / maxCount]);
    heatLayer = L.heatLayer(heatData, {
      radius: 30,
      blur: 22,
      maxZoom: 13,
      max: 1.0,
      gradient: { 0.0: "#22c55e", 0.35: "#84cc16", 0.6: "#eab308", 0.8: "#f97316", 1.0: "#ef4444" },
    }).addTo(alertMap);

    // Circle markers on top for interactivity (popup on click)
    points.forEach(p => {
      const r = p.count / maxCount;
      const color = r >= 0.8 ? "#ef4444" : r >= 0.6 ? "#f97316" : r >= 0.35 ? "#eab308" : "#22c55e";
      const radius = Math.max(4, Math.min(18, 4 + r * 14));
      const m = L.circleMarker([p.lat, p.lng], {
        radius, color, fillColor: color, fillOpacity: 0.15, weight: 1.5, opacity: 0.7,
      }).addTo(alertMap);
      m.bindPopup(
        `<strong>${p.area}</strong><br>` +
        `<span style="color:#94a3b8">${p.count.toLocaleString("he-IL")} התרעות</span>`
      );
      mapDotMarkers.push(m);
    });
  } catch(e) {
    logErr("Map load failed:", e);
  }
}

/* ===== Areas table ===== */
let _allAreasData = [];

function renderAreasTable(allAreas, filter) {
  _allAreasData = allAreas;
  const q = (filter || "").trim().toLowerCase();
  const filtered = q ? allAreas.filter(a => a.area.toLowerCase().includes(q)) : allAreas;
  const maxCount = allAreas[0]?.count || 1;

  $("areasTableCount").textContent = `${filtered.length.toLocaleString("he-IL")} אזורים`;

  const tbody = $("areasTableBody");
  tbody.innerHTML = filtered.map((a, i) => {
    const sel = state.selectedAreas.has(a.area);
    const pinned = state.pinnedCity === a.area;
    const pct = Math.round((a.count / maxCount) * 100);
    const rank = q ? allAreas.indexOf(a) + 1 : i + 1;
    return `<tr class="${sel ? "selected" : ""} ${pinned ? "pinned-row" : ""}" data-area="${a.area.replace(/"/g,'&quot;')}">
      <td>${rank}</td>
      <td class="area-name">${a.area}${pinned ? ' <span class="pin-indicator">📍</span>' : ''}</td>
      <td class="area-count">${a.count.toLocaleString("he-IL")}</td>
      <td class="bar-col"><div class="area-bar-bg"><div class="area-bar-fill" style="width:${pct}%"></div></div></td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("tr").forEach(row => {
    row.addEventListener("click", () => {
      const area = row.dataset.area;
      // pin/unpin for chart comparison (no page-wide filter change)
      if (state.pinnedCity === area) {
        setPinnedCity(null);
      } else {
        setPinnedCity(area);
      }
      // re-render rows to reflect pin state
      tbody.querySelectorAll("tr").forEach(r => {
        const isPinned = state.pinnedCity === r.dataset.area;
        r.classList.toggle("pinned-row", isPinned);
        const nameCell = r.querySelector(".area-name");
        if (nameCell) {
          nameCell.innerHTML = r.dataset.area + (isPinned ? ' <span class="pin-indicator">📍</span>' : '');
        }
      });
      render();
    });
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

    $("hourChart").closest(".chart-section").style.display = hasData ? "" : "none";
    $("areasSection").style.display                        = hasData ? "" : "none";

    if (hasData) {
      const cityHours = await fetchPinnedCityHours();
      renderCityWidget();
      renderHourChart(data.hour_daily_avg || data.hour_buckets, data.week_hour_daily_avg || [], data.total_days || 1, cityHours);
      loadMap();
      renderAreasTable(data.top_areas, $("areasTableSearch").value);
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
  renderCityWidget();

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

  $("areasTableSearch").addEventListener("input", () => {
    renderAreasTable(_allAreasData, $("areasTableSearch").value);
  });

  $("btnGeocodeMore").addEventListener("click", async () => {
    $("btnGeocodeMore").disabled = true;
    $("btnGeocodeMore").textContent = "⏳ ממפה...";
    try {
      await fetch("/api/geocode", { method: "POST" });
      $("mapGeoBadge").textContent = "ממפה ברקע... (כ-90 שניות)";
      setTimeout(loadMap, 95000);
    } finally {
      setTimeout(() => {
        $("btnGeocodeMore").disabled = false;
        $("btnGeocodeMore").textContent = "📍 טען מיקומים";
      }, 95000);
    }
  });

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
