// ═══════════════════════════════════════════════════════════════════
// UI — Panel updates, file tree, directory browser
// ═══════════════════════════════════════════════════════════════════

import { playback, agents, vram, log, swarm, graph, project, debugLog, addLogEntry } from './state.js?v=20260418';
import { AGENTS, LABS, TOTAL_VRAM, SYSTEM_RAM_TOTAL, SWARM_MODEL, getManifest, initializeHardwareFromAPI } from './config.js';

let _activeFileTreeRow = null;

export function updateUI() {
  updatePlayButtons();
  updateAgentCards();
  updateVramPanel();
  updateMetrics();
  updateStepDisplay();
  updateSwarmStatus();
  updateLogDisplay();
  updateProjectInfo();
}

function updatePlayButtons() {
  const playBtn = document.getElementById('playBtn');
  const pauseBtn = document.getElementById('pauseBtn');
  const playBtnRight = document.getElementById('playBtnRight');

  if (playBtn) {
    if (playback.isPlaying && !playback.isPaused) {
      playBtn.textContent = '⏸ Pause';
      playBtn.classList.add('active');
      if (playBtnRight) playBtnRight.textContent = '⏸ Pause';
      if (pauseBtn) pauseBtn.disabled = false;
    } else {
      playBtn.textContent = '▶ Play';
      playBtn.classList.remove('active');
      if (playBtnRight) playBtnRight.textContent = '▶ Play';
      if (pauseBtn) pauseBtn.disabled = true;
    }
  }

  const statusEl = document.getElementById('playbackStatus');
  if (statusEl) {
    if (playback.isPlaying && !playback.isPaused) {
      statusEl.textContent = 'Running';
      statusEl.style.color = '#00d4ff';
    } else if (playback.isPaused) {
      statusEl.textContent = 'Paused';
      statusEl.style.color = '#ffd700';
    } else {
      statusEl.textContent = 'Idle';
      statusEl.style.color = '#888';
    }
  }
}

function updateAgentCards() {
  const container = document.getElementById('agentCardList');
  if (!container) return;

  let activeCount = 0;
  AGENTS.forEach(a => {
    const agState = agents[a.id];
    if (!agState) return;
    const card = document.getElementById(`card-${a.id}`);
    if (!card) return;

    const stateEl = document.getElementById(`astate-${a.id}`);
    const locEl = document.getElementById(`aloc-${a.id}`);

    card.classList.remove('working', 'idle');
    // Strictly use per-agent runtimeAvailable (no OR fallback)
    const isRuntimeAvailable = agState.runtimeAvailable === true;
    const isMappedButUnavailable = agState.agent?.muscle && !isRuntimeAvailable;
    
    if (agState.phase === 'unloaded' && !isRuntimeAvailable) {
      card.classList.add('idle');
      // Dim unavailable agents more if they're mapped but missing
      card.style.opacity = isMappedButUnavailable ? '0.2' : '0.35';
    } else {
      const isWorking = agState.phase === 'executing';
      card.classList.add(isWorking ? 'working' : 'idle');
      card.style.opacity = agState.phase === 'unloaded' ? '0.68' : '';
      if (isWorking) activeCount++;
    }

    // ── Status text with task info ──
    if (stateEl) {
      if (agState.phase === 'unloaded') {
        if (isRuntimeAvailable) {
          stateEl.textContent = 'Available';
          stateEl.style.color = '#60A5FA';
        } else if (isMappedButUnavailable) {
          stateEl.textContent = 'Unavailable';
          stateEl.style.color = '#ff6b6b';
        } else {
          stateEl.textContent = 'Unloaded';
          stateEl.style.color = '';
        }
      } else if (agState.phase === 'executing') {
        // Show elapsed time
        const elapsed = agState.taskStartedAt ? Math.round((Date.now() - agState.taskStartedAt) / 1000) : 0;
        const timeStr = elapsed > 0 ? ` (${elapsed}s)` : '';
        stateEl.textContent = `Working${timeStr}`;
        stateEl.style.color = '#00ff88';
      } else if (agState.phase === 'loaded' || agState.phase === 'returning') {
        stateEl.textContent = 'Idle';
        stateEl.style.color = '#ffaa00';
      } else {
        stateEl.textContent = agState.phase;
        stateEl.style.color = '';
      }
    }

    // ── Location: show current task or last completed task ──
    if (locEl) {
      if (agState.phase === 'executing' && agState.currentTask) {
        const taskText = agState.currentTask.length > 35 ? agState.currentTask.substring(0, 32) + '...' : agState.currentTask;
        locEl.textContent = taskText;
        locEl.style.color = '#00d4ff';
      } else if (agState.phase === 'executing' && agState.currentLab) {
        const node = graph.nodes.find(n => n.id === agState.currentLab);
        locEl.textContent = node ? node.label : '—';
        locEl.style.color = '#00d4ff';
      } else if (agState.lastTask && agState.lastTaskStatus) {
        const icon = agState.lastTaskStatus === 'completed' ? '✓' : '✗';
        const taskText = agState.lastTask.length > 30 ? agState.lastTask.substring(0, 27) + '...' : agState.lastTask;
        locEl.textContent = `${icon} ${taskText}`;
        locEl.style.color = agState.lastTaskStatus === 'completed' ? '#69f0ae' : '#ff5252';
      } else {
        locEl.textContent = '—';
        locEl.style.color = '';
      }
    }

    // ── Tooltip with full details ──
    const tooltip = _buildAgentTooltip(agState, a);
    card.title = tooltip;
  });

  const swarmActive = swarm.units.filter(u => u.active).length;
  const agentCountEl = document.getElementById('agentCount');
  if (agentCountEl) agentCountEl.textContent = activeCount + (swarmActive > 0 ? ` + ${swarmActive}` : '');
}

