// ═══════════════════════════════════════════════════════════════════
// OPENCLAW VISUAL BRIDGE — Maps real OpenClaw gateway events to
// THE MACHINE visual layer (agents/nodes/state).
//
// OpenClaw is a chat-gateway with two broadcast event types:
//   • chat           { sessionKey, runId, state: ‘delta’|‘final’|‘error’, message }
//   • chat.side_result  async tool results for a run
//
// Per-node session keys use the pattern:  agent:{agentId}:machine-{nodeId}
// ═══════════════════════════════════════════════════════════════════

import { subscribe, connect, disconnect, getConnectionState, getGatewayUrl, subscribeSession, unsubscribeSession, callMethod } from './openclaw-client.mjs?v=20260427b';
import { initNodes, setNodeCompleted, setNodeActive } from './nodes.js';
import { forceLoadAgent, sendAgentToNode, spawnSwarm, despawnSwarm } from './agents.js';
import { graph, vram, agents, addLogEntry, addChatBubble, project } from './state.js?v=20260416';
import { addNodeAPI, resetNodeAPI, reloadManifest } from './workflow.js';
import { LABS, AGENTS, setDetectedModels, setSystemInfo } from './config.js';

// ── Session ↔ node mapping ────────────────────────────────────────
// Maps OpenClaw session keys → node ids and back
const _sessionToNode = new Map();  // sessionKey → { lab, agentId, ocAgentId }
const _nodeToSession = new Map();  // nodeId     → sessionKey

/**
 * Register a node–session binding and subscribe to its chat stream.
 * Session key pattern: agent:{agentId}:machine-{nodeId}
 */
export function registerNodeSession(labId, projectId, nodeId, ocAgentId) {
  const lab = LABS.find(l => l.id === labId);
  if (!lab) return;
  const resolvedOcAgentId = String(
    ocAgentId || window.OC_DEFAULT_AGENT_ID || 'main'
  ).trim();
  const sessionKey = `agent:${resolvedOcAgentId}:machine-${nodeId}`;
  const agentId = `agent-${nodeId}`;

  // If this node was already registered under a different session key, clean it up first.
  const oldSessionKey = _nodeToSession.get(labId);
  if (oldSessionKey && oldSessionKey !== sessionKey) {
    unsubscribeSession(oldSessionKey, onChatEvent);
    _sessionToNode.delete(oldSessionKey);
  }

  _sessionToNode.set(sessionKey, { lab, agentId, ocAgentId: resolvedOcAgentId });
  _nodeToSession.set(labId, sessionKey);
  subscribeSession(sessionKey, onChatEvent);
}

// ── Real OpenClaw chat event handler ──────────────────────────────

function onChatEvent(payload) {
  const sessionKey = payload?.sessionKey || payload?.data?.sessionKey;
  if (!sessionKey) return;

  const entry = _sessionToNode.get(sessionKey);
  if (!entry) return;
  const { lab, agentId } = entry;

  const state   = payload?.state   || payload?.data?.state;
  const message = payload?.message || payload?.data?.message || '';

  if (state === 'delta') {
    // First delta — activate node + send agent
    if (lab.status !== 'running') {
      lab.status = 'running';
      setNodeActive(lab.id, true);
      forceLoadAgent(agentId);
      sendAgentToNode(agentId, lab.id);
      spawnSwarm(2, lab.id, agentId);
      addLogEntry(`▶ ${lab.label} — streaming via OpenClaw`, 'log-cyan');
    }
    // Stream token as chat bubble (short TTL)
    if (message) {
      addChatBubble(agentId, message.slice(0, 120), lab.color, 1800);
    }

  } else if (state === 'final') {
    lab.status = 'done';
    setNodeActive(lab.id, false);
    setNodeCompleted(lab.id, true);
    despawnSwarm(2);
    addChatBubble(agentId, `✓ Done`, lab.color, 4000);
    addLogEntry(`✓ ${lab.label} complete`, 'log-green');
    _refreshFileTree();

  } else if (state === 'error') {
    lab.status = 'error';
    setNodeActive(lab.id, false);
    despawnSwarm(2);
    const errMsg = (message || 'error').slice(0, 80);
    addChatBubble(agentId, `✗ ${errMsg}`, '#ff5252', 6000);
    addLogEntry(`✗ ${lab.label} — ${errMsg}`, 'log-pink');
  }
}

