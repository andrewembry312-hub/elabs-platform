// ═══════════════════════════════════════════════════════════════════
// NODES — Node graph with live lab iframes (root) + simple file nodes
// ═══════════════════════════════════════════════════════════════════

import { LABS, NODE_GRAPH_RADIUS } from './config.js';
import { graph, viewport } from './state.js?v=20260418';
import { worldToScreen } from './canvas.js';

const NODE_SIZE = 120;
const IFRAME_DISPLAY_SIZE = 160;

// ── 7 artistic lab scenes to assign to root task nodes ──────────
const LAB_SCENES = [
  'the-machine-lab-3d-v2.html',
  'lab-hypothesis.html',
  'lab-implement.html',
  'lab-validation.html',
  'lab-review-revert.html',
  'lab-agent-coordination.html',
  'lab-tooling-utility.html',
  'lab-knowledge-store.html',
];

export function initNodes() {
  const centerX = 0;
  const centerY = 0;

  graph.coreX = centerX;
  graph.coreY = centerY;

  const count = LABS.length || 1;
  const radius = count <= 4 ? NODE_GRAPH_RADIUS * 0.6
               : count <= 8 ? NODE_GRAPH_RADIUS
               : NODE_GRAPH_RADIUS * (1 + (count - 8) * 0.1);

  graph.nodes = LABS.map((lab, i) => {
    const angle = (i / count) * Math.PI * 2 - Math.PI / 2;
    const x = centerX + Math.cos(angle) * radius;
    const y = centerY + Math.sin(angle) * radius;

    return {
      id: lab.id,
      nodeId: lab.nodeId,
      label: lab.label,
      x,
      y,
      color: lab.color,
      icon: lab.icon,
      muscle: lab.muscle,
      task: lab.task,
      status: lab.status || 'pending',
      active: false,
      completed: lab.status === 'done' || lab.status === 'complete',
      agentCount: 0,
      node_type: lab.node_type || 'root',
      parent_id: lab.parent_id || null,
      // Assign a lab scene (cycle through available scenes)
      src: LAB_SCENES[i % LAB_SCENES.length],
    };
  });

  // Create iframe overlays for root nodes
  createIframeLayer();
}

// ── Iframe layer for root nodes ─────────────────────────────────

function createIframeLayer() {
  let layer = document.getElementById('iframeLayer');
  if (!layer) {
    layer = document.createElement('div');
    layer.id = 'iframeLayer';
    layer.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;z-index:5;pointer-events:none';
    const mainView = document.querySelector('.main-view');
    if (mainView) mainView.appendChild(layer);
  }
  layer.innerHTML = '';

  graph.nodes.forEach(node => {
    if (node.node_type !== 'root') return;

    const wrap = document.createElement('div');
    wrap.className = 'lab-iframe-wrap';
    wrap.id = `iframe-wrap-${node.id}`;

    // Hexagonal clipping border (glow layer)
    const border = document.createElement('div');
    border.className = 'lab-iframe-border';
    border.style.width = IFRAME_DISPLAY_SIZE + 'px';
    border.style.height = IFRAME_DISPLAY_SIZE + 'px';
    border.style.background = `radial-gradient(circle, ${node.color}44 0%, ${node.color}11 70%, transparent 100%)`;

    // Clip container
    const clip = document.createElement('div');
    clip.className = 'lab-iframe-clip';
    clip.style.width = IFRAME_DISPLAY_SIZE + 'px';
    clip.style.height = IFRAME_DISPLAY_SIZE + 'px';

    // Live iframe — render lab at 600px then scale down into hexagon
    const iframe = document.createElement('iframe');
    iframe.src = node.src;
    iframe.style.width = '600px';
    iframe.style.height = '600px';
    iframe.style.transform = `scale(${IFRAME_DISPLAY_SIZE / 600})`;
    iframe.style.transformOrigin = '0 0';
    iframe.style.border = 'none';
    iframe.style.pointerEvents = 'none';
    iframe.style.display = 'block';
    iframe.sandbox = 'allow-scripts allow-same-origin';
    iframe.loading = 'lazy';

    clip.appendChild(iframe);

    // PURPOSE label (task description, never agent name)
    const label = document.createElement('div');
    label.className = 'lab-iframe-label';
    label.style.color = node.color;
    const purposeText = node.task
      ? (node.task.length > 40 ? node.task.substring(0, 37) + '...' : node.task)
      : node.label;
    label.textContent = purposeText;

    wrap.appendChild(border);
    wrap.appendChild(clip);
    wrap.appendChild(label);
    layer.appendChild(wrap);

    node._wrap = wrap;
  });
}