function _buildAgentTooltip(agState, agentCfg) {
  const lines = [`${agentCfg.name} (${agentCfg.muscle || agentCfg.id})`];
  // Status
  if (agState.phase === 'executing') {
    const elapsed = agState.taskStartedAt ? Math.round((Date.now() - agState.taskStartedAt) / 1000) : 0;
    lines.push(`Status: Working (${elapsed}s elapsed)`);
  } else if (agState.phase === 'loaded' || agState.phase === 'returning') {
    lines.push('Status: Idle — waiting for next task');
  } else if (agState.phase === 'unloaded') {
    if (agState.runtimeAvailable) {
      const runtime = agState.runtimeAgentId || 'OpenClaw';
      lines.push(`Status: Available via ${runtime} — standing by`);
    } else if (agState.agent?.muscle) {
      lines.push(`Status: Unavailable — ${agState.agent.muscle} not detected in OpenClaw`);
    } else {
      lines.push('Status: Unloaded — model not in VRAM');
    }
  } else {
    lines.push(`Status: ${agState.phase}`);
  }
  // Current task
  if (agState.currentTask) {
    lines.push(`Current: ${agState.currentTask}`);
  }
  // Current node
  if (agState.currentLab) {
    const node = graph.nodes.find(n => n.id === agState.currentLab);
    if (node) lines.push(`Node: ${node.label || agState.currentLab}`);
  }
  // Last task
  if (agState.lastTask) {
    const icon = agState.lastTaskStatus === 'completed' ? '✓' : '✗';
    lines.push(`Last: ${icon} ${agState.lastTask}`);
  }
  // VRAM
  if (agState.loaded) {
    lines.push(`VRAM: ${agentCfg.vram || '?'}GB loaded`);
  }
  return lines.join('\n');
}

// Poll real VRAM + RAM from backend every 5s (first call is immediate)
let _lastVramPoll = -5000;
async function _pollRealVram() {
  const now = Date.now();
  if (now - _lastVramPoll < 5000) return;
  _lastVramPoll = now;
  try {
    const [vramResp, hwResp] = await Promise.all([
      fetch('/api/vram'),
      fetch('/api/hardware')
    ]);
    if (vramResp.ok) {
      const data = await vramResp.json();
      vram.used = data.vram_used_gb || 0;
      vram.total = data.vram_total_gb || TOTAL_VRAM;
      vram.modelsLoaded = data.models || [];
    }
    if (hwResp.ok) {
      const hw = await hwResp.json();
      vram.ramTotalGb = hw.system_ram_total_gb || 0;
      vram.ramUsedGb = hw.system_ram_used_gb ?? (hw.system_ram_available_gb != null ? hw.system_ram_total_gb - hw.system_ram_available_gb : 0);
    }
  } catch (_) { /* offline fallback: keep last values */ }
}

