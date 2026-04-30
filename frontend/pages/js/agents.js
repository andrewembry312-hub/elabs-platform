// ═══════════════════════════════════════════════════════════════════
// AGENTS — VRAM-aware lifecycle management with orb rendering
// ═══════════════════════════════════════════════════════════════════

import { agents, graph, swarm, vram, addLogEntry, updateVramUsage, viewport, chatBubbles, addChatBubble } from './state.js?v=20260418';
import { AGENTS, TOTAL_VRAM, SWARM_MODEL } from './config.js';

// ─── VRAM Management ─────────────────────────────────────

/**
 * Check if an agent model can be loaded into VRAM
 * Returns true if there's space now, false if queue is needed
 */
export function canLoadAgent(agentId) {
  const agentState = agents[agentId];
  if (!agentState || agentState.loaded) return false;

  const needed = agentState.agent.vram;
  const available = vram.total - vram.used - vram.reserved;
  return available >= needed;
}

/**
 * Request agent to load into VRAM
 * If full, adds to queue; if space available, loads immediately
 */
export function requestLoadAgent(agentId) {
  const agentState = agents[agentId];
  if (!agentState || agentState.loaded || agentState.queuedForLoad) return;

  if (canLoadAgent(agentId)) {
    _loadAgentImmediate(agentId);
  } else {
    agentState.queuedForLoad = true;
    vram.loadingQueue.push(agentId);
    agentState.phase = 'loading';
    addLogEntry(`📋 ${agentState.agent.name} queued (needs ${(agentState.agent.vram)}GB)`, 'log-yellow');
  }
}

/**
 * Internal: load agent into VRAM immediately
 */
function _loadAgentImmediate(agentId) {
  const agentState = agents[agentId];
  if (!agentState) return;

  agentState.loaded = true;
  agentState.vramInUse = 0; // Real VRAM tracked via /api/vram polling
  agentState.phase = 'loaded';
  agentState.queuedForLoad = false;
  // Don't accumulate fake VRAM — real usage comes from backend polling
  if (!vram.modelsLoaded.includes(agentState.agent.name)) {
    vram.modelsLoaded.push(agentState.agent.name);
  }

  addLogEntry(`⬆ ${agentState.agent.name} ready`, 'log-purple');
}

/**
 * Force-load agent regardless of VRAM (for swarm visualization)
 */
export function forceLoadAgent(agentId) {
  const agentState = agents[agentId];
  if (!agentState || agentState.loaded) return;
  _loadAgentImmediate(agentId);
}

/**
 * Process loading queue — drain as VRAM becomes available
 */
export function processLoadingQueue() {
  while (vram.loadingQueue.length > 0) {
    const agentId = vram.loadingQueue[0];
    if (canLoadAgent(agentId)) {
      vram.loadingQueue.shift();
      _loadAgentImmediate(agentId);
    } else {
      break;
    }
  }
}

/**
 * Unload agent from VRAM
 */
export function unloadAgent(agentId) {
  const agentState = agents[agentId];
  if (!agentState || !agentState.loaded) return;

  if (agentState.phase === 'executing') {
    _returnAgentToCore(agentId);
  }

  agentState.loaded = false;
  agentState.vramInUse = 0;
  agentState.phase = 'unloaded';
  vram.used -= agentState.agent.vram;
  const idx = vram.modelsLoaded.indexOf(agentState.agent.name);
  if (idx >= 0) vram.modelsLoaded.splice(idx, 1);

  addLogEntry(`⬇ ${agentState.agent.name} unloaded (freed ${agentState.agent.vram}GB)`, 'log-pink');

  processLoadingQueue();
}

// ─── Agent Lifecycle ─────────────────────────────────────

export function sendAgentToNode(agentId, nodeId) {
  const agentState = agents[agentId];
  const node = graph.nodes.find(n => n.id === nodeId)
            || graph.subNodes.find(n => n.id === nodeId)
            || graph.taskNodes.find(n => n.id === nodeId);

  if (!agentState || !node || !agentState.loaded) return;

  agentState.fromX = agentState.x || graph.coreX;
  agentState.fromY = agentState.y || graph.coreY;
  agentState.toX = node.x;
  agentState.toY = node.y;
  agentState.travelProgress = 0;
  agentState.travelDir = 'executing';
  agentState.currentLab = nodeId;
  agentState.trail = [];
  agentState.phase = 'executing';

  node.agentCount = (node.agentCount || 0) + 1;

  addLogEntry(`→ ${agentState.agent.name} executing at ${node.label}`, 'log-green');
}

function _returnAgentToCore(agentId) {
  const agentState = agents[agentId];
  if (!agentState) return;

  const node = graph.nodes.find(n => n.id === agentState.currentLab)
             || graph.subNodes.find(n => n.id === agentState.currentLab);

  agentState.fromX = agentState.x;
  agentState.fromY = agentState.y;
  agentState.toX = graph.coreX;
  agentState.toY = graph.coreY;
  agentState.travelProgress = 0;
  agentState.travelDir = 'returning';
  agentState.trail = [];
  agentState.phase = 'returning';

  if (node) node.agentCount = Math.max(0, (node.agentCount || 1) - 1);

  addLogEntry(`↩ ${agentState.agent.name} returning to core`, 'log-cyan');
}

export function returnAllAgents() {
  Object.keys(agents).forEach(aid => {
    if (agents[aid].phase === 'executing') {
      _returnAgentToCore(aid);
    }
  });
}

// ─── Swarm ──────────────────────────────────────────────

/**
 * Spawn N swarm units as individual orbs
 * Each orb color matches the assigned agent model color
 */