// ── Detection helpers (call OpenClaw gateway via RPC) ──────────────

export async function detectModels() {
  try {
    const result = await callMethod('models.list', {});
    const models = Array.isArray(result) ? result : (result?.models || []);
    setDetectedModels(models);
    window.OC_MODELS = models;
    addLogEntry(`🤖 Models detected: ${models.map(m => m.id || m.name || m).join(', ').slice(0, 80)}`, 'log-purple');
    const sel = document.getElementById('ocModelsSelect');
    if (sel) {
      sel.innerHTML = models.map(m => {
        const label = m.id || m.name || String(m);
        return `<option value="${label}">${label}</option>`;
      }).join('');
    }
    return models;
  } catch (e) {
    console.warn('[bridge] detectModels failed:', e.message);
    return [];
  }
}

export async function detectAgents() {
  // Immediately render deterministic local fallback to avoid perpetual loading UIs.
  const immediateFallback = _fallbackAgentsFromConfig();
  const immediateDefault = immediateFallback[0]?.id || 'main';
  _renderDetectedAgents(immediateFallback, immediateDefault);

  try {
    const result = await callMethod('agents.list', {}, 5000);
    const agentList = Array.isArray(result) ? result : (result?.agents || []);
    const defaultAgentId = result?.defaultId || agentList?.[0]?.id || 'main';
    window.OC_DEFAULT_AGENT_ID = defaultAgentId;
    window.OC_AGENTS = agentList;
    _renderDetectedAgents(agentList, defaultAgentId);
    _markConfiguredAgentsAvailable(agentList, defaultAgentId);
    addLogEntry(`🤖 Agents: ${agentList.map(a => a.id || a.name || a).join(', ').slice(0, 80)}`, 'log-purple');
    return agentList;
  } catch (e) {
    console.warn('[bridge] detectAgents failed:', e.message);
    const fallbackAgents = _fallbackAgentsFromConfig();
    const fallbackDefault = fallbackAgents[0]?.id || 'main';
    window.OC_DEFAULT_AGENT_ID = fallbackDefault;
    window.OC_AGENTS = fallbackAgents;
    _renderDetectedAgents(fallbackAgents, fallbackDefault);
    addLogEntry('⚠ OpenClaw agent discovery timed out — using local fallback agent list', 'log-pink');
    return fallbackAgents;
  }
}

function _fallbackAgentsFromConfig() {
  const seen = new Set();
  const fallback = [];

  for (const agent of AGENTS || []) {
    const id = String(agent?.model || agent?.name || '').trim().toLowerCase();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    fallback.push({ id, name: String(agent?.name || id) });
  }

  if (fallback.length === 0) fallback.push({ id: 'main', name: 'Main (fallback)' });
  return fallback;
}

export async function detectHealth() {
  try {
    const result = await callMethod('health', {});
    setSystemInfo(result || {});
    window.OC_HEALTH = result;
    const el = document.getElementById('ocSystemInfo');
    if (el && result) el.textContent = JSON.stringify(result).slice(0, 120);
    return result;
  } catch (e) {
    console.warn('[bridge] detectHealth failed:', e.message);
    return null;
  }
}

// ── Execute a node via OpenClaw chat.send ─────────────────────────

