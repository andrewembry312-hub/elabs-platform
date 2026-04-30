// ═══════════════════════════════════════════════════════════════════
// WORKFLOW — Real API execution with visual feedback
// ═══════════════════════════════════════════════════════════════════

import { playback, agents, swarm, vram, addLogEntry, updateVramUsage, project, addChatBubble, graph, logDebugEvent } from './state.js?v=20260418';
import { WORKFLOW_STEPS, LABS, AGENTS, SWARM_MODEL, TOTAL_VRAM, getManifest } from './config.js';
import { sendAgentToNode, returnAllAgents, unloadAgent, requestLoadAgent, processLoadingQueue, forceLoadAgent, spawnSwarm, despawnSwarm, despawnAllSwarm, spawnSubNode, spawnTaskNode, completeTaskNode, failTaskNode, spawnRevisionNode } from './agents.js';
import { setNodeActive, setNodeCompleted, setNodeAgentCount, refreshNodeStatuses } from './nodes.js';
import { updateUI, renderFileTree } from './ui.js';

const API_BASE = '/api/machine';

let _subAgentCounter = 0;

/**
 * Spawn a dynamic sub-agent: creates a new agent entry + state + task node + sprite.
 * Returns the new agentId.
 */
function _spawnSubAgent(parentNodeId, taskName, color) {
  _subAgentCounter++;
  const agentId = `sub-agent-${_subAgentCounter}`;

  // Create agent config entry
  const agentCfg = {
    id: agentId,
    name: `Sub-Agent ${_subAgentCounter}`,
    model: 'sub-agent',
    vram: 0,
    color: color || '#38BDF8',
    emoji: '🤖',
    nodeId: parentNodeId,
  };
  AGENTS.push(agentCfg);

  // Create agent state
  agents[agentId] = {
    agent: agentCfg,
    state: 'idle',
    phase: 'unloaded',
    currentLab: null,
    x: graph.coreX,
    y: graph.coreY,
    fromX: graph.coreX,
    fromY: graph.coreY,
    toX: graph.coreX,
    toY: graph.coreY,
    travelProgress: 1.0,
    travelDir: null,
    trail: [],
    bobPhase: Math.random() * Math.PI * 2,
    loaded: false,
    vramInUse: 0,
    queuedForLoad: false,
    workDurationMs: 0,
  };

  // Spawn task node for this sub-agent
  const taskNode = spawnTaskNode(parentNodeId, `🤖 ${taskName}`, color, agentId, 'active');

  // Load and send the sprite to the new task node
  if (taskNode) {
    forceLoadAgent(agentId);
    sendAgentToNode(agentId, taskNode.id);
    addChatBubble(agentCfg.name, `Spawned for ${taskName}`, color, 3000);
  }

  addLogEntry(`🤖 Sub-Agent ${_subAgentCounter} spawned → ${taskName}`, 'log-cyan');
  return agentId;
}

// ─── Helper: return a specific agent from its current node ──

function _returnAgentFromNode(agentId) {
  const ag = agents[agentId];
  if (!ag) return;
  ag.fromX = ag.x;
  ag.fromY = ag.y;
  ag.toX = graph.coreX;
  ag.toY = graph.coreY;
  ag.travelProgress = 0;
  ag.travelDir = 'returning';
  ag.phase = 'returning';
  ag.trail = [];
  ag.currentLab = null;
}

// ─── Helper: extract sections from long result text for child node spawning ──

function _extractResultSections(text) {
  // Try to find markdown headings, code blocks, or numbered items
  const sections = [];
  const headingRe = /^#{1,3}\s+(.+)/gm;
  let match;
  while ((match = headingRe.exec(text)) !== null && sections.length < 6) {
    sections.push({ label: match[1].substring(0, 40) });
  }
  if (sections.length > 0) return sections;
  // Fallback: split by code blocks
  const codeBlocks = text.match(/```[\w]*\n/g);
  if (codeBlocks && codeBlocks.length > 0) {
    return codeBlocks.slice(0, 5).map((cb, i) => ({
      label: `code-block-${i + 1}`
    }));
  }
  // Last resort: split by length
  const chunkSize = Math.ceil(text.length / 3);
  return [
    { label: 'output-part-1' },
    { label: 'output-part-2' },
    { label: 'output-part-3' },
  ];
}

// Debounced file tree refresh
let _treeRefreshTimer = null;
function scheduleTreeRefresh() {
  if (_treeRefreshTimer) clearTimeout(_treeRefreshTimer);
  _treeRefreshTimer = setTimeout(async () => {
    if (!project.id) return;
    try {
      const resp = await fetch(`${API_BASE}/projects/${project.id}/tree`);
      if (!resp.ok) return;
      const data = await resp.json();
      project.fileTree = data.tree;
      const container = document.getElementById('fileTree');
      if (container) renderFileTree(container, data.tree, data.linked_dir || project.linkedDir || null);
    } catch {}
  }, 1500); // 1.5s debounce
}

// ─── Execute a single node via API ───────────────────────

async function executeNodeAPI(nodeIdx) {
  const lab = LABS[nodeIdx];
  if (!lab || !project.id) return null;

  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes/${lab.nodeId}/run`, { method: 'POST' });
  if (!resp.ok) {
    const err = await resp.text();
    addLogEntry(`✗ Node ${lab.label} failed: ${err}`, 'log-pink');
    return null;
  }
  return resp.json();
}

