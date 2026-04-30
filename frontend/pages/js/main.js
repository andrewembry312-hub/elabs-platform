// ═══════════════════════════════════════════════════════════════════
// MAIN — Entry point: fetch manifest, initialize, render loop
// ═══════════════════════════════════════════════════════════════════

import { initCanvas, resizeCanvas, renderGrid, canvas, setCoreClickHandler, setCoreDblClickHandler } from './canvas.js';
import { initNodes, drawNodes, updateIframePositions, setNodeCompleted, setNodeActive } from './nodes.js';
import { updateAgents, drawAgents, drawActiveAgentHUD, drawChatBubbles, drawTaskNodes, forceLoadAgent, sendAgentToNode } from './agents.js';
import { resetExec, connectFileEvents, executeDAG, addNodeAPI, resetNodeAPI, editNodeAPI, removeNodeAPI, runSingleNodeAPI, reloadManifest } from './workflow.js';
import { drawHolographicBrain } from './brain.js';
import { playback, addLogEntry, viewport, graph, agents, pipeline, swarm, project, initAgentStates, chatBubbles } from './state.js?v=20260418';
import { updateUI, renderFileTree, setupControlButtons, showNodeInspector } from './ui.js';
import { LABS, AGENTS, SWARM_MODEL, loadFromManifest, getManifest, initializeHardwareFromAPI } from './config.js';
import { initOpenClawBridge, registerNodeSession, detectModels, detectAgents, detectHealth } from './openclaw-visual-bridge.mjs?v=20260427b';

const API_BASE = '/api/machine';
let lastTime = 0;
let elapsed = 0;
let isInitialized = false;

// ── Get project ID from URL ──────────────────────────────

function getProjectId() {
  const params = new URLSearchParams(window.location.search);
  return params.get('project_id') || params.get('id');
}

// ── Fetch manifest from API ──────────────────────────────

async function fetchManifest(projectId) {
  const resp = await fetch(`${API_BASE}/projects/${projectId}`);
  if (!resp.ok) throw new Error(`Project not found: ${projectId}`);
  return resp.json();
}

// ── Fetch directory tree ─────────────────────────────────

async function fetchFileTree(projectId) {
  try {
    const resp = await fetch(`${API_BASE}/projects/${projectId}/tree`);
    if (!resp.ok) return null;
    return resp.json();
  } catch { return null; }
}

// ── Initialization ───────────────────────────────────────