export async function executeNodeViaOpenClaw(lab, projectId) {
  if (!lab || !projectId) return;
  const existingSessionKey = _nodeToSession.get(lab.id);
  const fallbackAgentId = String(window.OC_DEFAULT_AGENT_ID || 'main').trim();
  const sessionKey = existingSessionKey || `agent:${fallbackAgentId}:machine-${lab.nodeId}`;
  const prompt = _buildTaskPrompt(lab);
  const runId = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID()
    : `run-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  addLogEntry(`→ ${lab.label} — sending to OpenClaw session: ${sessionKey}`, 'log-cyan');
  try {
    await callMethod('chat.send', { sessionKey, message: prompt, idempotencyKey: runId });
  } catch (e) {
    addLogEntry(`✗ ${lab.label} send failed: ${e.message}`, 'log-pink');
  }
}

function _markConfiguredAgentsAvailable(agentList, defaultAgentId) {
  // Import MUSCLE_TO_OC_AGENT mapping to check per-agent availability
  import('./config.js').then(cfg => {
    const muscleToOc = cfg.MUSCLE_TO_OC_AGENT || {};
    const ocAgents = Array.isArray(agentList) ? agentList : [];

    // Only override availability when OC returns a non-empty agent list.
    // When OC is unreachable/returns empty, leave the defaults set in initAgentStates()
    // (system muscles default to runtimeAvailable=true for direct Ollama routing).
    if (ocAgents.length === 0) {
      addLogEntry('ℹ️ OpenClaw: no agents registered — using direct routing', 'log-cyan');
      return;
    }

    // If OC exposes a catch-all router agent ("main", "router", "default"),
    // all muscles route through it and are therefore available — keep defaults.
    const hasCatchAll = ocAgents.some(a =>
      ['main', 'router', 'default', 'gateway'].includes(String(a?.id || a?.name || '').toLowerCase())
    );
    if (hasCatchAll) {
      addLogEntry('ℹ️ OpenClaw: catch-all router detected — all muscles available', 'log-cyan');
      return;
    }

    const availableOcIds = new Set(ocAgents.map(a => String(a?.id || '').toLowerCase()));

    // Override availability based on OC's actual response
    Object.values(agents).forEach((agentState) => {
      // agent.muscle may not exist on AGENTS built from config.js; use agent.model (lowercase)
      const muscleKey = (agentState.agent?.muscle || (agentState.agent?.model || '')).toUpperCase();
      const mappedOcId = muscle => muscleToOc[muscle];
      const ocId = mappedOcId(muscleKey);
      const isAvailable = ocId && availableOcIds.has(ocId.toLowerCase());

      agentState.runtimeAvailable = isAvailable;
      agentState.runtimeAgentId = isAvailable ? ocId : null;
    });
  }).catch(e => {
    console.warn('[bridge] Failed to load MUSCLE_TO_OC_AGENT mapping:', e.message);
    // Don't change agent availability on mapping failure
  });
}

function _renderDetectedAgents(agentList, defaultAgentId) {
  const list = Array.isArray(agentList) ? agentList : [];

  const addNodeSelect = document.getElementById('addNodeMuscle');
  if (addNodeSelect) {
    if (list.length === 0) {
      addNodeSelect.innerHTML = '<option value="">No OpenClaw agents found</option>';
    } else {
      addNodeSelect.innerHTML = list.map((agent) => {
        const id = String(agent?.id || '').trim();
        const name = String(agent?.name || id).trim();
        const label = name && name !== id ? `${name} (${id})` : id;
        const selected = id === defaultAgentId ? ' selected' : '';
        return `<option value="${id}"${selected}>${label}</option>`;
      }).join('');
    }
  }

  const checkboxes = document.getElementById('agentSelectionCheckboxes');
  if (checkboxes) {
    checkboxes.innerHTML = '';
    if (list.length === 0) {
      const empty = document.createElement('div');
      empty.style.fontSize = '10px';
      empty.style.color = '#888';
      empty.textContent = 'No OpenClaw agents detected yet';
      checkboxes.appendChild(empty);
    } else {
      for (const agent of list) {
        const id = String(agent?.id || '').trim();
        if (!id) continue;
        const name = String(agent?.name || id).trim();

        const label = document.createElement('label');
        label.style.display = 'flex';
        label.style.alignItems = 'center';
        label.style.gap = '6px';
        label.style.fontSize = '10px';
        label.style.cursor = 'pointer';
        label.style.padding = '2px 4px';
        label.style.borderRadius = '3px';
        label.style.transition = 'background 0.2s';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = id;
        checkbox.checked = true;
        checkbox.style.cursor = 'pointer';

        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(name && name !== id ? `${name} (${id})` : id));

        label.addEventListener('mouseover', () => {
          label.style.background = 'rgba(255,255,255,0.05)';
        });
        label.addEventListener('mouseout', () => {
          label.style.background = '';
        });

        checkboxes.appendChild(label);
      }
    }

    window.selectedAgents = () => {
      const nodes = checkboxes.querySelectorAll('input[type="checkbox"]');
      return Array.from(nodes)
        .filter((node) => node.checked)
        .map((node) => node.value);
    };
  }
}

function _buildTaskPrompt(lab) {
  const parts = [];
  if (lab.task)  parts.push(lab.task);
  if (lab.label && lab.label !== lab.task) parts.push(`(${lab.label})`);
  return parts.join(' — ') || 'Execute task';
}

// ── Bubble colours by event category ─────────────────────────────
const BUBBLE_COLORS = {
  task_running:   '#00d4ff',
  task_progress:  '#ffd700',
  task_output:    '#69f0ae',
  task_completed: '#4ade80',
  task_failed:    '#ff5252',
  task_queued:    '#60A5FA',
  model_update:   '#c084fc',
  default:        '#a0a0a0',
};

// ── Agent id pool (cycles through configured agents) ──────────────
let _agentPool = ['nemotron', 'max', 'gwen', 'toolcaller'];
let _agentPoolIdx = 0;

function _nextAgentId() {
  const id = _agentPool[_agentPoolIdx % _agentPool.length];
  _agentPoolIdx++;
  return id;
}

function _agentForTask(taskPayload) {
  // If the event carries an explicit agent_id, use it; otherwise pick from pool
  const raw = taskPayload?.agent_id || '';
  if (raw && _agentPool.includes(raw)) return raw;
  // Try to infer from model_id
  const m = (taskPayload?.model_id || '').toLowerCase();
  if (m.includes('gwen') || m.includes('code')) return 'gwen';
  if (m.includes('max') || m.includes('writ')) return 'max';
  if (m.includes('nemotron') || m.includes('research')) return 'nemotron';
  return _nextAgentId();
}

// ── Node id lookup — maps OpenClaw task_id / node_id to graph node ─
function _findNode(payload) {
  const candidates = [payload?.node_id, payload?.task_id];
  for (const id of candidates) {
    if (!id) continue;
    const n = graph.nodes.find(n => String(n.id) === String(id) || String(n.nodeId) === String(id));
    if (n) return n;
  }
  return null;
}

// ── Event handlers ────────────────────────────────────────────────

function onFlowCreated(payload) {
  // A brand-new OpenClaw flow was created — re-initialize nodes from manifest
  addLogEntry(`⚡ OpenClaw flow created: ${payload.flow_id}`, 'log-cyan');
  initNodes();
}

function onTaskQueued(payload) {
  addLogEntry(`📋 Queued: ${payload.task_id}`, 'log-yellow');
}

function onTaskRunning(payload) {
  const node = _findNode(payload);
  const agentId = _agentForTask(payload);

  if (node) setNodeActive(node.id);
  forceLoadAgent(agentId);
  if (node) sendAgentToNode(agentId, node.id);

  const label = node?.label || payload.task_id || 'task';
  addLogEntry(`▶ Running: ${label} (${agentId})`, 'log-cyan');
  addChatBubble(agentId, `Running: ${label}`, BUBBLE_COLORS.task_running, 3500);
}

function onTaskProgress(payload) {
  const agentId = _agentForTask(payload);
  const msg = payload.message || '';
  const pct = payload.percent != null ? ` ${payload.percent}%` : '';
  const node = _findNode(payload);
  const label = node?.label || payload.task_id || '';

  addLogEntry(`◦ Progress${label ? ' [' + label + ']' : ''}:${pct} ${msg}`, 'log-gold');
  if (msg) addChatBubble(agentId, msg.slice(0, 120), BUBBLE_COLORS.task_progress, 3000);
}

function onTaskOutput(payload) {
  const agentId = _agentForTask(payload);
  const paths = (payload.artifact_paths || []).join(', ');
  const node = _findNode(payload);
  const label = node?.label || payload.task_id || 'task';

  addLogEntry(`📄 Output: ${label} → ${paths || '(files)'}`, 'log-green');
  if (paths) addChatBubble(agentId, `Output: ${paths.slice(0, 80)}`, BUBBLE_COLORS.task_output, 4000);

  // Ask the existing file-tree refresh to reload
  _refreshFileTree();
}

function onTaskFailed(payload) {
  const node = _findNode(payload);
  if (node) {
    // Mark as error via DOM (we don't have a dedicated setNodeFailed export)
    const stepEl = document.querySelector(`[data-node-id="${node.id}"]`);
    if (stepEl) stepEl.classList.add('error');
  }
  const agentId = _agentForTask(payload);
  const label = node?.label || payload.task_id || 'task';
  const err = (payload.error || 'unknown error').slice(0, 120);

  addLogEntry(`✗ Failed: ${label} — ${err}`, 'log-pink');
  addChatBubble(agentId, `Failed: ${err}`, BUBBLE_COLORS.task_failed, 5000);
}

function onTaskCompleted(payload) {
  const node = _findNode(payload);
  if (node) setNodeCompleted(node.id);

  const agentId = _agentForTask(payload);
  const label = node?.label || payload.task_id || 'task';
  const paths = (payload.artifact_paths || []).join(', ');

  addLogEntry(`✓ Completed: ${label}${paths ? ' → ' + paths : ''}`, 'log-green');
  addChatBubble(agentId, `Done: ${label}`, BUBBLE_COLORS.task_completed, 4000);

  _refreshFileTree();
}

function onFlowPaused(payload) {
  addLogEntry(`⏸ Flow paused: ${payload.reason || ''}`, 'log-yellow');
}

function onFlowResumed() {
  addLogEntry('▶ Flow resumed', 'log-cyan');
}

function onFlowCancelled(payload) {
  addLogEntry(`⏹ Flow cancelled: ${payload.reason || ''}`, 'log-pink');
}

function onResourceUpdate(payload) {
  // Live VRAM/RAM from OpenClaw runtime — overrides hardcoded 12.0 GB default
  if (payload.vram_used_gb != null) {
    vram.used = parseFloat(payload.vram_used_gb) || 0;
  }
  if (payload.ram_used_gb != null) {
    // Expose on vram object so ui.js can display it if desired
    vram.ramUsedGb = parseFloat(payload.ram_used_gb) || 0;
  }
  // Update DOM elements directly for the VRAM panel
  const usedEl  = document.getElementById('vramUsed');
  const totalEl = document.getElementById('vramTotal');
  const barEl   = document.getElementById('vramBar');
  const textEl  = document.getElementById('vramText');

  const total   = parseFloat(payload.vram_total_gb ?? vram.total) || 12.0;
  const used    = vram.used;
  const pct     = Math.min(100, Math.round((used / total) * 100));

  if (usedEl)  usedEl.textContent  = `${used.toFixed(1)} GB`;
  if (totalEl) totalEl.textContent = `${total.toFixed(1)} GB`;
  if (barEl)   barEl.style.width   = `${pct}%`;
  if (textEl)  textEl.textContent  = `${pct}%`;
}

function onModelUpdate(payload) {
  const statusEmoji = { loaded: '⬆', unloaded: '⬇', switch: '↔', fallback: '⚠' }[payload.status] || '◦';
  addLogEntry(`${statusEmoji} Model: ${payload.model_id} — ${payload.status}`, 'log-purple');
}

function onFileUpdate() {
  _refreshFileTree();
}

function onFlowCompleted(payload) {
  addLogEntry(`🏁 Flow completed: ${payload.flow_id}`, 'log-green');
  _refreshFileTree();
  // Reload manifest to pick up any newly produced artifacts
  if (project.id) reloadManifest(project.id).catch(() => {});
}

// ── Internal helpers ──────────────────────────────────────────────

function _refreshFileTree() {
  // Trigger the file-tree reload already wired in workflow.js
  try {
    const pid = project.id;
    if (!pid) return;
    // Dispatch a synthetic file-tree-update event that ui.js listens to
    window.dispatchEvent(new CustomEvent('openclaw:file-tree-refresh', { detail: { projectId: pid } }));
  } catch (_) {}
}

// ── Bootstrap ─────────────────────────────────────────────────────

/**
 * Wire all OpenClaw event subscriptions.
 * Call this once from main.js after the project manifest is loaded.
 *
 * @param {string[]} [agentPool]  — override default agent id pool
 */
export function initOpenClawBridge(agentPool) {
  if (agentPool && agentPool.length) _agentPool = agentPool;

  // ── Real OpenClaw events ──────────────────────────────────────────
  // Session-level chat events are handled per-node via subscribeSession()
  // (registered by registerNodeSession(), called from main.js after manifest load)

  // ── Shared resource / model / file events (still dispatched by OpenClaw) ──
  subscribe('resource_update', onResourceUpdate);
  subscribe('model_update',    onModelUpdate);
  subscribe('file_update',     onFileUpdate);
  subscribe('flow_completed',  onFlowCompleted);

  subscribe('_client_connected', (payload) => {
    addLogEntry(`🔗 OpenClaw connected: ${payload.gateway}`, 'log-cyan');
  });

  addLogEntry('⚙ OpenClaw bridge initialised', 'log-purple');
}

/**
 * Returns whether the bridge is currently connected.
 */
export function isBridgeConnected() {
  return getConnectionState() === 'connected';
}

/**
 * Expose connect/disconnect so UI controls can call them.
 */
export { connect as openClawConnect, disconnect as openClawDisconnect };