export function spawnSwarm(count, targetNodeId, assignedAgentId) {
  const node = graph.nodes.find(n => n.id === targetNodeId);
  if (!node) return;

  const currentActive = swarm.units.filter(u => u.active).length;
  const toSpawn = Math.min(count, SWARM_MODEL.maxCount - currentActive);

  let color = SWARM_MODEL.color;
  if (assignedAgentId && agents[assignedAgentId]) {
    color = agents[assignedAgentId].agent.color;
  }

  for (let i = 0; i < toSpawn; i++) {
    const id = swarm.nextId++;
    const angle = Math.random() * Math.PI * 2;
    const orbitR = 20 + Math.random() * 15;

    swarm.units.push({
      id,
      x: graph.coreX + Math.cos(angle) * orbitR,
      y: graph.coreY + Math.sin(angle) * orbitR,
      fromX: graph.coreX,
      fromY: graph.coreY,
      toX: node.x + (Math.random() - 0.5) * 40,
      toY: node.y + (Math.random() - 0.5) * 40,
      progress: 0,
      speed: 0.4 + Math.random() * 0.3,
      targetNode: targetNodeId,
      color: color,
      phase: Math.random() * Math.PI * 2,
      active: true,
      returning: false,
      modelId: assignedAgentId,
    });
  }

  if (toSpawn > 0) {
    const newTotal = currentActive + toSpawn;
    const isSwarmMode = newTotal >= 5;
    const modeLabel = isSwarmMode ? ' [SWARM MODE]' : '';
    addLogEntry(`${SWARM_MODEL.emoji} ${toSpawn}× ${SWARM_MODEL.name} → ${node.label}${modeLabel}`, 'log-cyan');
  }
  updateVramUsage();
}

export function despawnSwarm(count) {
  let removed = 0;
  for (let i = swarm.units.length - 1; i >= 0 && removed < count; i--) {
    if (swarm.units[i].active && !swarm.units[i].returning) {
      swarm.units[i].returning = true;
      swarm.units[i].fromX = swarm.units[i].x;
      swarm.units[i].fromY = swarm.units[i].y;
      swarm.units[i].toX = graph.coreX + (Math.random() - 0.5) * 30;
      swarm.units[i].toY = graph.coreY + (Math.random() - 0.5) * 30;
      swarm.units[i].progress = 0;
      removed++;
    }
  }
  if (removed > 0) {
    const freedVram = (removed * SWARM_MODEL.vram).toFixed(2);
    addLogEntry(`↓ Recalling ${removed}× Bonsai (freed ${freedVram}GB)`, 'log-orange');
  }
}

export function despawnAllSwarm() {
  for (const u of swarm.units) {
    if (u.active) {
      u.returning = true;
      u.fromX = u.x;
      u.fromY = u.y;
      u.toX = graph.coreX + (Math.random() - 0.5) * 30;
      u.toY = graph.coreY + (Math.random() - 0.5) * 30;
      u.progress = 0;
    }
  }
}

// ─── Sub-node spawning (hex-grid snapped around parent) ──────

let subNodeId = 0;

/**
 * Hex-grid offset positions around a parent node.
 * Ring 1: 6 positions (flat-top hexagon neighbors)
 * Ring 2: 12 positions (second ring)
 * Each entry is [dx, dy] in world units.
 */
const HEX_CHILD_DIST = 100; // distance between child hex centers
const HEX_RING_1 = (() => {
  const positions = [];
  for (let i = 0; i < 6; i++) {
    const angle = (i / 6) * Math.PI * 2 - Math.PI / 6; // flat-top orientation
    positions.push([
      Math.cos(angle) * HEX_CHILD_DIST,
      Math.sin(angle) * HEX_CHILD_DIST,
    ]);
  }
  return positions;
})();

const HEX_RING_2 = (() => {
  const positions = [];
  for (let i = 0; i < 12; i++) {
    const angle = (i / 12) * Math.PI * 2 - Math.PI / 6;
    positions.push([
      Math.cos(angle) * HEX_CHILD_DIST * 1.85,
      Math.sin(angle) * HEX_CHILD_DIST * 1.85,
    ]);
  }
  return positions;
})();

function getHexSlot(index) {
  if (index < HEX_RING_1.length) return HEX_RING_1[index];
  const ring2Idx = index - HEX_RING_1.length;
  if (ring2Idx < HEX_RING_2.length) return HEX_RING_2[ring2Idx];
  // Fallback: spiral outward
  const angle = (index / 6) * Math.PI * 2;
  const dist = HEX_CHILD_DIST * (2.5 + Math.floor(index / 12) * 0.8);
  return [Math.cos(angle) * dist, Math.sin(angle) * dist];
}

export function spawnSubNode(parentNodeId, label, color, glowType = 'create') {
  const parent = graph.nodes.find(n => n.id === parentNodeId);
  if (!parent) return null;

  // Check if a child with this label already exists under this parent
  const existing = graph.subNodes.find(
    n => n.parentId === parentNodeId && n.label === label
  );
  if (existing) {
    // Just re-glow the existing node
    existing.glowType = glowType;
    existing.glowStart = Date.now();
    return existing;
  }

  const id = `sub-${subNodeId++}`;

  // Count existing children of this parent to find next hex slot
  const siblingCount = graph.subNodes.filter(n => n.parentId === parentNodeId).length;
  const [offsetX, offsetY] = getHexSlot(siblingCount);

  const subNode = {
    id,
    label: label || 'file',
    x: parent.x + offsetX,
    y: parent.y + offsetY,
    offsetX,          // offset from parent center (for drag-with-parent)
    offsetY,
    color: color || parent.color,
    parentId: parentNodeId,
    active: false,
    completed: false,
    agentCount: 0,
    isSubNode: true,
    spawnTime: Date.now(),
    glowType,         // 'create' | 'modify' | 'delete' | null
    glowStart: Date.now(),
  };

  graph.subNodes.push(subNode);
  addLogEntry(`◇ "${label}" spawned near ${parent.label}`, 'log-gold');
  return subNode;
}

export function removeSubNode(subNodeId) {
  const idx = graph.subNodes.findIndex(n => n.id === subNodeId);
  if (idx >= 0) {
    graph.subNodes[idx].completed = true;
    // Fade-out handled in render; remove after delay
    setTimeout(() => {
      const i = graph.subNodes.findIndex(n => n.id === subNodeId);
      if (i >= 0) graph.subNodes.splice(i, 1);
    }, 1500);
  }
}

// ─── Task Node Spawning (branching tree pattern) ─────────

let taskNodeId = 0;

/**
 * getTreeSlot — compute branch position for a task child node.
 * Creates organic branching pattern (like the reference images):
 * - First few children fan outward from parent at varying angles
 * - Additional children branch further out in a fractal pattern
 * - Slight randomness for natural appearance
 */
