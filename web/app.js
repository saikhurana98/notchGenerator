'use strict';

const SVG_NS = 'http://www.w3.org/2000/svg';

const ROLES = [
  { key: 'outer', label: 'Outer profile' },
  { key: 'interior', label: 'Interior profiles' },
  { key: 'bend', label: 'Bend lines' },
  { key: 'extent', label: 'Bend extents' },
];

const STROKE = {
  outer: { color: 'var(--outer)', width: 1.6, dash: null, label: 'Outer profile' },
  interior: { color: 'var(--interior)', width: 1.4, dash: null, label: 'Interior' },
  bend: { color: 'var(--bend)', width: 1.2, dash: '6 3', label: 'Bend line' },
  extent: { color: 'var(--extent)', width: 1, dash: '3 3', label: 'Bend extent' },
  notch: { color: 'var(--notch)', width: 2.4, dash: null, label: 'New notch' },
  ghost: { color: 'var(--ghost)', width: 1, dash: null, label: 'Original' },
};

const DRAW_ORDER = ['ghost', 'extent', 'bend', 'interior', 'outer', 'notch'];
const TOLERANCE_FIELDS = [
  'stitch_tol', 'snap_tol', 'sliver_tol', 'chord_tol', 'bridge_tol', 'max_stub',
];

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

  $('change-file').addEventListener('click', () => {
    $('step-upload').hidden = false;
    $('filebar').hidden = true;
    $('step-map').hidden = true;
    $('step-review').hidden = true;
    input.value = '';
  });
}

async function upload(file) {
  hide('upload-error');
  try {
    const data = await request('/api/upload', { method: 'POST', body: withFile(file) });
    state.sessionId = data.session_id;
    state.layers = data.layers;
    state.before = data.geometry;
    state.bounds = data.bounds;

    renderRoles(data.suggested_mapping);
    $('filename').textContent = data.filename;
    $('filebar').hidden = false;
    $('step-upload').hidden = true;
    $('step-map').hidden = false;
    $('step-review').hidden = true;
  } catch (err) {
    show('upload-error', err.message);
  }
}

function withFile(file) {
  const body = new FormData();
  body.append('file', file);
  return body;
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
    select.append(option('', 'None'));
    for (const layer of state.layers) {
      select.append(option(layer.name, `${layer.name} · ${layer.total}`));
    }
    select.value = suggested[role.key] || '';

    label.append(select);
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
    single_layer: $('single_layer').checked,
  };
  if ($('thickness').value !== '') cfg.thickness = Number($('thickness').value);
  for (const field of TOLERANCE_FIELDS) {
    if ($(field).value !== '') cfg[field] = Number($(field).value);
  }
  return cfg;
}

async function run() {
  const button = $('run');
  button.disabled = true;
  button.textContent = 'Working…';
  hide('run-error');
  try {
    const data = await request('/api/process', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readConfig()),
    });
    showResult(data);
  } catch (err) {
    show('run-error', err.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Generate notches';
  }
}

// ---------------------------------------------------------------- result

function showResult(data) {
  state.after = data.after || [];
  if (data.before?.length) state.before = data.before;
  if (data.bounds) state.bounds = data.bounds;

  $('step-review').hidden = false;
  const count = data.notches?.length || 0;
  const removed = (data.area_before || 0) - (data.area_after || 0);

  if (data.ok) {
    $('result-title').textContent =
      count === 0
        ? 'No notches were needed'
        : `${count} notch${count === 1 ? '' : 'es'} · ${fmt(removed)} mm² removed`;
    $('download').hidden = !data.download_ready;
    $('download').href = `/api/download/${state.sessionId}`;
    if (data.download_name) $('download').setAttribute('download', data.download_name);
    $('download-name').textContent = data.download_name || '';
  } else {
    $('result-title').textContent = 'Could not generate a safe result';
    $('download').hidden = true;
    $('download-name').textContent = '';
  }

  renderDiagnostics(data.diagnostics);
  resetView();
  $('step-review').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderDiagnostics(items) {
  const host = $('diagnostics');
  host.replaceChildren();
  // Only things the user can act on. Progress notes stay in the CLI output.
  const notable = (items || []).filter((d) => d.level === 'error' || d.level === 'warn');
  notable.sort((a, b) => (a.level === b.level ? 0 : a.level === 'error' ? -1 : 1));
  for (const d of notable) {
    const row = document.createElement('div');
    row.className = `diag ${d.level}`;
    row.textContent = d.message;
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
  // DXF is y-up and SVG is y-down, so everything below stays in model coordinates.
  root.setAttribute('transform', 'scale(1,-1)');
  const sorted = [...layers].sort(
    (a, b) => DRAW_ORDER.indexOf(a.role) - DRAW_ORDER.indexOf(b.role),
  );
  for (const item of sorted) {
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
    root.append(path);
  }
  svg.append(root);
}

function paint() {
  // The ghost is the original outline only; overlaying bend and extent lines as well would
  // bury the thing the overlay exists to show.
  const ghost = state.before
    .filter((i) => i.role === 'outer' || i.role === 'interior')
    .map((i) => ({ ...i, role: 'ghost' }));
  const after = state.after.length ? state.after : state.before;
  const shown = state.mode === 'overlay' ? [...ghost, ...state.after] : after;

  drawInto($('svg-before'), state.before);
  drawInto($('svg-after'), shown.length ? shown : state.before);
  applyView();
  renderLegend(state.mode === 'split' ? [...state.before, ...after] : shown);
}

function renderLegend(layers) {
  const roles = new Set(layers.map((i) => i.role));
  const host = $('legend');
  host.replaceChildren();
  for (const role of DRAW_ORDER) {
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
  // The y range is negated to match the scale(1,-1) used when drawing.
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
    // Letterbox rather than distort: grow whichever dimension this panel needs.
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
      // The panels change shape between modes, so the letterboxing has to be redone.
      requestAnimationFrame(paint);
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
  // Zoom about the cursor, so whatever is under it stays put.
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
  if (!modelPoint(event, svg)) return;
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
    throw new Error(detailOf(body) || `${response.status} ${response.statusText}`);
  }
  return body;
}

function detailOf(body) {
  const detail = body?.detail;
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors arrive as a list of objects.
  if (Array.isArray(detail)) return detail.map((d) => d.msg || String(d)).join('; ');
  return null;
}

function show(id, message) {
  const el = $(id);
  el.hidden = false;
  el.textContent = message;
}

function hide(id) {
  $(id).hidden = true;
}

wireUpload();
wireViewControls();
$('run').addEventListener('click', run);