// ─── Execute all nodes via swarm API ─────────────────────

async function executeSwarmAPI() {
  if (!project.id) return null;
  // Pass project's swarm_model if configured; backend falls back to qwen3:4b
  const swarmModel = (project.swarm_model && project.swarm_model !== 'null') ? project.swarm_model : null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/swarm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ swarm_model: swarmModel }),
  });
  if (!resp.ok) {
    const err = await resp.text();
    addLogEntry(`✗ Swarm failed: ${err}`, 'log-pink');
    return null;
  }
  return resp.json();
}

// ─── Execute nodes via DAG API (respects dependencies) ───

async function executeDAGAPI() {
  if (!project.id) return null;
  // Pass project's swarm_model if configured; backend falls back to qwen3:4b
  const swarmModel = (project.swarm_model && project.swarm_model !== 'null') ? project.swarm_model : null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/dag`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ swarm_model: swarmModel }),
  });
  if (!resp.ok) {
    const err = await resp.text();
    addLogEntry(`✗ DAG execution failed: ${err}`, 'log-pink');
    return null;
  }
  return resp.json();
}

// ─── Poll project status ─────────────────────────────────

async function pollProject() {
  if (!project.id) { console.warn('[pollProject] no project.id'); return null; }
  try {
    const resp = await fetch(`${API_BASE}/projects/${project.id}`);
    if (!resp.ok) { console.warn('[pollProject] bad status:', resp.status); return null; }
    return resp.json();
  } catch (err) {
    console.error('[pollProject] fetch error:', err);
    return null;
  }
}

// ─── Step execution with visual feedback ─────────────────

export async function executeStep(stepIdx) {
  if (stepIdx >= LABS.length) {
    playback.isPlaying = false;
    playback.currentStep = -1;
    addLogEntry('✓ All nodes complete!', 'log-green');
    returnAllAgents();
    despawnAllSwarm();
    disconnectFileEvents();
    updateUI();
    return;
  }

  // Connect file events on first step
  if (stepIdx === 0) connectFileEvents();

  playback.currentStep = stepIdx;
  const lab = LABS[stepIdx];
  const agentId = 'agent-' + lab.nodeId;

  // Visual: activate node and send agent
  addLogEntry(`→ Node ${stepIdx}: ${lab.label} [${lab.muscle}]`, 'log-cyan');
  setNodeActive(lab.id, true);

  // Spawn a branching task node for this execution step
  const _stepFwBadge = lab.framework === 'openclaw' ? '⚡ ' : '';
  const _stepFwColor = lab.framework === 'openclaw' ? '#76b900' : lab.color;
  spawnTaskNode(lab.id, `${_stepFwBadge}${lab.muscle} exec`, _stepFwColor, lab.muscle, 'active');

  if (agents[agentId]) {
    forceLoadAgent(agentId);
    sendAgentToNode(agentId, lab.id);
  }
  setNodeAgentCount(lab.id, 1);

  // Spawn swarm carriers for visual effect
  spawnSwarm(3, lab.id, agentId);

  updateVramUsage();
  updateUI();

  // Call real API
  const result = await executeNodeAPI(stepIdx);

  // Visual: complete node
  setNodeActive(lab.id, false);
  setNodeCompleted(lab.id, true);
  setNodeAgentCount(lab.id, 0);

  if (result) {
    lab.status = 'done';
    addLogEntry(`✓ Node ${stepIdx} complete (${result.tokens_used || 0} tokens)`, 'log-gold');
    // Complete the task node branch
    const _doneFwBadge = lab.framework === 'openclaw' ? '⚡ ' : '';
    const _doneFwColor = lab.framework === 'openclaw' ? '#76b900' : lab.color;
    spawnTaskNode(lab.id, `${_doneFwBadge}${lab.muscle} exec`, _doneFwColor, lab.muscle, 'completed');
  }

  if (agents[agentId] && agents[agentId].phase === 'executing') {
    returnAllAgents();
  }
  despawnSwarm(3);
  updateUI();

  // Auto-continue if playing
  if (playback.isPlaying && !playback.isPaused) {
    await executeStep(stepIdx + 1);
  }
}

// ─── Swarm execution (all nodes parallel) ────────────────

export async function executeSwarm() {
  if (!project.id) return;

  playback.isPlaying = true;
  playback.isPaused = false;
  startTimer();

  addLogEntry('⚡ SWARM MODE — executing all nodes in parallel', 'log-cyan');

  // Connect filesystem event listener for live child node spawning
  connectFileEvents();

  // Visual: activate all nodes and send per-node agents
  LABS.forEach((lab, i) => {
    if (lab.status === 'pending') {
      setNodeActive(lab.id, true);
      const agentId = `agent-${lab.nodeId}`;
      if (agents[agentId]) {
        forceLoadAgent(agentId);
        sendAgentToNode(agentId, lab.id);
      }
      spawnSwarm(2, lab.id, agentId);
      // Spawn task branch for each active swarm node
      spawnTaskNode(lab.id, `swarm-${i}`, lab.color, lab.muscle, 'active');
    }
  });
  updateUI();

  // Call swarm API
  const result = await executeSwarmAPI();

  if (result && result.error) {
    addLogEntry(`✗ Swarm error: ${result.error}`, 'log-pink');
  }

  // Poll until all done
  await _pollUntilDone();
}

// ─── OpenClaw-native DAG execution ─────────────────────

/**
 * Execute all pending nodes in parallel via OpenClaw chat.send.
 * Visual feedback is driven entirely by real gateway chat events (delta/final/error).
 * No polling needed — event-driven.
 */
export async function executeViaOpenClaw() {
  if (!project.id) return;

  const { executeNodeViaOpenClaw } = await import('./openclaw-visual-bridge.mjs');

  playback.isPlaying = true;
  playback.isPaused = false;
  startTimer();

  addLogEntry('⚡ OpenClaw DAG — sending nodes to gateway', 'log-cyan');
  connectFileEvents();

  // Build dependency-aware execution order (same as backend DAG)
  const pending = LABS.filter(l => l.status === 'pending' || l.status === 'running');
  const completed = new Set(LABS.filter(l => l.status === 'done').map(l => String(l.nodeId)));

  // Fire off nodes whose dependencies are satisfied, then poll for newly unblocked nodes
  const fired = new Set();

  async function _fireReady() {
    let anyFired = false;
    for (const lab of pending) {
      if (fired.has(lab.id)) continue;
      const deps = (lab.depends_on || []).map(String);
      const ready = deps.every(d => completed.has(d));
      if (!ready) continue;
      fired.add(lab.id);
      anyFired = true;
      await executeNodeViaOpenClaw(lab, project.id);
    }
    return anyFired;
  }

  await _fireReady();
  updateUI();
}

/**
 * Abort all running OpenClaw sessions for this project.
 */
export async function stopViaOpenClaw() {
  const { callMethod } = await import('./openclaw-client.mjs');
  for (const lab of LABS) {
    const sessionKey = `machine:${project.id}:${lab.nodeId}`;
    callMethod('chat.abort', { sessionKey }).catch(() => {});
  }
  addLogEntry('⏹ OpenClaw — abort sent to all sessions', 'log-pink');
  playback.isPlaying = false;
  stopTimer();
  updateUI();
}

// ─── DAG execution (respects dependencies) ───────────────

export async function executeDAG() {
  if (!project.id) return;

  // Route through OpenClaw when it's enabled and connected
  if (window.OPENCLAW_ENABLED && window.OPENCLAW_MODE === 'openclaw_primary') {
    try {
      const { isBridgeConnected } = await import('./openclaw-visual-bridge.mjs');
      if (isBridgeConnected()) {
        return executeViaOpenClaw();
      }
    } catch (_) {}
  }

  playback.isPlaying = true;
  playback.isPaused = false;
  startTimer();

  addLogEntry('🔀 DAG MODE — executing with dependency awareness', 'log-cyan');

  connectFileEvents();

  // Visual: activate nodes with no deps immediately, others stay pending
  LABS.forEach((lab) => {
    if (lab.status === 'pending' && (!lab.depends_on || lab.depends_on.length === 0)) {
      setNodeActive(lab.id, true);
      spawnSwarm(2, lab.id, lab.muscle.toLowerCase());
    }
  });
  updateUI();

  const result = await executeDAGAPI();

  if (result && result.error) {
    addLogEntry(`✗ DAG error: ${result.error}`, 'log-pink');
  }

  // Poll until all done
  await _pollUntilDone();
}

// ─── Auto-resume: detect running project and start poll + visuals ───

export function _autoResumePolling() {
  console.log('[autoResume] starting, LABS:', LABS.length, 'agents:', Object.keys(agents));
  playback.isPlaying = true;
  playback.isPaused = false;
  startTimer();

  // Activate nodes that are already running and send per-node agents to them
  LABS.forEach((lab) => {
    if (lab.status === 'running' || lab.status === 'pending') {
      setNodeActive(lab.id, true);
      const agentId = `agent-${lab.nodeId}`;
      if (agents[agentId]) {
        forceLoadAgent(agentId);
        sendAgentToNode(agentId, lab.id);
      }
      spawnSwarm(2, lab.id, agentId);
      const _resumeFwBadge = lab.framework === 'openclaw' ? '⚡ ' : '';
      const _resumeFwColor = lab.framework === 'openclaw' ? '#76b900' : lab.color;
      spawnTaskNode(lab.id, `${_resumeFwBadge}${lab.muscle} exec`, _resumeFwColor, lab.muscle, 'active');
      lab._resumeSeen = true;
    }
  });
  updateUI();

  // Start polling (with error catch to prevent silent failures)
  _pollUntilDone().catch(err => {
    console.error('[autoResume] poll error:', err);
    addLogEntry(`❌ Polling error: ${err.message}`, 'log-pink');
  });
}

// ─── Shared polling loop for swarm/DAG ───────────────────

let _pollLoopActive = false;
async function _pollUntilDone() {
  if (_pollLoopActive) { console.warn('[pollUntilDone] already running, skipping duplicate'); return; }
  _pollLoopActive = true;
  let polls = 0;
  const maxPolls = 300; // 5 minutes at 1s intervals
  console.log('[pollUntilDone] starting poll loop, project:', project.id);
  while (polls < maxPolls) {
    await sleep(1000);
    const manifest = await pollProject();
    if (!manifest) { console.warn('[pollUntilDone] no manifest, breaking'); break; }
    console.log('[pollUntilDone] poll', polls, 'status:', manifest.status);

    // Sync _manifest node statuses so updateProjectInfo() sees current data
    const storedManifest = getManifest();
    if (storedManifest && manifest.nodes) {
      storedManifest.status = manifest.status;
      storedManifest.completed_nodes = manifest.completed_nodes;
      storedManifest.total_tokens = manifest.total_tokens;
      storedManifest.total_time = manifest.total_time;
      for (const polledNode of manifest.nodes) {
        const stored = storedManifest.nodes.find(n => n.id === polledNode.id);
        if (stored) {
          stored.status = polledNode.status;
          stored.result = polledNode.result;
          stored.model_used = polledNode.model_used;
          stored.tokens_used = polledNode.tokens_used;
          stored.completed_at = polledNode.completed_at;
        }
      }
    }

    // Update node statuses from manifest
    let allDone = true;
    for (const node of (manifest.nodes || [])) {
      const labId = `node-${node.id}`;
      const lab = LABS.find(l => l.id === labId);
      if (!lab) continue;

      if (node.status === 'complete' && lab.status !== 'done') {
        lab.status = 'done';
        lab.result = node.result;
        setNodeActive(labId, false);
        setNodeCompleted(labId, true);
        // Return agent from completed node
        const doneAgentId = 'agent-' + node.id;
        if (agents[doneAgentId] && agents[doneAgentId].phase === 'executing' && agents[doneAgentId].currentLab === labId) {
          _returnAgentFromNode(doneAgentId);
        }
        // Clear task tracking
        if (agents[doneAgentId]) {
          agents[doneAgentId].lastTask = agents[doneAgentId].currentTask || node.task || lab.label;
          agents[doneAgentId].lastTaskStatus = 'completed';
          agents[doneAgentId].currentTask = null;
          agents[doneAgentId].taskStartedAt = null;
        }
        // Chat: completion summary
        const tokenStr = node.tokens_used ? `${node.tokens_used} tokens` : '';
        addChatBubble(lab.muscle, `✓ Done${tokenStr ? ' · ' + tokenStr : ''}`, lab.color, 4000);
        logDebugEvent('node_complete', doneAgentId, { nodeId: node.id, tokens: node.tokens_used, model: node.model_used });
        // Spawn child nodes for result chunks
        if (node.result && node.result.length > 200) {
          const chunks = _extractResultSections(node.result);
          chunks.forEach((chunk, ci) => {
            setTimeout(() => {
              spawnTaskNode(labId, chunk.label, lab.color, lab.muscle, 'completed');
            }, ci * 300);
          });
        }
        addLogEntry(`✓ ${lab.label} complete`, 'log-green');
      } else if (node.status === 'running' && lab.status !== 'running') {
        lab.status = 'running';
        setNodeActive(labId, true);
        // Send agent to node (the key visual movement)
        const agentId = 'agent-' + node.id;
        if (agents[agentId]) {
          forceLoadAgent(agentId);
          sendAgentToNode(agentId, labId);
          // Track task on agent
          agents[agentId].currentTask = node.task || lab.label;
          agents[agentId].taskStartedAt = Date.now();
        }
        // Chat: model loading + task start
        const modelName = node.model_used || 'model';
        addChatBubble(lab.muscle, `Loading ${modelName}...`, lab.color, 3000);
        setTimeout(() => addChatBubble(lab.muscle, `Thinking...`, lab.color, 5000), 3000);
        logDebugEvent('node_running', agentId, { nodeId: node.id, muscle: lab.muscle, model: modelName });
        spawnSwarm(2, labId, agentId);
        const _pollFwBadge = lab.framework === 'openclaw' ? '⚡ ' : '';
        const _pollFwColor = lab.framework === 'openclaw' ? '#76b900' : lab.color;
        spawnTaskNode(labId, `${_pollFwBadge}${lab.muscle} exec`, _pollFwColor, lab.muscle, 'active');
        addLogEntry(`→ ${lab.label} started`, 'log-cyan');
      } else if (node.status === 'error' && lab.status !== 'error') {
        lab.status = 'error';
        lab.result = node.result;
        setNodeActive(labId, false);
        // Return agent from failed node
        const errAgentId = 'agent-' + node.id;
        if (agents[errAgentId] && agents[errAgentId].phase === 'executing' && agents[errAgentId].currentLab === labId) {
          _returnAgentFromNode(errAgentId);
        }
        // Chat: error message
        const errMsg = (node.result || 'Unknown error').substring(0, 60);
        addChatBubble(lab.muscle, `✗ ${errMsg}`, '#ff5252', 6000);
        logDebugEvent('node_error', errAgentId, { nodeId: node.id, error: errMsg });
        spawnTaskNode(labId, `error`, lab.color, lab.muscle, 'failed');
        addLogEntry(`✗ ${lab.label} failed`, 'log-pink');
      } else if (node.status === 'running' && lab.status === 'running') {
        // Only show fallback bubble if the SSE task_event stream has been silent
        // for >15 seconds — real events from handleTaskEvent() take priority.
        // _lastRealEventAt is updated in handleTaskEvent() on every backend event.
        const silentMs = Date.now() - (_lastRealEventAt || 0);
        if (silentMs > 15000) {
          const lastShown = _lastWaitingBubbleAt.get(node.id) || 0;
          if (Date.now() - lastShown > 25000) {  // 25s cooldown per node — prevents parallel-node stacking
            const elapsed = node.started_at
              ? Math.round((Date.now() - new Date(node.started_at).getTime()) / 1000)
              : 0;
            addChatBubble(lab.muscle, `⏳ Running ${elapsed}s — waiting for events…`, lab.color, 6000);
            _lastWaitingBubbleAt.set(node.id, Date.now());
          }
        }
      }
      if (node.status !== 'complete' && node.status !== 'error') allDone = false;
    }

    refreshNodeStatuses();
    updateUI();

    if (allDone || manifest.status === 'complete' || manifest.status === 'error') break;
    polls++;
  }

  _pollLoopActive = false;
  despawnAllSwarm();
  returnAllAgents();

  playback.isPlaying = false;
  stopTimer();
  addLogEntry('✓ Execution complete!', 'log-green');
  updateUI();
}

// ─── Playback controls ───────────────────────────────────

export function togglePlay() {
  if (playback.isPlaying && !playback.isPaused) {
    pauseExec();
    return;
  }

  if (playback.isPaused) {
    playback.isPaused = false;
    playback.isPlaying = true;
    startTimer();
    executeStep(playback.currentStep + 1);
    updateUI();
    return;
  }

  playback.isPlaying = true;
  playback.isPaused = false;
  addLogEntry('▶ Sequential execution started', 'log-green');
  startTimer();
  executeStep(0);
  updateUI();
}

export function pauseExec() {
  playback.isPaused = true;
  playback.isPlaying = false;
  clearTimeout(playback.stepTimeout);
  stopTimer();
  addLogEntry('⏸ Paused', 'log-gold');
  updateUI();
}

export function resetExec() {
  playback.isPlaying = false;
  playback.isPaused = false;
  playback.currentStep = -1;
  clearTimeout(playback.stepTimeout);
  stopTimer();
  playback.timerElapsed = 0;

  returnAllAgents();
  despawnAllSwarm();
  disconnectFileEvents();

  Object.keys(agents).forEach(agId => {
    agents[agId].loaded = false;
    agents[agId].phase = 'unloaded';
    agents[agId].queuedForLoad = false;
    agents[agId].vramInUse = 0;
  });

  vram.loadingQueue.length = 0;
  vram.used = 0;
  vram.reserved = 0;
  vram.modelsLoaded.length = 0;
  swarm.units.length = 0;
  swarm.nextId = 0;

  LABS.forEach(lab => {
    setNodeActive(lab.id, false);
    setNodeCompleted(lab.id, false);
    setNodeAgentCount(lab.id, 0);
  });

  updateVramUsage();
  addLogEntry('↺ Reset', 'log-pink');
  updateUI();
}

export function stepOnce() {
  if (playback.isPlaying) return;

  playback.isPlaying = true;
  playback.isPaused = false;

  const next = playback.currentStep + 1;
  if (next >= LABS.length) {
    playback.isPlaying = false;
    return;
  }

  executeStep(next).then(() => {
    playback.isPlaying = false;
    playback.isPaused = true;
    updateUI();
  });
}

export function updateSpeed(val) {
  playback.speedMultiplier = parseFloat(val);
  updateUI();
}

// ─── Timer ───────────────────────────────────────────────

let timerInterval = null;

function startTimer() {
  playback.timerStart = Date.now() - playback.timerElapsed;
  timerInterval = setInterval(updateTimerDisplay, 200);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
}

function updateTimerDisplay() {
  playback.timerElapsed = Date.now() - playback.timerStart;
  const s = Math.floor(playback.timerElapsed / 1000);
  const m = Math.floor(s / 60);
  const timerEl = document.getElementById('timer-display');
  if (timerEl) {
    timerEl.textContent = `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}


