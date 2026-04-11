import './style.css';

// ── States ────────────────────────────────────────────────────────────────────
const STATES = {
  CLEAR:  { icon: '🧘', text: 'NO ANOMALY',       bannerClass: 'banner-clear',  eventClass: 'event-clear',  bgMode: '' },
  MOTION: { icon: '⚠️', text: 'MOTION DETECTED',  bannerClass: 'banner-motion', eventClass: 'event-motion', bgMode: 'alert-mode' },
  DOOR:   { icon: '🚪', text: 'DOOR EVENT',        bannerClass: 'banner-door',   eventClass: 'event-door',   bgMode: '' },
};

let currentState    = STATES.CLEAR;
let THRESHOLD       = 5;
let lastEvent       = null;
let directionTimeout;
let currentPropertyId = 'Unknown';
let ws              = null;
let occupiedStart   = null;
let occupancyTimer  = null;

// ── Chart setup ───────────────────────────────────────────────────────────────
const ctx = document.getElementById('csi-chart').getContext('2d');

const gradientBlue   = ctx.createLinearGradient(0, 0, 0, 400);
gradientBlue.addColorStop(0, 'rgba(0, 195, 255, 0.5)');
gradientBlue.addColorStop(1, 'rgba(0, 195, 255, 0.05)');
const gradientRed    = ctx.createLinearGradient(0, 0, 0, 400);
gradientRed.addColorStop(0, 'rgba(255, 59, 59, 0.6)');
gradientRed.addColorStop(1, 'rgba(255, 59, 59, 0.05)');
const gradientYellow = ctx.createLinearGradient(0, 0, 0, 400);
gradientYellow.addColorStop(0, 'rgba(234, 179, 8, 0.5)');
gradientYellow.addColorStop(1, 'rgba(234, 179, 8, 0.05)');

const csiChart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: Array(100).fill(''),
    datasets: [
      {
        label: 'CSI Variance',
        data: Array(100).fill(0),
        borderColor: '#00c3ff',
        backgroundColor: gradientBlue,
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: Array(100).fill(0),
        pointBackgroundColor: '#ff3b3b',
        pointBorderWidth: Array(100).fill(0),
        pointBorderColor: Array(100).fill('transparent'),
      },
      {
        label: 'Threshold',
        data: Array(100).fill(THRESHOLD),
        borderColor: '#ff3b3b',
        borderWidth: 2,
        borderDash: [5, 5],
        fill: false,
        pointRadius: 0,
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 50, easing: 'easeOutQuad' },
    plugins: { legend: { labels: { color: '#cbd5e1' } } },
    scales: {
      y: {
        title: { display: true, text: 'Variance', color: '#cbd5e1', font: { size: 13, weight: 'bold' } },
        min: 0, max: 25,
        grid: { color: '#333333' },
        ticks: { color: '#cbd5e1', stepSize: 0.5 }
      },
      x: {
        title: { display: true, text: 'Time', color: '#cbd5e1', font: { size: 13, weight: 'bold' } },
        grid: { display: false }
      }
    }
  }
});