export function updateIframePositions() {
  graph.nodes.forEach(node => {
    if (!node._wrap) return;
    const { screenX, screenY } = worldToScreen(node.x, node.y);
    const scale = viewport.zoom;

    node._wrap.style.left = screenX + 'px';
    node._wrap.style.top = screenY + 'px';
    node._wrap.style.transform = `translate(-50%, -50%) scale(${scale})`;
    node._wrap.style.transformOrigin = 'center center';

    node._wrap.classList.toggle('active', node.active);
    node._wrap.classList.toggle('completed', node.completed);

    // Hide if too zoomed out
    node._wrap.style.display = scale < 0.15 ? 'none' : '';
  });
}

// Refresh node statuses from updated LABS data
export function refreshNodeStatuses() {
  for (const lab of LABS) {
    const node = graph.nodes.find(n => n.id === lab.id);
    if (node) {
      node.status = lab.status;
      if (lab.status === 'done' || lab.status === 'complete') node.completed = true;
      if (lab.status === 'running') node.active = true;
    }
  }
}

// ── Canvas rendering ────────────────────────────────────────────

export function drawNodes(ctx, time, zoom) {
  for (const node of graph.nodes) {
    const nx = node.x;
    const ny = node.y;

    if (node.node_type === 'root') {
      // Root nodes: hexagon frame drawn on canvas (iframe overlay on top)
      ctx.save();
      ctx.translate(nx, ny);
      drawHexagon(ctx, IFRAME_DISPLAY_SIZE, node.color, node.active, node.completed, time);
      ctx.restore();
    } else {
      // File nodes: simple circle
      ctx.save();
      ctx.translate(nx, ny);
      drawFileNode(ctx, node, time);
      ctx.restore();

      // File name label
      ctx.save();
      ctx.fillStyle = node.color;
      ctx.font = `600 ${10 / zoom}px 'JetBrains Mono', monospace`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const fileName = node.label.split('/').pop().substring(0, 20);
      ctx.fillText(fileName, nx, ny + 35);
      ctx.restore();
    }

    // Agent count badge removed — sprites are the visual indicator
  }

  // Draw child sub-nodes (hex-grid snapped, glowing)
  for (const sub of graph.subNodes) {
    const sx = sub.x;
    const sy = sub.y;
    const subSize = 50;
    const alpha = sub.completed ? Math.max(0, 1 - (Date.now() - (sub._fadeStart || Date.now())) / 1500) : 1;

    // ── Glow effect for create/modify/delete ──
    const glowAge = sub.glowStart ? (Date.now() - sub.glowStart) / 1000 : 99;
    const glowActive = glowAge < 3.0; // glow for 3 seconds
    const glowPulse = glowActive ? (Math.sin(glowAge * 6) * 0.5 + 0.5) : 0;

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.translate(sx, sy);

    // Outer glow ring when file is being created/modified/deleted
    if (glowActive) {
      const glowColor = sub.glowType === 'create' ? '#00ff88'
                       : sub.glowType === 'modify' ? '#ffaa00'
                       : sub.glowType === 'delete' ? '#ff3366'
                       : sub.color;
      const glowRadius = subSize * 0.5 + 8 + glowPulse * 6;
      const glowGrad = ctx.createRadialGradient(0, 0, subSize * 0.3, 0, 0, glowRadius);
      glowGrad.addColorStop(0, `${glowColor}00`);
      glowGrad.addColorStop(0.6, `${glowColor}${Math.round(glowPulse * 60).toString(16).padStart(2, '0')}`);
      glowGrad.addColorStop(1, `${glowColor}00`);
      ctx.beginPath();
      ctx.arc(0, 0, glowRadius, 0, Math.PI * 2);
      ctx.fillStyle = glowGrad;
      ctx.fill();
    }

    drawHexagon(ctx, subSize, sub.color, sub.active || glowActive, sub.completed, time);
    ctx.restore();

    // File name label (truncated)
    ctx.save();
    ctx.globalAlpha = alpha * (glowActive ? 1.0 : 0.7);
    ctx.fillStyle = sub.color;
    ctx.font = `600 ${8 / zoom}px 'JetBrains Mono', monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const displayLabel = sub.label.length > 18
      ? sub.label.substring(0, 15) + '...'
      : sub.label;
    ctx.fillText(displayLabel, sx, sy + subSize * 0.38);
    ctx.restore();

    // Connector line to parent
    const parent = graph.nodes.find(n => n.id === sub.parentId);
    if (parent) {
      ctx.save();
      ctx.globalAlpha = alpha * (glowActive ? 0.6 : 0.3);
      ctx.beginPath();
      ctx.setLineDash([4 / zoom, 6 / zoom]);
      ctx.moveTo(parent.x, parent.y);
      ctx.lineTo(sx, sy);
      ctx.strokeStyle = sub.color;
      ctx.lineWidth = (glowActive ? 2.0 : 1.0) / zoom;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    // Auto-clear glow after 3 seconds
    if (glowActive && glowAge >= 3.0) {
      sub.glowType = null;
      sub.glowStart = null;
    }

    if (sub.completed && !sub._fadeStart) sub._fadeStart = Date.now();
  }
}

// ── Hexagon frame (drawn on canvas behind iframes) ──────────────

function drawHexagon(ctx, size, color, isActive, isCompleted, time) {
  const sides = 6;
  ctx.beginPath();
  for (let i = 0; i < sides; i++) {
    const angle = (i / sides) * Math.PI * 2 - Math.PI / 6;
    const wobble = isActive ? Math.sin(time * 3 + i) * 2 : 0;
    const x = Math.cos(angle) * (size * 0.5 + wobble);
    const y = Math.sin(angle) * (size * 0.5 + wobble);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.closePath();

  const g = ctx.createRadialGradient(0, 0, 0, 0, 0, size * 0.6);
  g.addColorStop(0, `${color}22`);
  g.addColorStop(1, `${color}00`);
  ctx.fillStyle = g;
  ctx.fill();

  const cpulse = (isCompleted && !isActive) ? (Math.sin(time * 1.5) * 0.35 + 0.65) : 1;
  ctx.strokeStyle = isActive ? `${color}cc`
                  : isCompleted ? `${color}${Math.round(cpulse * 140).toString(16).padStart(2, '0')}`
                  : `${color}44`;
  ctx.lineWidth = isActive ? 2.5 : isCompleted ? 1.5 + cpulse * 0.5 : 1.5;
  ctx.stroke();

  if (isActive) {
    ctx.beginPath();
    ctx.arc(0, 0, size * 0.55, -Math.PI / 2, -Math.PI / 2 + (time * 0.5) % (Math.PI * 2));
    ctx.strokeStyle = `${color}aa`;
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.stroke();
  }

  // Completed: slow outer breathing glow ring
  if (isCompleted && !isActive) {
    const glowR = size * 0.58 + cpulse * 10;
    const gGlow = ctx.createRadialGradient(0, 0, size * 0.35, 0, 0, glowR);
    gGlow.addColorStop(0, `${color}00`);
    gGlow.addColorStop(1, `${color}${Math.round(cpulse * 45).toString(16).padStart(2, '0')}`);
    ctx.beginPath();
    ctx.arc(0, 0, glowR, 0, Math.PI * 2);
    ctx.fillStyle = gGlow;
    ctx.fill();
  }
}

// ── Simple file node (for file tree children) ───────────────────

function drawFileNode(ctx, node, time) {
  const color = node.color;
  const radius = 25;

  if (node.active) {
    ctx.beginPath();
    ctx.arc(0, 0, radius * 1.4, 0, Math.PI * 2);
    ctx.fillStyle = `${color}12`;
    ctx.fill();
  }

  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.fillStyle = `${color}25`;
  ctx.fill();
  ctx.strokeStyle = node.active ? `${color}99` : `${color}55`;
  ctx.lineWidth = node.active ? 2 : 1;
  ctx.stroke();

  ctx.fillStyle = color;
  ctx.font = 'bold 12px serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('📄', 0, -2);

  if (node.completed) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(0, 0, radius * 0.7, 0, Math.PI * 2);
    ctx.stroke();
  }
}

// ── Public API ──────────────────────────────────────────────────

export function setNodeActive(nodeId, isActive) {
  const node = graph.nodes.find(n => n.id === nodeId);
  if (node) node.active = isActive;
}

export function setNodeCompleted(nodeId, isCompleted) {
  const node = graph.nodes.find(n => n.id === nodeId);
  if (node) node.completed = isCompleted;
}

export function setNodeAgentCount(nodeId, count) {
  const node = graph.nodes.find(n => n.id === nodeId);
  if (node) node.agentCount = count;
}

export function getNodeScreenPosition(nodeId) {
  const node = graph.nodes.find(n => n.id === nodeId);
  if (!node) return null;
  return worldToScreen(node.x, node.y);
}
