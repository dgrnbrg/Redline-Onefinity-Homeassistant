/**
 * Application state manager, sequencer canvas, auto-sync timer, and pause manager.
 */

const App = (() => {
  let config = {};
  let stagedJobs = [];
  let fixtureSlots = [];
  let activeStrategy = "part_by_part";
  let activePreviewJobId = null;
  let activeGeometryPoints = [];

  // Playback state
  let isPlaying = false;
  let playDirection = 1; // 1 = forward, -1 = reverse
  let playSpeed = 1.0;
  let playAnimFrame = null;
  let currentScrubIdx = 0;

  // Auto-sync idle timer
  let autoSyncTimer = null;
  let autoSyncCountdownInterval = null;
  let remainingSeconds = 10;

  const WCS_OPTIONS = ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3"];

  async function init() {
    GcodeViewer.init('glCanvas');
    await loadConfig();
    await checkControllerStatus();
    await refreshStaged();

    // Check controller status periodically
    setInterval(checkControllerStatus, 10000);
  }

  async function loadConfig() {
    try {
      const res = await fetch('/api/config');
      config = await res.json();
    } catch (e) {
      console.warn("Could not load config", e);
    }
  }

  async function checkControllerStatus() {
    const badge = document.getElementById('connBadge');
    try {
      const res = await fetch('/api/controller/status');
      const data = await res.json();
      if (data.connected) {
        badge.className = 'badge badge-conn-ok';
        badge.innerHTML = `<span class="dot">●</span> Controller: Connected (${data.folder})`;
      } else {
        badge.className = 'badge badge-conn-off';
        badge.innerHTML = `<span class="dot">●</span> Controller: Offline`;
      }
    } catch {
      badge.className = 'badge badge-conn-off';
      badge.innerHTML = `<span class="dot">●</span> Controller: Offline`;
    }
  }

  async function refreshStaged() {
    const container = document.getElementById('stagedJobsList');
    container.innerHTML = `<div style="color: var(--text-muted); font-size: 0.85rem; padding: 12px 0;">Scanning...</div>`;
    try {
      const res = await fetch('/api/staged');
      const data = await res.json();
      stagedJobs = data.jobs || [];
      renderStagedJobs();
    } catch (e) {
      container.innerHTML = `<div style="color: var(--danger); font-size: 0.85rem;">Error loading staged jobs: ${e.message}</div>`;
    }
  }

  function renderStagedJobs() {
    const container = document.getElementById('stagedJobsList');
    if (!stagedJobs || stagedJobs.length === 0) {
      container.innerHTML = `
        <div class="card" style="text-align: center; color: var(--text-muted); font-size: 0.85rem;">
          No G-code jobs found in watched folder.<br>
          <small>Export .ngc files from CAM into your watched folder.</small>
        </div>`;
      return;
    }

    container.innerHTML = stagedJobs.map(job => {
      const toolTags = job.tools_used.map(t => `<span class="badge" style="background: rgba(14,165,233,0.15); color: var(--accent); padding: 2px 6px; font-size: 0.75rem;">T${t}</span>`).join(' ');
      const multiBadge = job.is_multi_file
        ? `<span class="badge" style="background: rgba(168,85,247,0.2); color: #c084fc; padding: 2px 6px; font-size: 0.75rem;">Multi-file (${job.subfiles_count} ops)</span>`
        : '';
      const zOrigin = job.z_origin || {};
      const zBadgeColor = zOrigin.origin === 'machine_bed' ? 'rgba(16,185,129,0.2); color: var(--success);' : (zOrigin.origin === 'top_of_stock' ? 'rgba(14,165,233,0.2); color: var(--accent);' : 'rgba(245,158,11,0.2); color: var(--warning);');

      return `
        <div class="card" style="margin-bottom: 8px;">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px;">
            <div>
              <strong style="font-size: 0.95rem;">${escapeHtml(job.name)}</strong>
              ${multiBadge}
            </div>
            <button class="btn btn-sm btn-primary" onclick="App.addJobToSequence('${job.id}')">+ Add to Run</button>
          </div>
          <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 6px;">
            ${toolTags}
            <span class="badge" style="background: ${zBadgeColor} padding: 2px 6px; font-size: 0.75rem;" title="${escapeHtml(zOrigin.rationale || '')}">
              Z: ${escapeHtml(zOrigin.label || 'Unknown')}
            </span>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted);">
            <span>${job.total_lines} lines</span>
            <span>Cut Z: ${job.extents.min_cutting_z} to ${job.extents.max_cutting_z} mm</span>
            <a href="javascript:void(0)" onclick="App.previewJob('${job.id}')" style="color: var(--accent); text-decoration: none;">View 3D ↗</a>
          </div>
        </div>
      `;
    }).join('');
  }

  function addJobToSequence(jobId) {
    const job = stagedJobs.find(j => j.id === jobId);
    if (!job) return;

    // Pick next available WCS
    const usedWcs = fixtureSlots.map(s => s.wcs);
    let nextWcs = "G54";
    for (const opt of WCS_OPTIONS) {
      if (!usedWcs.includes(opt)) {
        nextWcs = opt;
        break;
      }
    }

    const slotId = `slot_${Date.now()}_${Math.random().toString(36).substr(2, 4)}`;
    fixtureSlots.push({
      slot_id: slotId,
      job_id: job.id,
      job_name: job.name,
      wcs: nextWcs,
      z_shift: 0.0,
      is_air_carve: false,
      vacuum_mode: "default",
      air_assist_mode: "default",
      pauses: []
    });

    renderFixtureSlots();
    previewJob(job.id);
    scheduleAutoSync();
  }

  function renderFixtureSlots() {
    const container = document.getElementById('fixtureSlotsContainer');
    if (!fixtureSlots || fixtureSlots.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 24px 0; color: var(--text-muted); font-size: 0.85rem;">
          No jobs in fixture sequence yet.<br>Click <strong>"+ Add to Run"</strong> from the staging bin below.
        </div>`;
      updateAirCarveBanner();
      return;
    }

    container.innerHTML = fixtureSlots.map((slot, idx) => {
      const wcsSelect = WCS_OPTIONS.map(opt => `<option value="${opt}" ${opt === slot.wcs ? 'selected' : ''}>${opt}</option>`).join('');
      const pauseCount = slot.pauses.length;

      return `
        <div class="slot-card" id="${slot.slot_id}">
          <div class="slot-header">
            <span>Slot ${idx + 1}: ${escapeHtml(slot.job_name)}</span>
            <div class="btn-group">
              <button class="btn btn-sm" onclick="App.moveSlot(${idx}, -1)" ${idx === 0 ? 'disabled' : ''}>▲</button>
              <button class="btn btn-sm" onclick="App.moveSlot(${idx}, 1)" ${idx === fixtureSlots.length - 1 ? 'disabled' : ''}>▼</button>
              <button class="btn btn-sm" onclick="App.duplicateSlot(${idx})">📋</button>
              <button class="btn btn-sm" style="color: var(--danger);" onclick="App.removeSlot(${idx})">✕</button>
            </div>
          </div>

          <div class="slot-row">
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;">WCS:</label>
              <select onchange="App.updateSlot('${slot.slot_id}', 'wcs', this.value)" style="font-weight: 700;">
                ${wcsSelect}
              </select>
            </div>
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;">Z-Shift (mm):</label>
              <input type="number" step="0.1" value="${slot.z_shift}" style="width: 80px;" onchange="App.updateSlot('${slot.slot_id}', 'z_shift', parseFloat(this.value)||0)">
            </div>
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;">Air Carve:</label>
              <label style="display: flex; align-items: center; gap: 4px; cursor: pointer; padding-top: 6px;">
                <input type="checkbox" ${slot.is_air_carve ? 'checked' : ''} onchange="App.updateSlot('${slot.slot_id}', 'is_air_carve', this.checked)">
                <span style="font-weight: 600; color: ${slot.is_air_carve ? 'var(--warning)' : 'inherit'};">Dry Run (+25mm)</span>
              </label>
            </div>
          </div>

          <div class="slot-row" style="font-size: 0.8rem;">
            <div>
              <label style="font-size: 0.7rem; color: var(--text-muted); display: block;">Vacuum (M8):</label>
              <select onchange="App.updateSlot('${slot.slot_id}', 'vacuum_mode', this.value)" style="padding: 3px 6px;">
                <option value="default" ${slot.vacuum_mode === 'default' ? 'selected' : ''}>CAM Default</option>
                <option value="force_on" ${slot.vacuum_mode === 'force_on' ? 'selected' : ''}>Force ON</option>
                <option value="force_off" ${slot.vacuum_mode === 'force_off' ? 'selected' : ''}>Force OFF</option>
              </select>
            </div>
            <div>
              <label style="font-size: 0.7rem; color: var(--text-muted); display: block;">Air Assist (M7):</label>
              <select onchange="App.updateSlot('${slot.slot_id}', 'air_assist_mode', this.value)" style="padding: 3px 6px;">
                <option value="default" ${slot.air_assist_mode === 'default' ? 'selected' : ''}>CAM Default</option>
                <option value="force_on" ${slot.air_assist_mode === 'force_on' ? 'selected' : ''}>Force ON</option>
                <option value="force_off" ${slot.air_assist_mode === 'force_off' ? 'selected' : ''}>Force OFF</option>
              </select>
            </div>
            <div style="margin-left: auto; display: flex; align-items: flex-end;">
              <span class="badge" style="background: rgba(245,158,11,0.15); color: var(--warning); font-size: 0.75rem;">
                ${pauseCount} pause${pauseCount === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        </div>
      `;
    }).join('');

    updateAirCarveBanner();
    renderTimelinePauseMarkers();
  }

  function updateSlot(slotId, key, val) {
    const slot = fixtureSlots.find(s => s.slot_id === slotId);
    if (!slot) return;
    slot[key] = val;
    updateAirCarveBanner();
    scheduleAutoSync();
  }

  function moveSlot(idx, dir) {
    const target = idx + dir;
    if (target < 0 || target >= fixtureSlots.length) return;
    const temp = fixtureSlots[idx];
    fixtureSlots[idx] = fixtureSlots[target];
    fixtureSlots[target] = temp;
    renderFixtureSlots();
    scheduleAutoSync();
  }

  function duplicateSlot(idx) {
    const original = fixtureSlots[idx];
    const slotId = `slot_${Date.now()}_${Math.random().toString(36).substr(2, 4)}`;
    fixtureSlots.splice(idx + 1, 0, {
      ...original,
      slot_id: slotId,
      pauses: [...original.pauses]
    });
    renderFixtureSlots();
    scheduleAutoSync();
  }

  function removeSlot(idx) {
    fixtureSlots.splice(idx, 1);
    renderFixtureSlots();
    scheduleAutoSync();
  }

  function onStrategyChanged(val) {
    activeStrategy = val;
    scheduleAutoSync();
  }

  function updateAirCarveBanner() {
    const banner = document.getElementById('aircarveBanner');
    const hasAirCarve = fixtureSlots.some(s => s.is_air_carve);
    banner.style.display = hasAirCarve ? 'inline-flex' : 'none';
  }

  async function previewJob(jobId) {
    activePreviewJobId = jobId;
    try {
      const res = await fetch(`/api/job/${jobId}`);
      const data = await res.json();
      activeGeometryPoints = data.geometry || [];
      GcodeViewer.loadGeometry(activeGeometryPoints);

      const slider = document.getElementById('scrubberSlider');
      slider.max = Math.max(0, activeGeometryPoints.length - 1);
      slider.value = 0;
      onScrub(0);
      renderTimelinePauseMarkers();
    } catch (e) {
      console.error("Failed to load job geometry", e);
    }
  }

  // Scrubber & Playback Controls
  function onScrub(val) {
    currentScrubIdx = parseInt(val, 10);
    GcodeViewer.setCursor(currentScrubIdx);
    updateHud(currentScrubIdx);
  }

  function updateHud(idx) {
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;
    const p = activeGeometryPoints[idx] || activeGeometryPoints[0];
    document.getElementById('hudLine').textContent = p.line;
    document.getElementById('hudCmd').textContent = p.type === 'rapid' ? 'G0 (Rapid)' : (p.type === 'arc' ? 'G2/G3 (Arc)' : 'G1 (Feed)');
    document.getElementById('hudPos').textContent = `X ${p.x.toFixed(2)} Y ${p.y.toFixed(2)} Z ${p.z.toFixed(2)}`;
    document.getElementById('hudFeed').textContent = `${p.feed.toFixed(0)} mm/min`;
    document.getElementById('hudTool').textContent = `T${p.tool}`;
  }

  function step(dir) {
    stopPlay();
    const slider = document.getElementById('scrubberSlider');
    const maxVal = parseInt(slider.max, 10);
    const nextVal = Math.max(0, Math.min(maxVal, currentScrubIdx + dir));
    slider.value = nextVal;
    onScrub(nextVal);
  }

  function togglePlay(dir) {
    if (isPlaying && playDirection === dir) {
      stopPlay();
      return;
    }
    stopPlay();
    isPlaying = true;
    playDirection = dir;
    document.getElementById(dir > 0 ? 'btnPlayFwd' : 'btnPlayRev').style.background = 'var(--accent)';

    let lastTime = performance.now();
    function animate(time) {
      if (!isPlaying) return;
      const dt = (time - lastTime) / 1000;
      lastTime = time;

      // Rate: e.g. 50 points per second * playSpeed
      const stepDelta = Math.max(1, Math.round(60 * playSpeed * dt));
      const slider = document.getElementById('scrubberSlider');
      const maxVal = parseInt(slider.max, 10);
      let nextVal = currentScrubIdx + (stepDelta * playDirection);

      if (nextVal >= maxVal) {
        nextVal = maxVal;
        stopPlay();
      } else if (nextVal <= 0) {
        nextVal = 0;
        stopPlay();
      }

      slider.value = nextVal;
      onScrub(nextVal);

      if (isPlaying) {
        playAnimFrame = requestAnimationFrame(animate);
      }
    }
    playAnimFrame = requestAnimationFrame(animate);
  }

  function stopPlay() {
    isPlaying = false;
    if (playAnimFrame) cancelAnimationFrame(playAnimFrame);
    document.getElementById('btnPlayFwd').style.background = '';
    document.getElementById('btnPlayRev').style.background = '';
  }

  function setPlaybackSpeed(val) {
    playSpeed = parseFloat(val) || 1.0;
  }

  // Visual Pause Insertion
  function openInsertPauseDialog() {
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) {
      alert("Please load a job first to scrub and insert pauses.");
      return;
    }
    const curPoint = activeGeometryPoints[currentScrubIdx] || activeGeometryPoints[0];
    document.getElementById('modalPauseLine').textContent = curPoint.line;
    document.getElementById('modalPauseTool').textContent = `T${curPoint.tool}`;
    document.getElementById('modalPauseMsg').value = '';
    document.getElementById('pauseModal').style.display = 'flex';
  }

  function closeInsertPauseDialog() {
    document.getElementById('pauseModal').style.display = 'none';
  }

  function confirmInsertPause() {
    const curPoint = activeGeometryPoints[currentScrubIdx] || activeGeometryPoints[0];
    const pauseType = document.getElementById('modalPauseType').value;
    const retractMode = document.getElementById('modalPauseRetract').value;
    const message = document.getElementById('modalPauseMsg').value.trim();

    // Attach to matching fixture slot (or the first slot if multiple)
    if (fixtureSlots.length === 0) {
      alert("Add this job to the fixture sequence first.");
      closeInsertPauseDialog();
      return;
    }

    const targetSlot = fixtureSlots.find(s => s.job_id === activePreviewJobId) || fixtureSlots[0];
    targetSlot.pauses.push({
      line: curPoint.line,
      type: pauseType,
      retract: retractMode,
      msg: message
    });

    closeInsertPauseDialog();
    renderFixtureSlots();
    renderTimelinePauseMarkers();
    scheduleAutoSync();
  }

  function renderTimelinePauseMarkers() {
    const track = document.getElementById('pauseMarkersTrack');
    track.innerHTML = '';
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;

    // Collect all pauses for active preview job
    const slot = fixtureSlots.find(s => s.job_id === activePreviewJobId);
    if (!slot || !slot.pauses) return;

    const maxLine = activeGeometryPoints[activeGeometryPoints.length - 1].line;
    slot.pauses.forEach(p => {
      // Find point index matching pause line
      const ptIdx = activeGeometryPoints.findIndex(pt => pt.line >= p.line);
      if (ptIdx >= 0) {
        const pct = (ptIdx / (activeGeometryPoints.length - 1)) * 100;
        const pin = document.createElement('div');
        pin.className = 'pause-marker-pin';
        pin.style.left = `${pct}%`;
        pin.title = `Pause at line ${p.line}: ${p.type} (${p.msg || 'No msg'})`;
        track.appendChild(pin);
      }
    });
  }

  // Auto-sync after idle timer
  function scheduleAutoSync() {
    if (!config.auto_sync_enabled) return;
    cancelAutoSync();

    remainingSeconds = Math.round(config.auto_sync_idle_seconds || 10);
    const alertBox = document.getElementById('autoSyncAlert');
    const timerText = document.getElementById('autoSyncTimerText');
    alertBox.style.display = 'block';
    timerText.textContent = `Auto-syncing to controller in ${remainingSeconds}s...`;

    autoSyncCountdownInterval = setInterval(() => {
      remainingSeconds -= 1;
      if (remainingSeconds <= 0) {
        cancelAutoSync();
        syncNow();
      } else {
        timerText.textContent = `Auto-syncing to controller in ${remainingSeconds}s...`;
      }
    }, 1000);
  }

  function cancelAutoSync() {
    if (autoSyncCountdownInterval) clearInterval(autoSyncCountdownInterval);
    document.getElementById('autoSyncAlert').style.display = 'none';
  }

  async function syncNow() {
    cancelAutoSync();
    if (fixtureSlots.length === 0) {
      alert("No jobs in fixture sequence. Add jobs first.");
      return;
    }

    const payload = {
      slots: fixtureSlots,
      strategy: activeStrategy
    };

    try {
      const res = await fetch('/api/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.ok) {
        alert(`Successfully synced ${data.uploaded.join(', ')} to gcode/workflow/!\n` +
              `Earlier versions replaced: ${data.replaced_earlier.length}\n` +
              `Stale jobs pruned: ${data.pruned_stale.length}`);
        await checkControllerStatus();
      } else {
        alert(`Sync error: ${data.error || 'Unknown error'}`);
      }
    } catch (e) {
      alert(`Sync failed: ${e.message}`);
    }
  }

  // Settings
  function openSettings() {
    document.getElementById('cfgWatchedDir').value = config.watched_dir || '';
    document.getElementById('cfgHost').value = config.controller_host || '192.168.1.150';
    document.getElementById('cfgPort').value = config.controller_port || 8080;
    document.getElementById('cfgTtl').value = config.stale_ttl_hours || 24;
    document.getElementById('cfgAutoSyncIdle').value = config.auto_sync_idle_seconds || 10;
    document.getElementById('cfgEpilogue').value = config.custom_epilogue || '';
    document.getElementById('settingsModal').style.display = 'flex';
  }

  function closeSettings() {
    document.getElementById('settingsModal').style.display = 'none';
  }

  async function saveSettings() {
    const updated = {
      ...config,
      watched_dir: document.getElementById('cfgWatchedDir').value.trim(),
      controller_host: document.getElementById('cfgHost').value.trim(),
      controller_port: parseInt(document.getElementById('cfgPort').value, 10) || 8080,
      stale_ttl_hours: parseFloat(document.getElementById('cfgTtl').value) || 24.0,
      auto_sync_idle_seconds: parseFloat(document.getElementById('cfgAutoSyncIdle').value) || 10.0,
      custom_epilogue: document.getElementById('cfgEpilogue').value,
    };

    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updated)
      });
      const data = await res.json();
      if (data.ok) {
        config = data.config;
        closeSettings();
        await refreshStaged();
        await checkControllerStatus();
      }
    } catch (e) {
      alert(`Error saving settings: ${e.message}`);
    }
  }

  // Mobile tab switcher
  function switchMobileTab(tab) {
    document.querySelectorAll('.mobile-tab-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById('paneLeft').classList.remove('tab-active');
    document.getElementById('paneRight').classList.remove('tab-active');

    if (tab === 'sequence') {
      document.querySelectorAll('.mobile-tab-btn')[0].classList.add('active');
      document.getElementById('paneLeft').classList.add('tab-active');
    } else if (tab === 'viewer') {
      document.querySelectorAll('.mobile-tab-btn')[1].classList.add('active');
      document.getElementById('paneRight').classList.add('tab-active');
    } else if (tab === 'controller') {
      document.querySelectorAll('.mobile-tab-btn')[2].classList.add('active');
      document.getElementById('paneLeft').classList.add('tab-active');
    }
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  window.addEventListener('DOMContentLoaded', init);

  return {
    openSettings,
    closeSettings,
    saveSettings,
    refreshStaged,
    addJobToSequence,
    updateSlot,
    moveSlot,
    duplicateSlot,
    removeSlot,
    onStrategyChanged,
    previewJob,
    onScrub,
    step,
    togglePlay,
    setPlaybackSpeed,
    openInsertPauseDialog,
    closeInsertPauseDialog,
    confirmInsertPause,
    cancelAutoSync,
    syncNow,
    switchMobileTab,
  };
})();