async function init() {
  try {
    console.log('🚀 Initializing THE MACHINE...');

    const projectId = getProjectId();
    if (!projectId) {
      document.getElementById('loadingOverlay').innerHTML = '<div style="color:#ff5252;font-size:18px">No project ID in URL. Add ?id=PROJECT_ID</div>';
      return;
    }

    // Show loading
    const overlay = document.getElementById('loadingOverlay');
    if (overlay) overlay.innerHTML = '<div style="color:#00d4ff;font-size:14px">Loading project manifest...</div>';

    // Fetch manifest
    const manifest = await fetchManifest(projectId);
    console.log('✓ Manifest loaded:', manifest.project_id, `(${manifest.nodes?.length} nodes)`);

    // Store project metadata
    project.id = manifest.project_id;
    project.prompt = manifest.prompt;
    project.linked_dir = manifest.linked_dir;
    project.status = manifest.status;

    // Drive config from manifest
    if (!manifest || !manifest.nodes) {
      throw new Error('Invalid manifest: missing nodes');
    }
    loadFromManifest(manifest);
    initAgentStates();
    console.log('✓ Config loaded:', LABS.length, 'nodes,', AGENTS.length, 'agents');

    // Initialize hardware detection from API
    await initializeHardwareFromAPI();

    // Agents are NOT pre-loaded — they appear only when their model is onloaded to VRAM.
    // The poll loop (workflow.js _pollUntilDone) calls forceLoadAgent + sendAgentToNode
    // when a node transitions to 'running'.
    console.log('✓ Agents configured:', Object.keys(agents).length, '(will appear when models load)');

    // Canvas setup
    const bgCanvas = document.getElementById('bgCanvas');
    const fgCanvas = document.getElementById('fgCanvas');
    if (!bgCanvas || !fgCanvas) {
      console.error('❌ Canvas elements not found');
      return;
    }

    initCanvas(bgCanvas, fgCanvas);
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);

    // Load nodes
    await initNodes();
    console.log('✓ Nodes initialized');

    // Build sidebar and agent cards
    if (LABS && LABS.length > 0) {
      buildStepSidebar();
    }
    if (AGENTS && AGENTS.length > 0) {
      buildAgentCards();
    }

    // Setup controls
    setupControls();

    // Setup core click → chat, settings modal, help button, project buttons
    setupCoreChat();
    setupSettingsModal();
    setupHelpButton();
    setupProjectButtons();

    // Init agent selection checkboxes
    _initAgentSelection();


    // Init UI
    updateUI();
    addLogEntry(`Project loaded: ${manifest.prompt?.substring(0, 60)}...`, 'log-green');
    addLogEntry(`${LABS.length} nodes · ${AGENTS.length} agents · Ready`, 'log-cyan');

    // Load file tree — linked dir or workspace
    if (manifest.linked_dir) {
      addLogEntry(`📁 Linked: ${manifest.linked_dir}`, 'log-cyan');
    }
    const treeData = await fetchFileTree(projectId);
    if (treeData) {
      project.fileTree = treeData.tree;
      const treeContainer = document.getElementById('fileTree');
      renderFileTree(treeContainer, treeData.tree, treeData.linked_dir || manifest.linked_dir);
    }

    // Always connect SSE for live file sub-node spawning
    connectFileEvents();

    // OpenClaw visual bridge — wire event subscriptions (auto-connects to gateway)
    if (window.OPENCLAW_ENABLED !== false) {
      if (window.OPENCLAW_CONFIG_READY && typeof window.OPENCLAW_CONFIG_READY.then === 'function') {
        await window.OPENCLAW_CONFIG_READY;
      }
      initOpenClawBridge(AGENTS.map(a => a.id));

      // Populate agent controls even when gateway auth is missing by using detectAgents fallback.
      const initialAgents = await detectAgents().catch(() => []);
      const initialDefaultAgentId =
        (window.OC_DEFAULT_AGENT_ID || (initialAgents && initialAgents[0] && initialAgents[0].id) || 'main');
      LABS.forEach(lab => registerNodeSession(lab.id, project.id, lab.nodeId, initialDefaultAgentId));

      // When gateway connects, refresh models/agents/health with live data.
      const { subscribe: ocSubscribe } = await import('./openclaw-client.mjs?v=20260427b');
      ocSubscribe('_client_connected', async () => {
        detectModels().catch(() => {});
        const ocAgents = await detectAgents().catch(() => []);

        const defaultAgentId =
          (window.OC_DEFAULT_AGENT_ID || (ocAgents && ocAgents[0] && ocAgents[0].id) || 'main');

        // Refresh per-node OpenClaw session keys with discovered/default agent id.
        LABS.forEach(lab => registerNodeSession(lab.id, project.id, lab.nodeId, defaultAgentId));

        detectHealth().catch(() => {});
        window.OC_CONNECTED = true;
        if (typeof window._ocUpdateDagButton === 'function') window._ocUpdateDagButton();
      });
    }

    // Auto-resume visual tracking if project is actively executing
    if (manifest.status === 'swarming' || manifest.status === 'running') {
      console.log('[main] auto-resume: manifest.status =', manifest.status);
      addLogEntry('⚡ Detected active execution — resuming visual tracking', 'log-cyan');
      const wf = await import('./workflow.js?v=20260418');
      console.log('[main] workflow module keys:', Object.keys(wf));
      if (wf._autoResumePolling) {
        console.log('[main] calling _autoResumePolling');
        wf._autoResumePolling();
      } else {
        console.error('[main] _autoResumePolling NOT found in workflow module!');
      }
    } else if (manifest.status === 'ready' || manifest.status === 'pending') {
      // Project not yet executing — start background poller to detect external swarm launch
      console.log('[main] project ready — starting background execution detector');
      _startExecutionDetector(project.id);
    } else if (manifest.status === 'complete') {
      // Project already finished — place agents at completed nodes then let existing
      // poll loop fire the real completion chat bubbles via _autoResumePolling
      console.log('[main] project complete — loading agents then resuming poll for visuals');
      LABS.forEach((lab) => {
        const agentId = `agent-${lab.nodeId}`;
        const gNode = graph.nodes.find(n => n.id === lab.id);
        if (agents[agentId] && gNode) {
          forceLoadAgent(agentId);
          sendAgentToNode(agentId, lab.id);
          // Teleport agent to node position (skip travel animation)
          agents[agentId].x = gNode.x;
          agents[agentId].y = gNode.y;
          agents[agentId].travelProgress = 1;
          // Do NOT set lab.status = 'done' here — let _pollUntilDone see them as new-complete
          // so it fires the real addChatBubble / spawnTaskNode / etc
        }
        // Mark all nodes complete visually
        setNodeCompleted(lab.id, true);
      });
      addLogEntry(`✅ Project complete (${manifest.completed_nodes}/${manifest.total_nodes} nodes)`, 'log-green');
      // Re-use existing auto-resume path which polls once, fires completion bubbles, then stops
      const wf = await import('./workflow.js?v=20260418');
      if (wf._autoResumePolling) wf._autoResumePolling();
    }

    // Hide loading overlay
    if (overlay) overlay.style.display = 'none';

    isInitialized = true;
    console.log('✅ THE MACHINE ready!');
    requestAnimationFrame(frame);
  } catch (err) {
    console.error('❌ Initialization error:', err);
    const overlay = document.getElementById('loadingOverlay');
    if (overlay) overlay.innerHTML = `<div style="color:#ff5252;font-size:14px">Error: ${err.message}</div>`;
  }
}

// ─── Background execution detector ─────────────────────
// Polls project status every 2s to detect externally-launched swarm/DAG
let _detectorInterval = null;
function _startExecutionDetector(projectId) {
  if (_detectorInterval) return;
  const API = `${window.location.origin}/api/machine/projects/${projectId}`;
  _detectorInterval = setInterval(async () => {
    try {
      const resp = await fetch(API);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.status === 'swarming' || data.status === 'running') {
        clearInterval(_detectorInterval);
        _detectorInterval = null;
        console.log('[executionDetector] detected external execution start:', data.status);
        addLogEntry('⚡ Execution detected — starting visual tracking', 'log-cyan');
        // Reload manifest to get updated node statuses
        const { loadFromManifest } = await import('./config.js');
        loadFromManifest(data);
        const wf = await import('./workflow.js?v=20260418');
        if (wf._autoResumePolling) wf._autoResumePolling();
      }
    } catch (e) { /* ignore polling errors */ }
  }, 2000);
}