// ── Subcarrier sparkline chart ────────────────────────────────────────────────
const subCtx = document.getElementById('sub-chart').getContext('2d');
const subChart = new Chart(subCtx, {
  type: 'bar',
  data: {
    labels: ['SC19','SC20','SC21','SC22','SC23','SC24','SC25','SC26','SC38','SC39'],
    datasets: [{
      label: 'Variance',
      data: Array(10).fill(0),
      backgroundColor: 'rgba(0,195,255,0.6)',
      borderColor: '#00c3ff',
      borderWidth: 1,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 100 },
    plugins: { legend: { display: false } },
    scales: {
      y: { min: 0, ticks: { color: '#cbd5e1', stepSize: 1 }, grid: { color: '#333' } },
      x: { ticks: { color: '#cbd5e1', font: { size: 10 } }, grid: { display: false } }
    }
  }
});

// ── Heatmap chart (24h) ───────────────────────────────────────────────────────
const hmCtx = document.getElementById('heatmap-chart').getContext('2d');
const heatmapChart = new Chart(hmCtx, {
  type: 'bar',
  data: {
    labels: Array.from({length: 24}, (_, i) => `${i}h`),
    datasets: [{
      label: 'Motion Events',
      data: Array(24).fill(0),
      backgroundColor: 'rgba(255,59,59,0.5)',
      borderColor: '#ff3b3b',
      borderWidth: 1,
    }]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 200 },
    plugins: { legend: { display: false } },
    scales: {
      y: { min: 0, ticks: { color: '#cbd5e1' }, grid: { color: '#333' } },
      x: { ticks: { color: '#cbd5e1', font: { size: 9 } }, grid: { display: false } }
    }
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtDuration(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function setConnectionBadge(status) {
  const badge = document.getElementById('conn-badge');
  if (!badge) return;
  const map = {
    calibrating: ['🔧 Calibrating', '#eab308'],
    live:        ['🟢 Live',        '#00ff88'],
    disconnected:['🔴 Disconnected','#ff3b3b'],
  };
  const [text, color] = map[status] || map.disconnected;
  badge.innerText = text;
  badge.style.color = color;
}

function renderVariance(variance) {
  const display = document.getElementById('current-variance-display');
  if (!display) return;
  display.innerText = variance.toFixed(2);
  if (variance >= THRESHOLD) {
    display.style.color = '#ff3b3b';
    display.style.textShadow = '0 0 25px rgba(255,59,59,0.8)';
  } else {
    display.style.color = '#00c3ff';
    display.style.textShadow = '0 0 15px rgba(0,195,255,0.6)';
  }
}

function updateSessionStats(session) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
  set('stat-uptime',       fmtDuration(session.uptime_s));
  set('stat-motion-count', session.motion_events);
  set('stat-clear-var',    session.avg_clear_var.toFixed(2));
  set('stat-motion-var',   session.avg_motion_var.toFixed(2));
}

function updateOccupancyTimer(occupiedSince) {
  const el = document.getElementById('occupancy-timer');
  if (!el) return;
  if (occupiedSince) {
    const secs = Math.floor(Date.now() / 1000 - occupiedSince);
    el.innerText = `Occupied for: ${fmtDuration(secs)}`;
    el.style.color = '#ff3b3b';
  } else {
    el.innerText = 'Room: Clear';
    el.style.color = '#00ff88';
  }
}

// ── Event log ─────────────────────────────────────────────────────────────────
const eventLog = document.getElementById('event-log');

function addLogEvent(message, eventClass) {
  const li = document.createElement('li');
  li.className = eventClass;
  const now = new Date();
  const icon = eventClass === 'event-clear' ? '🧘' : '⚠️';
  const color = eventClass === 'event-clear' ? '#00ff88' : '#ff3b3b';
  li.innerHTML = `
    <span class="timestamp">[${now.toLocaleTimeString()}]</span>
    <span style="color:${color};font-weight:600;">${icon} ${message}</span>
  `;
  eventLog.prepend(li);
  if (eventLog.children.length > 20) eventLog.removeChild(eventLog.lastChild);
}

// ── User alert panel ──────────────────────────────────────────────────────────
function emitUserAlert(type) {
  const now = new Date();
  const detected_time = now.toLocaleTimeString('en-US', { hour12: false });
  const telegram_time = new Date(now.getTime() + 2000).toLocaleTimeString('en-US', { hour12: false });

  const map = {
    motion: { id: 'log-alert',      timeId: 'time-alert',      icon: '🚨', title: 'Motion Detected' },
    door:   { id: 'log-door',       timeId: 'time-door',       icon: '🚪', title: 'Door Opened' },
    entry:  { id: 'log-entry-exit', timeId: 'time-entry-exit', icon: '⬅️', title: 'Entry Detected' },
    exit:   { id: 'log-entry-exit', timeId: 'time-entry-exit', icon: '➡️', title: 'Exit Detected' },
  };
  const cfg = map[type]; if (!cfg) return;
  const container = document.getElementById(cfg.id);
  const timeLabel = document.getElementById(cfg.timeId);
  const animLabel = document.getElementById(`anim-${cfg.timeId}`);
  if (!container || !timeLabel) return;

  if (type === 'motion') {
    timeLabel.innerText = telegram_time;
    if (animLabel) { animLabel.classList.remove('fly-animate'); void animLabel.offsetWidth; animLabel.classList.add('fly-animate'); }
  }

  const emptyLog = container.querySelector('.empty-log');
  if (emptyLog) emptyLog.remove();

  const telegramHTML = type === 'motion'
    ? `<span style="color:var(--text-secondary)">Sent: <span style="color:var(--text-primary);font-weight:bold">${telegram_time}</span></span>`
    : `<span style="color:var(--text-secondary)">Sent: <span style="color:#475569;font-style:italic">Not Sent</span></span>`;

  const card = document.createElement('div');
  card.className = 'user-alert-card';
  card.innerHTML = `
    <div class="alert-header" style="font-size:1rem;color:var(--text-primary);margin-bottom:5px">${cfg.icon} ${cfg.title}</div>
    <div style="display:flex;justify-content:space-between;font-size:0.9rem">
      <span style="color:var(--text-secondary)">Detected: <span style="color:var(--text-primary);font-weight:bold">${detected_time}</span></span>
      ${telegramHTML}
    </div>
  `;
  container.prepend(card);

  if (type === 'motion') {
    const tableBody = document.getElementById('telegram-table-body');
    if (tableBody) {
      const d = new Date();
      const dateStr = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
      const row = document.createElement('div');
      row.style = 'display:grid;grid-template-columns:2fr 3fr 2fr 1.5fr;gap:15px;padding:15px 20px;border-bottom:1px solid var(--border-color);font-size:0.9rem;color:var(--text-primary);animation:slideIn 0.3s ease-out';
      row.innerHTML = `<div>${currentPropertyId}</div><div>🚨 Motion at ${detected_time}</div><div>${dateStr} ${telegram_time}</div><div style="color:#00ff88">Sent ✔️</div>`;
      tableBody.prepend(row);
      document.getElementById('telegram-empty-msg')?.remove();
      const totalEl = document.getElementById('telegram-total');
      if (totalEl) totalEl.innerText = ((parseInt(totalEl.innerText.replace(/,/g,''),10)||0)+1).toLocaleString();
    }
  }
}

// ── Direction indicator ───────────────────────────────────────────────────────
function triggerDirection(type) {
  const indicator = document.getElementById('direction-indicator');
  const dirText   = document.getElementById('direction-text');
  if (type === 'ENTRY') {
    indicator.className = 'direction-indicator direction-entry';
    dirText.innerText = '⬅️ ENTRY DETECTED';
    addLogEvent('ENTRY DETECTED', 'event-door');
    emitUserAlert('entry');
  } else {
    indicator.className = 'direction-indicator direction-exit';
    dirText.innerText = '➡️ EXIT DETECTED';
    addLogEvent('EXIT DETECTED', 'event-door');
    emitUserAlert('exit');
  }
  clearTimeout(directionTimeout);
  directionTimeout = setTimeout(() => indicator.classList.add('hidden'), 4000);
}

// ── State machine ─────────────────────────────────────────────────────────────
const statusBanner = document.getElementById('status-banner');
const statusIcon   = document.getElementById('status-icon');
const statusText   = document.getElementById('status-text');

function updateState(newState) {
  if (currentState === newState) return;

  let newEvent = newState === STATES.MOTION ? 'MOTION' : newState === STATES.DOOR ? 'DOOR' : null;
  if (newEvent) {
    if (lastEvent === 'MOTION' && newEvent === 'DOOR') triggerDirection('ENTRY');
    else if (lastEvent === 'DOOR' && newEvent === 'MOTION') triggerDirection('EXIT');
    lastEvent = newEvent;
  }

  statusBanner.classList.remove(currentState.bannerClass);
  if (currentState.bgMode) document.body.classList.remove(currentState.bgMode);
  currentState = newState;
  statusBanner.classList.add(currentState.bannerClass);
  if (currentState.bgMode) document.body.classList.add(currentState.bgMode);

  statusIcon.innerText = currentState.icon;
  statusText.innerText = currentState.text;
  statusIcon.classList.remove('status-pop'); void statusIcon.offsetWidth; statusIcon.classList.add('status-pop');
  statusText.classList.remove('status-pop'); void statusText.offsetWidth; statusText.classList.add('status-pop');

  if (currentState === STATES.MOTION) { addLogEvent('MOTION DETECTED', 'event-motion'); emitUserAlert('motion'); }
  else if (currentState === STATES.DOOR) { addLogEvent('DOOR EVENT', 'event-door'); emitUserAlert('door'); }
  else addLogEvent('No Anomaly', 'event-clear');
}

// ── Theme toggle ──────────────────────────────────────────────────────────────
const themeBtn     = document.getElementById('theme-toggle-btn');
const userThemeBtn = document.getElementById('user-theme-toggle');

function applyTheme(dark) {
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  themeBtn.innerText = dark ? '☀️ Switch to Light Mode' : '🌙 Switch to Dark Mode';
  if (userThemeBtn) userThemeBtn.innerText = dark ? '☀️ Light' : '🌙 Dark';
  const tc = dark ? '#cbd5e1' : '#475569';
  const gc = dark ? '#333333' : '#e2e8f0';
  csiChart.options.scales.x.title.color = tc;
  csiChart.options.scales.y.title.color = tc;
  csiChart.options.scales.y.ticks.color = tc;
  csiChart.options.scales.y.grid.color  = gc;
  csiChart.options.plugins.legend.labels.color = tc;
  csiChart.update('none');
}

themeBtn.addEventListener('click', () => {
  applyTheme(document.documentElement.getAttribute('data-theme') !== 'dark');
});
if (userThemeBtn) userThemeBtn.addEventListener('click', () => themeBtn.click());

document.getElementById('logout-btn')?.addEventListener('click', () => {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  document.getElementById('app').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';
  // Reset form
  document.getElementById('login-form').reset();
  document.getElementById('user-role-display').innerText = '';
  eventLog.innerHTML = '';
});

document.getElementById('user-logout-btn')?.addEventListener('click', () => {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  document.getElementById('user-app').style.display = 'none';
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('login-form').reset();
});

// ── Sync user dashboard status banner + counters from WS data ─────────────────
function updateUserDashboard(data) {
  // Mirror status banner
  const banner = document.getElementById('user-status-banner');
  const icon   = document.getElementById('user-status-icon');
  const text   = document.getElementById('user-status-text');
  if (banner && icon && text) {
    if (data.status === 'motion') {
      banner.className = 'status-banner banner-motion';
      icon.innerText = '⚠️'; text.innerText = 'MOTION DETECTED';
    } else {
      banner.className = 'status-banner banner-clear';
      icon.innerText = '🧘'; text.innerText = 'NO ANOMALY';
    }
  }
  // Occupancy timer
  const ot = document.getElementById('user-occupancy-timer');
  if (ot) {
    if (data.occupied_since) {
      const secs = Math.floor(Date.now() / 1000 - data.occupied_since);
      ot.innerText = `Occupied: ${fmtDuration(secs)}`;
      ot.style.color = '#ff3b3b';
    } else {
      ot.innerText = 'Room: Clear';
      ot.style.color = '#00ff88';
    }
  }
  // Motion count
  const mc = document.getElementById('user-motion-count');
  if (mc && data.session) mc.innerText = data.session.motion_events;
}
// ── Event log toggle ──────────────────────────────────────────────────────────
document.getElementById('toggle-events-btn').addEventListener('click', function() {
  eventLog.classList.toggle('collapsed');
  this.innerText = eventLog.classList.contains('collapsed') ? '▼' : '▲';
});

// ── Recalibrate button ────────────────────────────────────────────────────────
const calibrateBtn       = document.getElementById('calibrate-btn');
const calProgressWrapper = document.getElementById('cal-progress-wrapper');
const calProgress        = document.getElementById('cal-progress');

calibrateBtn.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    addLogEvent('Not connected to backend', 'event-motion'); return;
  }
  ws.send(JSON.stringify({ cmd: 'recalibrate' }));
  calibrateBtn.classList.add('hidden');
  calProgressWrapper.classList.remove('hidden');
  calProgress.style.width = '0%';
  addLogEvent('Recalibration requested...', 'event-clear');
  // Progress bar animates while calibrating flag is true (driven by WS messages)
});

