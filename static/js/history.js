/**
 * history.js – history page charts and data table.
 *
 * initHistory() is called from the template.
 * setRange(hours, btn) is called by the time-range toggle buttons.
 */

let socChart   = null;
let powerChart = null;
let energyChart = null;
let currentHours = 24;

// ── Shared chart defaults ─────────────────────────────────────────────────────
const GRID_COLOR  = 'rgba(255,255,255,0.04)';
const TICK_COLOR  = '#64748b';
const FONT        = { family: 'ui-sans-serif, system-ui, sans-serif', size: 11 };
Chart.defaults.color = TICK_COLOR;
Chart.defaults.font  = FONT;

function timeScaleX() {
  return {
    type: 'time',
    time: { tooltipFormat: 'dd MMM HH:mm' },
    grid: { color: GRID_COLOR },
    ticks: { color: TICK_COLOR, maxTicksLimit: 8, source: 'auto' },
  };
}

function linearScaleY(label, min, max) {
  return {
    min, max,
    grid: { color: GRID_COLOR },
    ticks: { color: TICK_COLOR, callback: v => v + (label || '') },
    title: { display: false },
  };
}

// ── SOC chart ────────────────────────────────────────────────────────────────
function toDate(ts) {
  return new Date(ts.replace(/\+00:00$/, 'Z'));
}

function buildSocChart(rows) {
  const ctx = document.getElementById('soc-chart');
  if (!ctx) return;
  const labels = rows.map(r => toDate(r.timestamp));
  const data   = rows.map(r => r.battery_soc);

  const ds = [{
    label: 'SOC %',
    data,
    borderColor: '#f97316',
    backgroundColor: 'rgba(249,115,22,0.07)',
    fill: true, tension: 0.4, pointRadius: 0, borderWidth: 2,
  }];

  if (socChart) {
    socChart.data.labels = labels;
    socChart.data.datasets[0].data = data;
    socChart.update();
    return;
  }
  socChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: ds },
    options: {
      responsive: true, animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: { x: timeScaleX(), y: linearScaleY('%', 0, 100) },
    }
  });
}

// ── Power chart ───────────────────────────────────────────────────────────────
function buildPowerChart(rows) {
  const ctx = document.getElementById('power-chart');
  if (!ctx) return;
  const labels = rows.map(r => toDate(r.timestamp));

  const datasets = [
    { label: 'Solar (W)',    data: rows.map(r => r.solar_power_w),  borderColor: '#facc15', backgroundColor: 'rgba(250,204,21,0.08)',  fill: false, tension: 0.4, pointRadius: 0, borderWidth: 2 },
    { label: 'AC In (W)',    data: rows.map(r => r.ac_in_power_w),  borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.08)',   fill: false, tension: 0.4, pointRadius: 0, borderWidth: 2 },
    { label: 'AC Out (W)',   data: rows.map(r => r.ac_out_power_w), borderColor: '#60a5fa', backgroundColor: 'rgba(96,165,250,0.08)',   fill: false, tension: 0.4, pointRadius: 0, borderWidth: 2 },
    { label: 'DC/USB (W)',   data: rows.map(r => r.dc_out_power_w), borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.08)', fill: false, tension: 0.4, pointRadius: 0, borderWidth: 1.5 },
  ];

  if (powerChart) {
    powerChart.data.labels = labels;
    datasets.forEach((ds, i) => { powerChart.data.datasets[i].data = ds.data; });
    powerChart.update();
    return;
  }
  powerChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, animation: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: TICK_COLOR, boxWidth: 12, padding: 16 },
        },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: { x: timeScaleX(), y: linearScaleY('W', 0, undefined) },
    }
  });
}

// ── Daily energy chart ────────────────────────────────────────────────────────
function buildEnergyChart(rows) {
  const ctx = document.getElementById('energy-chart');
  if (!ctx) return;
  const labels = rows.map(r => r.date);

  const datasets = [
    { label: 'Solar (kWh)',      data: rows.map(r => r.solar_kwh),     backgroundColor: 'rgba(250,204,21,0.7)' },
    { label: 'Charged (kWh)',    data: rows.map(r => r.charge_kwh),    backgroundColor: 'rgba(74,222,128,0.7)' },
    { label: 'Discharged (kWh)', data: rows.map(r => r.discharge_kwh), backgroundColor: 'rgba(96,165,250,0.7)' },
  ];

  if (energyChart) {
    energyChart.data.labels = labels;
    datasets.forEach((ds, i) => { energyChart.data.datasets[i].data = ds.data; });
    energyChart.update();
    return;
  }
  energyChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true, animation: false,
      plugins: {
        legend: {
          position: 'top',
          labels: { color: TICK_COLOR, boxWidth: 12, padding: 16 },
        },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: {
          grid: { color: GRID_COLOR },
          ticks: { color: TICK_COLOR },
        },
        y: linearScaleY(' kWh', 0, undefined),
      }
    }
  });
}

// ── Energy table ─────────────────────────────────────────────────────────────
function buildEnergyTable(rows) {
  const tbody = document.getElementById('energy-table');
  if (!tbody) return;

  if (!rows || rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-slate-500">No data yet.</td></tr>';
    return;
  }

  const sorted = [...rows].reverse();
  tbody.innerHTML = sorted.map(r => `
    <tr class="border-b border-slate-800/50 hover:bg-slate-800/20 transition-colors">
      <td class="py-2.5 pr-4 font-medium">${r.date}</td>
      <td class="py-2.5 pr-4 text-yellow-400">${fmt(r.solar_kwh)}</td>
      <td class="py-2.5 pr-4 text-green-400">${fmt(r.charge_kwh)}</td>
      <td class="py-2.5 pr-4 text-blue-400">${fmt(r.discharge_kwh)}</td>
      <td class="py-2.5 text-slate-300">${fmt(r.usage_kwh)}</td>
    </tr>
  `).join('');
}

function fmt(v) {
  return v != null ? Number(v).toFixed(2) : '—';
}

// ── Data loading ──────────────────────────────────────────────────────────────
async function loadReadings(hours) {
  // Downsample: 1pt/min for ≤24h, 1pt/5min for 7d, 1pt/30min for 30d
  const step = hours <= 24 ? 60 : hours <= 168 ? 300 : 1800;
  const res = await fetch(`/api/readings?hours=${hours}&step=${step}`);
  if (!res.ok) return [];
  return res.json();
}

async function loadEnergy(days) {
  const res = await fetch(`/api/energy?days=${days}`);
  if (!res.ok) return [];
  return res.json();
}

async function refresh(hours) {
  try {
    const [readings, energy] = await Promise.all([
      loadReadings(hours),
      loadEnergy(Math.ceil(hours / 24) + 1),
    ]);
    buildSocChart(readings);
    buildPowerChart(readings);
    buildEnergyChart(energy);
    buildEnergyTable(energy);
  } catch (e) {
    console.error('History refresh failed:', e);
  }
}

// ── Range toggle ─────────────────────────────────────────────────────────────
function setRange(hours, btn) {
  currentHours = hours;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active-range'));
  if (btn) btn.classList.add('active-range');
  refresh(hours);
}

// ── Entry point ───────────────────────────────────────────────────────────────
function initHistory() {
  refresh(currentHours);
  // Auto-refresh every 5 minutes.
  setInterval(() => refresh(currentHours), 300_000);
}
