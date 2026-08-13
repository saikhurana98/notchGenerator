'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';

const ROLES = [
  { key: 'outer', label: 'Outer profile', help: 'The outline the notches are cut into.' },
  { key: 'interior', label: 'Interior profiles', help: 'Holes and cutouts, passed through untouched.' },
  { key: 'bend', label: 'Bend lines', help: 'One line per bend. Dropped from the output.' },
  { key: 'extent', label: 'Bend extent lines', help: 'Two per bend. Dropped from the output.' },
];

const STROKE = {
  outer: { color: 'var(--outer)', width: 1.6, dash: null, label: 'Outer profile' },
  interior: { color: 'var(--interior)', width: 1.4, dash: null, label: 'Interior' },
  bend: { color: 'var(--bend)', width: 1.2, dash: '6 3', label: 'Bend line' },
  extent: { color: 'var(--extent)', width: 1, dash: '3 3', label: 'Bend extent' },
  notch: { color: 'var(--notch)', width: 2.2, dash: null, label: 'New notch' },
  ghost: { color: 'var(--ghost)', width: 1, dash: null, label: 'Original' },
};

const TOLERANCE_FIELDS = ['stitch_tol', 'snap_tol', 'sliver_tol', 'chord_tol', 'max_stub'];

const state = {
  sessionId: null,
  layers: [],
  before: [],
  after: [],
  bounds: null,
  view: null,
  home: null,
  mode: 'split',
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- upload

function wireUpload() {
  const zone = $('dropzone');
  const input = $('file');

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => input.files[0] && upload(input.files[0]));

  for (const event of ['dragenter', 'dragover']) {
    zone.addEventListener(event, (e) => { e.preventDefault(); zone.classList.add('hot'); });
  }
  for (const event of ['dragleave', 'drop']) {
    zone.addEventListener(event, (e) => { e.preventDefault(); zone.classList.remove('hot'); });
  }
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) upload(file);
  });
}

async function upload(file) {
  setStatus('upload-status', `Reading ${file.name}…`);
  const body = new FormData();
  body.append('file', file);
  try {
    const data = await request('/api/upload', { method: 'POST', body });
    state.sessionId = data.session_id;
    state.layers = data.layers;
    state.before = data.geometry;
    state.bounds = data.bounds;
    renderRoles(data.suggested_mapping);
    setStatus('upload-status', `${file.name} — ${data.layers.length} layers.`, 'ok');
    $('step-map').hidden = false;
    $('step-review').hidden = true;
    renderDiagnostics(data.diagnostics);
    $('step-map').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    setStatus('upload-status', err.message, 'error');
  }
}

// ---------------------------------------------------------------- mapping

function renderRoles(suggested) {
  const host = $('roles');
  host.replaceChildren();
  for (const role of ROLES) {
    const label = document.createElement('label');
    label.textContent = role.label;

    const select = document.createElement('select');
    select.id = `role-${role.key}`;
    select.append(option('', '— none —'));
    for (const layer of state.layers) {
      const contents = Object.entries(layer.counts)
        .map(([type, n]) => `${n}×${type}`)
        .join(', ');
      select.append(option(layer.name, `${layer.name} (${contents})`));
    }
    select.value = suggested[role.key] || '';

    const meta = document.createElement('span');
    meta.className = 'role-meta';
    meta.textContent = role.help;

    label.append(select, meta);
    host.append(label);
  }
}

function option(value, text) {
  const el = document.createElement('option');
  el.value = value;
  el.textContent = text;
  return el;
}

function readConfig() {
  const cfg = {
    session_id: state.sessionId,
    mapping: Object.fromEntries(ROLES.map((r) => [r.key, $(`role-${r.key}`).value])),
    depth: Number($('depth').value),
    depth_from: $('depth-from').value,
    shape: $('shape').value,
    merge_overlapping: $('merge_overlapping').checked,
  };
  const thickness = $('thickness').value;
  if (thickness !== '') cfg.thickness = Number(thickness);
  for (const field of TOLERANCE_FIELDS) {
    const raw = $(field).value;
    if (raw !== '') cfg[field] = Number(raw);
  }
  return cfg;
}