// ── Sensitivity multiplier ────────────────────────────────────────────────────
const mulSlider = document.getElementById('mul-slider');
const mulVal    = document.getElementById('mul-val');
if (mulSlider) {
  mulSlider.addEventListener('change', () => {
    const v = parseFloat(mulSlider.value);
    mulVal.innerText = v.toFixed(1) + 'x';
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ cmd: 'set_mul', value: v }));
      addLogEvent(`Sensitivity set to ${v.toFixed(1)}x — recalibrating`, 'event-clear');
    }
  });
}

// ── Mute button ───────────────────────────────────────────────────────────────
document.getElementById('mute-btn')?.addEventListener('click', () => {
  const mins = parseInt(document.getElementById('mute-mins').value || '10', 10);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ cmd: 'mute', minutes: mins }));
    addLogEvent(`Alerts muted for ${mins} minutes`, 'event-clear');
  }
});

// ── Export button ─────────────────────────────────────────────────────────────
document.getElementById('export-btn')?.addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ cmd: 'export' }));
  }
});

// ── WebSocket ─────────────────────────────────────────────────────────────────
function initBackend() {
  setConnectionBadge('disconnected');
  const wsInput = document.getElementById('ws-url');
  const WS_URL  = (wsInput && wsInput.value.trim()) ? wsInput.value.trim() : 'ws://localhost:8000/ws';
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    addLogEvent('Connected to backend', 'event-clear');
    setConnectionBadge('live');
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      // Export download
      if (data.export !== undefined) {
        const blob = new Blob([data.export], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `csi_session_${Date.now()}.csv`;
        a.click();
        return;
      }

      // Calibration state
      if (data.calibrating) {
        setConnectionBadge('calibrating');
        calProgressWrapper.classList.remove('hidden');
        calibrateBtn.classList.add('hidden');
        // Animate progress bar while calibrating
        calProgress.style.width = Math.min(
          parseFloat(calProgress.style.width || '0') + 1, 95
        ) + '%';
        return;
      } else {
        setConnectionBadge('live');
        calProgress.style.width = '100%';
        setTimeout(() => {
          calProgressWrapper.classList.add('hidden');
          calibrateBtn.classList.remove('hidden');
          calProgress.style.width = '0%';
        }, 600);
      }

      // Threshold from backend
      if (data.threshold && data.threshold > 0) {
        THRESHOLD = data.threshold;
        const tv = document.getElementById('threshold-val');
        if (tv) tv.innerText = THRESHOLD.toFixed(2);
        csiChart.data.datasets[1].data = Array(100).fill(THRESHOLD);
      }

      // Baseline display
      const bv = document.getElementById('baseline-val');
      if (bv && data.baseline) bv.innerText = data.baseline.toFixed(2);

      const mv = document.getElementById('mul-display');
      if (mv && data.threshold_mul) mv.innerText = data.threshold_mul.toFixed(1) + 'x';

      // Main variance + chart
      const variance = data.variance;
      renderVariance(variance);

      const dataset = csiChart.data.datasets[0];
      dataset.data.shift(); dataset.data.push(variance);
      const isSpike = variance >= THRESHOLD;
      dataset.pointRadius.shift();      dataset.pointRadius.push(isSpike ? 6 : 0);
      dataset.pointBorderWidth.shift(); dataset.pointBorderWidth.push(isSpike ? 4 : 0);
      dataset.pointBorderColor.shift(); dataset.pointBorderColor.push(isSpike ? 'rgba(255,59,59,0.4)' : 'transparent');

      let nextState = STATES.CLEAR;
      if (data.status === 'motion') {
        nextState = STATES.MOTION;
        dataset.borderColor = '#ff3b3b'; dataset.backgroundColor = gradientRed;
      } else {
        dataset.borderColor = '#00c3ff'; dataset.backgroundColor = gradientBlue;
      }
      updateState(nextState);
      csiChart.update();

      // Subcarrier sparklines
      if (data.subcarriers) {
        subChart.data.datasets[0].data = data.subcarriers;
        const maxSub = Math.max(...data.subcarriers);
        subChart.data.datasets[0].backgroundColor = data.subcarriers.map(
          v => v === maxSub ? 'rgba(255,59,59,0.8)' : 'rgba(0,195,255,0.6)'
        );
        subChart.update('none');
      }

      // Heatmap
      if (data.heatmap) {
        heatmapChart.data.datasets[0].data = data.heatmap;
        heatmapChart.update('none');
      }

      // Session stats
      if (data.session) updateSessionStats(data.session);

      // Occupancy timer
      updateOccupancyTimer(data.occupied_since);

      // Sync user dashboard if open
      updateUserDashboard(data);

    } catch (err) {
      console.error('WS parse error:', err);
    }
  };

  ws.onclose = () => {
    setConnectionBadge('disconnected');
    addLogEvent('Connection lost. Retrying in 3s...', 'event-motion');
    setTimeout(initBackend, 3000);
  };

  ws.onerror = () => {};
}

