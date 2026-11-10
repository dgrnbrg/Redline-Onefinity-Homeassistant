/**
 * Application state manager, sequencer canvas, auto-sync timer, and pause manager.
 */

const App = (() => {
  let config = {};
  let allStagedJobs = [];
  let stagedJobs = [];
  let stagedTimeFilter = '24h'; // '24h' by default, or 'all'
  let stagedSortOrder = 'chrono_desc'; // 'chrono_desc' (default: newest first), 'chrono_asc', 'alpha_asc', 'alpha_desc'
  let userExplicitSort = false;
  let stagedSearchQuery = '';
  let fixtureSlots = [];
  let selectedSlotId = null;
  let isShowingSlotOnlyInEditor = false;
  let activeStrategy = "part_by_part";
  let isSinglePartMode = false;
  let isSyncing = false;
  let activePreviewJobId = null;
  let activeGeometryPoints = [];
  let activeSafeZThreshold = 0.0;
  let activeInferredSafeZ = 0.0;
  let persistedSafeZByJobId = {};
  let manualSplitsByJobId = {};
  let activeClickedRapid = null;
  let safeZDebounceTimer = null;

  // Playback state
  let isPlaying = false;
  let playDirection = 1; // 1 = forward, -1 = reverse
  let playSpeed = 1.0;
  let playAnimFrame = null;
  let currentScrubIdx = 0;
  let currentScrubTime = 0;
  let totalToolpathTime = 0;
  let cumMoveTimes = [];

  let activeJobData = null;
  let activeJobUnits = 'metric';
  let activeEditorSlotId = null;
  let originalJobGcode = '';
  let currentEditorGcode = '';

  const DEFAULT_RAPID_SPEED = 6000; // mm/min (100 mm/sec) for G0
  const DEFAULT_FEED_SPEED = 1000;  // mm/min default cutting feed if unspecified
  const MIN_MOVE_TIME = 0.02;       // minimum duration (sec) for stationary commands

  function formatDuration(sec) {
    if (!sec || isNaN(sec) || sec < 0) sec = 0;
    const totalSec = Math.floor(sec);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    const sPad = s < 10 ? '0' + s : s;
    if (m >= 60) {
      const h = Math.floor(m / 60);
      const mRem = m % 60;
      const mPad = mRem < 10 ? '0' + mRem : mRem;
      return `${h}:${mPad}:${sPad}`;
    }
    return `${m}:${sPad}`;
  }

  // Auto-sync idle timer
  let autoSyncTimer = null;
  let autoSyncCountdownInterval = null;
  let remainingSeconds = 10;

  const WCS_OPTIONS = ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3", "none"];

  async function init() {
    GcodeViewer.init('glCanvas');
    await loadConfig();
    await checkControllerStatus();
    await refreshStaged();
    updateCollapseButtonsState();

    // Check controller status periodically
    setInterval(checkControllerStatus, 10000);

    // Close rapid tooltip when clicking outside
    document.addEventListener('click', (e) => {
      const tooltip = document.getElementById('rapidSplitTooltip');
      if (!tooltip || tooltip.style.display === 'none') return;
      if (tooltip.contains(e.target) || e.target.closest('.rapid-marker-dot')) {
        return;
      }
      closeRapidTooltip();
    });
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
      allStagedJobs = data.jobs || [];
      allStagedJobs.forEach(job => {
        if (persistedSafeZByJobId[job.id] !== undefined) {
          job.safe_z_threshold = persistedSafeZByJobId[job.id];
          job.custom_safe_z = persistedSafeZByJobId[job.id];
        } else {
          try {
            const saved = localStorage.getItem(`safe_z_${job.id}`);
            if (saved !== null && !isNaN(parseFloat(saved))) {
              job.safe_z_threshold = parseFloat(saved);
              job.custom_safe_z = parseFloat(saved);
              persistedSafeZByJobId[job.id] = parseFloat(saved);
            }
          } catch (_) {}
        }
      });
      // Dynamic sorting applied in applyStagedFilters()
      updateAutocompleteOptions();
      applyStagedFilters();
    } catch (e) {
      container.innerHTML = `<div style="color: var(--danger); font-size: 0.85rem;">Error loading staged jobs: ${e.message}</div>`;
    }
  }

  function applyStagedFilters() {
    const ttlHours = (config && config.stale_ttl_hours) ? config.stale_ttl_hours : 24.0;
    const maxAgeSec = ttlHours * 3600;
    const nowSec = Date.now() / 1000;

    // 1. Time-based truncation (default past 24 hours / configured TTL)
    let jobs = allStagedJobs;
    let olderHiddenCount = 0;

    if (stagedTimeFilter === '24h') {
      const recent = [];
      for (const j of allStagedJobs) {
        if (!j.mtime || (nowSec - j.mtime) <= maxAgeSec) {
          recent.push(j);
        } else {
          olderHiddenCount++;
        }
      }
      jobs = recent;
    }

    // 2. Real-time search & autocomplete query filter
    if (stagedSearchQuery && stagedSearchQuery.trim()) {
      const q = stagedSearchQuery.trim().toLowerCase();
      jobs = jobs.filter(j => {
        if (j.name && j.name.toLowerCase().includes(q)) return true;
        if (j.tools_used && j.tools_used.some(t => (`t${t}`).includes(q) || String(t) === q)) return true;
        if (j.subfiles && j.subfiles.some(sf => (sf.filename && sf.filename.toLowerCase().includes(q)) || (sf.op && sf.op.toLowerCase().includes(q)))) return true;
        return false;
      });
    }

    // 3. Sorting (chronological by default for all files, or alphabetical)
    if (stagedSortOrder === 'alpha_desc') {
      jobs.sort((a, b) => (b.name || '').localeCompare(a.name || '', undefined, { numeric: true, sensitivity: 'base' }));
    } else if (stagedSortOrder === 'alpha_asc' || stagedSortOrder === 'alpha') {
      jobs.sort((a, b) => (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' }));
    } else if (stagedSortOrder === 'chrono_asc') {
      jobs.sort((a, b) => {
        const timeA = a.mtime || 0;
        const timeB = b.mtime || 0;
        if (timeA !== timeB) return timeA - timeB;
        return (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' });
      });
    } else {
      // 'chrono_desc' / 'chrono' (default: newest first)
      jobs.sort((a, b) => {
        const timeA = a.mtime || 0;
        const timeB = b.mtime || 0;
        if (timeB !== timeA) return timeB - timeA;
        return (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' });
      });
    }

    stagedJobs = jobs;

    // 4. Update filter button styles and count badges
    const btn24h = document.getElementById('btnFilter24h');
    const btnAll = document.getElementById('btnFilterAll');
    if (btn24h && btnAll) {
      btn24h.textContent = `⏱ Past ${Math.round(ttlHours)}h`;
      if (stagedTimeFilter === '24h') {
        btn24h.className = 'btn btn-sm btn-primary';
        btnAll.className = 'btn btn-sm';
      } else {
        btn24h.className = 'btn btn-sm';
        btnAll.className = 'btn btn-sm btn-primary';
      }
    }

    // 5. Update sort button styles
    const btnSortChrono = document.getElementById('btnSortChrono');
    const btnSortAlpha = document.getElementById('btnSortAlpha');
    if (btnSortChrono && btnSortAlpha) {
      if (stagedSortOrder.startsWith('alpha')) {
        btnSortChrono.className = 'btn btn-sm';
        btnSortAlpha.className = 'btn btn-sm btn-primary';
        btnSortChrono.textContent = '🕒 Date';
        btnSortAlpha.textContent = stagedSortOrder === 'alpha_desc' ? '🔤 Z-A' : '🔤 A-Z';
        btnSortAlpha.title = 'Sorted alphabetically. Click to toggle Z-A / A-Z';
        btnSortChrono.title = 'Switch to chronological sort';
      } else {
        btnSortChrono.className = 'btn btn-sm btn-primary';
        btnSortAlpha.className = 'btn btn-sm';
        btnSortChrono.textContent = stagedSortOrder === 'chrono_asc' ? '🕒 Oldest' : '🕒 Newest';
        btnSortAlpha.textContent = '🔤 Name';
        btnSortChrono.title = 'Sorted chronologically. Click to toggle Newest / Oldest';
        btnSortAlpha.title = 'Switch to alphabetical sort';
      }
    }

    const badge = document.getElementById('stagedCountBadge');
    if (badge) {
      if (stagedJobs.length === allStagedJobs.length) {
        badge.textContent = `(${allStagedJobs.length})`;
      } else {
        badge.textContent = `(${stagedJobs.length} of ${allStagedJobs.length})`;
      }
    }

    const note = document.getElementById('stagedActiveFilterNote');
    if (note) {
      if (stagedTimeFilter === '24h' && olderHiddenCount > 0) {
        note.innerHTML = `${olderHiddenCount} older hidden · <a href="javascript:void(0)" onclick="App.setStagedTimeFilter('all')" style="color:var(--accent); text-decoration:none;">Show all</a>`;
      } else {
        note.textContent = '';
      }
    }

    renderStagedJobs();
  }

  function updateAutocompleteOptions() {
    const dl = document.getElementById('stagedAutocompleteList');
    if (!dl) return;
    const suggestions = new Set();
    allStagedJobs.forEach(job => {
      suggestions.add(job.name);
      if (job.subfiles) {
        job.subfiles.forEach(sf => {
          suggestions.add(sf.filename);
          if (sf.op) suggestions.add(sf.op);
        });
      }
      if (job.tools_used) {
        job.tools_used.forEach(t => suggestions.add(`T${t}`));
      }
    });
    dl.innerHTML = Array.from(suggestions)
      .slice(0, 40)
      .map(s => `<option value="${escapeHtml(s)}">`)
      .join('');
  }

  function onStagedSearch(val) {
    stagedSearchQuery = val || '';
    const btn = document.getElementById('btnClearStagedSearch');
    if (btn) btn.style.display = stagedSearchQuery ? 'block' : 'none';
    applyStagedFilters();
  }

  function clearStagedSearch() {
    stagedSearchQuery = '';
    const input = document.getElementById('stagedSearchInput');
    if (input) input.value = '';
    const btn = document.getElementById('btnClearStagedSearch');
    if (btn) btn.style.display = 'none';
    applyStagedFilters();
  }

  function setStagedTimeFilter(filterType) {
    stagedTimeFilter = filterType;
    if (filterType === 'all') {
      // Default to chronological order (newest first) when showing all jobs
      if (!userExplicitSort) {
        stagedSortOrder = 'chrono_desc';
      }
    }
    applyStagedFilters();
  }

  function setStagedSortOrder(order) {
    if (order === 'chrono') {
      if (stagedSortOrder === 'chrono_desc') {
        stagedSortOrder = 'chrono_asc';
      } else {
        stagedSortOrder = 'chrono_desc';
      }
    } else if (order === 'alpha') {
      if (stagedSortOrder === 'alpha_asc') {
        stagedSortOrder = 'alpha_desc';
      } else {
        stagedSortOrder = 'alpha_asc';
      }
    } else {
      stagedSortOrder = order;
    }
    userExplicitSort = true;
    applyStagedFilters();
  }

  function formatTimeAgo(mtime) {
    if (!mtime) return '';
    const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - mtime));
    if (diffSec < 60) return 'just now';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  }

  function renderStagedJobs() {
    const container = document.getElementById('stagedJobsList');
    if (!allStagedJobs || allStagedJobs.length === 0) {
      container.innerHTML = `
        <div class="card" style="text-align: center; color: var(--text-muted); font-size: 0.85rem;">
          No G-code jobs found in watched folder.<br>
          <small>Export .ngc files from CAM into your watched folder.</small>
        </div>`;
      return;
    }

    if (!stagedJobs || stagedJobs.length === 0) {
      const isSearch = stagedSearchQuery && stagedSearchQuery.trim();
      container.innerHTML = `
        <div class="card" style="text-align: center; color: var(--text-muted); font-size: 0.85rem; padding: 18px 12px;">
          No staged jobs match current ${isSearch ? 'search filter' : 'time filter'}.<br>
          <div style="margin-top: 8px; display: flex; gap: 8px; justify-content: center;">
            ${isSearch ? '<button class="btn btn-sm" onclick="App.clearStagedSearch()">Clear Search</button>' : ''}
            ${stagedTimeFilter === '24h' ? '<button class="btn btn-sm btn-primary" onclick="App.setStagedTimeFilter(\'all\')">Show All Files (' + allStagedJobs.length + ')</button>' : ''}
          </div>
        </div>`;
      return;
    }

    const ttlHours = (config && config.stale_ttl_hours) ? config.stale_ttl_hours : 24.0;
    const maxAgeSec = ttlHours * 3600;
    const nowSec = Date.now() / 1000;

    container.innerHTML = stagedJobs.map(job => {
      const toolTags = job.tools_used.map(t => `<span class="badge" style="background: rgba(14,165,233,0.15); color: var(--accent); padding: 2px 6px; font-size: 0.75rem;">T${t}</span>`).join(' ');
      const multiBadge = job.is_multi_file
        ? `<span class="badge" style="background: rgba(168,85,247,0.2); color: #c084fc; padding: 2px 6px; font-size: 0.75rem;">Multi-file (${job.subfiles_count} ops)</span>`
        : '';
      const zOrigin = job.z_origin || {};
      const zBadgeColor = zOrigin.origin === 'machine_bed' ? 'rgba(16,185,129,0.2); color: var(--success);' : (zOrigin.origin === 'top_of_stock' ? 'rgba(14,165,233,0.2); color: var(--accent);' : 'rgba(245,158,11,0.2); color: var(--warning);');

      const isOlder = job.mtime && (nowSec - job.mtime) > maxAgeSec;
      const timeStr = formatTimeAgo(job.mtime);
      const timeBadge = timeStr
        ? `<span class="badge" style="background: ${isOlder ? 'rgba(100,116,139,0.2); color: var(--text-muted);' : 'rgba(16,185,129,0.15); color: var(--success);'} font-size: 0.7rem; padding: 2px 5px;" title="${new Date((job.mtime || 0) * 1000).toLocaleString()}">${isOlder ? '🕒 ' : ''}${escapeHtml(timeStr)}</span>`
        : '';

      const unitSuffix = job.unit_suffix || (job.units === 'imperial' ? 'in' : 'mm');
      const unitBadge = `<span class="badge" style="background: ${job.units === 'imperial' ? 'rgba(245, 158, 11, 0.2); color: var(--warning);' : 'rgba(14, 165, 233, 0.2); color: var(--accent);'} font-size: 0.72rem; padding: 2px 6px;">${job.units === 'imperial' ? 'INCH (G20)' : 'MM (G21)'}</span>`;
      const ops = (job.operations && job.operations.length > 0) ? job.operations : [];
      const opsCount = ops.length > 0 ? ops.length : (job.subfiles_count || 1);
      const opsBadge = `<span id="opCountBadge_${job.id}" class="badge" style="background: rgba(168,85,247,0.2); color: #c084fc; padding: 2px 6px; font-size: 0.72rem;" title="${ops.length > 0 ? ops.map(o => o.name).join(' | ') : '1 operation'}">${opsCount} op${opsCount === 1 ? '' : 's'} (${opsCount} slot${opsCount === 1 ? '' : 's'})</span>`;

      const isSelected = (job.id === activePreviewJobId);

      return `
        <div class="card staged-job-card ${isSelected ? 'selected' : ''}" id="stagedJobCard_${job.id}" onclick="App.previewJob('${job.id}')" style="margin-bottom: 8px; cursor: pointer; ${isOlder ? 'opacity: 0.85;' : ''}" title="Click anywhere to inspect 3D toolpath & G-code">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px;">
            <div>
              <strong style="font-size: 0.95rem;">${escapeHtml(job.name)}</strong>
              ${multiBadge}
              ${opsBadge}
            </div>
            <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); App.addJobToSequence('${job.id}')" title="Add this job to active fixture sequence">+ Add to Run</button>
          </div>
          <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; align-items: center;">
            ${toolTags}
            ${unitBadge}
            <span class="badge" style="background: ${zBadgeColor} padding: 2px 6px; font-size: 0.75rem;" title="${escapeHtml(zOrigin.rationale || '')}">
              Z: ${escapeHtml(zOrigin.label || 'Unknown')}
            </span>
            ${timeBadge}
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); align-items: center;">
            <span>${job.total_lines} lines</span>
            <span>Cut Z: ${job.extents.min_cutting_z} to ${job.extents.max_cutting_z} ${unitSuffix}</span>
          </div>
        </div>
      `;
    }).join('');
  }

  function addJobToSequence(jobId) {
    const job = (allStagedJobs.find(j => j.id === jobId)) || (stagedJobs.find(j => j.id === jobId));
    if (!job) return;

    // Pick next available WCS (or none if strategy is single_part_no_wcs)
    let nextWcs = "G54";
    if (activeStrategy === 'single_part_no_wcs') {
      nextWcs = "none";
    } else {
      const usedWcs = fixtureSlots.map(s => s.wcs);
      for (const opt of WCS_OPTIONS) {
        if (opt !== 'none' && !usedWcs.includes(opt)) {
          nextWcs = opt;
          break;
        }
      }
    }

    // Determine operations: from recalculated job.operations or multi-file subfiles
    const ops = (job.operations && job.operations.length > 0)
      ? job.operations
      : (job.subfiles && job.subfiles.length > 0 ? job.subfiles : []);

    let slotsAdded = 0;
    if (ops.length > 1) {
      // Propagate into multiple slots in fixture sequence
      ops.forEach((op, idx) => {
        const slotId = `slot_${Date.now()}_${idx}_${Math.random().toString(36).substr(2, 4)}`;
        const opName = op.name || op.op || `Op ${idx + 1}`;
        const opTool = op.tool || op.tool_number || (job.tools_used && job.tools_used[0] ? job.tools_used[0] : 1);
        const startLine = op.start_line !== undefined ? op.start_line : 1;
        const endLine = op.end_line !== undefined ? op.end_line : (op.lines || job.total_lines);

        fixtureSlots.push({
          slot_id: slotId,
          job_id: job.id,
          job_name: `${job.name} - ${opName}`,
          op_index: op.op || (idx + 1),
          op_name: opName,
          tool: opTool,
          start_line: startLine,
          end_line: endLine,
          wcs: nextWcs,
          z_shift: 0.0,
          is_air_carve: false,
          vacuum_mode: "default",
          air_assist_mode: "default",
          pauses: [],
          custom_gcode: null,
        });
        slotsAdded++;
      });
    } else {
      // Single operation: 1 slot
      const slotId = `slot_${Date.now()}_${Math.random().toString(36).substr(2, 4)}`;
      const singleOp = (job.operations && job.operations[0]) || null;
      fixtureSlots.push({
        slot_id: slotId,
        job_id: job.id,
        job_name: job.name,
        op_index: 1,
        op_name: singleOp ? singleOp.name : null,
        tool: singleOp ? singleOp.tool : (job.tools_used && job.tools_used[0] ? job.tools_used[0] : 1),
        start_line: singleOp ? singleOp.start_line : 1,
        end_line: singleOp ? singleOp.end_line : job.total_lines,
        wcs: nextWcs,
        z_shift: 0.0,
        is_air_carve: false,
        vacuum_mode: "default",
        air_assist_mode: "default",
        pauses: [],
        custom_gcode: null,
      });
      slotsAdded = 1;
    }

    renderFixtureSlots();
    previewJob(job.id);
    scheduleAutoSync();
    showToast(`Added ${slotsAdded} slot${slotsAdded === 1 ? '' : 's'} for "${job.name}" to sequence`, "info");
  }

  function renderFixtureSlots() {
    const container = document.getElementById('fixtureSlotsContainer');
    if (!fixtureSlots || fixtureSlots.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 24px 0; color: var(--text-muted); font-size: 0.85rem;">
          No jobs in fixture sequence yet.<br>Click <strong>"+ Add to Run"</strong> from the staging bin below.
        </div>`;
      selectedSlotId = null;
      updateAirCarveBanner();
      return;
    }

    let toolbarHtml = '';
    if (selectedSlotId) {
      const selSlot = fixtureSlots.find(s => s.slot_id === selectedSlotId);
      if (selSlot) {
        const selIdx = fixtureSlots.indexOf(selSlot) + 1;
        toolbarHtml = `
          <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; margin-bottom: 10px; background: rgba(14, 165, 233, 0.15); border: 1px solid var(--accent); border-radius: 6px;">
            <span style="font-size: 0.78rem; color: #38bdf8; font-weight: 700;">
              🔍 Focusing Slot ${selIdx}: ${escapeHtml(selSlot.job_name)}
            </span>
            <button class="btn btn-sm" onclick="App.clearSelectedSlot()" style="padding: 2px 8px; font-size: 0.72rem;" title="Clear slot selection and show full job in 3D & timeline">Clear Focus</button>
          </div>
        `;
      } else {
        selectedSlotId = null;
      }
    }

    const cardsHtml = fixtureSlots.map((slot, idx) => {
      const isSelected = (slot.slot_id === selectedSlotId);
      const wcsSelect = WCS_OPTIONS.map(opt => {
        const lbl = opt === 'none' ? 'None (Preserve File WCS)' : opt;
        return `<option value="${opt}" ${opt === slot.wcs ? 'selected' : ''}>${lbl}</option>`;
      }).join('');
      const pauseCount = slot.pauses.length;
      const editedBadge = slot.custom_gcode
        ? `<span class="badge" style="background: rgba(16,185,129,0.2); color: var(--success); font-size: 0.7rem; padding: 2px 6px; margin-left: 6px;">[Edited]</span>`
        : '';
      const toolBadge = slot.tool
        ? `<span class="badge" style="background: rgba(14,165,233,0.15); color: var(--accent); font-size: 0.72rem; padding: 2px 6px; margin-left: 6px;" title="Tool assigned to this slot">T${slot.tool}</span>`
        : '';
      const opBadge = slot.op_name
        ? `<span class="badge" style="background: rgba(168,85,247,0.18); color: #c084fc; font-size: 0.72rem; padding: 2px 6px; margin-left: 6px;" title="Operation name">${escapeHtml(slot.op_name)}</span>`
        : '';

      return `
        <div class="slot-card ${isSelected ? 'selected' : ''}" id="${slot.slot_id}" onclick="App.handleSlotCardClick(event, '${slot.slot_id}')" title="Click to focus and highlight Slot ${idx + 1} across 3D visualizer, timeline, and editor">
          <div class="slot-header">
            <span>Slot ${idx + 1}: ${escapeHtml(slot.job_name)}${opBadge}${toolBadge}${editedBadge}</span>
            <div class="btn-group">
              <button class="btn btn-sm ${isSelected ? 'btn-primary' : ''}" onclick="App.selectSlot('${slot.slot_id}')" title="Focus and highlight this slot in 3D visualizer, timeline, and editor">${isSelected ? '✓ Focused' : '🔍 Focus'}</button>
              <button class="btn btn-sm" onclick="App.openSlotInEditor('${slot.slot_id}')" title="Edit G-code text for this sequence slot">📝 Edit</button>
              <button class="btn btn-sm" onclick="App.moveSlot(${idx}, -1)" ${idx === 0 ? 'disabled' : ''} title="Move slot up in sequence">▲</button>
              <button class="btn btn-sm" onclick="App.moveSlot(${idx}, 1)" ${idx === fixtureSlots.length - 1 ? 'disabled' : ''} title="Move slot down in sequence">▼</button>
              <button class="btn btn-sm" onclick="App.duplicateSlot(${idx})" title="Duplicate this slot">📋</button>
              <button class="btn btn-sm" style="color: var(--danger);" onclick="App.removeSlot(${idx})" title="Remove this slot from sequence">✕</button>
            </div>
          </div>

          <div class="slot-row">
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;" title="Work Coordinate System">WCS:</label>
              ${isSinglePartMode
                ? `<span class="badge" style="background: rgba(14,165,233,0.15); color: var(--accent); font-weight: 700; padding: 5px 8px; display: inline-block; font-size: 0.78rem;" title="Single part mode active: original WCS preserved without overrides">Original (Preserved)</span>`
                : `<select onchange="App.updateSlot('${slot.slot_id}', 'wcs', this.value)" style="font-weight: 700;" title="Coordinate system (G54-G59)">
                    ${wcsSelect}
                  </select>`
              }
            </div>
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;" title="Vertical offset shift in millimeters">Z-Shift (mm):</label>
              <input type="number" step="0.1" value="${slot.z_shift}" style="width: 80px;" onchange="App.updateSlot('${slot.slot_id}', 'z_shift', parseFloat(this.value)||0)" title="Z-Shift offset in mm">
            </div>
            <div>
              <label style="font-size: 0.75rem; color: var(--text-muted); display: block;" title="Dry run air carving mode">Air Carve:</label>
              <label style="display: flex; align-items: center; gap: 4px; cursor: pointer; padding-top: 6px;" title="Dry run: raises Z ceiling by +25mm and suppresses coolant">
                <input type="checkbox" ${slot.is_air_carve ? 'checked' : ''} onchange="App.updateSlot('${slot.slot_id}', 'is_air_carve', this.checked)">
                <span style="font-weight: 600; color: ${slot.is_air_carve ? 'var(--warning)' : 'inherit'};">Dry Run (+25mm)</span>
              </label>
            </div>
          </div>

          <div class="slot-row" style="font-size: 0.8rem;">
            <div>
              <label style="font-size: 0.7rem; color: var(--text-muted); display: block;" title="Vacuum / Dust Collection (M8)">Vacuum (M8):</label>
              <select onchange="App.updateSlot('${slot.slot_id}', 'vacuum_mode', this.value)" style="padding: 3px 6px;" title="Vacuum / M8 relay override">
                <option value="default" ${slot.vacuum_mode === 'default' ? 'selected' : ''}>CAM Default</option>
                <option value="force_on" ${slot.vacuum_mode === 'force_on' ? 'selected' : ''}>Force ON</option>
                <option value="force_off" ${slot.vacuum_mode === 'force_off' ? 'selected' : ''}>Force OFF</option>
              </select>
            </div>
            <div>
              <label style="font-size: 0.7rem; color: var(--text-muted); display: block;" title="Air Assist / Mist (M7)">Air Assist (M7):</label>
              <select onchange="App.updateSlot('${slot.slot_id}', 'air_assist_mode', this.value)" style="padding: 3px 6px;" title="Air assist / M7 relay override">
                <option value="default" ${slot.air_assist_mode === 'default' ? 'selected' : ''}>CAM Default</option>
                <option value="force_on" ${slot.air_assist_mode === 'force_on' ? 'selected' : ''}>Force ON</option>
                <option value="force_off" ${slot.air_assist_mode === 'force_off' ? 'selected' : ''}>Force OFF</option>
              </select>
            </div>
            <div style="margin-left: auto; display: flex; align-items: flex-end;">
              <span class="badge" style="background: rgba(245,158,11,0.15); color: var(--warning); font-size: 0.75rem;" title="${pauseCount} programmed visual pause point${pauseCount === 1 ? '' : 's'} on this slot">
                ${pauseCount} pause${pauseCount === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        </div>
      `;
    }).join('');

    container.innerHTML = toolbarHtml + cardsHtml;
    updateAirCarveBanner();
    renderTimelinePauseMarkers();
    if (selectedSlotId) {
      const activeSlot = fixtureSlots.find(s => s.slot_id === selectedSlotId);
      if (activeSlot) {
        renderTimelineSlotHighlight(activeSlot);
      }
    }
  }

  function handleSlotCardClick(e, slotId) {
    if (e.target.closest('button') || e.target.closest('input') || e.target.closest('select') || e.target.closest('option') || e.target.closest('label')) {
      return;
    }
    selectSlot(slotId);
  }

  async function selectSlot(slotId) {
    if (selectedSlotId === slotId) {
      // Toggle off if clicking the already focused slot
      clearSelectedSlot();
      return;
    }

    const slot = fixtureSlots.find(s => s.slot_id === slotId);
    if (!slot) return;

    selectedSlotId = slotId;
    activeEditorSlotId = slotId;

    // 1. Ensure the correct job is loaded into the 3D viewer & timeline
    if (activePreviewJobId !== slot.job_id) {
      await previewJob(slot.job_id);
    }

    // 2. Highlight card in fixture sequence list
    renderFixtureSlots();

    // 3. Highlight toolpath lines in 3D WebGL visualizer
    if (typeof GcodeViewer.setHighlightRange === 'function') {
      const sLine = (slot.start_line !== undefined) ? slot.start_line : 1;
      const eLine = (slot.end_line !== undefined) ? slot.end_line : (activeJobData ? activeJobData.total_lines : 999999);
      GcodeViewer.setHighlightRange(sLine, eLine);

      // Position toolhead cursor at the start of this slot's cut
      if (activeGeometryPoints && activeGeometryPoints.length > 0) {
        const ptIdx = activeGeometryPoints.findIndex(pt => pt.line >= sLine);
        if (ptIdx >= 0) {
          GcodeViewer.setCursor(ptIdx);
        }
      }
    }

    // 4. Highlight zone in timeline scrubber
    renderTimelineSlotHighlight(slot);

    // 5. Highlight and scroll in G-code text editor
    await updateEditorForSelectedSlot(slot);
  }

  function clearSelectedSlot() {
    selectedSlotId = null;
    activeEditorSlotId = null;

    // Clear 3D highlight
    if (typeof GcodeViewer.setHighlightRange === 'function') {
      GcodeViewer.setHighlightRange(null, null);
    }

    // Clear timeline highlight
    const hlTrack = document.getElementById('timelineSlotHighlightTrack');
    if (hlTrack) hlTrack.innerHTML = '';

    // Clear editor banner & selection
    const banner = document.getElementById('editorSlotBanner');
    if (banner) banner.style.display = 'none';

    const label = document.getElementById('editorTargetLabel');
    if (label && activeJobData) {
      label.textContent = `Previewing: ${activeJobData.name || activePreviewJobId}`;
    }

    // Restore full job gcode in editor
    if (activePreviewJobId) {
      loadJobGcodeIntoEditor(activePreviewJobId, activeJobData ? activeJobData.name : null);
    }

    renderFixtureSlots();
  }

  function renderTimelineSlotHighlight(slot) {
    const track = document.getElementById('timelineSlotHighlightTrack');
    if (!track) return;
    track.innerHTML = '';

    if (!slot || !activeGeometryPoints || activeGeometryPoints.length === 0 || totalToolpathTime <= 0) {
      return;
    }

    const sLine = (slot.start_line !== undefined) ? slot.start_line : 1;
    const eLine = (slot.end_line !== undefined) ? slot.end_line : (activeJobData ? activeJobData.total_lines : 999999);

    let startIdx = activeGeometryPoints.findIndex(pt => pt.line >= sLine);
    if (startIdx < 0) startIdx = 0;

    let endIdx = -1;
    for (let i = activeGeometryPoints.length - 1; i >= 0; i--) {
      if (activeGeometryPoints[i].line <= eLine) {
        endIdx = i;
        break;
      }
    }
    if (endIdx < 0) endIdx = activeGeometryPoints.length - 1;

    const startTime = (cumMoveTimes && cumMoveTimes[startIdx] !== undefined) ? cumMoveTimes[startIdx] : 0;
    const endTime = (cumMoveTimes && cumMoveTimes[endIdx] !== undefined) ? cumMoveTimes[endIdx] : totalToolpathTime;

    const startPct = Math.max(0, Math.min(100, (startTime / totalToolpathTime) * 100));
    const endPct = Math.max(startPct, Math.min(100, (endTime / totalToolpathTime) * 100));
    const widthPct = Math.max(1.5, endPct - startPct);

    const slotIdx = fixtureSlots.indexOf(slot) + 1;
    const slotLabel = `Slot ${slotIdx}: ${slot.op_name || slot.job_name || 'Op'}`;

    track.innerHTML = `
      <div class="timeline-slot-highlight-bar" style="left: ${startPct}%; width: ${widthPct}%;">
        <div class="timeline-slot-highlight-label">
          ${escapeHtml(slotLabel)}
        </div>
      </div>
    `;

    // Move scrubber to the start of this slot
    onScrub(startTime);
    const slider = document.getElementById('scrubberSlider');
    if (slider) slider.value = startTime;
  }

  async function updateEditorForSelectedSlot(slot) {
    if (!slot) return;
    const slotIdx = fixtureSlots.indexOf(slot) + 1;

    // Expand editor pane if collapsed so the user immediately sees it
    const pane = document.getElementById('paneEditor');
    if (pane && pane.classList.contains('collapsed')) {
      pane.classList.remove('collapsed');
      updateCollapseButtonsState();
      setTimeout(() => GcodeViewer.resizeCanvas(), 200);
    }

    // Switch mobile tab if on smaller screen
    if (window.innerWidth < 1100) {
      switchMobileTab('editor');
    }

    const label = document.getElementById('editorTargetLabel');
    if (label) {
      label.innerHTML = `<span style="color: var(--accent); font-weight: 700;">Slot ${slotIdx}: ${escapeHtml(slot.job_name)}</span> <span style="color: var(--text-muted); font-size: 0.72rem;">(WCS: ${slot.wcs}${slot.start_line ? ', Lines ' + slot.start_line + '–' + slot.end_line : ''})</span>`;
    }

    // Show slot banner
    const banner = document.getElementById('editorSlotBanner');
    const bannerText = document.getElementById('editorSlotBannerText');
    if (banner && bannerText) {
      banner.style.display = 'flex';
      bannerText.textContent = `🎯 Focusing Slot ${slotIdx}: ${slot.job_name} (Lines ${slot.start_line || 1} - ${slot.end_line || 'end'})`;
    }

    const btnToggle = document.getElementById('btnToggleSlotView');
    if (btnToggle) {
      btnToggle.textContent = isShowingSlotOnlyInEditor ? 'Show Full Job' : 'Show Slot Only';
    }

    const editor = document.getElementById('gcodeEditor');
    if (!editor) return;

    if (slot.custom_gcode) {
      editor.value = slot.custom_gcode;
      currentEditorGcode = slot.custom_gcode;
      updateEditorStats();
      const badge = document.getElementById('editorStatusBadge');
      if (badge) {
        badge.textContent = 'Sequence Override Active';
        badge.style.background = 'rgba(16, 185, 129, 0.2)';
        badge.style.color = 'var(--success)';
      }
      return;
    }

    if (isShowingSlotOnlyInEditor) {
      // Fetch only this slot's sliced G-code
      try {
        let url = `/api/job/${slot.job_id}/gcode`;
        if (slot.op_index !== undefined && slot.start_line !== undefined && slot.end_line !== undefined) {
          url += `?op=${slot.op_index}&start=${slot.start_line}&end=${slot.end_line}`;
        }
        const res = await fetch(url);
        const data = await res.json();
        editor.value = data.gcode || '';
        currentEditorGcode = editor.value;
        updateEditorStats();
        const badge = document.getElementById('editorStatusBadge');
        if (badge) {
          badge.textContent = 'Slot Sliced View';
          badge.style.background = 'rgba(14, 165, 233, 0.2)';
          badge.style.color = 'var(--accent)';
        }
      } catch (e) {
        console.warn('Could not load sliced slot G-code', e);
      }
    } else {
      // Ensure full job G-code is loaded, then select and scroll to slot's lines
      if (!originalJobGcode || activeEditorSlotId !== slot.slot_id) {
        try {
          const res = await fetch(`/api/job/${slot.job_id}/gcode`);
          const data = await res.json();
          originalJobGcode = data.gcode || '';
        } catch (e) {
          originalJobGcode = '';
        }
      }
      editor.value = originalJobGcode;
      currentEditorGcode = originalJobGcode;
      updateEditorStats();

      const badge = document.getElementById('editorStatusBadge');
      if (badge) {
        badge.textContent = 'Original Source (Slot Highlighted)';
        badge.style.background = 'rgba(14, 165, 233, 0.2)';
        badge.style.color = 'var(--accent)';
      }

      // Calculate character range and scroll
      if (slot.start_line && slot.end_line) {
        const lines = editor.value.split('\n');
        let charStart = 0;
        let charEnd = editor.value.length;
        let offset = 0;
        for (let i = 0; i < lines.length; i++) {
          const lnNum = i + 1;
          if (lnNum === slot.start_line) {
            charStart = offset;
          }
          offset += lines[i].length + 1;
          if (lnNum === slot.end_line) {
            charEnd = Math.min(editor.value.length, offset - 1);
            break;
          }
        }
        editor.focus();
        editor.setSelectionRange(charStart, charEnd);

        // Scroll so start_line is at top third of editor
        const totalLines = lines.length || 1;
        const lineFraction = Math.max(0, slot.start_line - 1) / totalLines;
        editor.scrollTop = Math.max(0, lineFraction * editor.scrollHeight - 40);
      }
    }
  }

  function toggleSlotGcodeView() {
    isShowingSlotOnlyInEditor = !isShowingSlotOnlyInEditor;
    if (selectedSlotId) {
      const slot = fixtureSlots.find(s => s.slot_id === selectedSlotId);
      if (slot) updateEditorForSelectedSlot(slot);
    }
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

  function onSinglePartToggle(val) {
    isSinglePartMode = !!val;
    renderFixtureSlots();
    scheduleAutoSync();
  }

  function updateAirCarveBanner() {
    const banner = document.getElementById('aircarveBanner');
    const hasAirCarve = fixtureSlots.some(s => s.is_air_carve);
    banner.style.display = hasAirCarve ? 'inline-flex' : 'none';
  }

  function calculateGeometryTimes(pts, units) {
    const times = new Array(pts.length);
    times[0] = 0;
    if (pts.length <= 1) return { cumTimes: times, totalTime: 0 };

    const isImp = units === 'imperial';
    const rapidSpeed = isImp ? 240 : DEFAULT_RAPID_SPEED; // 240 IPM vs 6000 mm/min (~100 mm/s)
    const defaultFeed = isImp ? 40 : DEFAULT_FEED_SPEED;   // 40 IPM vs 1000 mm/min

    let runningTime = 0;
    for (let i = 1; i < pts.length; i++) {
      const prev = pts[i - 1];
      const cur = pts[i];
      const dx = (cur.x || 0) - (prev.x || 0);
      const dy = (cur.y || 0) - (prev.y || 0);
      const dz = (cur.z || 0) - (prev.z || 0);
      const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);

      let speedMin = 0;
      if (cur.type === 'rapid') {
        speedMin = rapidSpeed;
      } else if (cur.feed && cur.feed > 0) {
        speedMin = cur.feed;
      } else {
        speedMin = defaultFeed;
      }

      const speedSec = Math.max(0.1, speedMin / 60);
      let dur = dist / speedSec;
      if (dist < 0.001) {
        dur = MIN_MOVE_TIME;
      }
      runningTime += dur;
      times[i] = runningTime;
    }
    return { cumTimes: times, totalTime: runningTime };
  }

  function findSegmentIndex(t) {
    if (!cumMoveTimes || cumMoveTimes.length <= 1) return 0;
    let low = 0;
    let high = cumMoveTimes.length - 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (cumMoveTimes[mid] <= t) {
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    return Math.max(0, Math.min(cumMoveTimes.length - 2, high));
  }

  async function previewJob(jobId) {
    activePreviewJobId = jobId;

    // Highlight selected card in staging bin
    document.querySelectorAll('.staged-job-card').forEach(c => {
      c.classList.remove('selected');
    });
    const selCard = document.getElementById(`stagedJobCard_${jobId}`);
    if (selCard) {
      selCard.classList.add('selected');
    }

    try {
      const res = await fetch(`/api/job/${jobId}`);
      const data = await res.json();
      activeJobData = data;
      activeJobUnits = data.units || 'metric';
      activeGeometryPoints = data.geometry || [];

      // Safe Z threshold & draggable widget setup
      const serverInferred = (typeof data.inferred_safe_z_threshold === 'number')
        ? data.inferred_safe_z_threshold
        : (typeof data.safe_z_threshold === 'number' ? data.safe_z_threshold : 0.0);
      activeInferredSafeZ = serverInferred || inferSafeZThreshold(activeGeometryPoints, activeJobUnits);
      if (persistedSafeZByJobId[jobId] !== undefined) {
        activeSafeZThreshold = persistedSafeZByJobId[jobId];
      } else {
        try {
          const stored = localStorage.getItem(`safe_z_${jobId}`);
          if (stored !== null && !isNaN(parseFloat(stored))) {
            activeSafeZThreshold = parseFloat(stored);
            persistedSafeZByJobId[jobId] = activeSafeZThreshold;
          } else {
            activeSafeZThreshold = activeInferredSafeZ;
          }
        } catch (_) {
          activeSafeZThreshold = activeInferredSafeZ;
        }
      }

      if (!manualSplitsByJobId[jobId]) {
        try {
          const storedSplits = localStorage.getItem(`manual_splits_${jobId}`);
          if (storedSplits) {
            manualSplitsByJobId[jobId] = new Set(JSON.parse(storedSplits));
          } else {
            manualSplitsByJobId[jobId] = new Set();
          }
        } catch (_) {
          manualSplitsByJobId[jobId] = new Set();
        }
      }
      closeRapidTooltip();

      const ext = data.extents || {};
      const minZ = (typeof ext.min_z === 'number') ? ext.min_z : -10;
      const maxZ = (typeof ext.max_z === 'number') ? ext.max_z : 25;
      const zMinSlider = Math.floor(minZ * 10) / 10;
      let zMaxSlider = Math.ceil(maxZ * 10) / 10;
      if (zMaxSlider <= zMinSlider) {
        zMaxSlider = zMinSlider + (activeJobUnits === 'imperial' ? 1.0 : 25.0);
      }

      const zSlider = document.getElementById('safeZSlider');
      if (zSlider) {
        zSlider.min = zMinSlider;
        zSlider.max = zMaxSlider;
        zSlider.step = activeJobUnits === 'imperial' ? 0.005 : 0.05;
        zSlider.value = activeSafeZThreshold;
      }
      const minDisp = document.getElementById('safeZMinDisplay');
      if (minDisp) minDisp.textContent = `${minZ.toFixed(2)} ${activeJobUnits === 'imperial' ? 'in' : 'mm'}`;
      const maxDisp = document.getElementById('safeZMaxDisplay');
      if (maxDisp) maxDisp.textContent = `${maxZ.toFixed(2)} ${activeJobUnits === 'imperial' ? 'in' : 'mm'}`;
      updateSafeZValDisplay();

      GcodeViewer.loadGeometry(activeGeometryPoints, activeSafeZThreshold);

      // Re-evaluate ops if safe Z was customized or manual splits exist
      if (Math.abs(activeSafeZThreshold - activeInferredSafeZ) > 1e-4 || (manualSplitsByJobId[jobId] && manualSplitsByJobId[jobId].size > 0)) {
        recalculateOperationsFromThreshold(activeSafeZThreshold);
      }

      const timing = calculateGeometryTimes(activeGeometryPoints, activeJobUnits);
      cumMoveTimes = timing.cumTimes;
      totalToolpathTime = timing.totalTime;

      const slider = document.getElementById('scrubberSlider');
      slider.min = 0;
      slider.max = totalToolpathTime > 0 ? totalToolpathTime : 1;
      slider.step = totalToolpathTime > 0 ? Math.min(0.05, totalToolpathTime / 1000) : 0.01;
      slider.value = 0;
      currentScrubTime = 0;
      currentScrubIdx = 0;

      onScrub(0);
      renderTimelineRapidMarkers();
      renderTimelineToolMarkers();
      renderTimelinePartMarkers();
      renderTimelinePauseMarkers();

      if (selectedSlotId) {
        const selSlot = fixtureSlots.find(s => s.slot_id === selectedSlotId);
        if (selSlot && selSlot.job_id === jobId) {
          renderTimelineSlotHighlight(selSlot);
          if (typeof GcodeViewer.setHighlightRange === 'function') {
            GcodeViewer.setHighlightRange(selSlot.start_line, selSlot.end_line);
          }
        } else {
          clearSelectedSlot();
        }
      } else {
        if (typeof GcodeViewer.setHighlightRange === 'function') {
          GcodeViewer.setHighlightRange(null, null);
        }
      }

      loadJobGcodeIntoEditor(jobId, data.name);
    } catch (e) {
      console.error("Failed to load job geometry", e);
    }
  }

  function updateSafeZValDisplay() {
    const disp = document.getElementById('safeZValDisplay');
    if (!disp) return;
    const uSuffix = activeJobUnits === 'imperial' ? ' in' : ' mm';
    disp.textContent = `${activeSafeZThreshold.toFixed(3)}${uSuffix}`;
  }

  function onSafeZThresholdChange(val) {
    activeSafeZThreshold = (typeof val === 'number' && !isNaN(val)) ? val : 0.0;
    if (activePreviewJobId) {
      persistedSafeZByJobId[activePreviewJobId] = activeSafeZThreshold;
      try {
        localStorage.setItem(`safe_z_${activePreviewJobId}`, String(activeSafeZThreshold));
      } catch (_) {}
    }

    updateSafeZValDisplay();
    GcodeViewer.updateSafeZThreshold(activeSafeZThreshold);
    recalculateOperationsFromThreshold(activeSafeZThreshold);
    renderTimelinePartMarkers();
    renderTimelineRapidMarkers();

    // Debounce background persist to server
    if (safeZDebounceTimer) clearTimeout(safeZDebounceTimer);
    safeZDebounceTimer = setTimeout(() => {
      if (activePreviewJobId) {
        fetch(`/api/job/${activePreviewJobId}/safe_z`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ safe_z: activeSafeZThreshold })
        }).catch(err => console.warn('Could not persist safe Z to server', err));
      }
    }, 350);
  }

  function inferSafeZThreshold(geometry, units) {
    if (!geometry || geometry.length === 0) return 0.0;
    const rapidPts = geometry.filter(p => p.type === 'rapid');
    if (!rapidPts || rapidPts.length === 0) return 0.0;

    const rapidZList = rapidPts.map(p => Math.round(p.z * 10000) / 10000);
    const distinctZ = Array.from(new Set(rapidZList)).sort((a, b) => a - b);
    const maxRapid = distinctZ[distinctZ.length - 1];
    const fallback = Math.round((maxRapid + (units === 'imperial' ? 0.1 : 2.0)) * 10000) / 10000;

    if (distinctZ.length <= 1) return fallback;

    const cutPts = geometry.filter(p => p.type === 'linear' || p.type === 'arc');
    if (!cutPts || cutPts.length === 0) return fallback;

    const firstCutLine = Math.min(...cutPts.map(p => p.line));
    const lastCutLine = Math.max(...cutPts.map(p => p.line));

    const intermediateRapids = rapidPts.filter(p => p.line > firstCutLine && p.line < lastCutLine);
    if (!intermediateRapids || intermediateRapids.length === 0) return fallback;

    const minGap = units === 'imperial' ? 0.25 : 6.0;

    for (let i = 1; i < distinctZ.length; i++) {
      const lowerZ = distinctZ[i - 1];
      const upperZ = distinctZ[i];
      const gap = upperZ - lowerZ;

      if (gap < minGap) continue;

      let retractEvents = 0;
      for (let idx = 0; idx < geometry.length; idx++) {
        const pt = geometry[idx];
        if (pt.line > firstCutLine && pt.line < lastCutLine) {
          if (pt.type === 'rapid' && pt.z >= upperZ - 1e-4) {
            const prevPt = idx > 0 ? geometry[idx - 1] : null;
            if (!prevPt || prevPt.type !== 'rapid' || prevPt.z < upperZ - 1e-4) {
              retractEvents++;
            }
          }
        }
      }

      if (retractEvents >= 1 && retractEvents <= 2) {
        return Math.round(((lowerZ + upperZ) / 2.0) * 10000) / 10000;
      }
    }

    return fallback;
  }

  function resetSafeZToInferred() {
    if (activeGeometryPoints && activeGeometryPoints.length > 0) {
      activeInferredSafeZ = inferSafeZThreshold(activeGeometryPoints, activeJobUnits);
    }
    activeSafeZThreshold = activeInferredSafeZ;

    if (activePreviewJobId) {
      delete persistedSafeZByJobId[activePreviewJobId];
      delete manualSplitsByJobId[activePreviewJobId];
      try {
        localStorage.removeItem(`safe_z_${activePreviewJobId}`);
        localStorage.removeItem(`manual_splits_${activePreviewJobId}`);
      } catch (_) {}

      fetch(`/api/job/${activePreviewJobId}/safe_z`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          safe_z: activeInferredSafeZ,
          manual_splits: [],
          reset_to_inferred: true
        })
      }).then(res => res.json()).then(data => {
        if (data && data.ok && data.operations) {
          if (activeJobData) {
            activeJobData.operations = data.operations;
            activeJobData.safe_z_threshold = data.safe_z_threshold;
          }
          const tgtAll = allStagedJobs.find(j => j.id === activePreviewJobId);
          if (tgtAll) {
            tgtAll.operations = data.operations;
            tgtAll.safe_z_threshold = data.safe_z_threshold;
            delete tgtAll.custom_safe_z;
          }
          const tgtStaged = stagedJobs.find(j => j.id === activePreviewJobId);
          if (tgtStaged) {
            tgtStaged.operations = data.operations;
            tgtStaged.safe_z_threshold = data.safe_z_threshold;
            delete tgtStaged.custom_safe_z;
          }
          const cardBadge = document.getElementById(`opCountBadge_${activePreviewJobId}`);
          if (cardBadge) {
            cardBadge.textContent = `${data.operations.length} op${data.operations.length === 1 ? '' : 's'} (${data.operations.length} slot${data.operations.length === 1 ? '' : 's'})`;
            cardBadge.title = data.operations.map(o => o.name).join(' | ');
          }
          renderTimelinePartMarkers();
          renderTimelineRapidMarkers();
        }
      }).catch(err => console.warn('Could not reset safe Z on server', err));
    }

    const zSlider = document.getElementById('safeZSlider');
    if (zSlider) zSlider.value = activeSafeZThreshold;
    updateSafeZValDisplay();
    closeRapidTooltip();
    GcodeViewer.updateSafeZThreshold(activeSafeZThreshold);
    recalculateOperationsFromThreshold(activeSafeZThreshold);
    renderTimelinePartMarkers();
    renderTimelineRapidMarkers();
  }

  function recalculateOperationsFromThreshold(threshold) {
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;

    const manualSplits = (activePreviewJobId && manualSplitsByJobId[activePreviewJobId])
      ? manualSplitsByJobId[activePreviewJobId]
      : new Set();

    const cuttingPoints = activeGeometryPoints.filter(p => p.type === 'linear' || p.type === 'arc');
    const cuttingLines = new Set(cuttingPoints.map(p => p.line));
    const maxCuttingLine = cuttingPoints.length > 0 ? Math.max(...cuttingLines) : 0;

    let firstTool = 1;
    for (let p of activeGeometryPoints) {
      if (p.tool) { firstTool = p.tool; break; }
    }

    const splits = [];
    let currentTool = firstTool;
    let hasCutInOp = false;

    for (let i = 0; i < activeGeometryPoints.length; i++) {
      const p = activeGeometryPoints[i];

      // 0. Manual op split marked by user at this line
      if (manualSplits.has(p.line)) {
        splits.push({ line: p.line, tool: currentTool });
        hasCutInOp = false;
        continue;
      }

      if (p.tool && p.tool !== currentTool) {
        splits.push({ line: p.line, tool: p.tool });
        currentTool = p.tool;
        hasCutInOp = false;
        continue;
      }

      if (p.type === 'linear' || p.type === 'arc') {
        hasCutInOp = true;
      }

      if (hasCutInOp && p.line < maxCuttingLine) {
        if (p.type === 'rapid' && p.z >= threshold) {
          splits.push({ line: p.line, tool: currentTool });
          hasCutInOp = false;
        }
      }
    }

    const deduped = [];
    for (const sp of splits) {
      if (deduped.length === 0 || sp.line > deduped[deduped.length - 1].line + 1) {
        deduped.push(sp);
      }
    }

    const ranges = [];
    let prevStart = 1;
    let prevTool = firstTool;
    for (const sp of deduped) {
      ranges.push({ start: prevStart, end: sp.line - 1, tool: prevTool });
      prevStart = sp.line;
      prevTool = sp.tool;
    }
    const maxLine = activeGeometryPoints[activeGeometryPoints.length - 1].line || 1000;
    ranges.push({ start: prevStart, end: maxLine, tool: prevTool });

    const newOps = [];
    ranges.forEach((rng, idx) => {
      const opPts = activeGeometryPoints.filter(pt => pt.line >= rng.start && pt.line <= rng.end);
      const opCuts = opPts.filter(pt => pt.type === 'linear' || pt.type === 'arc');
      if (opCuts.length === 0 && newOps.length > 0 && cuttingPoints.length > 0) {
        newOps[newOps.length - 1].end_line = rng.end;
        return;
      }
      newOps.push({
        op: newOps.length + 1,
        name: `Op ${newOps.length + 1} (T${rng.tool})`,
        tool: rng.tool,
        start_line: rng.start,
        end_line: rng.end,
        lines: rng.end - rng.start + 1,
      });
    });

    if (activeJobData) {
      activeJobData.operations = newOps;
      activeJobData.safe_z_threshold = threshold;
    }

    // Persist operations and threshold in staging bin job objects
    if (activePreviewJobId) {
      const tgtAll = allStagedJobs.find(j => j.id === activePreviewJobId);
      if (tgtAll) {
        tgtAll.operations = newOps;
        tgtAll.safe_z_threshold = threshold;
        tgtAll.custom_safe_z = threshold;
      }
      const tgtStaged = stagedJobs.find(j => j.id === activePreviewJobId);
      if (tgtStaged) {
        tgtStaged.operations = newOps;
        tgtStaged.safe_z_threshold = threshold;
        tgtStaged.custom_safe_z = threshold;
      }
    }

    // Update staging bin card badge live to show how many slots are in that job
    const cardBadge = document.getElementById(`opCountBadge_${activePreviewJobId}`);
    if (cardBadge) {
      cardBadge.textContent = `${newOps.length} op${newOps.length === 1 ? '' : 's'} (${newOps.length} slot${newOps.length === 1 ? '' : 's'})`;
      cardBadge.title = newOps.map(o => o.name).join(' | ');
    }
  }

  // Scrubber & Playback Controls
  function onScrub(val) {
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;
    const t = Math.max(0, Math.min(totalToolpathTime, parseFloat(val) || 0));
    currentScrubTime = t;

    if (activeGeometryPoints.length === 1) {
      currentScrubIdx = 0;
      const p = activeGeometryPoints[0];
      GcodeViewer.setCursor(0, [p.x, p.y, p.z]);
      updateHud(0, p.x, p.y, p.z);
      updateTimeDisplay(t);
      return;
    }

    const segIdx = findSegmentIndex(t);
    const tStart = cumMoveTimes[segIdx] || 0;
    const tEnd = cumMoveTimes[segIdx + 1] !== undefined ? cumMoveTimes[segIdx + 1] : tStart;
    const segDur = tEnd - tStart;
    const alpha = segDur > 1e-6 ? Math.max(0, Math.min(1, (t - tStart) / segDur)) : 1.0;

    const pFrom = activeGeometryPoints[segIdx];
    const pTo = activeGeometryPoints[segIdx + 1] || pFrom;

    const curX = pFrom.x + alpha * (pTo.x - pFrom.x);
    const curY = pFrom.y + alpha * (pTo.y - pFrom.y);
    const curZ = pFrom.z + alpha * (pTo.z - pFrom.z);

    currentScrubIdx = alpha >= 0.5 ? Math.min(activeGeometryPoints.length - 1, segIdx + 1) : segIdx;
    GcodeViewer.setCursor(currentScrubIdx, [curX, curY, curZ]);
    updateHud(segIdx + 1 < activeGeometryPoints.length ? segIdx + 1 : segIdx, curX, curY, curZ);
    updateTimeDisplay(t);
  }

  function updateTimeDisplay(t) {
    const el = document.getElementById('scrubberTimeDisplay');
    if (el) {
      el.textContent = `${formatDuration(t)} / ${formatDuration(totalToolpathTime)}`;
    }
  }

  function updateHud(idx, interpX, interpY, interpZ) {
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;
    const p = activeGeometryPoints[idx] || activeGeometryPoints[0];
    document.getElementById('hudLine').textContent = p.line;
    document.getElementById('hudCmd').textContent = p.type === 'rapid' ? 'G0 (Rapid)' : (p.type === 'arc' ? 'G2/G3 (Arc)' : 'G1 (Feed)');
    const x = typeof interpX === 'number' ? interpX : p.x;
    const y = typeof interpY === 'number' ? interpY : p.y;
    const z = typeof interpZ === 'number' ? interpZ : p.z;
    const uSuffix = activeJobUnits === 'imperial' ? ' in' : ' mm';
    const feedSuffix = activeJobUnits === 'imperial' ? ' IPM' : ' mm/min';
    document.getElementById('hudPos').textContent = `X ${x.toFixed(3)} Y ${y.toFixed(3)} Z ${z.toFixed(3)}${uSuffix}`;
    document.getElementById('hudFeed').textContent = `${(p.feed || 0).toFixed(0)}${feedSuffix}`;
    document.getElementById('hudTool').textContent = `T${p.tool || 1}`;
  }

  function jumpToPoint(idx) {
    stopPlay();
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;
    idx = Math.max(0, Math.min(activeGeometryPoints.length - 1, idx));
    const targetTime = (cumMoveTimes && cumMoveTimes[idx] !== undefined) ? cumMoveTimes[idx] : 0;
    const slider = document.getElementById('scrubberSlider');
    if (slider) slider.value = targetTime;
    onScrub(targetTime);
  }

  function step(dir) {
    stopPlay();
    if (!activeGeometryPoints || activeGeometryPoints.length === 0) return;
    let nextIdx = currentScrubIdx + dir;
    nextIdx = Math.max(0, Math.min(activeGeometryPoints.length - 1, nextIdx));
    jumpToPoint(nextIdx);
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

      const timeDelta = dt * playSpeed * playDirection;
      const slider = document.getElementById('scrubberSlider');
      let nextTime = currentScrubTime + timeDelta;

      if (nextTime >= totalToolpathTime) {
        nextTime = totalToolpathTime;
        slider.value = nextTime;
        onScrub(nextTime);
        stopPlay();
        return;
      } else if (nextTime <= 0) {
        nextTime = 0;
        slider.value = nextTime;
        onScrub(nextTime);
        stopPlay();
        return;
      }

      slider.value = nextTime;
      onScrub(nextTime);

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

  function renderTimelineRapidMarkers() {
    const track = document.getElementById('timelineMarkersTrack');
    if (!track) return;
    track.innerHTML = '';
    if (!activeGeometryPoints || activeGeometryPoints.length === 0 || totalToolpathTime <= 0) return;

    const manualSplits = (activePreviewJobId && manualSplitsByJobId[activePreviewJobId])
      ? manualSplitsByJobId[activePreviewJobId]
      : new Set();

    for (let i = 0; i < activeGeometryPoints.length; i++) {
      const p = activeGeometryPoints[i];
      if (p.type === 'rapid' && (i === 0 || activeGeometryPoints[i - 1].type !== 'rapid')) {
        const pct = (cumMoveTimes[i] / totalToolpathTime) * 100;
        const isOpChange = p.z >= activeSafeZThreshold;
        const isManualSplit = manualSplits.has(p.line);

        const dot = document.createElement('div');
        dot.className = 'rapid-marker-dot' + (isManualSplit ? ' manual-split' : '');
        dot.style.left = `${pct}%`;
        if (isManualSplit) {
          // Styled by .manual-split
        } else if (isOpChange) {
          dot.style.background = '#fbbf24';
          dot.style.boxShadow = '0 0 6px #f59e0b';
        } else {
          dot.style.background = '#f43f5e';
          dot.style.boxShadow = '0 0 6px #e11d48';
        }

        const splitLabel = isManualSplit
          ? ' [Manual Op Split]'
          : (isOpChange ? ' [Op Change Retract]' : ' [Stepover Hop]');
        dot.title = `Line ${p.line} (Z: ${p.z.toFixed(2)})${splitLabel} - Click to configure op split`;

        const rapidIdx = i;
        const rapidPct = pct;
        dot.addEventListener('click', (e) => {
          e.stopPropagation();
          jumpToPoint(rapidIdx);
          openRapidSplitTooltip(rapidIdx, rapidPct);
        });
        track.appendChild(dot);
      }
    }
  }

  function openRapidSplitTooltip(rapidIdx, pct) {
    if (!activeGeometryPoints || !activeGeometryPoints[rapidIdx]) return;
    const pt = activeGeometryPoints[rapidIdx];
    activeClickedRapid = { index: rapidIdx, line: pt.line, z: pt.z, tool: pt.tool, pct };

    const tooltip = document.getElementById('rapidSplitTooltip');
    if (!tooltip) return;

    const manualSplits = (activePreviewJobId && manualSplitsByJobId[activePreviewJobId])
      ? manualSplitsByJobId[activePreviewJobId]
      : new Set();
    const isManualSplit = manualSplits.has(pt.line);
    const isOpChange = pt.z >= activeSafeZThreshold;

    const titleEl = document.getElementById('rapidTooltipTitle');
    if (titleEl) {
      titleEl.innerHTML = `<span>⚡</span> Rapid at Line ${pt.line}`;
    }

    const infoEl = document.getElementById('rapidTooltipInfo');
    if (infoEl) {
      const zStr = `${pt.z.toFixed(2)} ${activeJobUnits === 'imperial' ? 'in' : 'mm'}`;
      const typeStr = isManualSplit
        ? '<strong style="color:#c084fc;">Manual Op Split</strong>'
        : (isOpChange ? '<strong style="color:#fbbf24;">Safe Z Clearance Retract</strong>' : '<strong style="color:#f43f5e;">Stepover Hop</strong>');
      infoEl.innerHTML = `Height: <code>Z ${zStr}</code><br>Type: ${typeStr}<br>Tool: T${pt.tool || 1}`;
    }

    const btn = document.getElementById('btnToggleOpSplit');
    if (btn) {
      if (isManualSplit) {
        btn.textContent = '✕ Remove Operation Split';
        btn.className = 'btn btn-sm btn-danger';
        btn.title = 'Remove this manual split point';
      } else {
        btn.textContent = '✂ Mark as separate operation?';
        btn.className = 'btn btn-sm btn-primary';
        btn.title = 'Split the job into a separate operation starting at this rapid move';
      }
    }

    const clampedPct = Math.max(12, Math.min(88, pct));
    tooltip.style.left = `${clampedPct}%`;
    tooltip.style.display = 'block';
  }

  function closeRapidTooltip() {
    const tooltip = document.getElementById('rapidSplitTooltip');
    if (tooltip) tooltip.style.display = 'none';
    activeClickedRapid = null;
  }

  function toggleManualOpSplitAtActiveRapid() {
    if (!activeClickedRapid || !activePreviewJobId) return;
    const line = activeClickedRapid.line;

    if (!manualSplitsByJobId[activePreviewJobId]) {
      manualSplitsByJobId[activePreviewJobId] = new Set();
    }
    const splits = manualSplitsByJobId[activePreviewJobId];

    if (splits.has(line)) {
      splits.delete(line);
    } else {
      splits.add(line);
    }

    try {
      localStorage.setItem(`manual_splits_${activePreviewJobId}`, JSON.stringify(Array.from(splits)));
    } catch (_) {}

    fetch(`/api/job/${activePreviewJobId}/safe_z`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        safe_z: activeSafeZThreshold,
        manual_splits: Array.from(splits)
      })
    }).catch(err => console.warn('Could not persist manual splits to server', err));

    recalculateOperationsFromThreshold(activeSafeZThreshold);
    renderTimelinePartMarkers();
    renderTimelineRapidMarkers();

    if (activeClickedRapid) {
      openRapidSplitTooltip(activeClickedRapid.index, activeClickedRapid.pct);
    }
  }

  function renderTimelinePauseMarkers() {
    const track = document.getElementById('pauseMarkersTrack');
    if (!track) return;
    track.innerHTML = '';
    if (!activeGeometryPoints || activeGeometryPoints.length === 0 || totalToolpathTime <= 0) return;

    // Collect all pauses for active preview job
    const slot = fixtureSlots.find(s => s.job_id === activePreviewJobId);
    if (!slot || !slot.pauses) return;

    slot.pauses.forEach(p => {
      // Find point index matching pause line
      const ptIdx = activeGeometryPoints.findIndex(pt => pt.line >= p.line);
      if (ptIdx >= 0) {
        const pct = (cumMoveTimes[ptIdx] / totalToolpathTime) * 100;
        const pin = document.createElement('div');
        pin.className = 'pause-marker-pin';
        pin.style.left = `${pct}%`;
        pin.title = `Pause at line ${p.line}: ${p.type} (${p.msg || 'No msg'}) - Click to jump`;
        pin.addEventListener('click', (e) => {
          e.stopPropagation();
          jumpToPoint(ptIdx);
        });
        track.appendChild(pin);
      }
    });
  }

  const TOOL_COLORS = {
    1: '#06b6d4', // Cyan
    2: '#a855f7', // Purple
    3: '#f59e0b', // Amber
    4: '#10b981', // Emerald
    5: '#ec4899', // Rose
    6: '#6366f1', // Indigo
  };

  function getToolColor(toolNum) {
    if (TOOL_COLORS[toolNum]) return TOOL_COLORS[toolNum];
    const hue = (toolNum * 67) % 360;
    return `hsl(${hue}, 80%, 55%)`;
  }

  function renderTimelineToolMarkers() {
    const track = document.getElementById('timelineMarkersTrack');
    if (!track || !activeGeometryPoints || activeGeometryPoints.length === 0 || totalToolpathTime <= 0) return;

    let lastTool = null;
    for (let i = 0; i < activeGeometryPoints.length; i++) {
      const p = activeGeometryPoints[i];
      if (p.tool !== lastTool) {
        lastTool = p.tool;
        const pct = (cumMoveTimes[i] / totalToolpathTime) * 100;
        const color = getToolColor(p.tool);

        const pin = document.createElement('div');
        pin.className = 'timeline-map-pin tool-pin';
        pin.style.left = `${pct}%`;
        pin.style.color = color;
        pin.title = `Tool Change: T${p.tool} at line ${p.line} - Click to jump`;

        const badge = document.createElement('div');
        badge.className = 'timeline-map-pin-badge';
        badge.style.background = color;
        badge.textContent = `T${p.tool}`;

        const needle = document.createElement('div');
        needle.className = 'timeline-map-pin-needle';
        needle.style.background = color;

        pin.appendChild(badge);
        pin.appendChild(needle);

        const ptIdx = i;
        pin.addEventListener('click', (e) => {
          e.stopPropagation();
          jumpToPoint(ptIdx);
        });

        track.appendChild(pin);
      }
    }
  }

  function renderTimelinePartMarkers() {
    const track = document.getElementById('timelineMarkersTrack');
    if (!track || !activeGeometryPoints || activeGeometryPoints.length === 0 || totalToolpathTime <= 0) return;
    if (!activeJobData || !activeJobData.operations || activeJobData.operations.length <= 1) return;

    const opColors = ['#3b82f6', '#8b5cf6', '#0284c7', '#0d9488', '#d97706'];

    activeJobData.operations.forEach((op, opIdx) => {
      const ptIdx = activeGeometryPoints.findIndex(pt => pt.line >= op.start_line);
      if (ptIdx >= 0) {
        const pct = (cumMoveTimes[ptIdx] / totalToolpathTime) * 100;
        const color = opColors[opIdx % opColors.length];

        const pin = document.createElement('div');
        pin.className = 'timeline-map-pin part-pin';
        pin.style.left = `${pct}%`;
        pin.style.top = '-46px'; // Positioned above tool pin tier
        pin.style.color = color;
        pin.title = `Op ${op.op}: ${escapeHtml(op.name)} at line ${op.start_line} - Click to jump`;

        const badge = document.createElement('div');
        badge.className = 'timeline-map-pin-badge';
        badge.style.background = color;
        badge.textContent = `P${op.op}`;

        const needle = document.createElement('div');
        needle.className = 'timeline-map-pin-needle';
        needle.style.background = color;
        needle.style.height = '14px';

        pin.appendChild(badge);
        pin.appendChild(needle);

        pin.addEventListener('click', (e) => {
          e.stopPropagation();
          jumpToPoint(ptIdx);
        });

        track.appendChild(pin);
      }
    });
  }

  // G-code Text Editor Functions (Sequence Override)
  async function loadJobGcodeIntoEditor(jobId, jobName) {
    if (activeEditorSlotId) return; // Keep active slot if user is editing a specific slot
    const label = document.getElementById('editorTargetLabel');
    if (label) label.textContent = `Previewing: ${jobName || jobId}`;
    const badge = document.getElementById('editorStatusBadge');
    if (badge) {
      badge.textContent = 'Original Source';
      badge.style.background = 'rgba(148, 163, 184, 0.15)';
      badge.style.color = 'var(--text-muted)';
    }

    try {
      const res = await fetch(`/api/job/${jobId}/gcode`);
      const data = await res.json();
      originalJobGcode = data.gcode || '';
      currentEditorGcode = originalJobGcode;
      const editor = document.getElementById('gcodeEditor');
      if (editor) {
        editor.value = currentEditorGcode;
        updateEditorStats();
      }
    } catch (e) {
      console.warn("Could not load G-code for editor", e);
    }
  }

  async function openSlotInEditor(slotId) {
    await selectSlot(slotId);
  }

  function onEditorInput() {
    updateEditorStats();
    const badge = document.getElementById('editorStatusBadge');
    if (badge) {
      badge.textContent = 'Unsaved Changes';
      badge.style.background = 'rgba(245, 158, 11, 0.2)';
      badge.style.color = 'var(--warning)';
    }
  }

  function updateEditorStats() {
    const editor = document.getElementById('gcodeEditor');
    const stats = document.getElementById('editorStats');
    if (!editor || !stats) return;
    const lines = editor.value ? editor.value.split('\n').length : 0;
    const kb = (new Blob([editor.value]).size / 1024).toFixed(1);
    stats.textContent = `Lines: ${lines} | Size: ${kb} KB`;
  }

  async function applyEditorGcode() {
    const editor = document.getElementById('gcodeEditor');
    if (!editor) return;
    const gcodeText = editor.value;

    let targetSlot = activeEditorSlotId
      ? fixtureSlots.find(s => s.slot_id === activeEditorSlotId)
      : (fixtureSlots.find(s => s.job_id === activePreviewJobId) || fixtureSlots[0]);

    if (!targetSlot) {
      if (fixtureSlots.length === 0) {
        alert("Please add this job to the fixture sequence first to apply sequence edits.");
        return;
      }
      targetSlot = fixtureSlots[0];
    }

    targetSlot.custom_gcode = gcodeText;

    // Parse edited gcode via backend to update 3D toolpath & telemetry
    try {
      const res = await fetch('/api/parse_gcode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gcode: gcodeText, filename: `${targetSlot.job_name}_edited.ngc` }),
      });
      const parsed = await res.json();
      if (parsed.ok) {
        activeJobData = parsed;
        activeJobUnits = parsed.units || 'metric';
        activeGeometryPoints = parsed.geometry || [];

        activeInferredSafeZ = (typeof parsed.safe_z_threshold === 'number') ? parsed.safe_z_threshold : 0.0;
        activeSafeZThreshold = activeInferredSafeZ;

        const ext = parsed.extents || {};
        const minZ = (typeof ext.min_z === 'number') ? ext.min_z : -10;
        const maxZ = (typeof ext.max_z === 'number') ? ext.max_z : 25;
        const zMinSlider = Math.floor(minZ * 10) / 10;
        let zMaxSlider = Math.ceil(maxZ * 10) / 10;
        if (zMaxSlider <= zMinSlider) {
          zMaxSlider = zMinSlider + (activeJobUnits === 'imperial' ? 1.0 : 25.0);
        }

        const zSlider = document.getElementById('safeZSlider');
        if (zSlider) {
          zSlider.min = zMinSlider;
          zSlider.max = zMaxSlider;
          zSlider.step = activeJobUnits === 'imperial' ? 0.005 : 0.05;
          zSlider.value = activeSafeZThreshold;
        }
        const minDisp = document.getElementById('safeZMinDisplay');
        if (minDisp) minDisp.textContent = `${minZ.toFixed(2)} ${activeJobUnits === 'imperial' ? 'in' : 'mm'}`;
        const maxDisp = document.getElementById('safeZMaxDisplay');
        if (maxDisp) maxDisp.textContent = `${maxZ.toFixed(2)} ${activeJobUnits === 'imperial' ? 'in' : 'mm'}`;
        updateSafeZValDisplay();

        GcodeViewer.loadGeometry(activeGeometryPoints, activeSafeZThreshold);

        const timing = calculateGeometryTimes(activeGeometryPoints, activeJobUnits);
        cumMoveTimes = timing.cumTimes;
        totalToolpathTime = timing.totalTime;

        const slider = document.getElementById('scrubberSlider');
        slider.min = 0;
        slider.max = totalToolpathTime > 0 ? totalToolpathTime : 1;
        slider.value = 0;
        onScrub(0);
        renderTimelineRapidMarkers();
        renderTimelineToolMarkers();
        renderTimelinePartMarkers();
        renderTimelinePauseMarkers();

        const badge = document.getElementById('editorStatusBadge');
        if (badge) {
          badge.textContent = 'Sequence Override Active';
          badge.style.background = 'rgba(16, 185, 129, 0.2)';
          badge.style.color = 'var(--success)';
        }

        renderFixtureSlots();
        scheduleAutoSync();
      }
    } catch (e) {
      alert("Error parsing edited G-code: " + e.message);
    }
  }

  async function revertEditorGcode() {
    if (!confirm("Revert G-code back to original source file? Custom sequence edits for this slot will be discarded.")) {
      return;
    }
    const targetSlot = activeEditorSlotId
      ? fixtureSlots.find(s => s.slot_id === activeEditorSlotId)
      : fixtureSlots.find(s => s.job_id === activePreviewJobId);

    if (targetSlot) {
      targetSlot.custom_gcode = null;
    }

    const editor = document.getElementById('gcodeEditor');
    if (editor) {
      editor.value = originalJobGcode;
      updateEditorStats();
    }

    const badge = document.getElementById('editorStatusBadge');
    if (badge) {
      badge.textContent = 'Original Source';
      badge.style.background = 'rgba(148, 163, 184, 0.15)';
      badge.style.color = 'var(--text-muted)';
    }

    if (activePreviewJobId) {
      await previewJob(activePreviewJobId);
    }
    renderFixtureSlots();
    scheduleAutoSync();
  }

  const PANE_IDS = ['paneLeft', 'paneRight', 'paneEditor'];

  function getOpenPanes() {
    return PANE_IDS.filter(id => {
      const el = document.getElementById(id);
      return el && !el.classList.contains('collapsed');
    });
  }

  function updateCollapseButtonsState() {
    const openPanes = getOpenPanes();
    const isOnlyOneOpen = openPanes.length <= 1;

    PANE_IDS.forEach(id => {
      const btn = document.getElementById(`btnCollapse_${id}`);
      if (!btn) return;
      const pane = document.getElementById(id);
      const isThisOpen = pane && !pane.classList.contains('collapsed');

      if (isThisOpen && isOnlyOneOpen) {
        btn.disabled = true;
        btn.title = "Cannot collapse the only remaining open pane";
      } else {
        btn.disabled = false;
        btn.title = `Collapse ${id === 'paneLeft' ? 'sequence' : (id === 'paneRight' ? '3D' : 'editor')} pane`;
      }
    });

    // Flexible layout compensation if 3D viewport is collapsed
    const centerCollapsed = document.getElementById('paneRight')?.classList.contains('collapsed');
    const leftPane = document.getElementById('paneLeft');
    const editorPane = document.getElementById('paneEditor');
    if (centerCollapsed) {
      if (editorPane && !editorPane.classList.contains('collapsed')) {
        editorPane.style.flex = '1 1 auto';
      }
      if (leftPane && !leftPane.classList.contains('collapsed') && (!editorPane || editorPane.classList.contains('collapsed'))) {
        leftPane.style.flex = '1 1 auto';
      }
    } else {
      if (editorPane) editorPane.style.flex = '';
      if (leftPane) leftPane.style.flex = '';
    }
  }

  function togglePaneCollapse(paneId) {
    const pane = document.getElementById(paneId);
    if (!pane) return;

    const isCurrentlyCollapsed = pane.classList.contains('collapsed');

    // If trying to collapse, ensure at least one other pane remains open
    if (!isCurrentlyCollapsed) {
      const openPanes = getOpenPanes();
      if (openPanes.length <= 1 && openPanes.includes(paneId)) {
        showToast("At least one workspace pane must remain open.", "info");
        return;
      }
    }

    pane.classList.toggle('collapsed');
    updateCollapseButtonsState();

    setTimeout(() => {
      GcodeViewer.resizeCanvas();
    }, 220);
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

  function showToast(msg, type = 'info', actions = {}) {
    const toast = document.getElementById('syncToast');
    const toastMsg = document.getElementById('syncToastMsg');
    const btnDiag = document.getElementById('btnToastDiag');
    const btnRetry = document.getElementById('btnToastRetry');
    if (!toast || !toastMsg) return;

    toastMsg.textContent = msg;
    if (type === 'error') {
      toast.style.background = '#7f1d1d';
      toast.style.border = '1px solid #ef4444';
      toast.style.color = '#fee2e2';
    } else if (type === 'success') {
      toast.style.background = '#064e3b';
      toast.style.border = '1px solid #10b981';
      toast.style.color = '#d1fae5';
    } else {
      toast.style.background = '#1e293b';
      toast.style.border = '1px solid #38bdf8';
      toast.style.color = '#f0f9ff';
    }

    if (btnDiag) btnDiag.style.display = actions.showDiag ? 'inline-block' : 'none';
    if (btnRetry) btnRetry.style.display = actions.showRetry ? 'inline-block' : 'none';
    toast.style.display = 'flex';

    if (type === 'success') {
      setTimeout(() => { dismissToast(); }, 6000);
    }
  }

  function dismissToast() {
    const toast = document.getElementById('syncToast');
    if (toast) toast.style.display = 'none';
  }

  async function syncNow() {
    if (isSyncing) return;
    cancelAutoSync();
    if (fixtureSlots.length === 0) {
      showToast("No jobs in fixture sequence. Add jobs first.", "info");
      return;
    }

    isSyncing = true;
    const uploadBtns = document.querySelectorAll("button[onclick*='syncNow']");
    uploadBtns.forEach(b => {
      b.disabled = true;
      b.dataset.prevText = b.textContent;
      b.textContent = '⏳ Syncing...';
    });
    showToast("Syncing sequence to controller...", "info");

    const payload = {
      slots: fixtureSlots,
      strategy: activeStrategy,
      single_part: isSinglePartMode,
    };

    try {
      const res = await fetch('/api/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.ok) {
        const upMsg = data.uploaded && data.uploaded.length > 0 ? data.uploaded.join(', ') : 'files';
        showToast(`✓ Synced ${upMsg} to gcode/workflow/ (${data.duration_sec || 0}s)`, "success");
        await checkControllerStatus();
      } else {
        const errMsg = data.error || "Controller sync failed or rejected connection.";
        showToast(`⚠ Sync Error: ${errMsg}`, "error", { showDiag: true, showRetry: true });
      }
    } catch (e) {
      showToast(`⚠ Sync Network Error: ${e.message}`, "error", { showDiag: true, showRetry: true });
    } finally {
      isSyncing = false;
      uploadBtns.forEach(b => {
        b.disabled = false;
        if (b.dataset.prevText) b.textContent = b.dataset.prevText;
      });
    }
  }

  function checkAndDisplayPortWarning(port, errorStr) {
    const warningBox = document.getElementById('diagPortWarning');
    if (!warningBox) return;
    const isPort80 = (
      port === 80 ||
      (config && config.controller_port === 80) ||
      (errorStr && (errorStr.includes('200 OK') || errorStr.includes('uvicorn') || errorStr.includes('Port 80')))
    );
    warningBox.style.display = isPort80 ? 'block' : 'none';
  }

  async function fixPortTo8080() {
    config.controller_port = 8080;
    const portInput = document.getElementById('cfgPort');
    if (portInput) portInput.value = 8080;
    const diagPort = document.getElementById('diagTargetPort');
    if (diagPort) diagPort.textContent = '8080';
    const warningBox = document.getElementById('diagPortWarning');
    if (warningBox) warningBox.style.display = 'none';

    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...config,
          controller_port: 8080,
        })
      });
      const data = await res.json();
      if (data.ok) {
        config = data.config;
        showToast("Controller port updated to 8080 (RTS-X WebSocket)", "info");
        await testControllerConnectionDetailed();
      }
    } catch (e) {
      showToast("Error updating port: " + e.message, "danger");
    }
  }

  function openSyncDiagModal() {
    const modal = document.getElementById('syncDiagModal');
    if (modal) modal.style.display = 'flex';
    const hostSpan = document.getElementById('diagTargetHost');
    const portSpan = document.getElementById('diagTargetPort');
    if (hostSpan) hostSpan.textContent = config.controller_host || '192.168.1.150';
    if (portSpan) portSpan.textContent = config.controller_port || 8080;
    checkAndDisplayPortWarning(config.controller_port, null);
    loadSyncLogs();
  }

  function closeSyncDiagModal() {
    const modal = document.getElementById('syncDiagModal');
    if (modal) modal.style.display = 'none';
  }

  async function testControllerConnectionDetailed() {
    const resultSpan = document.getElementById('diagPingResult');
    if (resultSpan) {
      resultSpan.textContent = 'Testing connection...';
      resultSpan.style.color = 'var(--text-muted)';
    }
    try {
      const res = await fetch('/api/controller/test');
      const data = await res.json();
      if (data.ok) {
        if (resultSpan) {
          resultSpan.textContent = `✓ Online (${data.latency_ms} ms)`;
          resultSpan.style.color = 'var(--success)';
        }
        if (data.port) {
          config.controller_port = data.port;
          const diagPort = document.getElementById('diagTargetPort');
          if (diagPort) diagPort.textContent = data.port;
        }
        checkAndDisplayPortWarning(data.port, null);
        checkControllerStatus();
      } else {
        if (resultSpan) {
          resultSpan.textContent = `✕ Offline: ${data.error || 'Connection failed'}`;
          resultSpan.style.color = 'var(--danger)';
        }
        checkAndDisplayPortWarning(data.port, data.error);
      }
    } catch (e) {
      if (resultSpan) {
        resultSpan.textContent = `✕ Error: ${e.message}`;
        resultSpan.style.color = 'var(--danger)';
      }
    }
  }

  async function loadSyncLogs() {
    const term = document.getElementById('syncLogTerminal');
    if (!term) return;
    term.textContent = 'Fetching sync logs...';
    try {
      const res = await fetch('/api/sync/logs');
      const data = await res.json();
      const history = data.history || [];
      checkAndDisplayPortWarning(data.port, history.length > 0 ? history[0].error : null);
      if (history.length === 0) {
        term.textContent = `Target: ws://${data.host}:${data.port}\nNo sync operations logged yet. Click "⚡ Upload to RTS-X" or "⚡ Retry Sync" to test.`;
        return;
      }
      let out = `Target Controller: ws://${data.host}:${data.port}\n`;
      out += `Recent Sync History (${history.length} transactions):\n`;
      out += `========================================================\n\n`;
      history.forEach((h, idx) => {
        const status = h.ok ? '✓ SUCCESS' : '✕ FAILED';
        out += `[${h.timestamp}] ${status} | Workflow: ${h.workflow_key} | Duration: ${h.duration_sec || 0}s\n`;
        if (h.error) {
          out += `  ⚠ ERROR: ${h.error}\n`;
        }
        if (h.uploaded && h.uploaded.length > 0) {
          out += `  → Uploaded: ${h.uploaded.join(', ')}\n`;
        }
        if (h.replaced_earlier && h.replaced_earlier.length > 0) {
          out += `  → Replaced earlier: ${h.replaced_earlier.join(', ')}\n`;
        }
        if (h.pruned_stale && h.pruned_stale.length > 0) {
          out += `  → Pruned stale: ${h.pruned_stale.join(', ')}\n`;
        }
        if (h.logs && h.logs.length > 0) {
          out += `  Details:\n`;
          h.logs.forEach(l => { out += `    ${l}\n`; });
        }
        out += `--------------------------------------------------------\n`;
      });
      term.textContent = out;
    } catch (e) {
      term.textContent = `Failed to fetch logs: ${e.message}`;
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
    let rawHost = document.getElementById('cfgHost').value.trim();
    let port = parseInt(document.getElementById('cfgPort').value, 10) || 8080;

    // Clean scheme and host:port if user entered ws:// or http:// or ip:port
    if (rawHost.includes("://")) {
      rawHost = rawHost.split("://")[1];
    }
    if (rawHost.includes("/")) {
      rawHost = rawHost.split("/")[0];
    }
    if (rawHost.includes(":")) {
      const parts = rawHost.split(":");
      rawHost = parts[0];
      const p = parseInt(parts[1], 10);
      if (!isNaN(p) && p > 0) port = p;
    }

    if (port === 80) {
      showToast("RTS-X WebSocket runs on 8080 (port 80 is HTTP UI only). Automatically corrected to 8080.", "warning");
      port = 8080;
      const portInput = document.getElementById('cfgPort');
      if (portInput) portInput.value = 8080;
    }

    const updated = {
      ...config,
      watched_dir: document.getElementById('cfgWatchedDir').value.trim(),
      controller_host: rawHost,
      controller_port: port,
      stale_ttl_hours: parseFloat(document.getElementById('cfgTtl').value) || 24.0,
      auto_sync_idle_seconds: parseFloat(document.getElementById('cfgAutoSyncIdle').value) || 10.0,
      custom_epilogue: document.getElementById('cfgEpilogue').value,
    };

    // Immediately close modal so UI response is instantaneous
    closeSettings();

    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updated)
      });
      const data = await res.json();
      if (data.ok) {
        config = data.config;
        refreshStaged();
        checkControllerStatus();
      } else {
        alert('Error saving settings: ' + (data.error || 'Server error'));
      }
    } catch (e) {
      alert('Error saving settings: ' + e.message);
    }
  }

  // Mobile tab switcher
  function switchMobileTab(tab) {
    document.querySelectorAll('.mobile-tab-btn').forEach(btn => btn.classList.remove('active'));
    document.getElementById('paneLeft').classList.remove('tab-active');
    document.getElementById('paneRight').classList.remove('tab-active');
    const paneEditor = document.getElementById('paneEditor');
    if (paneEditor) paneEditor.classList.remove('tab-active');

    const tabs = document.querySelectorAll('.mobile-tab-btn');
    if (tab === 'sequence') {
      if (tabs[0]) tabs[0].classList.add('active');
      document.getElementById('paneLeft').classList.add('tab-active');
    } else if (tab === 'viewer') {
      if (tabs[1]) tabs[1].classList.add('active');
      document.getElementById('paneRight').classList.add('tab-active');
      setTimeout(() => GcodeViewer.resizeCanvas(), 50);
    } else if (tab === 'editor') {
      if (tabs[2]) tabs[2].classList.add('active');
      if (paneEditor) paneEditor.classList.add('tab-active');
    } else if (tab === 'controller') {
      if (tabs[3]) tabs[3].classList.add('active');
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
    onStagedSearch,
    clearStagedSearch,
    setStagedTimeFilter,
    setStagedSortOrder,
    addJobToSequence,
    updateSlot,
    moveSlot,
    duplicateSlot,
    removeSlot,
    onStrategyChanged,
    onSinglePartToggle,
    previewJob,
    onSafeZThresholdChange,
    resetSafeZToInferred,
    onScrub,
    step,
    jumpToPoint,
    openRapidSplitTooltip,
    closeRapidTooltip,
    toggleManualOpSplitAtActiveRapid,
    togglePlay,
    setPlaybackSpeed,
    openInsertPauseDialog,
    closeInsertPauseDialog,
    confirmInsertPause,
    cancelAutoSync,
    syncNow,
    switchMobileTab,
    openSlotInEditor,
    applyEditorGcode,
    revertEditorGcode,
    onEditorInput,
    togglePaneCollapse,
    openSyncDiagModal,
    closeSyncDiagModal,
    testControllerConnectionDetailed,
    loadSyncLogs,
    fixPortTo8080,
    selectSlot,
    clearSelectedSlot,
    handleSlotCardClick,
    toggleSlotGcodeView,
    dismissToast,
  };
})();