function updateVramPanel() {
  // Kick off async poll (non-blocking)
  _pollRealVram();

  // ── GPU VRAM Display ──
  const bar = document.getElementById('vramBar');
  if (bar) {
    const total = vram.total || TOTAL_VRAM;
    const pct = total > 0 ? (vram.used / total) * 100 : 0;
    bar.style.width = Math.min(100, pct) + '%';

    if (pct > 95) {
      bar.style.background = 'linear-gradient(90deg,#ff5252,#ff1744)';
    } else if (pct > 75) {
      bar.style.background = 'linear-gradient(90deg,#ffab00,#ff6e40)';
    } else {
      bar.style.background = 'linear-gradient(90deg,#00d4ff,#69f0ae)';
    }

    const textEl = document.getElementById('vramText');
    if (textEl) {
      let label = Math.round(pct) + '%';
      if (vram.modelsLoaded.length > 0) {
        label += ` · ${vram.modelsLoaded.join(', ')}`;
      }
      textEl.textContent = label;
    }

    const usedEl = document.getElementById('vramUsed');
    if (usedEl) usedEl.textContent = vram.used.toFixed(1) + ' GB';

    const totalEl = document.getElementById('vramTotal');
    if (totalEl) totalEl.textContent = total.toFixed(1) + ' GB';
  }

  // ── System RAM Display ──
  const ramBar = document.getElementById('ramBar');
  if (ramBar && vram.ramTotalGb) {
    const ramTotal = vram.ramTotalGb;
    const ramUsed = vram.ramUsedGb || 0;
    const ramPct = (ramUsed / ramTotal) * 100;
    ramBar.style.width = Math.min(100, ramPct) + '%';

    if (ramPct > 95) {
      ramBar.style.background = 'linear-gradient(90deg,#ff5252,#ff1744)';
    } else if (ramPct > 75) {
      ramBar.style.background = 'linear-gradient(90deg,#ffab00,#ff6e40)';
    } else {
      ramBar.style.background = 'linear-gradient(90deg,#60a5fa,#a78bfa)';
    }

    const ramTextEl = document.getElementById('ramText');
    if (ramTextEl) ramTextEl.textContent = Math.round(ramPct) + '%';

    const ramUsedEl = document.getElementById('ramUsed');
    if (ramUsedEl) ramUsedEl.textContent = ramUsed.toFixed(1) + ' GB';

    const ramTotalEl = document.getElementById('ramTotal');
    if (ramTotalEl) ramTotalEl.textContent = ramTotal.toFixed(1) + ' GB';
  }

  const queueEl = document.getElementById('loadingQueue');
  if (queueEl) queueEl.style.display = 'none';
}

function updateMetrics() {
  // Use manifest nodes as source of truth; fall back to LABS if no manifest yet
  const manifest = getManifest();
  const manifestNodes = manifest && manifest.nodes && manifest.nodes.length > 0 ? manifest.nodes : null;
  const total = manifestNodes ? manifestNodes.length : (LABS.length || 1);
  const done  = manifestNodes
    ? manifestNodes.filter(n => n.status === 'complete' || n.status === 'done').length
    : LABS.filter(l => l.status === 'done' || l.status === 'complete').length;
  const pct = (done / total) * 100;

  const workflowBar = document.getElementById('workflowBar');
  if (workflowBar) workflowBar.style.width = pct + '%';

  const workflowPct = document.getElementById('workflowPercent');
  if (workflowPct) workflowPct.textContent = Math.round(pct) + '%';

  const stepBar = document.getElementById('stepBar');
  if (stepBar) stepBar.style.width = (playback.currentStep >= 0 ? 100 : 0) + '%';

  const stepPct = document.getElementById('stepPercent');
  if (stepPct) stepPct.textContent = playback.currentStep >= 0 ? `${playback.currentStep + 1}/${total}` : '—';
}

