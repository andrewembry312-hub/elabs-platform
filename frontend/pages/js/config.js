// ═══════════════════════════════════════════════════════════════════
// CONFIG — Data-driven from API manifest, with sensible defaults
// ═══════════════════════════════════════════════════════════════════

// ── Static defaults (used if no manifest loaded) ─────────────────

export let LABS = [];
export let AGENTS = [];
export let WORKFLOW_STEPS = [];

export const SWARM_MODEL = {
  id: 'swarm-4b',
  name: '4B Swarm',
  model: 'qwen3:4b',
  vram: 2.5,
  color: '#44ddaa',
  maxCount: 12,
  emoji: '🌿',
  note: 'qwen3:4b — thinking disabled for parallel speed',
};

export let TOTAL_VRAM = 12.0;  // Default fallback; will be overridden by detected value
export let SYSTEM_RAM_TOTAL = 0.0;  // System RAM total
export const NODE_GRAPH_RADIUS = 500;

export async function initializeHardwareFromAPI() {
  try {
    const hw = await fetch('/api/hardware').then(r => r.json());
    TOTAL_VRAM = hw.gpu_vram_total_gb || 12.0;
    SYSTEM_RAM_TOTAL = hw.system_ram_total_gb || 0.0;
    console.log(`✓ Hardware initialized: GPU=${TOTAL_VRAM}GB, RAM=${SYSTEM_RAM_TOTAL}GB`);
  } catch (e) {
    console.warn('Hardware detection failed, using defaults:', e.message);
  }
}

export const CANVAS_CONFIG = {
  gridSize: 50,
  gridColor: 'rgba(0,212,255,0.08)',
  panSensitivity: 1.0,
  zoomSensitivity: 0.1,
  minZoom: 0.3,
  maxZoom: 2.5,
};

// ── Muscle → visual mapping ──────────────────────────────────────

const MUSCLE_VISUALS = {
  MAX:        { color: '#A78BFA', emoji: '🟣', vram: 0 },
  GWEN:       { color: '#34D399', emoji: '🟢', vram: 0 },
  NEMOTRON:   { color: '#60A5FA', emoji: '🔬', vram: 0 },
  TOOLCALLER: { color: '#F59E0B', emoji: '🟡', vram: 0 },
  ARTIFACT:   { color: '#F472B6', emoji: '🏗️', vram: 0 },
  FLEET:      { color: '#38BDF8', emoji: '🚀', vram: 0 },
};

// ── Load from API manifest ───────────────────────────────────────

let _manifest = null;

export function getManifest() { return _manifest; }

export function loadFromManifest(manifest) {
  if (!manifest) {
    LABS.splice(0, LABS.length);
    AGENTS.splice(0, AGENTS.length);
    WORKFLOW_STEPS.splice(0, WORKFLOW_STEPS.length);
    return { LABS, AGENTS, WORKFLOW_STEPS };
  }
  _manifest = manifest;

  // Build LABS from manifest nodes — mutate in place, don't reassign
  LABS.splice(0, LABS.length);
  const newLabs = (manifest.nodes || []).map((node, i) => ({
    id: `node-${node.id}`,
    nodeId: node.id,
    taskId: node.task_id || `t${node.id}`,
    label: node.task || node.label || `Node ${node.id}`,
    color: (MUSCLE_VISUALS[node.muscle] || MUSCLE_VISUALS.NEMOTRON).color,
    icon: node.icon || '⚙️',
    muscle: node.muscle,
    task: node.task,
    depends_on: node.depends_on || [],
    tier: node.tier || 'fast',
    status: node.status === 'complete' ? 'done' : (node.status || 'pending'),
    result: node.result,
    output_file: node.output_file,
    model_used: node.model_used || null,
    tokens_used: node.tokens_used || 0,
    started_at: node.started_at || null,
    completed_at: node.completed_at || null,
    node_type: 'root',
    parent_id: null,
    framework: (node.runtime_decision && node.runtime_decision.framework) || node.agent_framework || 'standard_local',
    framework_model: (node.runtime_decision && node.runtime_decision.model) || node.model || null,
    uses_agent_tech: (node.runtime_decision && node.runtime_decision.uses_agent_tech) || false,
  }));
  LABS.push(...newLabs);

  // Store execution mode
  _manifest.execution_mode = manifest.execution_mode || 'pipeline';

  // Build AGENTS — one per node so every node gets its own sprite
  const _MUSCLE_ROLE = {
    'MAX': 'Writer', 'GWEN': 'Coder', 'NEMOTRON': 'Researcher',
    'TOOLCALLER': 'Tool Agent', 'ARTIFACT': 'Builder', 'FLEET': 'Cloud Agent',
    'SWIFT': 'Analyst',
  };
  AGENTS.splice(0, AGENTS.length);
  for (const node of (manifest.nodes || [])) {
    const m = (node.muscle && node.muscle !== 'NONE') ? node.muscle : 'NEMOTRON';
    const role = _MUSCLE_ROLE[m.toUpperCase()] || m;
    const displayName = (node.muscle && node.muscle !== 'NONE') ? role : (node.task_id || 'bash');
    const vis = MUSCLE_VISUALS[m] || MUSCLE_VISUALS.NEMOTRON;
    AGENTS.push({
      id: `agent-${node.id}`,       // unique per node
      name: displayName,             // display name (role label for muscle, task_id for bash)
      model: m.toLowerCase(),
      vram: vis.vram,
      color: vis.color,
      emoji: vis.emoji,
      nodeId: `node-${node.id}`,     // which node this agent serves
    });
  }

  // Build WORKFLOW_STEPS: one step per node — mutate in place
  WORKFLOW_STEPS.splice(0, WORKFLOW_STEPS.length);
  const newSteps = LABS.map((lab, i) => ({
    lab: i,
    agents: [lab.muscle.toLowerCase()],
    desc: lab.task || lab.label,
    duration: 5000,
    swarmCount: 0,
    unload: [],
  }));
  WORKFLOW_STEPS.push(...newSteps);

  return { LABS, AGENTS, WORKFLOW_STEPS };
}

// ── OpenClaw agent scope mapping ─────────────────────────────────
// Maps THE MACHINE muscle names to OpenClaw agent IDs

export const MUSCLE_TO_OC_AGENT = {
  GWEN:       'gwen',
  MAX:        'max',
  NEMOTRON:   'nemotron',
  TOOLCALLER: 'toolcaller',
  ARTIFACT:   'artifact',
  FLEET:      'fleet',
};

// ── Detected runtime state (populated by openclaw-visual-bridge) ─

export let OC_DETECTED_MODELS = [];
export let OC_SYSTEM_INFO = {};

export function setDetectedModels(m) {
  OC_DETECTED_MODELS.splice(0, OC_DETECTED_MODELS.length, ...m);
}
export function setSystemInfo(s) {
  Object.assign(OC_SYSTEM_INFO, s);
}