function getTreeSlot(siblingIndex, siblingCount, parentAngle) {
  // Fan angle range grows with child count, max 240 degrees
  const fanSpread = Math.min(Math.PI * 1.33, Math.PI * 0.5 + siblingCount * 0.18);
  const startAngle = parentAngle - fanSpread / 2;
  const angleStep = siblingCount <= 1 ? 0 : fanSpread / (siblingCount - 1);
  const angle = startAngle + siblingIndex * angleStep;
  
  // Distance increases for deeper branches (3x extended for visual clarity)
  const ring = Math.floor(siblingIndex / 6);
  const dist = 270 + ring * 195 + (Math.sin(siblingIndex * 2.7) * 25);
  
  return {
    x: Math.cos(angle) * dist,
    y: Math.sin(angle) * dist,
    angle,
  };
}

/**
 * spawnTaskNode — Spawn a branching child node for a completed task/agent step.
 * These branch organically off parent nodes (like dendrites).
 * 
 * @param {string} parentNodeId — parent root node id
 * @param {string} label — task/agent step name (e.g. "architect", "s_exec_1")
 * @param {string} color — node color
 * @param {string} agentName — agent name that completed this task
 * @param {string} status — 'active' | 'completed' | 'failed'
 */
export function spawnTaskNode(parentNodeId, label, color, agentName = '', status = 'active') {
  const parent = graph.nodes.find(n => n.id === parentNodeId)
              || graph.taskNodes.find(n => n.id === parentNodeId);
  if (!parent) return null;

  // Check for existing task node with same label under same parent
  const existing = graph.taskNodes.find(
    n => n.parentId === parentNodeId && n.label === label
  );
  if (existing) {
    existing.status = status;
    existing.glowStart = Date.now();
    if (status === 'completed') {
      existing.completedAt = Date.now();
    }
    return existing;
  }

  const id = `task-${taskNodeId++}`;

  // Compute angle from core to parent for branch direction
  const dx = parent.x - graph.coreX;
  const dy = parent.y - graph.coreY;
  const parentAngle = Math.atan2(dy, dx);

  // Count existing task children of this parent
  const siblings = graph.taskNodes.filter(n => n.parentId === parentNodeId);
  const siblingCount = siblings.length + 1;
  const slot = getTreeSlot(siblings.length, siblingCount, parentAngle);

  let nx = parent.x + slot.x;
  let ny = parent.y + slot.y;

  // Auto-spacer: nudge outward if overlapping any existing node
  const MIN_DIST = 80; // minimum center-to-center distance
  const allNodes = [...graph.nodes, ...graph.taskNodes, ...(graph.subNodes || [])];
  for (let attempt = 0; attempt < 8; attempt++) {
    let collision = false;
    for (const other of allNodes) {
      const ddx = nx - other.x;
      const ddy = ny - other.y;
      if (ddx * ddx + ddy * ddy < MIN_DIST * MIN_DIST) {
        collision = true;
        // Push outward along parent→node direction
        const pushAngle = Math.atan2(ny - parent.y, nx - parent.x);
        nx += Math.cos(pushAngle) * 60 + (Math.random() - 0.5) * 30;
        ny += Math.sin(pushAngle) * 60 + (Math.random() - 0.5) * 30;
        break;
      }
    }
    if (!collision) break;
  }

  const taskNode = {
    id,
    label: label || 'task',
    x: nx,
    y: ny,
    branchAngle: slot.angle,
    color: color || parent.color,
    parentId: parentNodeId,
    agentName,
    status,       // 'active' | 'completed' | 'failed'
    spawnTime: Date.now(),
    completedAt: null,
    glowStart: Date.now(),
    size: 35,     // slightly smaller than root hexagons
    children: [], // can have sub-task children for deeper branching
  };

  graph.taskNodes.push(taskNode);

  // Auto-create chat bubble if agent is active
  if (agentName && status === 'active') {
    addChatBubble(agentName, `Working on ${label}...`, color, 3000);
  }

  addLogEntry(`🌿 ${label} branched from ${parent.label || parent.id}`, 'log-gold');
  return taskNode;
}

/**
 * spawnRevisionNode — Snap a small revision indicator directly against a parent node.
 * These sit tight to the parent (close orbit) to indicate an in-place update.
 */
export function spawnRevisionNode(parentNodeId, label, color) {
  const parent = graph.nodes.find(n => n.id === parentNodeId)
              || graph.taskNodes.find(n => n.id === parentNodeId);
  if (!parent) return null;

  const revCount = graph.taskNodes.filter(
    n => n.parentId === parentNodeId && n.isRevision
  ).length;

  const id = `task-${taskNodeId++}`;
  const angle = (revCount * Math.PI * 0.35) - Math.PI / 4;
  const dist = 55 + revCount * 20; // tight orbit around parent

  const revNode = {
    id,
    label: `✏️ ${label}`,
    x: parent.x + Math.cos(angle) * dist,
    y: parent.y + Math.sin(angle) * dist,
    branchAngle: angle,
    color: color || parent.color,
    parentId: parentNodeId,
    agentName: '',
    status: 'completed',
    spawnTime: Date.now(),
    completedAt: Date.now(),
    glowStart: Date.now(),
    size: 22,     // small — just a revision pip
    children: [],
    isRevision: true,
  };

  graph.taskNodes.push(revNode);
  addLogEntry(`✏️ Revision: ${label} on ${parent.label || parent.id}`, 'log-gold');
  return revNode;
}

/**
 * completeTaskNode — Mark a task node as completed (solid glow).
 */
export function completeTaskNode(taskNodeId) {
  const node = graph.taskNodes.find(n => n.id === taskNodeId);
  if (node) {
    node.status = 'completed';
    node.completedAt = Date.now();
    node.glowStart = Date.now();
  }
}

/**
 * failTaskNode — Mark a task node as failed (red glow).
 */
export function failTaskNode(taskNodeId) {
  const node = graph.taskNodes.find(n => n.id === taskNodeId);
  if (node) {
    node.status = 'failed';
    node.glowStart = Date.now();
  }
}

// ─── Update Loop ─────────────────────────────────────────