function updateStepDisplay() {
  const stepNum = document.getElementById('stepNum');
  if (stepNum) stepNum.textContent = playback.currentStep >= 0 ? playback.currentStep + 1 : '—';

  const phaseName = document.getElementById('phaseName');
  if (phaseName) {
    if (playback.currentStep >= 0 && playback.currentStep < LABS.length) {
      phaseName.textContent = LABS[playback.currentStep].label;
    } else {
      phaseName.textContent = '—';
    }
  }
}

function updateLogDisplay() {
  const logArea = document.getElementById('logArea');
  if (!logArea) return;

  logArea.innerHTML = log.entries.slice(-30).map(e => {
    const time = e.time.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    return `<div class="${e.cls}">[${time}] ${e.msg}</div>`;
  }).join('');

  logArea.scrollTop = logArea.scrollHeight;
}

export function updateStepSidebar() {
  document.querySelectorAll('.step-item').forEach((el, i) => {
    el.classList.remove('active', 'completed');
    if (i < playback.currentStep) el.classList.add('completed');
    if (i === playback.currentStep) el.classList.add('active');
  });
}

function updateSwarmStatus() {
  const activeUnits = swarm.units.filter(u => u.active);
  const activeCount = activeUnits.length;
  const isSwarmMode = activeCount >= 5;

  const card = document.getElementById('card-swarm');
  const stateEl = document.getElementById('astate-swarm');
  const locEl = document.getElementById('aloc-swarm');

  if (card) {
    card.classList.remove('working', 'idle');
    if (activeCount > 0) {
      card.classList.add('working');
      card.style.borderColor = isSwarmMode ? SWARM_MODEL.color + '66' : SWARM_MODEL.color + '33';
    } else {
      card.classList.add('idle');
      card.style.borderColor = SWARM_MODEL.color + '22';
    }
  }

  if (stateEl) {
    if (activeCount === 0) {
      stateEl.textContent = 'Standby';
    } else if (isSwarmMode) {
      stateEl.textContent = `SWARM ${activeCount}/${SWARM_MODEL.maxCount}`;
      stateEl.style.color = SWARM_MODEL.color;
    } else {
      stateEl.textContent = `Active ${activeCount}/${SWARM_MODEL.maxCount}`;
      stateEl.style.color = '';
    }
  }

  if (locEl) {
    const targets = [...new Set(activeUnits.map(u => u.targetNode).filter(Boolean))];
    if (targets.length > 0) {
      const names = targets.map(tid => {
        const n = graph.nodes.find(nd => nd.id === tid);
        return n ? n.label : tid;
      });
      locEl.textContent = names.join(', ');
    } else {
      locEl.textContent = '—';
    }
  }
}

function updateProjectInfo() {
  const infoEl = document.getElementById('projectInfo');
  if (!infoEl) return;

  const manifest = getManifest();
  if (!manifest) {
    infoEl.textContent = 'No project loaded';
    return;
  }

  const done = (manifest.nodes || []).filter(n => n.status === 'done' || n.status === 'complete').length;
  const total = (manifest.nodes || []).length;
  const mode = manifest.execution_mode || 'pipeline';
  infoEl.innerHTML = `
    <div style="font-size:11px;color:#888;margin-bottom:4px">${manifest.project_id}</div>
    <div style="font-size:12px;color:#ccc;margin-bottom:6px">${manifest.prompt || ''}</div>
    <div style="font-size:11px;color:#69f0ae">${done}/${total} nodes complete</div>
    <div style="font-size:10px;color:#ffd700;margin-top:2px">Mode: ${mode.toUpperCase()}</div>
    ${manifest.linked_dir ? `<div style="font-size:10px;color:#38BDF8;margin-top:4px">📁 ${manifest.linked_dir}</div>` : ''}
  `;
}

// ── File tree rendering ──────────────────────────────────

export function renderFileTree(container, tree, linkedDir) {
  if (!container || !tree) return;
  container.innerHTML = '';
  
  // Show linked directory path if available
  if (linkedDir) {
    const pathDiv = document.getElementById('linkedDirPath');
    if (pathDiv) {
      pathDiv.textContent = linkedDir;
      pathDiv.style.display = 'block';
    }
  }
  
  _renderTreeLevel(container, tree, 0);
}

