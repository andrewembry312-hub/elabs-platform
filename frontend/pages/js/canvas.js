// ═══════════════════════════════════════════════════════════════════
// CANVAS — Viewport and grid rendering
// ═══════════════════════════════════════════════════════════════════

import { viewport, input, graph } from './state.js?v=20260418';
import { CANVAS_CONFIG, LABS } from './config.js';

// Core click callback — set via setCoreClickHandler()
let _onCoreClick = null;
let _onCoreDblClick = null;
export function setCoreClickHandler(fn) { _onCoreClick = fn; }
export function setCoreDblClickHandler(fn) { _onCoreDblClick = fn; }

const CORE_HIT_RADIUS = 80; // same as brain render radius in world units

export const canvas = {
  bg: null,
  bgCtx: null,
  fg: null,
  fgCtx: null,
};

export function initCanvas(bgCanvas, fgCanvas) {
  canvas.bg = bgCanvas;
  canvas.bgCtx = bgCanvas.getContext('2d');
  canvas.fg = fgCanvas;
  canvas.fgCtx = fgCanvas.getContext('2d');

  resizeCanvas();
  setupInputHandlers();
}

export function resizeCanvas() {
  const container = canvas.bg.parentElement;
  const w = container.clientWidth;
  const h = container.clientHeight;
  const dpr = devicePixelRatio || 1;

  viewport.width = w;
  viewport.height = h;

  [canvas.bg, canvas.fg].forEach(c => {
    c.width = w * dpr;
    c.height = h * dpr;
    c.style.width = w + 'px';
    c.style.height = h + 'px';
    c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
  });
}

function setupInputHandlers() {
  canvas.fg.addEventListener('mousedown', onMouseDown);
  canvas.fg.addEventListener('mousemove', onMouseMove);
  canvas.fg.addEventListener('mouseup', onMouseUp);
  canvas.fg.addEventListener('dblclick', onDblClick);
  canvas.fg.addEventListener('wheel', onWheel, { passive: false });
  canvas.fg.addEventListener('contextmenu', onContextMenu);
  window.addEventListener('resize', resizeCanvas);
  // Close context menu on click anywhere
  document.addEventListener('click', () => {
    const menu = document.getElementById('nodeContextMenu');
    if (menu) menu.style.display = 'none';
  });
}

const NODE_HIT_RADIUS = 90; // world units — half the IFRAME_DISPLAY_SIZE

function findNodeAt(screenX, screenY) {
  const { worldX, worldY } = screenToWorld(screenX, screenY);
  for (const node of [...graph.nodes, ...(graph.subNodes || []), ...(graph.taskNodes || [])]) {
    const dx = worldX - node.x;
    const dy = worldY - node.y;
    const hitR = node.size ? node.size + 10 : NODE_HIT_RADIUS;
    if (dx * dx + dy * dy < hitR * hitR) return node;
  }
  return null;
}

function onMouseDown(e) {
  const rect = canvas.fg.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;

  input.mouseDown = true;
  input.dragStartX = e.clientX;
  input.dragStartY = e.clientY;
  input.isDragging = false;

  // Check if clicking on a node
  const node = findNodeAt(sx, sy);
  if (node) {
    input.dragTarget = { type: 'node', node };
  } else {
    input.dragTarget = null;
  }
}

function onMouseMove(e) {
  input.mouseX = e.clientX;
  input.mouseY = e.clientY;

  if (input.mouseDown) {
    _hideTooltip();
    const dx = e.clientX - input.dragStartX;
    const dy = e.clientY - input.dragStartY;

    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      input.isDragging = true;

      if (input.dragTarget && input.dragTarget.type === 'node') {
        // Move the node in world space
        const node = input.dragTarget.node;
        const deltaX = dx / viewport.zoom;
        const deltaY = dy / viewport.zoom;
        node.x += deltaX;
        node.y += deltaY;

        // Move all children (subNodes + taskNodes) with parent
        const moveChildren = (parentId) => {
          if (graph.subNodes) {
            for (const child of graph.subNodes) {
              if (child.parentId === parentId) {
                child.x += deltaX;
                child.y += deltaY;
              }
            }
          }
          if (graph.taskNodes) {
            for (const child of graph.taskNodes) {
              if (child.parentId === parentId) {
                child.x += deltaX;
                child.y += deltaY;
                // Recursively move grandchildren
                moveChildren(child.id);
              }
            }
          }
        };
        moveChildren(node.id);
      } else {
        // Pan the viewport
        viewport.panX += dx * CANVAS_CONFIG.panSensitivity;
        viewport.panY += dy * CANVAS_CONFIG.panSensitivity;
      }

      input.dragStartX = e.clientX;
      input.dragStartY = e.clientY;
    }
  } else {
    // Hover detection for tooltips
    const rect = canvas.fg.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const node = findNodeAt(sx, sy);
    if (node) {
      _showTooltip(node, e.clientX, e.clientY);
    } else {
      _hideTooltip();
    }
  }
}

