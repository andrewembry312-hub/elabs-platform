// ═══════════════════════════════════════════════════════════════════
// BRAIN — Holographic brain core with scan lines, hemispheres,
//         neural fire, orbiting data streams, and projection base
// ═══════════════════════════════════════════════════════════════════

// Seeded random for deterministic brain shape
function srand(seed) { let s = seed; return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; }; }

const rng = srand(42);

// Pre-generate hemisphere outlines
function makeHemi(count) {
  const pts = [];
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2;
    const r = 0.42 + rng() * 0.12 + Math.sin(a * 2) * 0.08 + Math.cos(a * 3) * 0.05;
    pts.push({ a, r });
  }
  return pts;
}
const HEMI_L = makeHemi(40);
const HEMI_R = makeHemi(40);

// Pre-generate sulci (brain ridge paths)
const SULCI = [];
for (let s = 0; s < 12; s++) {
  const path = [];
  const side = s < 6 ? -1 : 1;
  for (let p = 0; p < 8; p++) {
    path.push({ x: side * (0.1 + rng() * 0.3), y: -0.35 + p * 0.1 + (rng() - 0.5) * 0.06 });
  }
  SULCI.push(path);
}

export function drawHolographicBrain(ctx, x, y, time, zoom) {
  const s = 80 / zoom;
  ctx.save();
  ctx.translate(x, y);

  // ── Outer ambient glow ──
  const g0 = ctx.createRadialGradient(0, 0, 0, 0, 0, s * 2.5);
  g0.addColorStop(0, 'rgba(0,212,255,0.08)');
  g0.addColorStop(0.5, 'rgba(0,180,255,0.03)');
  g0.addColorStop(1, 'rgba(0,212,255,0)');
  ctx.fillStyle = g0;
  ctx.beginPath();
  ctx.arc(0, 0, s * 2.5, 0, Math.PI * 2);
  ctx.fill();

  // ── Holographic scan rings (rotating arcs with tick marks) ──
  for (let i = 0; i < 4; i++) {
    const r = s * (0.7 + i * 0.25);
    const rot = (i % 2 === 0 ? 1 : -1) * 0.3;
    const sa = time * rot + i * 0.8;
    const al = Math.PI * 0.3 + Math.sin(time + i) * 0.2;

    ctx.beginPath();
    ctx.arc(0, 0, r, sa, sa + al);
    ctx.strokeStyle = `rgba(0,212,255,${0.15 - i * 0.03})`;
    ctx.lineWidth = (2 - i * 0.3) / zoom;
    ctx.stroke();

    for (let t = 0; t < 8; t++) {
      const ta = sa + (t / 8) * al;
      const tx = Math.cos(ta) * r;
      const ty = Math.sin(ta) * r;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(tx * 1.05, ty * 1.05);
      ctx.strokeStyle = 'rgba(0,212,255,0.3)';
      ctx.lineWidth = 0.5 / zoom;
      ctx.stroke();
    }
  }

  // ── Projection base (hologram pedestal) ──
  ctx.beginPath();
  ctx.ellipse(0, s * 0.55, s * 0.45, s * 0.1, 0, 0, Math.PI * 2);
  const bg = ctx.createRadialGradient(0, s * 0.55, 0, 0, s * 0.55, s * 0.45);
  bg.addColorStop(0, 'rgba(0,212,255,0.12)');
  bg.addColorStop(1, 'rgba(0,212,255,0)');
  ctx.fillStyle = bg;
  ctx.fill();
  ctx.strokeStyle = 'rgba(0,212,255,0.2)';
  ctx.lineWidth = 0.8 / zoom;
  ctx.stroke();

  // ── Left hemisphere ──
  drawHemisphere(ctx, -s * 0.22, 0, s, HEMI_L, time, '#00d4ff', zoom);

  // ── Right hemisphere ──
  drawHemisphere(ctx, s * 0.22, 0, s, HEMI_R, time, '#00e5ff', zoom);

  // ── Central fissure ──
  ctx.beginPath();
  ctx.moveTo(0, -s * 0.45);
  for (let i = 0; i <= 10; i++) {
    const t = i / 10;
    ctx.lineTo(Math.sin(time * 0.8 + t * 4) * s * 0.02, -s * 0.45 + t * s * 0.9);
  }
  ctx.strokeStyle = 'rgba(0,212,255,0.4)';
  ctx.lineWidth = 1.5 / zoom;
  ctx.stroke();

  // ── Sulci (cortex ridges) ──
  SULCI.forEach((path, si) => {
    ctx.beginPath();
    path.forEach((p, pi) => {
      const px = p.x * s + Math.sin(time * 0.5 + si + pi * 0.3) * s * 0.01;
      pi === 0 ? ctx.moveTo(px, p.y * s) : ctx.lineTo(px, p.y * s);
    });
    ctx.strokeStyle = `rgba(0,200,255,${0.15 + Math.sin(time * 0.7 + si) * 0.05})`;
    ctx.lineWidth = 0.8 / zoom;
    ctx.stroke();
  });

  // ── Neural fire pulses ──
  for (let n = 0; n < 8; n++) {
    const phase = (time * 1.5 + n * 1.23) % 3.0;
    if (phase > 1) continue;
    const side = n < 4 ? -1 : 1;
    const sx = side * s * (0.1 + (n % 4) * 0.08);
    const sy = -s * 0.3 + (n % 4) * s * 0.15;
    const px = sx + side * s * 0.15 * phase;
    const py = sy + s * 0.1 * phase;
    const alpha = (1 - phase) * 0.8;

    const ng = ctx.createRadialGradient(px, py, 0, px, py, (3 + phase * 4) / zoom);
    ng.addColorStop(0, `rgba(255,255,255,${alpha})`);
    ng.addColorStop(1, 'rgba(0,212,255,0)');
    ctx.fillStyle = ng;
    ctx.beginPath();
    ctx.arc(px, py, (3 + phase * 4) / zoom, 0, Math.PI * 2);
    ctx.fill();
  }

  // ── Orbiting data stream particles ──
  for (let d = 0; d < 16; d++) {
    const orbitR = s * (0.55 + (d % 4) * 0.08);
    const speed = (d % 2 === 0 ? 1 : -0.7) * 0.8;
    const a = time * speed + d * Math.PI / 8;
    const dx = Math.cos(a) * orbitR;
    const dy = Math.sin(a) * orbitR * 0.5; // flattened orbit
    const da = 0.3 + Math.sin(time * 2 + d) * 0.15;

    ctx.beginPath();
    ctx.arc(dx, dy, 1.5 / zoom, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(0,212,255,${da})`;
    ctx.fill();
  }

  // ── Core pulse ──
  const coreR = s * 0.12 + Math.sin(time * 2.5) * s * 0.03;
  const cg = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR);
  cg.addColorStop(0, 'rgba(255,255,255,0.7)');
  cg.addColorStop(0.4, 'rgba(0,212,255,0.4)');
  cg.addColorStop(1, 'rgba(0,212,255,0)');
  ctx.fillStyle = cg;
  ctx.beginPath();
  ctx.arc(0, 0, coreR, 0, Math.PI * 2);
  ctx.fill();

  // ── Hologram scan line sweep ──
  const scanY = ((time * 40) % (s * 2)) - s;
  for (let sl = -s; sl < s; sl += 3 / zoom) {
    const dist = Math.abs(sl - scanY);
    if (dist < s * 0.15) {
      ctx.globalAlpha = 0.12 * (1 - dist / (s * 0.15));
      ctx.beginPath();
      ctx.moveTo(-s * 0.5, sl);
      ctx.lineTo(s * 0.5, sl);
      ctx.strokeStyle = '#00d4ff';
      ctx.lineWidth = 0.5 / zoom;
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;

  // ── Label ──
  ctx.font = `700 ${8 / zoom}px 'JetBrains Mono', monospace`;
  ctx.textAlign = 'center';
  ctx.fillStyle = `rgba(0,212,255,${0.3 + Math.sin(time * 2) * 0.1})`;
  ctx.fillText('CORE', 0, s * 0.75);

  ctx.restore();
}

function drawHemisphere(ctx, ox, oy, scale, points, time, color, zoom) {
  ctx.beginPath();
  points.forEach((p, i) => {
    const r = p.r * scale + Math.sin(time * 0.6 + p.a * 2) * scale * 0.01;
    const px = ox + Math.cos(p.a) * r;
    const py = oy + Math.sin(p.a) * r;
    i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
  });
  ctx.closePath();

  const hg = ctx.createRadialGradient(ox, oy, 0, ox, oy, scale * 0.5);
  hg.addColorStop(0, `${color}30`);
  hg.addColorStop(0.6, `${color}15`);
  hg.addColorStop(1, `${color}00`);
  ctx.fillStyle = hg;
  ctx.fill();
  ctx.strokeStyle = `${color}55`;
  ctx.lineWidth = 1 / zoom;
  ctx.stroke();

  // Internal ridge ellipses
  for (let r = 0; r < 4; r++) {
    const rr = scale * (0.15 + r * 0.08);
    ctx.beginPath();
    ctx.ellipse(ox, oy, rr, rr * 0.7, Math.sin(time * 0.2) * 0.1, 0, Math.PI * 2);
    ctx.strokeStyle = `${color}${Math.round(20 - r * 4).toString(16).padStart(2, '0')}`;
    ctx.lineWidth = 0.6 / zoom;
    ctx.stroke();
  }
}