// ── Occupancy timer tick ──────────────────────────────────────────────────────
setInterval(() => {
  const el = document.getElementById('occupancy-timer');
  if (!el) return;
  // Timer is updated from WS data, this just keeps it ticking
}, 1000);

// ── Login screen role toggle ───────────────────────────────────────────────────
const wsUrlGroup = document.getElementById('ws-url-group');
document.querySelectorAll('input[name="role"]').forEach(radio => {
  radio.addEventListener('change', () => {
    wsUrlGroup.style.display = radio.value === 'Admin' ? 'flex' : 'none';
  });
});
// Set initial state
wsUrlGroup.style.display = 'flex'; // Admin is checked by default

// ── Credentials ───────────────────────────────────────────────────────────────
const CREDENTIALS = {
  Admin: { username: 'admin', password: 'saygex@2026' },
  'Property Owner': { username: 'owner', password: 'saygex@2026' },
};

// ── Login ─────────────────────────────────────────────────────────────────────
document.getElementById('login-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const propertyId   = document.getElementById('property-id').value.trim();
  const password     = document.getElementById('password').value;
  const selectedRole = document.querySelector('input[name="role"]:checked').value;
  const loginError   = document.getElementById('login-error');

  const creds = CREDENTIALS[selectedRole];
  if (!creds || propertyId !== creds.username || password !== creds.password) {
    if (loginError) loginError.style.display = 'block';
    return;
  }

  if (loginError) loginError.style.display = 'none';
  currentPropertyId = propertyId;
  document.getElementById('login-screen').style.display = 'none';

  const roleDisplay = document.getElementById('user-role-display');
  if (roleDisplay) roleDisplay.innerText = `Role: ${selectedRole}`;

  if (selectedRole === 'Admin') {
    document.getElementById('app').style.display = 'flex';
    document.getElementById('admin-controls').style.display = 'block';
    document.getElementById('owner-controls').style.display = 'none';
    addLogEvent(`Authenticated: ${propertyId} [Admin]`, 'event-clear');
  } else {
    document.getElementById('user-app').style.display = 'flex';
  }

  initBackend();
});