function _renderTreeLevel(parent, items, depth) {
  for (const item of items) {
    const row = document.createElement('div');
    row.style.paddingLeft = (depth * 16 + 8) + 'px';
    row.style.fontSize = '11px';
    row.style.padding = '2px 8px 2px ' + (depth * 16 + 8) + 'px';
    row.style.cursor = (item.type === 'file' || item.type === 'dir') ? 'pointer' : 'default';
    row.style.color = item.type === 'dir' ? '#60A5FA' : '#ccc';
    row.style.whiteSpace = 'nowrap';
    row.style.userSelect = 'none';
    row.style.transition = 'background 0.15s, color 0.15s';
    row.style.borderRadius = '2px';

    if (item.type === 'dir') {
      row.textContent = `📁 ${item.name}`;
      row.style.fontWeight = '600';
      row.addEventListener('mouseover', () => {
        row.style.background = 'rgba(96,165,250,0.15)';
        row.style.color = '#87CEEB';
      });
      row.addEventListener('mouseout', () => {
        row.style.background = '';
        row.style.color = '#60A5FA';
      });
      row.addEventListener('dblclick', () => {
        // Open directory
        const path = item.path;
        if (path) {
          fetch('/api/machine/open-file', { 
            method: 'POST', 
            headers: {'Content-Type': 'application/json'}, 
            body: JSON.stringify({path}) 
          }).catch(e => console.log('Cannot open:', e.message));
        }
      });
      parent.appendChild(row);
      if (item.children) {
        const sub = document.createElement('div');
        _renderTreeLevel(sub, item.children, depth + 1);
        parent.appendChild(sub);
      }
    } else if (item.type === 'file') {
      const sizeKb = item.size ? (item.size / 1024).toFixed(1) + 'KB' : '';
      row.textContent = `📄 ${item.name}`;
      if (sizeKb) {
        const sizeSpan = document.createElement('span');
        sizeSpan.style.color = '#666';
        sizeSpan.style.fontSize = '9px';
        sizeSpan.style.marginLeft = '8px';
        sizeSpan.textContent = sizeKb;
        row.appendChild(sizeSpan);
      }
      row.addEventListener('mouseover', () => {
        row.style.background = 'rgba(0,212,255,0.15)';
        row.style.color = '#87CEEB';
      });
      row.addEventListener('mouseout', () => {
        row.style.background = '';
        row.style.color = '#ccc';
      });
      row.addEventListener('click', () => {
        // Mark as selected
        const prevSelected = document.querySelector('.file-tree-selected');
        if (prevSelected) prevSelected.classList.remove('file-tree-selected');
        row.classList.add('file-tree-selected');
        row.style.background = 'rgba(0,212,255,0.25)';
        // In-app file preview — read via linked endpoint
        _showFilePreview(item.path, item.name, item.size || 0);
      });
      row.addEventListener('dblclick', () => {
        // Open file in default editor
        if (item.path) {
          fetch('/api/machine/open-file', { 
            method: 'POST', 
            headers: {'Content-Type': 'application/json'}, 
            body: JSON.stringify({path: item.path}) 
          }).catch(e => console.log('Cannot open file:', e.message));
        }
      });
      parent.appendChild(row);
    }
  }
}

// ── In-app file preview ────────────────────────────────────

function _getFileIcon(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  const icons = { py:'🐍', js:'📜', ts:'📜', html:'🌐', css:'🎨', json:'📋', md:'📝', sh:'⚡', txt:'📄', yaml:'📋', yml:'📋' };
  return icons[ext] || '📄';
}

function _isTextFile(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  return ['py','js','ts','html','css','json','md','sh','txt','yaml','yml','toml','ini','cfg','log','xml','csv'].includes(ext);
}