function setupControls() {
  const resetBtn = document.getElementById('resetBtn');
  const dagBtn = document.getElementById('dagBtn');

  if (resetBtn) resetBtn.addEventListener('click', resetExec);
  if (dagBtn) dagBtn.addEventListener('click', executeDAG);

  // Show execution mode from manifest
  const manifest = getManifest();
  const execModeEl = document.getElementById('execMode');
  if (execModeEl && manifest) {
    execModeEl.textContent = `Mode: ${(manifest.execution_mode || 'pipeline').toUpperCase()}`;
  }

  // Add Node button
  const btnAddNode = document.getElementById('btnAddNode');
  if (btnAddNode) {
    btnAddNode.addEventListener('click', async () => {
      const taskInput = document.getElementById('addNodeTask');
      const muscleSelect = document.getElementById('addNodeMuscle');
      const task = taskInput?.value?.trim();
      if (!task) return;

      const manifestNow = getManifest();
      if (manifestNow && ['running', 'swarming'].includes(String(manifestNow.status || '').toLowerCase())) {
        addLogEntry('⚠ Add Node blocked while execution is active. Stop execution first.', 'log-yellow');
        return;
      }

      // Prefer dropdown selection; fall back to first checked agent checkbox
      let muscle = muscleSelect?.value;
      if (!muscle || muscle === 'AUTO') {
        const sel = window.selectedAgents ? window.selectedAgents() : [];
        muscle = sel.length > 0 ? sel[0] : 'NEMOTRON';
      }
      const result = await addNodeAPI(task, muscle);
      if (result && !result.__error) {
        taskInput.value = '';
        addLogEntry(`+ Added node: ${task.substring(0, 40)}`, 'log-green');
        // Reload manifest and rebuild UI
        await refreshProject();
      } else {
        const code = result?.status ? `HTTP ${result.status}` : 'unknown error';
        const detail = String(result?.detail || '').trim();
        addLogEntry(`✗ Add node failed (${code})${detail ? `: ${detail.slice(0, 120)}` : ''}`, 'log-red');
      }
    });
  }

  // Control buttons (Stop, Status)
  setupControlButtons();

  // Set up node click handlers for inspector
  setupNodeClickHandlers();

  // Set up context menu actions
  setupContextMenu();

  // Build execution summary
  updateExecSummary();
}

async function refreshProject() {
  const manifest = await reloadManifest();
  if (!manifest) return;
  loadFromManifest(manifest);
  initAgentStates();
  buildStepSidebar();
  buildAgentCards();
  await initNodes();
  updateUI();
  updateExecSummary();
}

function setupNodeClickHandlers() {
  // Add click handlers to sidebar step items
  const list = document.getElementById('stepSidebar');
  if (!list) return;
  list.querySelectorAll('.step-item').forEach((el, i) => {
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => {
      if (i < LABS.length) {
        showNodeInspector(LABS[i], {
          onReset: async (nodeId) => {
            const r = await resetNodeAPI(nodeId);
            if (r) { addLogEntry(`↺ Node ${nodeId} reset`, 'log-gold'); await refreshProject(); }
          },
          onRunSingle: async (nodeId) => {
            addLogEntry(`→ Running node ${nodeId}...`, 'log-cyan');
            const r = await runSingleNodeAPI(nodeId);
            if (r) { addLogEntry(`✓ Node ${nodeId} done`, 'log-green'); await refreshProject(); }
          },
          onEdit: async (nodeId, updates) => {
            const r = await editNodeAPI(nodeId, updates);
            if (r) { addLogEntry(`✏ Node ${nodeId} updated`, 'log-gold'); await refreshProject(); }
          },
          onRemove: async (nodeId) => {
            if (await removeNodeAPI(nodeId)) { addLogEntry(`🗑 Node ${nodeId} removed`, 'log-pink'); await refreshProject(); }
          },
          onReveal: (nodeId) => {
            const manifest = getManifest();
            const workspace = manifest?.workspace_dir;
            if (!workspace) { addLogEntry('No workspace directory', 'log-orange'); return; }
            fetch('/api/machine/open-file', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ path: `${workspace}/node_${nodeId}` }),
            }).then(r => {
              if (r.ok) addLogEntry(`📂 Opened: node_${nodeId}`, 'log-cyan');
              else addLogEntry('Could not open folder', 'log-orange');
            });
          },
        });
      }
    });
  });
}

function setupContextMenu() {
  const menu = document.getElementById('nodeContextMenu');
  if (!menu) return;

  menu.addEventListener('click', async (e) => {
    const item = e.target.closest('.ctx-item');
    if (!item) return;
    const action = item.dataset.action;
    const node = menu._node;
    menu.style.display = 'none';
    if (!node) return;

    // Find the matching LAB for this canvas node
    const lab = LABS.find(l => l.nodeId === node.nodeId || l.id === node.id);

    switch (action) {
      case 'inspect':
        if (lab) {
          // Click the sidebar item to trigger inspector
          const el = document.getElementById(`step-${LABS.indexOf(lab)}`);
          if (el) el.click();
        }
        break;

      case 'reveal': {
        // Reveal the node's output folder in file explorer
        const manifest = getManifest();
        const workspace = manifest?.workspace_dir;
        if (!workspace) { addLogEntry('No workspace directory set', 'log-orange'); break; }
        const nodePath = `${workspace}/node_${node.nodeId || lab?.nodeId}`;
        fetch('/api/machine/open-file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: nodePath }),
        }).then(r => {
          if (r.ok) addLogEntry(`📂 Opened: node_${node.nodeId || lab?.nodeId}`, 'log-cyan');
          else addLogEntry('Could not open folder', 'log-orange');
        });
        break;
      }

      case 'copy-result':
        if (lab?.result) {
          navigator.clipboard.writeText(lab.result).then(() => {
            addLogEntry('📋 Result copied to clipboard', 'log-green');
          });
        } else {
          addLogEntry('No result to copy', 'log-orange');
        }
        break;

      case 'run':
        if (lab) {
          addLogEntry(`→ Running node ${lab.nodeId}...`, 'log-cyan');
          const r = await runSingleNodeAPI(lab.nodeId);
          if (r) { addLogEntry(`✓ Node ${lab.nodeId} done`, 'log-green'); await refreshProject(); }
        }
        break;

      case 'reset':
        if (lab) {
          const r = await resetNodeAPI(lab.nodeId);
          if (r) { addLogEntry(`↺ Node ${lab.nodeId} reset`, 'log-gold'); await refreshProject(); }
        }
        break;
    }
  });
}

