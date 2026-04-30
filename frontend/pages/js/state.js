// ═══════════════════════════════════════════════════════════════════
// STATE — Global application state (deferred initialization)
// ═══════════════════════════════════════════════════════════════════

import { AGENTS, SWARM_MODEL } from './config.js';

// Workflow playback
export const playback = {
  isPlaying: false,
  isPaused: false,
  currentStep: -1,
  speedMultiplier: 1.0,
  stepTimeout: null,
  timerStart: 0,
  timerElapsed: 0,
};

// Canvas viewport
export const viewport = {
  width: 0,
  height: 0,
  panX: 0,
  panY: 0,
  zoom: 1.0,
};

// Node graph state
export const graph = {
  nodes: [],
  subNodes: [],
  taskNodes: [],   // Branching task child nodes (spawned per completed task/agent step)
  coreX: 0,
  coreY: 0,
  travels: [], // Agent travel routes: [{agentId, fromNodeId, toNodeId, startTime, duration, path}]
};

// Agent state — built lazily after manifest loads
export const agents = {};

// Core system muscles that are always available via direct Ollama routing
// (even without an OpenClaw gateway connection)
const KNOWN_SYSTEM_MODELS = new Set(['gwen', 'max', 'nemotron', 'toolcaller', 'artifact', 'fleet']);

// Initialize agent state objects from current AGENTS array
export function initAgentStates() {
  // Clear existing
  Object.keys(agents).forEach(k => delete agents[k]);
  
  AGENTS.forEach(a => {
    // Agents with a known system muscle are available by default via direct routing.
    // The OpenClaw bridge will override this if OC connects and provides agent info.
    const isSystemMuscle = KNOWN_SYSTEM_MODELS.has((a.model || '').toLowerCase());
    agents[a.id] = {
      agent: a,
      state: 'idle',
      phase: 'unloaded',
      currentLab: null,
      x: 0,
      y: 0,
      fromX: 0,
      fromY: 0,
      toX: 0,
      toY: 0,
      travelProgress: 1.0,
      travelDir: null,
      trail: [],
      bobPhase: Math.random() * Math.PI * 2,
      loaded: false,
      vramInUse: 0,
      queuedForLoad: false,
      runtimeAvailable: isSystemMuscle,
      runtimeAgentId: isSystemMuscle ? (a.model || null) : null,
      workDurationMs: 0,
    };
  });
}

// 1B swarm agents
export const swarm = {
  units: [],
  nextId: 0,
};

// Chat bubbles floating above agents
export const chatBubbles = {
  bubbles: [],  // [{id, agentId, text, x, y, startTime, duration, color}]
  nextId: 0,
};

// VRAM + RAM tracking
export const vram = {
  total: 12.0,
  used: 0.0,
  reserved: 0.0,
  loadingQueue: [],
  modelsLoaded: [],
  swarmLoaded: 0,
  ramTotalGb: 0.0,
  ramUsedGb: 0.0,
};

// Logging
export const log = {
  entries: [],
};

// Debug event log — ring buffer for downloadable diagnostics
export const debugLog = {
  events: [],   // [{ts, type, agentId, data}]
  maxEvents: 500,
};

export function logDebugEvent(type, agentId, data) {
  debugLog.events.push({
    ts: new Date().toISOString(),
    type,
    agentId: agentId || null,
    data: typeof data === 'string' ? data : JSON.stringify(data),
  });
  if (debugLog.events.length > debugLog.maxEvents) {
    debugLog.events.splice(0, debugLog.events.length - debugLog.maxEvents);
  }
}

// Pipeline particles
export const pipeline = {
  particles: [],
};

// Input state
export const input = {
  mouseDown: false,
  mouseX: 0,
  mouseY: 0,
  dragStartX: 0,
  dragStartY: 0,
  isDragging: false,
  dragTarget: null,
};

// Project metadata
export const project = {
  id: null,
  prompt: null,
  linked_dir: null,
  fileTree: null,
  status: null,
};

export function updateVramUsage() {
  // Real VRAM is polled from backend via _pollRealVram() in ui.js
  // Only update swarm count for display purposes (don't overwrite vram.used)
  vram.swarmLoaded = swarm.units.filter(u => u.active).length;
}

export function addLogEntry(msg, cls = '') {
  log.entries.push({ msg, cls, time: new Date() });
  if (log.entries.length > 100) log.entries.shift();
}

export function addChatBubble(agentId, text, color, duration = 4000) {
  const now = Date.now();
  // Deduplicate: skip if same agent already has this exact text active within last 2s
  const isDup = chatBubbles.bubbles.some(
    b => b.agentId === agentId && b.text === text && (now - b.startTime) < 2000
  );
  if (isDup) return null;
  const id = chatBubbles.nextId++;
  chatBubbles.bubbles.push({ id, agentId, text, color, startTime: now, duration });
  if (chatBubbles.bubbles.length > 30) chatBubbles.bubbles.shift();
  return id;
}