function _highlightCode(text, filename) {
  // Simple syntax-safe HTML escape
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/(".*?")/g, '<span style="color:#98c379">$1</span>')
    .replace(/(#[^\n]*)/g, '<span style="color:#5c6370;font-style:italic">$1</span>')
    .replace(/\b(def|class|import|from|return|if|else|elif|for|while|try|except|with|as|in|not|and|or|True|False|None)\b/g, '<span style="color:#c678dd">$1</span>')
    .replace(/\b(function|const|let|var|return|if|else|for|while|async|await|export|import)\b/g, '<span style="color:#c678dd">$1</span>');
}

async function _showFilePreview(filePath, fileName, fileSize) {
  // Get or create the preview panel
  let panel = document.getElementById('filePreviewPanel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'filePreviewPanel';
    panel.style.cssText = [
      'position:fixed', 'bottom:0', 'right:0', 'width:480px', 'max-height:60vh',
      'background:rgba(8,12,24,0.97)', 'border:1px solid rgba(0,212,255,0.25)',
      'border-radius:12px 0 0 0', 'z-index:8888', 'display:flex', 'flex-direction:column',
      'font-family:JetBrains Mono,monospace', 'box-shadow:-4px -4px 24px rgba(0,0,0,0.6)',
    ].join(';');
    document.body.appendChild(panel);
  }

  panel.style.display = 'flex';

  // Header
  const sizeLabel = fileSize > 1024 ? (fileSize / 1024).toFixed(1) + ' KB' : fileSize + ' B';
  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;
                border-bottom:1px solid rgba(0,212,255,0.15);flex-shrink:0">
      <span style="font-size:12px;color:#00d4ff">${_getFileIcon(fileName)} ${fileName}</span>
      <span style="display:flex;gap:8px;align-items:center">
        <span style="font-size:10px;color:#666">${sizeLabel}</span>
        <button id="fpOpenBtn" title="Open in OS" style="background:none;border:1px solid #444;
          border-radius:4px;color:#aaa;cursor:pointer;font-size:10px;padding:2px 8px">Open↗</button>
        <button id="fpCloseBtn" style="background:none;border:none;color:#888;cursor:pointer;font-size:16px;line-height:1">✕</button>
      </span>
    </div>
    <div id="fpContent" style="flex:1;overflow:auto;padding:12px;font-size:11px;line-height:1.6;
      color:#ccc;white-space:pre-wrap;word-break:break-word">
      <span style="color:#666">Loading…</span>
    </div>`;

  document.getElementById('fpCloseBtn').onclick = () => { panel.style.display = 'none'; };
  document.getElementById('fpOpenBtn').onclick = () => {
    fetch('/api/machine/open-file', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: filePath}) }).catch(() => {});
  };

  const content = document.getElementById('fpContent');

  // Detect project context from URL
  const urlParams = new URLSearchParams(window.location.search);
  const projectId = urlParams.get('id') || (window.project && window.project.id);

  if (!_isTextFile(fileName)) {
    // Non-text: show open-in-OS prompt
    content.innerHTML = `<div style="color:#888;text-align:center;margin-top:24px">
      <div style="font-size:24px;margin-bottom:8px">${_getFileIcon(fileName)}</div>
      <div style="font-size:12px;color:#555">${fileName}</div>
      <div style="margin-top:12px;font-size:11px;color:#444">Binary or media file — use Open↗ to view in system app</div>
    </div>`;
    return;
  }

  if (!projectId) {
    // No project context — try OS open instead
    content.innerHTML = `<span style="color:#888">No project context — double-click to open in OS app</span>`;
    return;
  }

  try {
    // Read via linked file endpoint
    const res = await fetch(`/api/machine/projects/${projectId}/linked/${encodeURIComponent(filePath)}`);
    if (!res.ok) {
      content.innerHTML = `<span style="color:#e55">Could not read file (${res.status})</span>`;
      return;
    }
    const data = await res.json();
    const text = data.content || '';
    if (!text.trim()) {
      content.innerHTML = '<span style="color:#666">(empty file)</span>';
      return;
    }
    content.innerHTML = _highlightCode(text, fileName);
  } catch (e) {
    content.innerHTML = `<span style="color:#e55">Error: ${e.message}</span>`;
  }
}

// ── Control button handlers ──────────────────────────────────

export function setupControlButtons() {
  const projectId = project.id;
  if (!projectId) return;

  const btnStop = document.getElementById('btnStop');
  const btnStatus = document.getElementById('btnStatus');
  const btnSaveWorkflow = document.getElementById('btnSaveWorkflow');

  if (btnSaveWorkflow) {
    btnSaveWorkflow.addEventListener('click', async () => {
      const name = prompt('Workflow name (leave blank to use the project prompt):');
      if (name === null) return; // cancelled
      const tagsRaw = prompt('Tags (comma-separated, optional):') || '';
      const tags = tagsRaw.split(',').map(t => t.trim()).filter(Boolean);
      try {
        const res = await fetch(`/api/machine/projects/${projectId}/save-workflow`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ name: name || null, tags }),
        });
        const data = await res.json();
        if (data.status === 'saved') {
          addLogEntry(`[WORKFLOW] Saved as workflow: "${data.template.name}"`, 'cyan');
          const toast = document.createElement('div');
          toast.style.cssText = 'position:fixed;bottom:60px;right:24px;z-index:9999;'
            + 'background:rgba(8,12,24,0.95);border:1px solid #76b900;border-radius:8px;'
            + 'padding:12px 18px;font-family:JetBrains Mono,monospace;font-size:12px;color:#76b900;'
            + 'box-shadow:0 4px 20px rgba(0,0,0,0.6);';
          toast.textContent = `✓ Saved as workflow: "${data.template.name}"`;
          document.body.appendChild(toast);
          setTimeout(() => toast.remove(), 4000);
        } else {
          addLogEntry(`[ERROR] Save failed: ${JSON.stringify(data)}`, 'orange');
        }
      } catch (e) {
        addLogEntry(`[ERROR] Save workflow failed: ${e.message}`, 'orange');
      }
    });
  }

  if (btnStop) {
    btnStop.addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/machine/projects/${projectId}/stop`, { method: 'POST' });
        const data = await res.json();
        addLogEntry('[CONTROL] Execution stopped', 'pink');
        updateUI();
      } catch (e) {
        addLogEntry(`[ERROR] Stop failed: ${e.message}`, 'orange');
      }
    });
  }

  if (btnStatus) {
    btnStatus.addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/machine/projects/${projectId}/status`);
        const data = await res.json();
        const nodesSummary = `${data.nodes.done}/${data.nodes.total} done, ${data.nodes.running} running, ${data.nodes.pending} pending`;
        const models = (data.vram.models || []).join(', ') || 'none';
        const vramSummary = `${data.vram.used}/${data.vram.total} GB VRAM (${models})`;
        // Show as visible toast on screen
        const toast = document.createElement('div');
        toast.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;'
          + 'background:rgba(8,12,24,0.95);border:1px solid rgba(0,212,255,0.5);border-radius:8px;'
          + 'padding:12px 20px;font-family:JetBrains Mono,monospace;font-size:12px;color:#e0e0e0;'
          + 'box-shadow:0 4px 20px rgba(0,0,0,0.6);min-width:300px;';
        toast.innerHTML = `<div style="color:#00d4ff;font-weight:bold;margin-bottom:6px">◈ Project Status</div>`
          + `<div>Status: <strong style="color:#69f0ae">${data.status}</strong></div>`
          + `<div>Nodes: ${nodesSummary}</div>`
          + `<div>VRAM: ${vramSummary}</div>`;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 5000);
        addLogEntry(`[STATUS] ${nodesSummary} | ${vramSummary}`, 'cyan');
      } catch (e) {
        addLogEntry(`[ERROR] Status check failed: ${e.message}`, 'orange');
      }
    });
  }
}


// ── Node Inspector ───────────────────────────────────────

export function showNodeInspector(lab, callbacks) {
  const panel = document.getElementById('nodeInspector');
  const content = document.getElementById('nodeInspectorContent');
  if (!panel || !content) return;

  panel.style.display = 'block';

  const statusCls = (lab.status === 'done' || lab.status === 'complete') ? 'complete' : lab.status || 'pending';
  const depsHtml = (lab.depends_on && lab.depends_on.length > 0)
    ? lab.depends_on.map(d => `<span class="ni-dep-tag">${d}</span>`).join('')
    : '<span style="color:#666;font-size:10px">None</span>';

  const resultHtml = lab.result
    ? `<div class="ni-result">${_escapeHtml(lab.result)}</div>`
    : '<div style="color:#666;font-size:10px;margin-top:4px">No output yet</div>';

  // Compute execution duration
  let durationStr = '—';
  if (lab.started_at && lab.completed_at) {
    const ms = new Date(lab.completed_at) - new Date(lab.started_at);
    durationStr = ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  } else if (lab.started_at) {
    durationStr = 'running...';
  }

  content.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-size:16px">${lab.icon}</span>
      <span style="font-size:13px;font-weight:600;color:${lab.color}">${(lab.muscle && lab.muscle !== 'NONE') ? lab.muscle : (lab.action === 'bash' ? 'bash' : '—')}</span>
      <span class="ni-status ${statusCls}">${statusCls}</span>
      ${lab.tier ? `<span style="font-size:9px;color:#888;background:rgba(255,255,255,.05);padding:1px 6px;border-radius:3px">${lab.tier}</span>` : ''}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 8px;margin-bottom:8px;font-size:10px">
      <div><span style="color:#888">Model:</span> <span style="color:#60A5FA">${_escapeHtml(lab.model_used || '—')}</span></div>
      <div><span style="color:#888">Tokens:</span> <span style="color:#A78BFA">${lab.tokens_used || '—'}</span></div>
      <div><span style="color:#888">Duration:</span> <span style="color:#34D399">${durationStr}</span></div>
      <div><span style="color:#888">Tier:</span> <span style="color:#F59E0B">${lab.tier || '—'}</span></div>
    </div>
    <div class="ni-label">Task</div>
    <div class="ni-value">${_escapeHtml(lab.task || lab.label)}</div>
    <div class="ni-label">Dependencies</div>
    <div style="margin-bottom:4px">${depsHtml}</div>
    <div class="ni-label">Task ID</div>
    <div class="ni-value" style="color:#666">${lab.taskId || '—'}</div>
    <div class="ni-label">Result</div>
    ${resultHtml}
    <div class="ni-actions" id="niActions"></div>
  `;

  // Build action buttons based on status
  const actions = document.getElementById('niActions');
  if (!actions) return;

  if (statusCls === 'pending' || statusCls === 'error') {
    const runBtn = document.createElement('button');
    runBtn.className = 'btn btn-secondary';
    runBtn.textContent = '▶ Run';
    runBtn.addEventListener('click', () => callbacks.onRunSingle(lab.nodeId));
    actions.appendChild(runBtn);
  }

  if (statusCls === 'complete' || statusCls === 'error') {
    const resetBtn = document.createElement('button');
    resetBtn.className = 'btn btn-secondary';
    resetBtn.textContent = '↺ Reset';
    resetBtn.addEventListener('click', () => callbacks.onReset(lab.nodeId));
    actions.appendChild(resetBtn);
  }

  if (statusCls === 'pending' || statusCls === 'error') {
    const editBtn = document.createElement('button');
    editBtn.className = 'btn btn-secondary';
    editBtn.textContent = '✏ Edit';
    editBtn.addEventListener('click', () => {
      const newTask = prompt('Edit task:', lab.task || lab.label);
      if (newTask && newTask.trim()) {
        callbacks.onEdit(lab.nodeId, { task: newTask.trim() });
      }
    });
    actions.appendChild(editBtn);

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-secondary';
    delBtn.style.color = '#ff5252';
    delBtn.textContent = '🗑';
    delBtn.addEventListener('click', () => {
      if (confirm(`Remove node "${lab.label}"?`)) {
        callbacks.onRemove(lab.nodeId);
        panel.style.display = 'none';
      }
    });
    actions.appendChild(delBtn);
  }

  // Always show Reveal in Explorer button
  const revealBtn = document.createElement('button');
  revealBtn.className = 'btn btn-secondary';
  revealBtn.textContent = '📂 Reveal';
  revealBtn.addEventListener('click', () => {
    if (callbacks.onReveal) callbacks.onReveal(lab.nodeId);
  });
  actions.appendChild(revealBtn);
}

function _escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── Debug log download ───────────────────────────────────

export function downloadDebugLog() {
  const snapshot = {
    timestamp: new Date().toISOString(),
    projectId: project.id,
    agents: Object.fromEntries(
      Object.entries(agents).map(([id, a]) => [id, {
        name: a.agent?.name,
        phase: a.phase,
        loaded: a.loaded,
        x: Math.round(a.x),
        y: Math.round(a.y),
        travelProgress: a.travelProgress,
        currentLab: a.currentLab,
      }])
    ),
    vram: { ...vram },
    labs: LABS.map(l => ({ id: l.id, status: l.status, muscle: l.muscle })),
    events: debugLog.events,
    logEntries: log.entries.slice(-100),
  };
  const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `machine-debug-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);
}