function onMouseUp(e) {
  const wasDragging = input.isDragging;
  input.mouseDown = false;
  input.isDragging = false;
  input.dragTarget = null;

  // If it was a click (not a drag), check for core hit
  if (!wasDragging && _onCoreClick) {
    const rect = canvas.fg.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const { worldX, worldY } = screenToWorld(sx, sy);
    const dx = worldX - graph.coreX;
    const dy = worldY - graph.coreY;
    if (dx * dx + dy * dy < CORE_HIT_RADIUS * CORE_HIT_RADIUS) {
      _onCoreClick();
    }
  }
}

function onDblClick(e) {
  if (!_onCoreDblClick) return;
  const rect = canvas.fg.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const { worldX, worldY } = screenToWorld(sx, sy);
  const dx = worldX - graph.coreX;
  const dy = worldY - graph.coreY;
  if (dx * dx + dy * dy < CORE_HIT_RADIUS * CORE_HIT_RADIUS) {
    _onCoreDblClick();
  }
}

function onContextMenu(e) {
  e.preventDefault();
  const rect = canvas.fg.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const node = findNodeAt(sx, sy);

  const menu = document.getElementById('nodeContextMenu');
  if (!menu || !node) {
    if (menu) menu.style.display = 'none';
    return;
  }

  // Store clicked node on the menu element for action handlers
  menu._node = node;

  // Position menu at cursor
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.style.display = 'block';

  // Ensure menu doesn't go off-screen
  requestAnimationFrame(() => {
    const r = menu.getBoundingClientRect();
    if (r.right > window.innerWidth) menu.style.left = (e.clientX - r.width) + 'px';
    if (r.bottom > window.innerHeight) menu.style.top = (e.clientY - r.height) + 'px';
  });
}

function onWheel(e) {
  e.preventDefault();
  const zoomDelta = e.deltaY > 0 ? 1 : -1;
  const newZoom = viewport.zoom * (1 + zoomDelta * CANVAS_CONFIG.zoomSensitivity);
  viewport.zoom = Math.max(CANVAS_CONFIG.minZoom, Math.min(CANVAS_CONFIG.maxZoom, newZoom));
}

export function renderGrid() {
  const ctx = canvas.bgCtx;
  const w = viewport.width;
  const h = viewport.height;

  // Clear
  ctx.fillStyle = '#030308';
  ctx.fillRect(0, 0, w, h);

  // Draw grid with pan/zoom
  ctx.save();
  ctx.translate(w / 2 + viewport.panX, h / 2 + viewport.panY);
  ctx.scale(viewport.zoom, viewport.zoom);

  ctx.strokeStyle = CANVAS_CONFIG.gridColor;
  ctx.lineWidth = 1 / viewport.zoom;
  const gridSize = CANVAS_CONFIG.gridSize;
  const range = Math.max(w, h) / viewport.zoom / 2;

  for (let x = -range; x < range; x += gridSize) {
    ctx.beginPath();
    ctx.moveTo(x, -range);
    ctx.lineTo(x, range);
    ctx.stroke();
  }

  for (let y = -range; y < range; y += gridSize) {
    ctx.beginPath();
    ctx.moveTo(-range, y);
    ctx.lineTo(range, y);
    ctx.stroke();
  }

  ctx.restore();
}

export function screenToWorld(screenX, screenY) {
  const w = viewport.width;
  const h = viewport.height;
  const worldX = (screenX - w / 2 - viewport.panX) / viewport.zoom;
  const worldY = (screenY - h / 2 - viewport.panY) / viewport.zoom;
  return { worldX, worldY };
}

export function worldToScreen(worldX, worldY) {
  const w = viewport.width;
  const h = viewport.height;
  const screenX = worldX * viewport.zoom + w / 2 + viewport.panX;
  const screenY = worldY * viewport.zoom + h / 2 + viewport.panY;
  return { screenX, screenY };
}