async function run() {
  const button = $('run');
  button.disabled = true;
  setStatus('run-status', 'Cutting notches…');
  try {
    const data = await request('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readConfig()),
    });
    showResult(data);
  } catch (err) {
    setStatus('run-status', err.message, 'error');
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- result

function showResult(data) {
  state.after = data.after || [];
  if (data.before?.length) state.before = data.before;
  if (data.bounds) state.bounds = data.bounds;

  $('step-review').hidden = false;
  const notches = data.notches?.length || 0;

  if (data.ok) {
    setStatus('run-status', `${notches} notch${notches === 1 ? '' : 'es'} cut.`, 'ok');
    $('download').hidden = !data.download_ready;
    $('download').href = `/api/download/${state.sessionId}`;
  } else {
    setStatus('run-status', 'Could not generate a safe result — see below.', 'error');
    $('download').hidden = true;
  }

  const removed = (data.area_before || 0) - (data.area_after || 0);
  $('summary').innerHTML = data.ok
    ? `<span>Notches <b>${notches}</b></span>
       <span>Area before <b>${fmt(data.area_before)} mm²</b></span>
       <span>Area after <b>${fmt(data.area_after)} mm²</b></span>
       <span>Removed <b>${fmt(removed)} mm²</b></span>`
    : '';

  renderDiagnostics(data.diagnostics);
  resetView();
  $('step-review').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderDiagnostics(items) {
  const host = $('diagnostics');
  host.replaceChildren();
  const order = { error: 0, warn: 1, info: 2 };
  for (const d of [...(items || [])].sort((a, b) => order[a.level] - order[b.level])) {
    const row = document.createElement('div');
    row.className = `diag ${d.level}`;
    const code = document.createElement('code');
    code.textContent = d.code;
    const text = document.createElement('span');
    text.textContent = d.message;
    row.append(code, text);
    host.append(row);
  }
}

function fmt(n) {
  return (n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// ---------------------------------------------------------------- drawing

function drawInto(svg, layers) {
  svg.replaceChildren();
  const root = document.createElementNS(SVG_NS, 'g');
  // DXF is y-up, SVG is y-down. Flipping here keeps every coordinate below in model space.
  root.setAttribute('transform', 'scale(1,-1)');
  for (const item of layers) {
    if (!item.pts || item.pts.length < 2) continue;
    const style = STROKE[item.role] || STROKE.outer;
    const path = document.createElementNS(SVG_NS, 'polyline');
    path.setAttribute('points', item.pts.map(([x, y]) => `${x},${y}`).join(' '));
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', style.color);
    path.setAttribute('stroke-width', style.width);
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('vector-effect', 'non-scaling-stroke');
    if (style.dash) path.setAttribute('stroke-dasharray', style.dash);
    if (item.role === 'ghost') path.setAttribute('opacity', '0.75');
    root.append(path);
  }
  svg.append(root);
}

function paint() {
  const beforeLayers = state.before;
  // The ghost is the original *outline* only. Overlaying the bend and extent lines too
  // would bury the thing the overlay exists to show.
  const ghost = state.before
    .filter((i) => i.role === 'outer' || i.role === 'interior')
    .map((i) => ({ ...i, role: 'ghost' }));
  const afterLayers = state.mode === 'overlay' ? [...ghost, ...state.after] : state.after;
  drawInto($('svg-before'), beforeLayers);
  drawInto($('svg-after'), afterLayers.length ? afterLayers : beforeLayers);
  applyView();
  renderLegend(afterLayers.length ? afterLayers : beforeLayers);
}

function renderLegend(layers) {
  const roles = new Set(layers.map((i) => i.role));
  if (state.mode === 'split') for (const i of state.before) roles.add(i.role);
  const host = $('legend');
  host.replaceChildren();
  for (const role of ['outer', 'interior', 'bend', 'extent', 'notch', 'ghost']) {
    if (!roles.has(role)) continue;
    const style = STROKE[role];
    const item = document.createElement('span');
    const swatch = document.createElement('i');
    swatch.style.color = style.color;
    if (style.dash) swatch.style.borderTopStyle = 'dashed';
    item.append(swatch, document.createTextNode(style.label));
    host.append(item);
  }
}

function resetView() {
  const b = state.bounds || { min: [0, 0], max: [1, 1] };
  const width = Math.max(b.max[0] - b.min[0], 1e-6);
  const height = Math.max(b.max[1] - b.min[1], 1e-6);
  const pad = 0.06 * Math.max(width, height);
  // The y range is negated to match the scale(1,-1) applied when drawing.
  state.home = {
    x: b.min[0] - pad,
    y: -(b.max[1] + pad),
    w: width + 2 * pad,
    h: height + 2 * pad,
  };
  state.view = { ...state.home };
  paint();
}

function applyView() {
  const v = state.view;
  if (!v) return;
  for (const svg of [$('svg-before'), $('svg-after')]) {
    const box = svg.getBoundingClientRect();
    const aspect = box.height > 0 && box.width > 0 ? box.height / box.width : 0.75;
    // Letterbox rather than distort: grow whichever dimension the panel needs.
    let { x, y, w, h } = v;
    if (h / w < aspect) {
      const grown = w * aspect;
      y -= (grown - h) / 2;
      h = grown;
    } else {
      const grown = h / aspect;
      x -= (grown - w) / 2;
      w = grown;
    }
    svg.setAttribute('viewBox', `${x} ${y} ${w} ${h}`);
  }
}

function wireViewControls() {
  for (const button of document.querySelectorAll('.segmented button')) {
    button.addEventListener('click', () => {
      for (const other of document.querySelectorAll('.segmented button')) {
        other.classList.toggle('active', other === button);
      }
      state.mode = button.dataset.mode;
      $('views').className = `views ${state.mode}`;
      paint();
    });
  }
  $('reset-view').addEventListener('click', resetView);
  window.addEventListener('resize', applyView);

  for (const svg of [$('svg-before'), $('svg-after')]) {
    svg.addEventListener('wheel', (e) => onWheel(e, svg), { passive: false });
    svg.addEventListener('pointerdown', (e) => onDragStart(e, svg));
  }
}

function modelPoint(event, svg) {
  const box = svg.getBoundingClientRect();
  const viewBox = svg.getAttribute('viewBox');
  if (!viewBox) return null;
  const [x, y, w, h] = viewBox.split(' ').map(Number);
  return {
    x: x + ((event.clientX - box.left) / box.width) * w,
    y: y + ((event.clientY - box.top) / box.height) * h,
  };
}

function onWheel(event, svg) {
  event.preventDefault();
  if (!state.view) return;
  const at = modelPoint(event, svg);
  if (!at) return;
  const factor = Math.exp(event.deltaY * 0.0015);
  const v = state.view;
  // Zoom about the cursor, so the point under it stays put.
  state.view = {
    x: at.x - (at.x - v.x) * factor,
    y: at.y - (at.y - v.y) * factor,
    w: v.w * factor,
    h: v.h * factor,
  };
  applyView();
}

function onDragStart(event, svg) {
  if (event.button !== 0 || !state.view) return;
  const start = modelPoint(event, svg);
  if (!start) return;
  const origin = { ...state.view };
  svg.setPointerCapture(event.pointerId);
  svg.classList.add('dragging');

  const move = (e) => {
    const box = svg.getBoundingClientRect();
    const viewBox = svg.getAttribute('viewBox').split(' ').map(Number);
    const dx = ((e.clientX - event.clientX) / box.width) * viewBox[2];
    const dy = ((e.clientY - event.clientY) / box.height) * viewBox[3];
    state.view = { ...origin, x: origin.x - dx, y: origin.y - dy };
    applyView();
  };
  const stop = () => {
    svg.releasePointerCapture(event.pointerId);
    svg.classList.remove('dragging');
    svg.removeEventListener('pointermove', move);
    svg.removeEventListener('pointerup', stop);
    svg.removeEventListener('pointercancel', stop);
  };
  svg.addEventListener('pointermove', move);
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
}

// ---------------------------------------------------------------- plumbing

async function request(url, options) {
  const response = await fetch(url, options);
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* fall through to the status text below */
  }
  if (!response.ok) {
    throw new Error(body?.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

function setStatus(id, message, kind) {
  const el = $(id);
  el.hidden = false;
  el.textContent = message;
  el.className = `status${kind ? ` ${kind}` : ''}`;
}

wireUpload();
wireViewControls();
$('run').addEventListener('click', run);