// ─── Node steering API calls ─────────────────────────────

export async function resetNodeAPI(nodeId) {
  if (!project.id) return null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes/${nodeId}/reset`, { method: 'POST' });
  if (!resp.ok) return null;
  return resp.json();
}

export async function editNodeAPI(nodeId, updates) {
  if (!project.id) return null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes/${nodeId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!resp.ok) return null;
  return resp.json();
}

export async function addNodeAPI(task, muscle = 'NEMOTRON', depends_on = []) {
  if (!project.id) return null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task, muscle, depends_on }),
  });
  if (!resp.ok) {
    let detail = '';
    try { detail = await resp.text(); } catch { /* ignore */ }
    return { __error: true, status: resp.status, detail };
  }
  return resp.json();
}

export async function removeNodeAPI(nodeId) {
  if (!project.id) return false;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes/${nodeId}`, { method: 'DELETE' });
  return resp.ok;
}

export async function runSingleNodeAPI(nodeId) {
  if (!project.id) return null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}/nodes/${nodeId}/run`, { method: 'POST' });
  if (!resp.ok) return null;
  return resp.json();
}

export async function reloadManifest() {
  if (!project.id) return null;
  const resp = await fetch(`${API_BASE}/projects/${project.id}`);
  if (!resp.ok) return null;
  return resp.json();
}


// ─── SSE: Filesystem event listener for child node spawning ──

let _eventSource = null;

export function connectFileEvents() {
  if (!project.id) return;
  disconnectFileEvents();

  const url = `${API_BASE}/projects/${project.id}/events`;
  _eventSource = new EventSource(url);

  _eventSource.addEventListener('fs_event', (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleFileEvent(evt);
    } catch (err) {
      console.warn('SSE parse error:', err);
    }
  });

  _eventSource.addEventListener('task_event', (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleTaskEvent(evt);
    } catch (err) {
      console.warn('SSE task event parse error:', err);
    }
  });

  _eventSource.addEventListener('artifact_update', (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleArtifactUpdate(evt);
    } catch (err) {
      console.warn('SSE artifact_update parse error:', err);
    }
  });

  // On connect, check if project already has an architect.html artifact and load it
  // Only attempt once per project, not on every reconnect
  if (!_architectLoaded) {
    _architectLoaded = true;
    // Only check for architect.html on architect-mode projects (has architect flag)
    if (project.mode === 'architect') {
      _loadExistingArtifact();
    }
  }

  _eventSource.onerror = () => {
    // Reconnect after 3s on error
    setTimeout(() => {
      if (project.id) connectFileEvents();
    }, 3000);
  };

  addLogEntry('📡 File watcher connected', 'log-cyan');
}

export function disconnectFileEvents() {
  if (_eventSource) {
    _eventSource.close();
    _eventSource = null;
  }
}

function handleFileEvent(evt) {
  // evt: { project_id, event, file, node_id, filename, glow, timestamp }
  const nodeId = evt.node_id;
  const filename = evt.filename || evt.file;
  const glowType = evt.glow || 'modify'; // create, modify, delete

  // Find the parent root node by node_id (matches manifest node id)
  let parentId = null;
  if (nodeId !== null && nodeId !== undefined) {
    // Find the LABS entry whose nodeId matches
    const lab = LABS.find(l => l.nodeId === nodeId);
    if (lab) parentId = lab.id;
  }

  // Fallback: assign to first node
  if (!parentId && LABS.length > 0) {
    parentId = LABS[0].id;
  }

  if (!parentId) return;

  // Get parent node's color
  const parent = LABS.find(l => l.id === parentId);
  const color = parent ? parent.color : '#00d4ff';

  // Spawn or re-glow child hexagon node
  const subNode = spawnSubNode(parentId, filename, color, glowType);

  // File branch duplication: if the sub-node already existed (re-glow), spawn a revision
  // snap node tight to the parent for subsequent modifications, or a new branch for first touch
  const fileLabel = filename.length > 30 ? '...' + filename.slice(-27) : filename;
  const existingBranches = graph.taskNodes.filter(
    n => n.parentId === parentId && n.label.includes(fileLabel) && !n.isRevision
  );
  if (existingBranches.length > 0 && glowType === 'modify') {
    // Snap a revision node to the existing file branch node
    const targetBranch = existingBranches[existingBranches.length - 1];
    spawnRevisionNode(targetBranch.id, `rev ${existingBranches.length}`, color);
  } else {
    // First touch: create a full branch
    spawnTaskNode(parentId, `📄 ${fileLabel}`, color, parent ? parent.muscle : '', glowType === 'create' ? 'completed' : 'active');
  }

  // Chat bubble for file creation
  if (glowType === 'create' && parent) {
    addChatBubble(parent.muscle || 'Agent', `Created ${filename}`, color, 3000);
  }

  // Log the event
  const icon = glowType === 'create' ? '🟢' : glowType === 'modify' ? '🟡' : '🔴';
  addLogEntry(`${icon} ${filename} [${glowType}]`, 'log-gold');

  // Stream file events to chat panel
  if (window._appendStreamMsg) {
    window._appendStreamMsg('terminal', `${icon} File ${glowType}`, filename);
  }

  // Refresh file tree in sidebar (debounced)
  scheduleTreeRefresh();
}

/**
 * Handle task lifecycle events from SSE — spawns branching task nodes.
 * evt: { project_id, node_id, task_name, agent_name, status, message, timestamp }
 * status: 'started' | 'completed' | 'failed'
 */
// Timestamp of the last real backend task_event received via SSE.
// Used by _pollUntilDone to suppress fake cycling bubbles when real events arrive.
let _lastRealEventAt = 0;
// Per-node cooldown map: nodeId → timestamp of last "waiting for events" bubble.
// Prevents stacking bubbles when multiple nodes run in parallel.
const _lastWaitingBubbleAt = new Map();

function handleTaskEvent(evt) {
  const nodeId = evt.node_id;
  const taskName = evt.task_name || evt.agent_name || 'task';
  const agentName = evt.agent_name || '';
  const status = evt.status || 'started';

  // Mark that a real backend event arrived — suppresses fallback cycling bubbles
  _lastRealEventAt = Date.now();

  // ── Track current task on agent state ──
  const agentId = 'agent-' + nodeId;
  if (agents[agentId]) {
    if (status === 'started') {
      agents[agentId].currentTask = taskName;
      agents[agentId].taskStartedAt = Date.now();
    } else if (status === 'completed' || status === 'failed') {
      agents[agentId].lastTask = agents[agentId].currentTask || taskName;
      agents[agentId].lastTaskStatus = status;
      agents[agentId].currentTask = null;
      agents[agentId].taskStartedAt = null;
    }
  }

  // ── Stream to chat panel ──
  if (window._appendStreamMsg) {
    if (status === 'cmd_exec') {
      // Terminal command execution event
      window._appendStreamMsg('terminal', `⚡ [node-${nodeId}] ${agentName}`, evt.message || taskName);
    } else {
      const icon = status === 'started' ? '▶' : status === 'completed' ? '✓' : '✗';
      const msg = evt.message ? ` — ${evt.message.substring(0, 200)}` : '';
      window._appendStreamMsg(
        'agent',
        `${icon} [${agentName || 'node-' + nodeId}] ${status}`,
        `${taskName}${msg}`
      );
    }
  }

  // Skip visual node updates for cmd_exec events
  if (status === 'cmd_exec') return;

  // Find parent root node
  let parentId = null;
  if (nodeId !== null && nodeId !== undefined) {
    const lab = LABS.find(l => l.nodeId === nodeId);
    if (lab) parentId = lab.id;
  }
  if (!parentId && LABS.length > 0) parentId = LABS[0].id;
  if (!parentId) return;

  const parent = LABS.find(l => l.id === parentId);
  const color = parent ? parent.color : '#00d4ff';

  if (status === 'started') {
    const taskNode = spawnTaskNode(parentId, taskName, color, agentName, 'active');
    // Spawn a sub-agent with its own sprite for this task
    if (taskNode) {
      _spawnSubAgent(parentId, taskName, color);
    }
    // Also move the parent node's main agent
    const agentId = 'agent-' + nodeId;
    if (agents[agentId] && agents[agentId].phase !== 'executing') {
      forceLoadAgent(agentId);
      sendAgentToNode(agentId, parentId);
    }
  } else if (status === 'completed') {
    // Find existing task node and complete it
    const existing = spawnTaskNode(parentId, taskName, color, agentName, 'completed');
    if (existing) completeTaskNode(existing.id);
  } else if (status === 'failed') {
    const existing = spawnTaskNode(parentId, taskName, color, agentName, 'failed');
    if (existing) failTaskNode(existing.id);
  }

  // Chat bubble with status message
  if (evt.message && agentName) {
    addChatBubble(agentName, evt.message, color, 4000);
  }
}


// ── Architect Mode: Live Artifact Viewer ──────────────────────────────────

let _architectViewer = null;
let _architectLoaded = false;

async function _loadExistingArtifact() {
  // If project already has architect.html, load it via raw URL
  try {
    const pid = project.id;
    if (!pid) return;
    // Quick ping to check if file exists
    const resp = await fetch(`/api/machine/projects/${pid}/files/architect.html`).catch(() => null);
    if (!resp || !resp.ok) return;
    const data = await resp.json();
    if (data.content && data.content.length > 100) {
      // Load via raw URL so canvas works correctly
      handleArtifactUpdate({ project_id: pid, iteration: '?', agent: 'cached', muscle: '', html: null });
    }
  } catch (e) { /* no artifact yet, that's fine */ }
}

function handleArtifactUpdate(evt) {
  const html = evt.html;
  const iteration = evt.iteration || 0;
  const agent = evt.agent || '?';
  const muscle = evt.muscle || '';
  const pid = evt.project_id || evt.projectId || project?.id;

  if (!html && !pid) return;

  // Create or update the architect viewer overlay
  if (!_architectViewer) {
    _architectViewer = _createArchitectViewer();
  }

  // Update the iframe — use src URL so window.innerWidth works properly
  // and pan/zoom state is preserved between updates
  const iframe = _architectViewer.querySelector('#architect-iframe');
  if (iframe) {
      const pid = evt.project_id || evt.projectId || project?.id;
      if (pid) {
      // Load via raw endpoint — append cache-bust only when iteration changes
      // srcdoc takes precedence over src per HTML spec, so remove it first
      iframe.removeAttribute('srcdoc');
      const currentSrc = iframe.getAttribute('src') || '';
      const newSrc = `/api/machine/projects/${pid}/raw/architect.html?v=${iteration}`;
      if (!currentSrc.includes(`/raw/architect.html`) || (`v=${iteration}` !== currentSrc.split('v=')[1])) {
        iframe.setAttribute('src', newSrc);
      }
    } else if (html) {
      // Fallback: srcdoc (no project id available)
      iframe.removeAttribute('src');
      iframe.srcdoc = html;
    }
  }

  // Update the status bar
  const status = _architectViewer.querySelector('#architect-status');
  if (status) {
    status.textContent = `Cycle ${iteration} — Agent ${agent} (${muscle})`;
  }

  // Show the viewer if hidden
  _architectViewer.style.display = 'flex';

  // Stream to chat panel
  if (window._appendStreamMsg) {
    window._appendStreamMsg('agent', `🎨 [${agent}] Canvas Updated`, `Cycle ${iteration} — artifact rebuilt`);
  }

  addLogEntry(`🎨 Architect canvas updated — cycle ${iteration} by ${agent}`, 'log-green');
}

function _createArchitectViewer() {
  // Remove any existing
  const existing = document.getElementById('architect-viewer');
  if (existing) existing.remove();

  const viewer = document.createElement('div');
  viewer.id = 'architect-viewer';
  viewer.style.cssText = `
    position: fixed; top: 5vh; left: 50%; transform: translateX(-50%);
    width: 85vw; height: 88vh; min-width: 700px; min-height: 500px;
    background: #050508; border: 2px solid #00d4ff; border-radius: 12px;
    z-index: 9000; display: flex; flex-direction: column;
    box-shadow: 0 0 60px rgba(0,212,255,0.4), 0 0 120px rgba(0,212,255,0.15);
    overflow: hidden;
  `;

  // Title bar
  const titleBar = document.createElement('div');
  titleBar.style.cssText = `
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 14px; background: #0a0a14; border-bottom: 1px solid #00d4ff33;
    cursor: move; user-select: none; flex-shrink: 0;
  `;

  const title = document.createElement('div');
  title.style.cssText = 'display:flex;align-items:center;gap:10px;';
  title.innerHTML = `
    <span style="color:#00d4ff;font-weight:bold;font-size:14px;">🏙️ CITY BUILDER — ARCHITECT MODE</span>
    <span id="architect-status" style="color:#666;font-size:11px;">Starting...</span>
  `;
  titleBar.appendChild(title);

  const btns = document.createElement('div');
  btns.style.cssText = 'display:flex;gap:6px;';

  // Minimize button
  const minBtn = document.createElement('button');
  minBtn.textContent = '—';
  minBtn.title = 'Minimize';
  minBtn.style.cssText = 'background:#1a1a2a;border:1px solid #333;color:#888;font-size:14px;cursor:pointer;padding:2px 8px;border-radius:4px;';
  let minimized = false;
  const iframeRef = { el: null };
  minBtn.onclick = () => {
    minimized = !minimized;
    if (iframeRef.el) iframeRef.el.style.display = minimized ? 'none' : 'flex';
    viewer.style.height = minimized ? '38px' : '88vh';
    minBtn.textContent = minimized ? '□' : '—';
  };
  btns.appendChild(minBtn);

  const closeBtn = document.createElement('button');
  closeBtn.textContent = '✕';
  closeBtn.title = 'Close';
  closeBtn.style.cssText = 'background:#1a1a2a;border:1px solid #333;color:#ff4444;font-size:14px;cursor:pointer;padding:2px 8px;border-radius:4px;';
  closeBtn.onclick = () => { viewer.style.display = 'none'; };
  btns.appendChild(closeBtn);
  titleBar.appendChild(btns);
  viewer.appendChild(titleBar);

  // Iframe for the artifact
  const iframe = document.createElement('iframe');
  iframe.id = 'architect-iframe';
  iframe.style.cssText = 'flex:1;border:none;background:#000;display:flex;';
  // Use allow-same-origin so window.innerWidth works inside the canvas
  iframe.sandbox = 'allow-scripts allow-same-origin';
  iframe.srcdoc = `<!DOCTYPE html><html><body style="background:#050508;color:#00d4ff;display:flex;
    align-items:center;justify-content:center;height:100vh;margin:0;font-family:monospace;flex-direction:column;gap:16px">
    <div style="font-size:32px">🏙️</div>
    <div style="font-size:18px;font-weight:bold;">CITY BUILDER</div>
    <div style="color:#666;font-size:13px;">Agents are starting to build your city...</div>
    <div style="color:#333;font-size:11px;margin-top:8px;">Pan: drag  ·  Zoom: scroll  ·  Arrow keys</div>
  </body></html>`;
  iframeRef.el = iframe;
  viewer.appendChild(iframe);

  // Make draggable
  let isDragging = false, startX, startY, origLeft, origTop;
  titleBar.addEventListener('mousedown', (e) => {
    if (e.target === minBtn || e.target === closeBtn) return;
    isDragging = true;
    startX = e.clientX; startY = e.clientY;
    const rect = viewer.getBoundingClientRect();
    origLeft = rect.left;
    origTop = rect.top;
    viewer.style.transform = 'none';
    viewer.style.left = origLeft + 'px';
    viewer.style.top = origTop + 'px';
  });
  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    viewer.style.left = (origLeft + e.clientX - startX) + 'px';
    viewer.style.top = (origTop + e.clientY - startY) + 'px';
  });
  document.addEventListener('mouseup', () => { isDragging = false; });

  document.body.appendChild(viewer);
  return viewer;
}