export function getTransformMatrix() {
  return {
    translate: { x: viewport.width / 2 + viewport.panX, y: viewport.height / 2 + viewport.panY },
    zoom: viewport.zoom,
  };
}

// ─── Node Tooltip ────────────────────────────────────────

let _tooltipNode = null;

function _showTooltip(node, cx, cy) {
  const tip = document.getElementById('nodeTooltip');
  if (!tip) return;
  if (_tooltipNode === node.id) {
    // Just reposition
    _positionTooltip(tip, cx, cy);
    return;
  }
  _tooltipNode = node.id;

  // Find matching LAB data for richer info
  const lab = LABS.find(l => l.id === node.id);
  const isTask = node.node_type === 'task' || (!lab && graph.taskNodes.some(t => t.id === node.id));
  const isRoot = !isTask;

  // Status badge
  const st = node.completed ? 'complete' : node.active ? 'running' : (node.status || 'pending');
  const stColor = st === 'complete' || st === 'done' ? '#69f0ae'
                : st === 'running' ? '#00d4ff'
                : st === 'error' ? '#ff5252' : '#888';

  let html = `<div style="margin-bottom:4px">`;
  html += `<span style="color:${node.color || '#00d4ff'};font-weight:700;font-size:12px">${node.icon || '⚙️'} ${isRoot ? 'Node' : 'Task'} ${node.nodeId || node.id}</span>`;
  html += `<span style="float:right;color:${stColor};font-weight:600;text-transform:uppercase;font-size:10px">${st}</span>`;
  html += `</div>`;

  // Task description
  const task = node.task || node.label || '';
  if (task) {
    html += `<div style="color:#ccc;margin-bottom:4px;line-height:1.3">${task.length > 120 ? task.substring(0, 117) + '...' : task}</div>`;
  }

  // Details grid
  html += `<div style="display:grid;grid-template-columns:auto 1fr;gap:1px 8px;font-size:10px;color:#999">`;
  if (node.muscle && node.muscle !== 'NONE') html += `<span>Agent:</span><span style="color:${node.color || '#ccc'}">${node.muscle}</span>`;
  else if (node.action === 'bash') html += `<span>Agent:</span><span style="color:#888">bash</span>`;
  if (lab?.model_used) html += `<span>Model:</span><span style="color:#60A5FA">${lab.model_used}</span>`;
  if (lab?.tokens_used) html += `<span>Tokens:</span><span style="color:#A78BFA">${lab.tokens_used.toLocaleString()}</span>`;
  if (lab?.started_at && lab?.completed_at) {
    const ms = new Date(lab.completed_at) - new Date(lab.started_at);
    html += `<span>Time:</span><span style="color:#34D399">${ms < 1000 ? ms + 'ms' : (ms/1000).toFixed(1) + 's'}</span>`;
  }
  if (lab?.depends_on?.length) html += `<span>Deps:</span><span>${lab.depends_on.join(', ')}</span>`;
  if (lab?.output_file) html += `<span>Output:</span><span style="color:#ffd700">${lab.output_file}</span>`;

  // Task node (child) specific info
  if (isTask) {
    if (node.filename) html += `<span>File:</span><span style="color:#ffd700">${node.filename}</span>`;
    if (node.parentId) {
      const parent = graph.nodes.find(n => n.id === node.parentId);
      html += `<span>Parent:</span><span>${parent ? parent.label?.substring(0, 30) : node.parentId}</span>`;
    }
  }
  html += `</div>`;

  tip.innerHTML = html;
  tip.style.display = 'block';
  _positionTooltip(tip, cx, cy);
}

function _positionTooltip(tip, cx, cy) {
  const container = tip.parentElement;
  if (!container) return;
  const cr = container.getBoundingClientRect();
  let x = cx - cr.left + 16;
  let y = cy - cr.top - 10;
  // Keep tooltip within bounds
  requestAnimationFrame(() => {
    const tr = tip.getBoundingClientRect();
    if (x + tr.width > cr.width) x = cx - cr.left - tr.width - 16;
    if (y + tr.height > cr.height) y = cr.height - tr.height - 8;
    if (y < 4) y = 4;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function _hideTooltip() {
  if (!_tooltipNode) return;
  _tooltipNode = null;
  const tip = document.getElementById('nodeTooltip');
  if (tip) tip.style.display = 'none';
}