export function updateAgents(dt, speedMult) {
  // Process loading queue
  processLoadingQueue();

  Object.keys(agents).forEach(agentId => {
    const agentState = agents[agentId];
    if (agentState.phase === 'unloaded' || !agentState.loaded) {
      agentState.x = 0;
      agentState.y = -100;
      return;
    }

    // Movement
    if (agentState.travelProgress < 1) {
      agentState.travelProgress += dt * 0.7 * speedMult;
      const et = easeInOutCubic(Math.min(1, agentState.travelProgress));

      const fromX = agentState.fromX || graph.coreX;
      const fromY = agentState.fromY || graph.coreY;
      const cpx = fromX + (agentState.toX - fromX) * 0.5;
      const cpy = fromY + (agentState.toY - fromY) * 0.5;

      agentState.x = bezier(fromX, cpx, agentState.toX, et);
      agentState.y = bezier(fromY, cpy, agentState.toY, et);

      agentState.trail.push({ x: agentState.x, y: agentState.y, a: 1 });
      if (agentState.trail.length > 20) agentState.trail.shift();

      if (agentState.travelProgress >= 1) {
        agentState.x = agentState.toX;
        agentState.y = agentState.toY;
        // When return travel completes, transition back to loaded/idle
        if (agentState.phase === 'returning') {
          agentState.phase = 'loaded';
        }
      }
    }

    for (const t of agentState.trail) t.a -= dt * 2.5;
    agentState.trail = agentState.trail.filter(t => t.a > 0);

    // Idle orbit at core (for loaded agents waiting for work)
    if (agentState.phase === 'loaded' && agentState.travelProgress >= 1) {
      agentState.bobPhase += dt * 1.2;
      const idx = Object.keys(agents).indexOf(agentId);
      const oR = 35 + (idx % 5) * 10;
      agentState.x = graph.coreX + Math.cos(agentState.bobPhase + idx * 1.3) * oR;
      agentState.y = graph.coreY + Math.sin(agentState.bobPhase + idx * 1.3) * oR;
    }
  });

  updateSwarm(dt, speedMult);

  updateVramUsage();
}

function updateSwarm(dt, speedMult) {
  for (let i = swarm.units.length - 1; i >= 0; i--) {
    const u = swarm.units[i];
    if (!u.active) continue;

    u.progress += dt * u.speed * speedMult;
    u.phase += dt * 2;

    if (u.progress < 1) {
      const et = easeInOutCubic(Math.min(1, u.progress));
      const cpx = u.fromX + (u.toX - u.fromX) * 0.5 + Math.sin(u.phase) * 15;
      const cpy = u.fromY + (u.toY - u.fromY) * 0.5 + Math.cos(u.phase) * 15;
      u.x = bezier(u.fromX, cpx, u.toX, et);
      u.y = bezier(u.fromY, cpy, u.toY, et);
    } else {
      u.x = u.toX;
      u.y = u.toY;

      if (u.returning) {
        // Reached core — remove
        u.active = false;
        swarm.units.splice(i, 1);
        continue;
      }

      // Reached node — start returning with data
      u.returning = true;
      u.fromX = u.x;
      u.fromY = u.y;
      u.toX = graph.coreX + (Math.random() - 0.5) * 30;
      u.toY = graph.coreY + (Math.random() - 0.5) * 30;
      u.progress = 0;
      u.speed = 0.3 + Math.random() * 0.25;
    }
  }
}

// ─── Drawing ─────────────────────────────────────────

export function drawAgents(ctx, time, zoom) {
  // Update agent travel paths
  updateAgentTravels(ctx, zoom);
  
  // Draw main agents as sprite characters
  const agentIds = Object.keys(agents);
  agentIds.forEach((agentId, index) => {
    const agentState = agents[agentId];
    const isStandby = !agentState.loaded && agentState.runtimeAvailable;
    if (!agentState.loaded && !isStandby) return;

    let ax = agentState.x;
    let ay = agentState.y;

    if (isStandby) {
      const angle = (Math.PI * 2 * index) / Math.max(1, agentIds.length) - Math.PI / 2;
      const radius = 115;
      ax = graph.coreX + Math.cos(angle) * radius;
      ay = graph.coreY + Math.sin(angle) * radius;
    }

    // Offset sprite above node when parked at destination (so it hovers above the hex)
    if (agentState.travelProgress >= 1 && agentState.phase === 'executing') {
      ay -= 70 / zoom;
    }

    if (isStandby) ctx.globalAlpha = 0.58;
    drawAgentOrb(ctx, ax, ay, agentState.agent.color, isStandby ? 'available' : agentState.phase, zoom, agentState);
    if (isStandby) ctx.globalAlpha = 1;
  });

  drawSwarm(ctx, time, zoom);
}

function drawAgentOrb(ctx, x, y, color, phase, zoom, agentState) {
  const time = Date.now() * 0.001;

  // Sprite + name label only — no circles, no badges, no arrows
  try {
    const spriteSize = Math.max(35, 50 / zoom);
    drawSpriteCharacter(ctx, x, y - spriteSize * 0.2, color, spriteSize, phase, time, agentState);

    // Name + ID label below sprite
    if (agentState && agentState.agent) {
      const fontSize = Math.max(9, Math.round(10 / zoom));
      ctx.font = `700 ${fontSize}px 'JetBrains Mono', monospace`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = '#ffffffdd';
      ctx.shadowColor = color;
      ctx.shadowBlur = 4;
      const label = `${agentState.agent.name}`;
      ctx.fillText(label, x, y + spriteSize * 0.45 + 2);
      ctx.shadowBlur = 0;
    }
  } catch (err) {
    console.error('[drawAgentOrb] sprite error:', err);
  }
}