function updateExecSummary() {
  const section = document.getElementById('execSummarySection');
  const container = document.getElementById('execSummary');
  if (!section || !container) return;

  const completed = LABS.filter(l => l.status === 'done' || l.status === 'complete');
  if (completed.length === 0) { section.style.display = 'none'; return; }

  section.style.display = 'block';
  const manifest = getManifest();
  const mode = (manifest?.execution_mode || 'pipeline').toUpperCase();
  const totalTokens = LABS.reduce((sum, l) => sum + (l.tokens_used || 0), 0);
  const models = [...new Set(LABS.map(l => l.model_used).filter(Boolean))];

  // Compute total time
  const starts = LABS.map(l => l.started_at).filter(Boolean).map(t => new Date(t));
  const ends = LABS.map(l => l.completed_at).filter(Boolean).map(t => new Date(t));
  let totalTime = '—';
  if (starts.length > 0 && ends.length > 0) {
    const earliest = new Date(Math.min(...starts));
    const latest = new Date(Math.max(...ends));
    const ms = latest - earliest;
    totalTime = ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  container.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;margin-bottom:6px">
      <div><span style="color:#888">Mode:</span> <span style="color:#76b900;font-weight:600">${mode}</span></div>
      <div><span style="color:#888">Nodes:</span> <span style="color:#e0e0e0">${completed.length}/${LABS.length}</span></div>
      <div><span style="color:#888">Total tokens:</span> <span style="color:#A78BFA">${totalTokens.toLocaleString()}</span></div>
      <div><span style="color:#888">Wall time:</span> <span style="color:#34D399">${totalTime}</span></div>
      <div style="grid-column:1/-1"><span style="color:#888">Models:</span> <span style="color:#60A5FA">${models.join(', ') || '—'}</span></div>
    </div>
    <div style="border-top:1px solid rgba(255,255,255,0.08);padding-top:4px;margin-top:2px">
      ${LABS.map(l => {
        const st = l.status === 'done' || l.status === 'complete' ? '✓' : l.status === 'running' ? '⚡' : '○';
        const stColor = st === '✓' ? '#69f0ae' : st === '⚡' ? '#00d4ff' : '#666';
        let dur = '—';
        if (l.started_at && l.completed_at) {
          const ms = new Date(l.completed_at) - new Date(l.started_at);
          dur = ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(1) + 's';
        }
        return `<div style="display:flex;justify-content:space-between;padding:1px 0;color:#aaa">
          <span><span style="color:${stColor}">${st}</span> ${(l.task || l.label || '').substring(0, 30)}</span>
          <span style="color:#888">${l.model_used || '—'} · ${l.tokens_used || 0}tk · ${dur}</span>
        </div>`;
      }).join('')}
    </div>
  `;
}

function buildStepSidebar() {
  const list = document.getElementById('stepSidebar');
  if (!list) return;

  list.innerHTML = '';
  LABS.forEach((lab, i) => {
    const el = document.createElement('div');
    el.className = 'step-item';
    el.id = `step-${i}`;

    const statusIcon = lab.status === 'done' ? '✓' : lab.status === 'running' ? '⚡' : '○';
    const statusColor = lab.status === 'done' ? '#69f0ae' : lab.status === 'running' ? '#00d4ff' : '#666';
    
    // For root nodes: show PURPOSE (task) as the main label
    const mainLabel = lab.node_type === 'root' && lab.task ? lab.task : lab.label;
    const purposeText = mainLabel.length > 50 ? mainLabel.substring(0, 47) + '...' : mainLabel;

    el.innerHTML = `
      <div class="step-dot" style="background:${lab.color}"></div>
      <div>
        <div class="step-label" title="${purposeText}">${lab.icon} ${purposeText}</div>
        <div class="step-sub" style="font-size:10px;color:${statusColor}">${statusIcon} Node ${i + 1}</div>
        <div class="step-sub" style="font-size:9px;color:#888">📁 Data Store</div>
      </div>
    `;
    list.appendChild(el);
  });
}

function buildAgentCards() {
  const container = document.getElementById('agentCardList');
  if (!container) return;

  container.innerHTML = AGENTS.map(a => `
    <div class="agent-card idle" id="card-${a.id}">
      <div class="agent-avatar" style="background:${a.color}">${a.emoji}</div>
      <div class="agent-info">
        <div class="agent-card-name">${a.name} <span style="font-size:9px;color:#666;font-weight:400">${a.vram}GB</span></div>
        <div class="agent-card-state" id="astate-${a.id}">Idle</div>
        <div class="agent-card-loc" id="aloc-${a.id}">—</div>
      </div>
    </div>
  `).join('');

  // Swarm card
  container.innerHTML += `
    <div class="agent-card idle" id="card-swarm" style="border-color:${SWARM_MODEL.color}22">
      <div class="agent-avatar" style="background:${SWARM_MODEL.color}">${SWARM_MODEL.emoji}</div>
      <div class="agent-info">
        <div class="agent-card-name">${SWARM_MODEL.name} <span style="font-size:9px;color:#666;font-weight:400">${SWARM_MODEL.vram}GB ea</span></div>
        <div class="agent-card-state" id="astate-swarm">Standby</div>
        <div class="agent-card-loc" id="aloc-swarm">—</div>
      </div>
    </div>
  `;
}

function _initAgentSelection() {
  const container = document.getElementById('agentSelectionCheckboxes');
  if (!container) return;

  const fallback = [];
  const seen = new Set();
  (AGENTS || []).forEach((agent) => {
    const id = String(agent?.model || agent?.id || agent?.name || '').trim().toLowerCase();
    if (!id || seen.has(id)) return;
    seen.add(id);
    fallback.push({ id, name: String(agent?.name || id) });
  });
  if (fallback.length === 0) fallback.push({ id: 'main', name: 'Main (fallback)' });

  const addNodeSelect = document.getElementById('addNodeMuscle');
  if (addNodeSelect) {
    addNodeSelect.innerHTML = fallback
      .map((agent, idx) => `<option value="${agent.id}"${idx === 0 ? ' selected' : ''}>${agent.name} (${agent.id})</option>`)
      .join('');
  }

  container.innerHTML = '';
  fallback.forEach((agent) => {
    const label = document.createElement('label');
    label.style.display = 'flex';
    label.style.alignItems = 'center';
    label.style.gap = '6px';
    label.style.fontSize = '10px';
    label.style.cursor = 'pointer';
    label.style.padding = '2px 4px';
    label.style.borderRadius = '3px';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.value = agent.id;
    checkbox.checked = true;
    checkbox.style.cursor = 'pointer';

    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(`${agent.name} (${agent.id})`));
    container.appendChild(label);
  });

  const hint = document.createElement('div');
  hint.style.fontSize = '10px';
  hint.style.color = '#888';
  hint.style.marginTop = '4px';
  hint.textContent = 'Using local fallback agents until OpenClaw discovery completes.';
  container.appendChild(hint);

  window.selectedAgents = () => {
    const checkboxes = container.querySelectorAll('input[type="checkbox"]');
    return Array.from(checkboxes)
      .filter(cb => cb.checked)
      .map(cb => cb.value);
  };
}

// ── Core Chat (draggable, resizable, with agent/terminal stream) ─────

function setupCoreChat() {
  const modal = document.getElementById('coreChatModal');
  const closeBtn = document.getElementById('chatCloseBtn');
  const minBtn = document.getElementById('chatMinBtn');
  const dragHandle = document.getElementById('chatDragHandle');
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSendBtn');
  const messages = document.getElementById('chatMessages');
  const typing = document.getElementById('chatTyping');
  const streamMessages = document.getElementById('streamMessages');
  if (!modal) return;

  let chatHistory = [];
  const projectId = getProjectId();
  let _savedHeight = null;

  function openChat() { modal.classList.add('open'); input?.focus(); }
  function closeChat() { modal.classList.remove('open'); }
  function minimizeChat() {
    if (_savedHeight) {
      modal.style.height = _savedHeight;
      _savedHeight = null;
    } else {
      _savedHeight = modal.style.height || modal.offsetHeight + 'px';
      modal.style.height = '42px';
    }
  }

  // ── Drag handling ──
  let isDragging = false, dragOffX = 0, dragOffY = 0;
  dragHandle?.addEventListener('mousedown', (e) => {
    if (e.target.tagName === 'BUTTON') return;
    isDragging = true;
    dragOffX = e.clientX - modal.offsetLeft;
    dragOffY = e.clientY - modal.offsetTop;
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    let x = e.clientX - dragOffX;
    let y = e.clientY - dragOffY;
    // Clamp to viewport
    x = Math.max(0, Math.min(x, window.innerWidth - 100));
    y = Math.max(0, Math.min(y, window.innerHeight - 40));
    modal.style.left = x + 'px';
    modal.style.top = y + 'px';
    modal.style.right = 'auto';
  });
  document.addEventListener('mouseup', () => { isDragging = false; });

  // ── Tab switching ──
  modal.querySelectorAll('.chat-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      modal.querySelectorAll('.chat-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === target));
      modal.querySelectorAll('.chat-tab-content').forEach(c => c.classList.toggle('active', c.dataset.tab === target));
    });
  });

  // Double-click core → open chat, single-click → pulse/highlight
  setCoreDblClickHandler(openChat);
  setCoreClickHandler(() => {
    addLogEntry('🧠 Double-click the core to open Project Chat', 'log-cyan');
  });

  closeBtn?.addEventListener('click', closeChat);
  minBtn?.addEventListener('click', minimizeChat);
  // Click outside does NOT close — it's a floating panel now

  function appendMsg(role, text, container) {
    const target = container || messages;
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    if (role === 'assistant') {
      div.innerHTML = renderMarkdown(text);
    } else {
      div.textContent = text;
    }
    target.appendChild(div);
    target.scrollTop = target.scrollHeight;
  }

  // ── Agent/terminal stream ──
  function appendStreamMsg(type, label, text) {
    if (!streamMessages) return;
    const div = document.createElement('div');
    div.className = `chat-msg ${type}`;
    const labelDiv = document.createElement('div');
    labelDiv.className = type === 'terminal' ? 'term-label' : 'agent-label';
    labelDiv.textContent = label;
    div.appendChild(labelDiv);
    const bodySpan = document.createElement('span');
    bodySpan.textContent = text;
    div.appendChild(bodySpan);
    streamMessages.appendChild(div);
    // Keep max 200 messages
    while (streamMessages.children.length > 200) {
      streamMessages.removeChild(streamMessages.firstChild);
    }
    streamMessages.scrollTop = streamMessages.scrollHeight;
  }

  // Expose stream append globally for SSE handlers
  window._appendStreamMsg = appendStreamMsg;

  function renderMarkdown(text) {
    let html = text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre style="background:rgba(255,255,255,0.05);padding:8px;border-radius:4px;overflow-x:auto;font-size:11px"><code>$2</code></pre>')
      .replace(/`([^`]+)`/g, '<code style="background:rgba(255,255,255,0.08);padding:1px 4px;border-radius:3px;font-size:11px">$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/\n/g, '<br>');
    return html;
  }

  async function sendMessage() {
    const text = input?.value?.trim();
    if (!text) return;
    input.value = '';

    appendMsg('user', text);
    chatHistory.push({ role: 'user', content: text });

    sendBtn.disabled = true;
    typing.textContent = '⏳ Thinking...';

    // Abort after 60 seconds to prevent infinite hang
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 60000);

    try {
      const resp = await fetch(`/api/machine/projects/${projectId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          history: chatHistory.slice(-20),
          confirmed: false,
        }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!resp.ok) throw new Error(`API error: ${resp.status}`);
      const data = await resp.json();

      // ── New project requires confirmation ──
      if (data.needs_confirmation) {
        appendMsg('assistant', data.response || 'Create a new project?');
        // Inject confirm/cancel buttons into the chat
        const confirmBar = document.createElement('div');
        confirmBar.className = 'chat-confirm-bar';
        confirmBar.innerHTML = `
          <button class="confirm-yes-btn">✅ Yes, create new project</button>
          <button class="confirm-no-btn">❌ Cancel</button>`;
        const chatLog = document.getElementById('chatMessages');
        chatLog.appendChild(confirmBar);
        chatLog.scrollTop = chatLog.scrollHeight;

        confirmBar.querySelector('.confirm-yes-btn').addEventListener('click', async () => {
          confirmBar.remove();
          appendMsg('user', '[Confirmed: create new project]');
          typing.textContent = '⏳ Creating project...';
          sendBtn.disabled = true;
          try {
            const r2 = await fetch(`/api/machine/projects/${projectId}/chat`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ message: text, history: chatHistory.slice(-20), confirmed: true }),
            });
            const d2 = await r2.json();
            if (d2.new_project_id) {
              appendMsg('assistant', `✅ New workflow created! Redirecting...`);
              addLogEntry(`🚀 New project: ${d2.new_project_id}`, 'log-green');
              setTimeout(() => {
                window.location.href = `/pages/the-machine-project-v2.html?project_id=${d2.new_project_id}`;
              }, 800);
            } else {
              appendMsg('assistant', d2.response || 'Project created.');
            }
          } catch (e) {
            appendMsg('system', `Error creating project: ${e.message}`);
          } finally {
            sendBtn.disabled = false;
            typing.textContent = '';
          }
        });
        confirmBar.querySelector('.confirm-no-btn').addEventListener('click', () => {
          confirmBar.remove();
          appendMsg('system', 'Cancelled — staying in current project.');
          typing.textContent = '';
          sendBtn.disabled = false;
        });
        return;
      }

      // ── New workflow created → redirect to it ──
      if (data.new_project_id) {
        appendMsg('assistant', `✅ New workflow created! Redirecting...`);
        addLogEntry(`🚀 New project: ${data.new_project_id}`, 'log-green');
        setTimeout(() => {
          window.location.href = `/pages/the-machine-project-v2.html?project_id=${data.new_project_id}`;
        }, 800);
        return;
      }

      const reply = data.response || 'No response';
      const muscle = data.muscle || '';

      appendMsg('assistant', reply);
      chatHistory.push({ role: 'assistant', content: reply });

      const muscleTag = muscle ? ` [${muscle}]` : '';
      addLogEntry(`💬 Chat${muscleTag}: ${text.substring(0, 40)}`, 'log-cyan');
    } catch (err) {
      if (err.name === 'AbortError') {
        appendMsg('system', '⏱️ Chat timed out — the model may be busy. Try again.');
      } else {
        appendMsg('system', `Error: ${err.message}`);
      }
    } finally {
      sendBtn.disabled = false;
      typing.textContent = '';
    }
  }

  sendBtn?.addEventListener('click', sendMessage);
  input?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
}

// ── Settings Modal ───────────────────────────────────────

function setupSettingsModal() {
  const modal = document.getElementById('settingsModal');
  const closeBtn = document.getElementById('settingsCloseBtn');
  const cancelBtn = document.getElementById('settingsCancelBtn');
  const saveBtn = document.getElementById('settingsSaveBtn');
  const resetBtn = document.getElementById('settingsResetBtn');
  const body = document.getElementById('settingsBody');
  const openBtn = document.getElementById('topSettingsBtn');
  if (!modal || !body) return;

  let currentSettings = {};

  function openSettings() { modal.classList.add('open'); loadSettings(); }
  function closeSettings() { modal.classList.remove('open'); }

  openBtn?.addEventListener('click', openSettings);
  closeBtn?.addEventListener('click', closeSettings);
  cancelBtn?.addEventListener('click', closeSettings);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeSettings(); });

  async function loadSettings() {
    body.innerHTML = '<div style="color:#888;font-size:12px;padding:20px;text-align:center">Loading...</div>';
    try {
      const [settingsResp, modelsResp] = await Promise.all([
        fetch('/api/swarm/settings'),
        fetch('/api/swarm/models'),
      ]);
      const settingsData = await settingsResp.json();
      const modelsData = await modelsResp.json();
      const rawSettings = settingsData.settings || {};
      const models = modelsData.models || [];
      // API returns {key: {value, default, label, group, type, help, ...}}
      // Decompose into flat currentSettings + meta
      currentSettings = {};
      const meta = {};
      for (const [key, obj] of Object.entries(rawSettings)) {
        if (typeof obj === 'object' && obj !== null && 'value' in obj) {
          currentSettings[key] = obj.value;
          meta[key] = obj;
        } else {
          currentSettings[key] = obj;
        }
      }

      // Group settings by category
      const groups = {};
      for (const [key, value] of Object.entries(currentSettings)) {
        const m = meta[key] || {};
        const group = m.group || 'other';
        if (!groups[group]) groups[group] = [];
        groups[group].push({ key, value, ...m });
      }

      const groupLabels = {
        models: '🤖 Models', workers: '👷 Workers', context: '📐 Context Budgets',
        timeouts: '⏱ Timeouts', fleet: '🚀 Fleet', apply: '🔧 Apply Pipeline',
        strategy: '📋 Strategy', other: '◈ Other',
      };

      let html = '';
      for (const [groupKey, items] of Object.entries(groups)) {
        html += `<div class="settings-group"><h4>${groupLabels[groupKey] || groupKey}</h4>`;
        for (const item of items) {
          const inputId = `sw-${item.key}`;
          let control = '';
          if (item.type === 'model' || item.options) {
            const opts = item.options || models.map(m => m.name);
            control = `<select id="${inputId}" data-key="${item.key}">`;
            for (const opt of opts) {
              const label = typeof opt === 'object' ? opt.label : opt;
              const val = typeof opt === 'object' ? opt.value : opt;
              control += `<option value="${val}"${val === item.value ? ' selected' : ''}>${label}</option>`;
            }
            // Add current value if not in options
            if (!opts.some(o => (typeof o === 'object' ? o.value : o) === item.value)) {
              control += `<option value="${item.value}" selected>${item.value}</option>`;
            }
            control += '</select>';
          } else if (item.type === 'boolean') {
            control = `<select id="${inputId}" data-key="${item.key}">
              <option value="true"${item.value ? ' selected' : ''}>Yes</option>
              <option value="false"${!item.value ? ' selected' : ''}>No</option>
            </select>`;
          } else {
            control = `<input type="${item.type === 'number' ? 'number' : 'text'}" id="${inputId}" data-key="${item.key}" value="${item.value}"${item.min != null ? ` min="${item.min}"` : ''}${item.max != null ? ` max="${item.max}"` : ''}>`;
          }
          const helpText = item.help ? `<div style="grid-column:1/-1;color:#666;font-size:10px;margin-bottom:4px">${item.help}</div>` : '';
          html += `<div class="settings-row"><label for="${inputId}">${item.label || item.key}</label>${control}</div>${helpText}`;
        }
        html += '</div>';
      }
      body.innerHTML = html;
    } catch (err) {
      body.innerHTML = `<div style="color:#ff5252;padding:20px;text-align:center">Failed to load settings: ${err.message}</div>`;
    }
  }

  saveBtn?.addEventListener('click', async () => {
    const inputs = body.querySelectorAll('[data-key]');
    const updates = {};
    inputs.forEach(el => {
      const key = el.dataset.key;
      let val = el.value;
      // Coerce types
      if (val === 'true') val = true;
      else if (val === 'false') val = false;
      else if (el.type === 'number' && val !== '') val = Number(val);
      updates[key] = val;
    });
    try {
      const resp = await fetch('/api/swarm/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates),
      });
      if (!resp.ok) throw new Error(`Save failed: ${resp.status}`);
      addLogEntry('⚙️ Settings saved', 'log-green');
      closeSettings();
    } catch (err) {
      addLogEntry(`⚙️ Save error: ${err.message}`, 'log-pink');
    }
  });

  resetBtn?.addEventListener('click', async () => {
    if (!confirm('Reset all swarm settings to defaults?')) return;
    try {
      await fetch('/api/swarm/settings/reset', { method: 'POST' });
      addLogEntry('⚙️ Settings reset to defaults', 'log-gold');
      loadSettings();
    } catch (err) {
      addLogEntry(`⚙️ Reset error: ${err.message}`, 'log-pink');
    }
  });
}

// ── Help Button ──────────────────────────────────────────

function setupProjectButtons() {
  const projectId = getProjectId();
  if (!projectId) return;

  const browseBtn = document.getElementById('btnBrowseProject');
  const webUIBtn = document.getElementById('btnOpenWebUI');

  browseBtn?.addEventListener('click', async () => {
    try {
      const resp = await fetch(`/api/machine/projects/${projectId}/workspace-path`);
      if (!resp.ok) throw new Error(`${resp.status}`);
      const paths = await resp.json();
      const dir = paths.linked_dir || paths.project_dir;
      await fetch('/api/machine/open-file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: dir }),
      });
      addLogEntry(`📂 Opened: ${dir}`, 'log-cyan');
    } catch (err) {
      addLogEntry(`❌ Browse failed: ${err.message}`, 'log-red');
    }
  });

  webUIBtn?.addEventListener('click', () => {
    window.open(`/?tab=machine&project=${projectId}`, '_blank');
    addLogEntry('💬 Opened project in WebUI', 'log-cyan');
  });
}

function setupHelpButton() {
  const btn = document.getElementById('topHelpBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    window.open('/help/the-machine-swarm.html', '_blank');
  });

  // Debug log download button
  const dbgBtn = document.getElementById('topDebugBtn');
  if (dbgBtn) {
    dbgBtn.addEventListener('click', async () => {
      const { downloadDebugLog } = await import('./ui.js');
      downloadDebugLog();
    });
  }
}

// ── Render loop ──────────────────────────────────────────

function frame(timestamp) {
  if (!isInitialized) {
    requestAnimationFrame(frame);
    return;
  }

  const dt = Math.min(0.1, (timestamp - lastTime) / 1000);
  lastTime = timestamp;
  elapsed += dt;

  renderGrid();

  const fgCtx = canvas.fgCtx;
  const w = viewport.width;
  const h = viewport.height;

  fgCtx.clearRect(0, 0, w, h);
  fgCtx.save();
  fgCtx.translate(w / 2 + viewport.panX, h / 2 + viewport.panY);
  fgCtx.scale(viewport.zoom, viewport.zoom);

  drawPipelineConnections(fgCtx, elapsed, dt, viewport.zoom);
  drawHolographicBrain(fgCtx, graph.coreX, graph.coreY, elapsed, viewport.zoom);
  updateAgents(dt, playback.speedMultiplier);
  drawNodes(fgCtx, elapsed, viewport.zoom);
  drawTaskNodes(fgCtx, elapsed, viewport.zoom);
  drawAgents(fgCtx, elapsed, viewport.zoom);
  drawChatBubbles(fgCtx, elapsed, viewport.zoom);

  fgCtx.restore();
  updateIframePositions();

  // Active agent HUD (screen space, bottom-right of canvas)
  drawActiveAgentHUD(fgCtx, w, h);

  if (Math.floor(elapsed * 4) % 1 === 0) {
    updateUI();
  }

  requestAnimationFrame(frame);
}

// ── Pipeline connections ─────────────────────────────────

function spawnPipelineParticles(dt) {
  Object.keys(agents).forEach(agentId => {
    const as = agents[agentId];
    if (as.travelProgress < 1 && as.travelProgress > 0) {
      if (Math.random() < dt * 18) {
        pipeline.particles.push({
          x: as.x + (Math.random() - 0.5) * 10,
          y: as.y + (Math.random() - 0.5) * 10,
          vx: (graph.coreX - as.x) * 0.03 * (as.travelDir === 'returning' ? 1 : -1),
          vy: (graph.coreY - as.y) * 0.03 * (as.travelDir === 'returning' ? 1 : -1),
          life: 1.2,
          color: as.agent.color,
          size: 2 + Math.random() * 1.5,
        });
      }
    }
  });

  // Swarm particles too
  for (const u of swarm.units) {
    if (!u.active || u.progress >= 1) continue;
    if (Math.random() < dt * 12) {
      pipeline.particles.push({
        x: u.x + (Math.random() - 0.5) * 6,
        y: u.y + (Math.random() - 0.5) * 6,
        vx: (u.toX - u.x) * 0.015,
        vy: (u.toY - u.y) * 0.015,
        life: 0.8,
        color: u.color,
        size: 1.5 + Math.random(),
      });
    }
  }
}

function updatePipelineParticles(dt) {
  for (let i = pipeline.particles.length - 1; i >= 0; i--) {
    const p = pipeline.particles[i];
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    p.life -= dt * 0.8;
    if (p.life <= 0) pipeline.particles.splice(i, 1);
  }
  if (pipeline.particles.length > 600) pipeline.particles.splice(0, pipeline.particles.length - 600);
}

function drawPipelineConnections(ctx, time, dt, zoom) {
  const allNodes = [...graph.nodes, ...graph.subNodes];

  for (const node of allNodes) {
    const isActive = node.active;
    const alpha = isActive ? 0.18 : 0.06;
    const isSub = node.isSubNode;

    const originX = isSub ? (graph.nodes.find(n => n.id === node.parentId)?.x ?? graph.coreX) : graph.coreX;
    const originY = isSub ? (graph.nodes.find(n => n.id === node.parentId)?.y ?? graph.coreY) : graph.coreY;

    ctx.beginPath();
    ctx.moveTo(originX, originY);

    const mx = (originX + node.x) / 2;
    const my = (originY + node.y) / 2;
    const perpX = -(node.y - originY) * 0.15;
    const perpY = (node.x - originX) * 0.15;
    ctx.quadraticCurveTo(mx + perpX, my + perpY, node.x, node.y);

    ctx.strokeStyle = `${node.color}${Math.round(alpha * 255).toString(16).padStart(2, '0')}`;
    ctx.lineWidth = (isActive ? 1.5 : isSub ? 0.5 : 0.8) / zoom;
    ctx.setLineDash(isActive ? [] : [4 / zoom, 4 / zoom]);
    ctx.stroke();
    ctx.setLineDash([]);

    if (isActive) {
      const pulseT = (time * 0.4) % 1;
      const px = (1 - pulseT) * (1 - pulseT) * originX + 2 * (1 - pulseT) * pulseT * (mx + perpX) + pulseT * pulseT * node.x;
      const py = (1 - pulseT) * (1 - pulseT) * originY + 2 * (1 - pulseT) * pulseT * (my + perpY) + pulseT * pulseT * node.y;

      ctx.beginPath();
      ctx.arc(px, py, 3 / zoom, 0, Math.PI * 2);
      ctx.fillStyle = `${node.color}aa`;
      ctx.fill();
    }
  }

  spawnPipelineParticles(dt);
  updatePipelineParticles(dt);

  for (const p of pipeline.particles) {
    const a = Math.max(0, p.life);
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.size / zoom, 0, Math.PI * 2);
    ctx.fillStyle = `${p.color}${Math.round(a * 180).toString(16).padStart(2, '0')}`;
    ctx.fill();
  }
}

// Init on page load
console.log('📄 Document ready state:', document.readyState);
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => init());
} else {
  init();
}
