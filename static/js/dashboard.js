/**
 * dashboard.js – live dashboard logic.
 *
 * initDashboard(sparklineData, initialReading) is called from the template.
 * It renders the sparkline chart immediately from server-side data, then
 * polls /api/current every 60 seconds to keep values fresh.
 */

const POLL_MS = 5_000; // 5 seconds – MQTT pushes every ~5 s

// Charging status → badge colour class
const STATUS_CLASSES = {
  charging:    'bg-green-900/40 text-green-400 border border-green-800/50',
  discharging: 'bg-blue-900/40 text-blue-400 border border-blue-800/50',
  full:        'bg-purple-900/40 text-purple-400 border border-purple-800/50',
  standby:     'bg-slate-800/60 text-slate-400 border border-slate-700',
  unknown:     'bg-slate-800/60 text-slate-400 border border-slate-700',
};

// ── SOC arc maths ────────────────────────────────────────────────────────────
// The SVG arc path spans from 10,70 to 110,70 (180° half-circle).
// Full arc length ≈ π * r = π * 55 ≈ 172.8
const ARC_LENGTH = Math.PI * 55;

function setArc(soc) {
  const arc = document.getElementById('soc-arc');
  if (!arc || soc == null) return;
  const offset = ARC_LENGTH - (ARC_LENGTH * Math.min(Math.max(soc, 0), 100)) / 100;
  arc.style.strokeDashoffset = offset;

  // colour: red < 20, amber 20-50, green > 50
  arc.style.stroke = soc < 20 ? '#ef4444' : soc < 50 ? '#f59e0b' : '#22c55e';
}

// ── DOM update helper ────────────────────────────────────────────────────────
function setText(id, value, decimals = 0, fallback = '—') {
  const el = document.getElementById(id);
  if (!el) return;
  const next = value != null ? Number(value).toFixed(decimals) : fallback;
  if (el.textContent !== next) {
    el.textContent = next;
    el.classList.remove('value-flash');
    void el.offsetWidth; // force reflow so animation restarts
    el.classList.add('value-flash');
  }
}

function applyReading(r) {
  if (!r || Object.keys(r).length === 0) return;

  // Battery
  setText('soc-value', r.battery_soc, 1);
  setArc(r.battery_soc);
  setText('battery-temp', r.battery_temp, 1);
  setText('battery-wh', r.battery_wh, 0);

  // Power
  setText('solar-power',  r.solar_power_w,  0);
  setText('ac-out-power', r.ac_out_power_w, 0);
  setText('dc-out-power', r.dc_out_power_w, 0);
  setText('ac-in-power',  r.ac_in_power_w,  0);
  setText('total-in',     r.total_in_w,     0);

  // Status badge
  const status = (r.charging_status || 'unknown').toLowerCase();
  const badge = document.getElementById('charging-badge');
  if (badge) {
    badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
    badge.className = 'mt-3 px-3 py-0.5 rounded-full text-xs font-medium ' +
                      (STATUS_CLASSES[status] || STATUS_CLASSES.unknown);
  }

  // Device online
  const devEl = document.getElementById('device-status');
  if (devEl) {
    devEl.textContent = r.device_online ? 'Online' : 'Offline';
    devEl.className = 'text-xl font-semibold ' + (r.device_online ? 'text-green-400' : 'text-red-400');
  }

  // Last updated — store timestamp for the ticker
  const tsEl = document.getElementById('last-updated');
  if (tsEl && r.timestamp) {
    const ts = r.timestamp.replace(/\+00:00$/, 'Z').replace(/(\.\d+)?([+-]\d{2}:\d{2})$/, (m, ms, tz) => (ms||'') + (tz === '+00:00' ? 'Z' : tz));
    const d = new Date(ts);
    if (!isNaN(d)) {
      tsEl._lastDate = d;
      tsEl.textContent = 'Updated just now';
    }
  }
}

// ── "X seconds ago" ticker ────────────────────────────────────────────────────
function startAgeTicker() {
  setInterval(() => {
    const tsEl = document.getElementById('last-updated');
    if (!tsEl || !tsEl._lastDate) return;
    const secs = Math.round((Date.now() - tsEl._lastDate.getTime()) / 1000);
    if (secs < 5)       tsEl.textContent = 'Updated just now';
    else if (secs < 60) tsEl.textContent = `Updated ${secs}s ago`;
    else                tsEl.textContent = `Updated ${Math.floor(secs/60)}m ago`;

    // dim the LIVE badge if data is stale (>30 s)
    const badge = document.getElementById('live-badge');
    if (badge) {
      if (secs > 30) {
        badge.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-slate-800/60 text-slate-400 border border-slate-700';
        badge.querySelector('span')?.classList.remove('animate-pulse');
      } else {
        badge.className = 'flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-green-900/40 text-green-400 border border-green-800/50';
        badge.querySelector('span')?.classList.add('animate-pulse');
      }
    }
  }, 1000);
}

// ── Sparkline chart ──────────────────────────────────────────────────────────
let sparkChart = null;

function buildSparkline(dataPoints) {
  const ctx = document.getElementById('sparkline-chart');
  if (!ctx) return;

  const labels = dataPoints.map(d => new Date(d.t.replace(/\+00:00$/, 'Z')));
  const values = dataPoints.map(d => d.v);

  if (sparkChart) {
    sparkChart.data.labels = labels;
    sparkChart.data.datasets[0].data = values;
    sparkChart.update();
    return;
  }

  sparkChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'SOC %',
        data: values,
        borderColor: '#f97316',
        backgroundColor: 'rgba(249,115,22,0.08)',
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      animation: false,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'minute', tooltipFormat: 'HH:mm' },
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: { color: '#64748b', maxTicksLimit: 8 },
        },
        y: {
          min: 0, max: 100,
          grid: { color: 'rgba(255,255,255,0.04)' },
          ticks: { color: '#64748b', callback: v => v + '%' },
        }
      }
    }
  });
}

// ── Polling ───────────────────────────────────────────────────────────────────
async function pollCurrent() {
  try {
    const res = await fetch('/api/current');
    if (!res.ok) return;
    const data = await res.json();
    applyReading(data);

    // Pulse the LIVE dot on every successful poll
    const dot = document.querySelector('#live-badge span');
    if (dot) {
      dot.style.transform = 'scale(1.6)';
      setTimeout(() => { dot.style.transform = ''; }, 200);
    }
  } catch (e) {
    console.warn('Dashboard poll failed:', e);
  }
}

async function refreshSparkline() {
  try {
    const rRes = await fetch('/api/readings?hours=2&step=60');
    if (rRes.ok) {
      const rows = await rRes.json();
      const points = rows
        .filter(r => r.battery_soc != null)
        .map(r => ({ t: r.timestamp, v: r.battery_soc }));
      buildSparkline(points);
    }
  } catch (e) {
    console.warn('Sparkline refresh failed:', e);
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────
function initDashboard(sparklineData, initialReading) {
  // Apply server-rendered data immediately (no flash of empty state).
  applyReading(initialReading);

  // Chart needs Chart.js + adapter – wait for them.
  if (typeof Chart !== 'undefined') {
    buildSparkline(sparklineData);
  } else {
    window.addEventListener('load', () => buildSparkline(sparklineData));
  }

  // Start age ticker (updates "X seconds ago" every second).
  startAgeTicker();

  // Poll live values every 5 s; refresh sparkline every 60 s.
  setInterval(pollCurrent, POLL_MS);
  setInterval(refreshSparkline, 60_000);
  // Fire both immediately after a short delay.
  setTimeout(pollCurrent, 500);
  setTimeout(refreshSparkline, 1000);
}