function drawSwarm(ctx, time, zoom) {
  const activeCount = swarm.units.filter(u => u.active).length;
  const isSwarmMode = activeCount >= 5;

  // Mesh
  if (isSwarmMode) {
    ctx.globalAlpha = 0.06;
    for (let i = 0; i < swarm.units.length; i++) {
      if (!swarm.units[i].active) continue;
      for (let j = i + 1; j < Math.min(i + 4, swarm.units.length); j++) {
        if (!swarm.units[j].active) continue;
        const u1 = swarm.units[i];
        const u2 = swarm.units[j];
        const dx = u2.x - u1.x;
        const dy = u2.y - u1.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 160) {
          ctx.beginPath();
          ctx.moveTo(u1.x, u1.y);
          ctx.lineTo(u2.x, u2.y);
          ctx.strokeStyle = u1.color;
          ctx.lineWidth = 0.4 / zoom;
          ctx.stroke();
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  // Individual swarm orbs — visible figures
  for (const u of swarm.units) {
    if (!u.active) continue;

    const orbSize = (5 + Math.sin(time * 4 + u.phase) * 2) / zoom;

    // Outer glow
    ctx.beginPath();
    ctx.arc(u.x, u.y, orbSize * 3, 0, Math.PI * 2);
    ctx.fillStyle = `${u.color}10`;
    ctx.fill();

    // Body — small diamond shape
    ctx.beginPath();
    ctx.moveTo(u.x, u.y - orbSize * 1.3);
    ctx.lineTo(u.x + orbSize, u.y);
    ctx.lineTo(u.x, u.y + orbSize * 1.3);
    ctx.lineTo(u.x - orbSize, u.y);
    ctx.closePath();
    ctx.fillStyle = `${u.color}cc`;
    ctx.fill();
    ctx.strokeStyle = `${u.color}`;
    ctx.lineWidth = 0.8 / zoom;
    ctx.stroke();

    // Core dot
    ctx.beginPath();
    ctx.arc(u.x, u.y, orbSize * 0.4, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffffcc';
    ctx.fill();

    if (u.returning) {
      // Data payload indicator — small white trail behind
      ctx.beginPath();
      ctx.arc(u.x, u.y, orbSize * 0.6, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff88';
      ctx.fill();
    }
  }

  // Status
  if (activeCount > 0) {
    const label = isSwarmMode ? '⚡ SWARM' : '🌱 Carriers';
    ctx.font = `600 ${10 / zoom}px 'JetBrains Mono', monospace`;
    ctx.textAlign = 'center';
    ctx.fillStyle = '#88ddff';
    ctx.shadowColor = '#44ffaa';
    ctx.shadowBlur = isSwarmMode ? 8 : 0;
    ctx.fillText(`${label} (${activeCount}×)`, graph.coreX, graph.coreY + 60 / zoom);
    ctx.shadowBlur = 0;
  }
}

function bezier(p0, p1, p2, t) {
  return (1 - t) * (1 - t) * p0 + 2 * (1 - t) * t * p1 + t * t * p2;
}

// ─── Active Agent HUD (bottom-right canvas overlay) ──────

/**
 * Draws an overlay in screen space showing currently active agents.
 * Called AFTER ctx.restore() so it's not affected by pan/zoom.
 */
export function drawActiveAgentHUD(ctx, canvasW, canvasH) {
  // Collect active agents + swarm count
  const activeAgents = [];
  Object.keys(agents).forEach(aid => {
    const as = agents[aid];
    if (as.loaded && (as.phase === 'executing' || as.phase === 'returning' || as.phase === 'loaded')) {
      activeAgents.push(as);
    }
  });
  const activeSwarm = swarm.units.filter(u => u.active).length;

  if (activeAgents.length === 0 && activeSwarm === 0) return;

  const padding = 12;
  const cardH = 44;
  const cardW = 190;
  const gap = 4;
  const totalItems = activeAgents.length + (activeSwarm > 0 ? 1 : 0);
  const panelH = totalItems * (cardH + gap) + padding * 2 + 20;
  const panelW = cardW + padding * 2;
  const px = canvasW - panelW - 12;
  const py = canvasH - panelH - 12;

  // Panel background
  ctx.fillStyle = 'rgba(8, 12, 20, 0.85)';
  ctx.strokeStyle = 'rgba(0, 212, 255, 0.2)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(px, py, panelW, panelH, 6);
  ctx.fill();
  ctx.stroke();

  // Title
  ctx.font = '600 10px "JetBrains Mono", monospace';
  ctx.fillStyle = '#00d4ff';
  ctx.textAlign = 'left';
  ctx.fillText('\u26a1 ACTIVE AGENTS', px + padding, py + padding + 10);

  let yOff = py + padding + 24;

  // Agent cards
  for (const as of activeAgents) {
    const isExec = as.phase === 'executing';
    const isIdle = as.phase === 'loaded' || as.phase === 'returning';
    const color = as.agent.color;

    // Card bg
    ctx.fillStyle = isExec ? `${color}18` : 'rgba(255,255,255,0.03)';
    ctx.beginPath();
    ctx.roundRect(px + padding, yOff, cardW, cardH, 4);
    ctx.fill();

    // Status dot
    ctx.beginPath();
    ctx.arc(px + padding + 12, yOff + 14, 4, 0, Math.PI * 2);
    ctx.fillStyle = isExec ? '#00ff88' : isIdle ? '#ffaa00' : '#888';
    ctx.fill();

    // Emoji
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(as.agent.emoji || '\u25cf', px + padding + 22, yOff + 18);

    // Name
    ctx.font = '600 10px "JetBrains Mono", monospace';
    ctx.fillStyle = '#ffffffcc';
    ctx.fillText(as.agent.name, px + padding + 40, yOff + 13);

    // Status text with elapsed time
    ctx.font = '9px "JetBrains Mono", monospace';
    if (isExec) {
      const elapsed = as.taskStartedAt ? Math.round((Date.now() - as.taskStartedAt) / 1000) : 0;
      const timeStr = elapsed > 0 ? ` ${elapsed}s` : '';
      ctx.fillStyle = '#00ff88';
      ctx.fillText(`Working${timeStr}`, px + padding + 40, yOff + 25);
    } else if (isIdle) {
      ctx.fillStyle = '#ffaa00aa';
      ctx.fillText('Idle', px + padding + 40, yOff + 25);
    } else {
      ctx.fillStyle = '#888';
      ctx.fillText(as.phase === 'returning' ? 'Returning' : 'Loaded', px + padding + 40, yOff + 25);
    }

    // Task line (truncated)
    const taskText = as.currentTask || (as.lastTask ? `✓ ${as.lastTask}` : '');
    if (taskText) {
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.fillStyle = isExec ? '#00d4ffaa' : '#888';
      const truncated = taskText.length > 28 ? taskText.substring(0, 25) + '...' : taskText;
      ctx.fillText(truncated, px + padding + 40, yOff + 37);
    }

    yOff += cardH + gap;
  }

  // Swarm row
  if (activeSwarm > 0) {
    const isSwarmMode = activeSwarm >= 5;
    ctx.fillStyle = isSwarmMode ? 'rgba(68, 255, 170, 0.08)' : 'rgba(255,255,255,0.03)';
    ctx.beginPath();
    ctx.roundRect(px + padding, yOff, cardW, cardH, 4);
    ctx.fill();

    ctx.beginPath();
    ctx.arc(px + padding + 12, yOff + cardH / 2, 4, 0, Math.PI * 2);
    ctx.fillStyle = isSwarmMode ? '#44ffaa' : '#88ddff';
    ctx.fill();

    ctx.font = '14px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('\ud83c\udf31', px + padding + 22, yOff + cardH / 2 + 5);

    ctx.font = '600 10px "JetBrains Mono", monospace';
    ctx.fillStyle = '#ffffffcc';
    ctx.fillText(`1B Swarm \u00d7${activeSwarm}`, px + padding + 40, yOff + 13);

    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.fillStyle = isSwarmMode ? '#44ffaa' : '#88ddff';
    ctx.fillText(isSwarmMode ? 'SWARM MODE' : 'Active', px + padding + 40, yOff + 25);
  }
}

function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

// ─── Agent Travel System ──────────────────────────────────

/**
 * Send an agent to travel from one node to another
 * @param {string} agentId - Agent identifier
 * @param {number} fromNodeId - Source node ID
 * @param {number} toNodeId - Target node ID
 * @param {number} duration - Travel duration in ms (default 2000)
 */
export function sendAgentTravelToNode(agentId, fromNodeId, toNodeId, duration = 2000) {
  const agentState = agents[agentId];
  if (!agentState) return;
  
  const fromNode = graph.nodes.find(n => n.id === fromNodeId);
  const toNode = graph.nodes.find(n => n.id === toNodeId);
  if (!fromNode || !toNode) return;

  const travel = {
    agentId,
    fromNodeId,
    toNodeId,
    startTime: Date.now(),
    duration,
    speed: window.fleetState?.currentSpeed || 1,
    fromX: fromNode.x,
    fromY: fromNode.y,
    toX: toNode.x,
    toY: toNode.y,
  };

  graph.travels.push(travel);
  agentState.travelDir = 'to-node';
}

/**
 * Render travel paths and update agent positions along them
 */
export function updateAgentTravels(ctx, zoom) {
  const now = Date.now();
  
  for (let i = graph.travels.length - 1; i >= 0; i--) {
    const travel = graph.travels[i];
    const elapsed = now - travel.startTime;
    const adjustedDuration = travel.duration / travel.speed;
    
    if (elapsed > adjustedDuration) {
      graph.travels.splice(i, 1);
      continue;
    }
    
    const progress = easeInOutCubic(Math.min(elapsed / adjustedDuration, 1));
    const x = travel.fromX + (travel.toX - travel.fromX) * progress;
    const y = travel.fromY + (travel.toY - travel.fromY) * progress;
    
    const agentState = agents[travel.agentId];
    if (agentState) {
      agentState.x = x;
      agentState.y = y;
      agentState.travelProgress = progress;
    }
    
    // Draw travel path (dotted line)
    if (ctx) {
      ctx.strokeStyle = `${agents[travel.agentId]?.agent.color || '#00d4ff'}44`;
      ctx.lineWidth = 2 / zoom;
      ctx.setLineDash([8 / zoom, 4 / zoom]);
      ctx.beginPath();
      ctx.moveTo(travel.fromX, travel.fromY);
      ctx.lineTo(travel.toX, travel.toY);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
}

// ─── Sprite Character Drawing (robot figure for active agents) ──

/**
 * Draw a small robot sprite character on the canvas.
 * Based on the sprite-demo character.html SVG design.
 * Color-coded per agent. Shows when agent is executing.
 */
function drawSpriteCharacter(ctx, x, y, color, size, phase, time, agentState) {
  const s = size / 180; // scale factor (original SVG is 120x180 viewBox)
  const bobY = Math.sin(time * 2.5 + (agentState?.bobPhase || 0)) * 3;
  
  ctx.save();
  ctx.translate(x, y + bobY);
  ctx.scale(s, s);

  // ── Stand/base ──
  ctx.fillStyle = '#1a1a2e';
  ctx.beginPath();
  ctx.ellipse(0, 90, 22, 6, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#2a2a3e';
  ctx.fillRect(-8, 78, 16, 14);

  // ── Body (egg/vase shape) ──
  const bodyGrad = ctx.createRadialGradient(0, -20, 5, 0, -5, 55);
  bodyGrad.addColorStop(0, '#ffffff');
  bodyGrad.addColorStop(0.5, color + 'cc');
  bodyGrad.addColorStop(1, color + '88');
  
  ctx.fillStyle = bodyGrad;
  ctx.beginPath();
  ctx.moveTo(0, -6);
  ctx.bezierCurveTo(28, -6, 28, 35, 28, 40);
  ctx.bezierCurveTo(28, 60, 20, 75, 0, 78);
  ctx.bezierCurveTo(-20, 75, -28, 60, -28, 40);
  ctx.bezierCurveTo(-28, 35, -28, -6, 0, -6);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = color + 'aa';
  ctx.lineWidth = 0.8;
  ctx.stroke();

  // ── Left arm ──
  ctx.fillStyle = color + 'bb';
  ctx.beginPath();
  ctx.moveTo(-26, 20);
  ctx.bezierCurveTo(-38, 23, -44, 32, -42, 42);
  ctx.bezierCurveTo(-41, 46, -38, 48, -34, 47);
  ctx.bezierCurveTo(-30, 46, -26, 38, -24, 30);
  ctx.closePath();
  ctx.fill();

  // ── Right arm (animated wave when executing) ──
  const armWave = phase === 'executing' ? Math.sin(time * 4) * 0.2 : 0;
  ctx.save();
  ctx.rotate(armWave);
  ctx.fillStyle = color + 'bb';
  ctx.beginPath();
  ctx.moveTo(26, 20);
  ctx.bezierCurveTo(38, 23, 44, 32, 42, 42);
  ctx.bezierCurveTo(41, 46, 38, 48, 34, 47);
  ctx.bezierCurveTo(30, 46, 26, 38, 24, 30);
  ctx.closePath();
  ctx.fill();
  ctx.restore();

  // ── Head (oval) ──
  const headGrad = ctx.createRadialGradient(-5, -35, 5, 0, -30, 35);
  headGrad.addColorStop(0, '#ffffff');
  headGrad.addColorStop(0.6, color + 'dd');
  headGrad.addColorStop(1, color + '99');
  
  ctx.fillStyle = headGrad;
  ctx.beginPath();
  ctx.ellipse(0, -30, 34, 26, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = color + '88';
  ctx.lineWidth = 0.5;
  ctx.stroke();

  // ── Visor/Face screen ──
  ctx.fillStyle = '#0a0a15';
  ctx.beginPath();
  ctx.ellipse(0, -32, 25, 14, 0, 0, Math.PI * 2);
  ctx.fill();

  // ── Eyes (glowing, color-matched) ──
  const eyePulse = Math.sin(time * 3) * 0.3 + 0.7;
  
  // Left eye
  ctx.shadowColor = color;
  ctx.shadowBlur = 6 * eyePulse;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.ellipse(-12, -33, 6, 8, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ffffff88';
  ctx.beginPath();
  ctx.ellipse(-12, -35, 3, 4, 0, 0, Math.PI * 2);
  ctx.fill();

  // Right eye
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.ellipse(12, -33, 6, 8, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#ffffff88';
  ctx.beginPath();
  ctx.ellipse(12, -35, 3, 4, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;

  // ── Head highlight ──
  ctx.fillStyle = 'rgba(255,255,255,0.15)';
  ctx.save();
  ctx.translate(-12, -46);
  ctx.rotate(-0.26);
  ctx.beginPath();
  ctx.ellipse(0, 0, 14, 5, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  // ── Antenna for executing agents ──
  if (phase === 'executing') {
    const antBob = Math.sin(time * 5) * 3;
    ctx.strokeStyle = color + 'aa';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(0, -56);
    ctx.lineTo(0, -68 + antBob);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(0, -70 + antBob, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  ctx.restore();
}

// ─── Chat Bubble Drawing ─────────────────────────────────

/**
 * Draw chat bubbles above agents. Called in world-space.
 */
export function drawChatBubbles(ctx, time, zoom) {
  const now = Date.now();
  
  // Expire old bubbles
  chatBubbles.bubbles = chatBubbles.bubbles.filter(b => now - b.startTime < b.duration);

  for (const bubble of chatBubbles.bubbles) {
    // Find position: loaded agent → node position → core
    let ax = graph.coreX, ay = graph.coreY;
    const agentState = _resolveBubbleAgent(bubble.agentId);
    if (agentState && agentState.loaded) {
      ax = agentState.x;
      ay = agentState.y;
    } else if (agentState && agentState.runtimeAvailable) {
      const agentList = Object.values(agents);
      const index = Math.max(0, agentList.indexOf(agentState));
      const angle = (Math.PI * 2 * index) / Math.max(1, agentList.length) - Math.PI / 2;
      ax = graph.coreX + Math.cos(angle) * 115;
      ay = graph.coreY + Math.sin(angle) * 115;
    } else {
      // Fall back to the node this muscle is assigned to
      const key = String(bubble.agentId || '').toLowerCase();
      const node = graph.nodes.find(n => String(n.muscle || '').toLowerCase() === key || String(n.id || '').toLowerCase() === key);
      if (node) {
        ax = node.x;
        ay = node.y;
      }
    }

    const age = (now - bubble.startTime) / 1000;
    const fadeIn = Math.min(1, age * 4);
    const fadeOut = Math.max(0, 1 - (age - (bubble.duration / 1000 - 0.5)) * 2);
    const alpha = Math.min(fadeIn, fadeOut);
    if (alpha <= 0) continue;

    const floatY = -Math.min(age * 8, 40); // float upward over time
    const bx = ax;
    const by = ay - 80 / zoom + floatY / zoom;

    const text = bubble.text.length > 40 ? bubble.text.substring(0, 37) + '...' : bubble.text;
    const fontSize = Math.max(7, 9 / zoom);
    ctx.font = `500 ${fontSize}px 'JetBrains Mono', monospace`;
    const metrics = ctx.measureText(text);
    const padX = 8 / zoom;
    const padY = 5 / zoom;
    const bw = metrics.width + padX * 2;
    const bh = fontSize + padY * 2;

    ctx.save();
    ctx.globalAlpha = alpha * 0.92;

    // Bubble background
    ctx.fillStyle = 'rgba(10, 14, 28, 0.9)';
    ctx.strokeStyle = bubble.color + '66';
    ctx.lineWidth = 1 / zoom;
    ctx.beginPath();
    ctx.roundRect(bx - bw / 2, by - bh, bw, bh, 4 / zoom);
    ctx.fill();
    ctx.stroke();

    // Tail triangle pointing down to agent
    ctx.fillStyle = 'rgba(10, 14, 28, 0.9)';
    ctx.beginPath();
    ctx.moveTo(bx - 4 / zoom, by);
    ctx.lineTo(bx + 4 / zoom, by);
    ctx.lineTo(bx, by + 6 / zoom);
    ctx.closePath();
    ctx.fill();

    // Text
    ctx.fillStyle = bubble.color + 'dd';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, bx, by - bh / 2);

    ctx.restore();
  }
}

function _resolveBubbleAgent(agentId) {
  const key = String(agentId || '').toLowerCase();
  if (!key) return null;
  if (agents[agentId]) return agents[agentId];
  return Object.values(agents).find((agentState) => {
    const agent = agentState.agent || {};
    return String(agent.id || '').toLowerCase() === key
      || String(agent.name || '').toLowerCase() === key
      || String(agent.model || '').toLowerCase() === key;
  }) || null;
}

// ─── Task Node Drawing ───────────────────────────────────

/**
 * Draw all branching task nodes and their organic connections.
 */
export function drawTaskNodes(ctx, time, zoom) {
  for (const tNode of graph.taskNodes) {
    const parent = graph.nodes.find(n => n.id === tNode.parentId)
                || graph.taskNodes.find(n => n.id === tNode.parentId);
    if (!parent) continue;

    const age = (Date.now() - tNode.spawnTime) / 1000;
    const spawnScale = Math.min(1, age * 3); // grow in over 0.33s
    const isActive = tNode.status === 'active';
    const isCompleted = tNode.status === 'completed';
    const isFailed = tNode.status === 'failed';

    // ── Organic branch connection (curved line from parent) ──
    ctx.save();
    const connAlpha = isActive ? 0.5 : isCompleted ? 0.35 : 0.15;
    ctx.globalAlpha = connAlpha * spawnScale;

    // Beziér curve with slight organic wobble
    const mx = (parent.x + tNode.x) / 2;
    const my = (parent.y + tNode.y) / 2;
    const perpScale = 0.12 + Math.sin(tNode.branchAngle * 3) * 0.05;
    const perpX = -(tNode.y - parent.y) * perpScale;
    const perpY = (tNode.x - parent.x) * perpScale;

    ctx.beginPath();
    ctx.moveTo(parent.x, parent.y);
    ctx.quadraticCurveTo(mx + perpX, my + perpY, tNode.x, tNode.y);

    const connColor = isFailed ? '#ff3366' : tNode.color;
    ctx.strokeStyle = connColor;
    ctx.lineWidth = (isActive ? 2.5 : 1.5) / zoom;
    ctx.stroke();

    // Animated pulse dot traveling along branch when active
    if (isActive) {
      const pulseT = (time * 0.6) % 1;
      const px = bezier(parent.x, mx + perpX, tNode.x, pulseT);
      const py = bezier(parent.y, my + perpY, tNode.y, pulseT);
      ctx.beginPath();
      ctx.arc(px, py, 3 / zoom, 0, Math.PI * 2);
      ctx.fillStyle = connColor + 'cc';
      ctx.fill();
    }
    ctx.restore();

    // ── Node body ──
    ctx.save();
    ctx.translate(tNode.x, tNode.y);
    ctx.scale(spawnScale, spawnScale);

    const nodeSize = tNode.size;

    // Glow aura
    const glowAge = tNode.glowStart ? (Date.now() - tNode.glowStart) / 1000 : 99;
    const glowPulse = isActive ? (Math.sin(time * 4) * 0.4 + 0.6) : (glowAge < 2 ? 1 - glowAge / 2 : 0);

    if (glowPulse > 0) {
      const glowColor = isFailed ? '#ff3366' : isCompleted ? tNode.color : '#ffffff';
      const glowR = nodeSize * 0.7 + glowPulse * 12;
      const gGrad = ctx.createRadialGradient(0, 0, nodeSize * 0.2, 0, 0, glowR);
      gGrad.addColorStop(0, `${glowColor}00`);
      gGrad.addColorStop(0.5, `${glowColor}${Math.round(glowPulse * 40).toString(16).padStart(2, '0')}`);
      gGrad.addColorStop(1, `${glowColor}00`);
      ctx.beginPath();
      ctx.arc(0, 0, glowR, 0, Math.PI * 2);
      ctx.fillStyle = gGrad;
      ctx.fill();
    }

    // Hexagon body (completed = solid fill, active = pulsing outline)
    ctx.beginPath();
    for (let i = 0; i < 6; i++) {
      const a = (i / 6) * Math.PI * 2 - Math.PI / 6;
      const wobble = isActive ? Math.sin(time * 3 + i * 1.1) * 1.5 : 0;
      const hx = Math.cos(a) * (nodeSize * 0.5 + wobble);
      const hy = Math.sin(a) * (nodeSize * 0.5 + wobble);
      i === 0 ? ctx.moveTo(hx, hy) : ctx.lineTo(hx, hy);
    }
    ctx.closePath();

    if (isCompleted) {
      // Solid filled hexagon for completed tasks
      const fillGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, nodeSize * 0.5);
      fillGrad.addColorStop(0, tNode.color + 'cc');
      fillGrad.addColorStop(1, tNode.color + '66');
      ctx.fillStyle = fillGrad;
      ctx.fill();
      ctx.strokeStyle = tNode.color;
      ctx.lineWidth = 2 / zoom;
      ctx.stroke();
    } else if (isFailed) {
      ctx.fillStyle = '#ff336622';
      ctx.fill();
      ctx.strokeStyle = '#ff3366cc';
      ctx.lineWidth = 2 / zoom;
      ctx.stroke();
    } else {
      // Active: transparent with pulsing border
      ctx.fillStyle = tNode.color + '15';
      ctx.fill();
      ctx.strokeStyle = tNode.color + 'cc';
      ctx.lineWidth = 2.5 / zoom;
      ctx.stroke();

      // Spinning progress arc
      ctx.beginPath();
      ctx.arc(0, 0, nodeSize * 0.55, -Math.PI / 2, -Math.PI / 2 + (time * 1.5) % (Math.PI * 2));
      ctx.strokeStyle = tNode.color + 'aa';
      ctx.lineWidth = 2.5 / zoom;
      ctx.lineCap = 'round';
      ctx.stroke();
    }

    // Task label
    const labelSize = Math.max(6, 8 / zoom);
    ctx.font = `600 ${labelSize}px 'JetBrains Mono', monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = isCompleted ? '#ffffffee' : isFailed ? '#ff6688dd' : tNode.color + 'dd';
    const displayLabel = tNode.label.length > 14 ? tNode.label.substring(0, 11) + '...' : tNode.label;
    ctx.fillText(displayLabel, 0, 0);

    // Agent name below (smaller)
    if (tNode.agentName) {
      ctx.font = `400 ${Math.max(5, 6 / zoom)}px 'JetBrains Mono', monospace`;
      ctx.fillStyle = '#ffffff66';
      ctx.fillText(tNode.agentName, 0, nodeSize * 0.35 + 4 / zoom);
    }

    // Completed checkmark overlay
    if (isCompleted) {
      ctx.strokeStyle = '#ffffffcc';
      ctx.lineWidth = 2.5 / zoom;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(-7, 2);
      ctx.lineTo(-2, 7);
      ctx.lineTo(8, -5);
      ctx.stroke();
    }

    // Failed X overlay
    if (isFailed) {
      ctx.strokeStyle = '#ff3366cc';
      ctx.lineWidth = 2.5 / zoom;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(-6, -6);
      ctx.lineTo(6, 6);
      ctx.moveTo(6, -6);
      ctx.lineTo(-6, 6);
      ctx.stroke();
    }

    ctx.restore();
  }
}
