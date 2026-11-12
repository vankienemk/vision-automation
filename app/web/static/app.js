/* Vision Automation — giao diện web (thuần JS, không cần build).
 *
 * Kiến trúc:
 *   S.state  – ảnh chụp trạng thái do server đẩy qua WebSocket (~8 lần/giây)
 *   S.config – bản config đã che bí mật (****) dùng để sửa rồi PUT lại toàn bộ
 *   Mọi thao tác đều đi qua REST API, không có trạng thái ẩn phía client.
 */
'use strict';

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function toast(message, kind = '', ms = 3800) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, ms);
  setTimeout(() => el.remove(), ms + 350);
}

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options);
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  let data = null;
  try { data = await response.json(); } catch (e) { data = null; }
  if (!response.ok) {
    const message = (data && (data.error || data.detail || data.message)) || ('Lỗi HTTP ' + response.status);
    const error = new Error(typeof message === 'string' ? message : JSON.stringify(message));
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }
function fmtTime(ts) {
  if (!ts) return '–';
  const d = ts > 1e12 ? new Date(ts) : new Date(ts * 1000);
  return d.toLocaleTimeString('vi-VN', { hour12: false });
}
function fmtUptime(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return d + ' ngày ' + h + ' giờ';
  if (h) return h + ' giờ ' + m + ' phút';
  if (m) return m + ' phút ' + sec + ' giây';
  return sec + ' giây';
}
function escapeHtml(text) {
  return String(text == null ? '' : text)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}
function debounce(fn, wait = 250) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
}
function slugify(text) {
  return String(text || '').toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replaceAll('đ', 'd')
    .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 48) || 'vung';
}

const PALETTE = ['#22c55e', '#38bdf8', '#f59e0b', '#a78bfa', '#f472b6', '#facc15', '#2dd4bf', '#fb7185'];

/* Trình duyệt này nhớ camera đang xem, để lần sau mở lại (hoặc app khởi động
 * lại) là vào đúng camera đó — không nhảy về camera đầu danh sách. */
const LS = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem('va.' + key);
      return value == null || value === '' ? fallback : value;
    } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('va.' + key, String(value)); } catch (e) { /* chế độ riêng tư */ }
  },
};

/* ------------------------------------------------------------------ state */
const S = {
  state: null,
  config: null,
  configVersion: 0,
  tab: 'live',
  liveCamera: LS.get('liveCamera'),
  zonePreviewTimer: null,
  logLines: [],
  events: [],
  ws: null,
  wsRetry: 0,
  scenarioDraft: null,
  scenarioTrigger: null,   // loại điều kiện đang sửa bước
  presetHint: '',
  pointerBusy: false,      // đang giữ chuột/ngón tay → KHÔNG dựng lại DOM
  camChecks: {},           // kết quả "Kiểm tra kết nối" của từng camera
  stopped: false,          // người dùng đã bấm Tắt ứng dụng → ngừng nối lại
  assetVersion: null,      // phiên bản app.js/style.css lúc mở trang
  staleNotified: false,
  // Theo dõi luồng MJPEG: trình duyệt chỉ xin luồng MỘT lần cho mỗi <img>, nên
  // khi máy chủ khởi động lại thì ảnh chết mà không ai xin lại (xem checkLiveStream).
  streamCam: null,         // camera của luồng đang gắn vào #live-img
  streamFails: 0,          // số lần đã thử nối lại
  streamDeadSince: 0,      // mốc thời gian phát hiện ảnh không còn khung hình
  wsEverOpen: false,       // đã từng nối được WebSocket
  wsDownAt: 0,             // lúc WebSocket rớt (để biết máy chủ vừa khởi động lại)
  everWentDown: false,     // màn "Ứng dụng đang dừng" đã thấy máy chủ tắt hẳn
};

const ZE = {
  cameraId: LS.get('zoneCamera'),
  zones: [],
  active: 0,
  mode: 'poly',           // công cụ vẽ vùng mới: 'poly' | 'rect'
  idLocked: true,         // mã vùng cố định → đổi tên không phá kịch bản
  idRenames: {},          // old_id → new_id, gửi kèm khi lưu
  autoIds: new Set(),     // mã vùng mới tạo (chưa lưu) → tự sinh theo tên
  draft: [],              // điểm đang vẽ (toạ độ chuẩn hoá)
  selectedVertex: -1,
  drag: null,
  hover: null,            // {kind, index, zoneIndex} đang rê chuột lên
  tipAt: 0,               // lần cuối hiện gợi ý (chống spam toast)
  rectStart: null,
  history: [],
  future: [],
  dirty: false,
  frozen: false,          // true = đang vẽ trên ẢNH TĨNH (đóng băng khung hình)
  frozenSrc: null,        // data URL của khung hình đã chụp
};

/* ------------------------------------------------------------------- tabs */
/* Có thay đổi vùng chưa lưu thì PHẢI hỏi trước khi ghi đè — trước đây đổi tab
 * hoặc đổi camera là mất sạch công sức kéo/thả mà không một lời cảnh báo. */
function confirmDiscardZones(reason) {
  if (!ZE.dirty) return true;
  const answer = window.confirm(
    'Bạn có thay đổi vùng CHƯA LƯU (' + reason + ').\n\n'
    + 'Bấm OK để BỎ các thay đổi đó.\n'
    + 'Bấm Cancel để ở lại tab “Vùng phát hiện” và bấm “💾 Lưu vùng” trước.'
  );
  if (answer) ZE.dirty = false;
  return answer;
}

let _dirtyShown = null;
function updateDirtyBadge() {
  if (_dirtyShown === ZE.dirty) return;   // chống ghi DOM mỗi khung hình
  _dirtyShown = ZE.dirty;
  const tab = document.querySelector('#tabs button[data-tab="zones"]');
  if (tab) {
    tab.classList.toggle('dirty', !!ZE.dirty);
    tab.textContent = (ZE.dirty ? '• ' : '') + 'Vùng phát hiện';
  }
  const save = $('#zone-save');
  if (save) save.classList.toggle('attention', !!ZE.dirty);
}

function switchTab(name) {
  if (S.tab === name) return;
  if (S.tab === 'zones' && !confirmDiscardZones('bạn đang chuyển sang tab khác')) return;
  S.tab = name;
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'zones') { loadZoneEditor().then(autoFreezeWhenReady); }
  if (name === 'scenarios') { renderScenarioPanel(); }
  if (name === 'settings') { renderSettings(); loadWebcams(); }
  if (name === 'mqtt') { loadMonitor(); }
  if (name === 'logs') { renderLogs(); }
  if (name === 'simulate') { renderSimulator(); }
  updateDirtyBadge();
}

/* ------------------------------------------------------------ websocket */
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = new URLSearchParams(location.search).get('token');
  const url = proto + '://' + location.host + '/ws' + (token ? '?token=' + encodeURIComponent(token) : '');
  let socket;
  try { socket = new WebSocket(url); } catch (e) { scheduleWsRetry(); return; }
  S.ws = socket;

  socket.onopen = () => {
    S.wsRetry = 0;
    const reconnected = S.wsEverOpen;
    S.wsEverOpen = true;
    // Nối lại được sau khi đã rớt ⇒ máy chủ (hoặc mạng) vừa trở lại: luồng
    // MJPEG cũ chắc chắn đã chết, phải xin luồng mới thì mới có hình.
    if (reconnected) resumeAfterReconnect();
  };
  socket.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (e) { return; }
    if (payload.type === 'state') { S.state = payload.state; noteAssetVersion(S.state); renderAll(); }
    else if (payload.type === 'event') { onEvent(payload.event); }
  };
  socket.onclose = (event) => {
    if (event.code === 4401) { location.href = '/login'; return; }
    if (!S.wsDownAt) S.wsDownAt = Date.now();
    scheduleWsRetry();
  };
  socket.onerror = () => { try { socket.close(); } catch (e) {} };
}

function scheduleWsRetry() {
  if (S.stopped) return;            // đang tắt hệ thống: đừng nối lại
  S.wsRetry = Math.min(15, S.wsRetry + 1);
  setTimeout(connectWs, 500 * S.wsRetry);
}

// --------------------------------------------------------------------------
// "Giao diện đang là bản cũ" — cảnh báo khi máy chủ đã có app.js mới nhưng tab
// này vẫn đang chạy bản cũ đã nạp trước đó.
//
// Vì sao cần: mỗi lần cập nhật, người dùng mở sẵn tab từ hôm trước; tab giữ
// app.js cũ trong bộ nhớ nên mọi nút mới (hoặc nút đã sửa) "bấm không ăn" dù
// máy chủ đã phục vụ bản mới. Banner này nói thẳng ra và cho bấm nạp lại.
function noteAssetVersion(state) {
  const version = state && state.asset_version;
  if (!version || S.staleNotified) return;
  if (!S.assetVersion) { S.assetVersion = String(version); return; }
  if (String(version) === S.assetVersion) return;
  S.staleNotified = true;
  showStaleBanner();
}

function showStaleBanner() {
  if ($('#ui-stale-banner')) return;
  const bar = document.createElement('div');
  bar.id = 'ui-stale-banner';
  bar.className = 'ui-stale-banner';
  bar.setAttribute('role', 'alert');
  bar.innerHTML = '<span>🔄 Máy chủ vừa có bản giao diện mới. Trang này vẫn đang chạy bản cũ nên một số nút có thể không phản hồi.</span>'
    + '<button type="button" id="ui-stale-reload">Nạp lại ngay</button>';
  document.body.appendChild(bar);
  $('#ui-stale-reload').addEventListener('click', () => location.reload());
}

function onEvent(event) {
  S.logLines.push(event);
  if (S.logLines.length > 800) S.logLines.splice(0, S.logLines.length - 800);
  if (['command', 'zone_enter', 'zone_exit', 'camera', 'mqtt', 'config', 'error'].includes(event.kind)) {
    S.events.push(event);
    if (S.events.length > 400) S.events.shift();
  }
  appendLogLine(event);
  appendEventRow(event);
  if (event.level === 'error') toast(event.message, 'err', 6000);
  if (event.kind === 'config' && S.tab === 'scenarios') { loadConfig(); }
}

/* --------------------------------------------------------------- rendering */
/* Vì sao cần setHtml(): trạng thái được đẩy về 8 lần/giây, nên các khối như
 * danh sách camera / nút mô phỏng bị dựng lại liên tục. Nếu dựng lại ĐÚNG LÚC
 * người dùng đang giữ chuột thì node cũ bị xoá trước khi nhả chuột → trình
 * duyệt KHÔNG bắn sự kiện click, người dùng thấy "nút bấm không ăn".
 * Hai lớp bảo vệ: (1) chỉ ghi DOM khi HTML thật sự khác, (2) không ghi DOM khi
 * đang giữ chuột — nhả ra sẽ vẽ lại một lần. */
function setHtml(el, html) {
  if (!el) return false;
  if (el.__vaHtml === html) return false;
  el.__vaHtml = html;
  if (S.pointerBusy) { el.__vaPending = html; return false; }
  el.__vaPending = null;
  el.innerHTML = html;
  return true;
}

function markPointerBusy(busy) {
  const was = S.pointerBusy;
  S.pointerBusy = busy;
  if (was && !busy) renderAll();
}

function installPointerGuard() {
  document.addEventListener('pointerdown', () => markPointerBusy(true), true);
  document.addEventListener('pointerup', () => markPointerBusy(false), true);
  document.addEventListener('pointercancel', () => markPointerBusy(false), true);
  window.addEventListener('blur', () => markPointerBusy(false));
}

function renderAll() {
  if (!S.state) return;
  renderHeader();
  renderLive();
  renderSimulator();
  renderScenarioStatus($('#sc-status-list'));
  renderScenarioStatus($('#log-sc-status'));
  if (S.tab === 'zones') renderZoneSidebar();
  if (S.tab === 'mqtt') renderMqttStatus();
  if (S.tab === 'settings') renderSettingsCameras();
  renderDiagnostics();
}

/* =========================== CHẨN ĐOÁN ================================ */
/* Một chỗ duy nhất trả lời "hệ thống đang khoẻ hay không": khung hình cũ bao
 * nhiêu ms, suy luận bao nhiêu fps, model có lỗi gì, MQTT có đang xếp hàng,
 * và vì sao từng kịch bản đang (không) làm gì. */
function diagLevel(value, warnAt, badAt) {
  if (value == null) return '';
  if (value >= badAt) return 'bad';
  if (value >= warnAt) return 'warn';
  return 'ok';
}

function renderDiagnostics() {
  const box = $('#diag-cameras');
  if (!box || !S.state) return;
  const diag = S.state.diagnostics || {};
  const cameras = diag.cameras || {};
  const ids = Object.keys(cameras);
  setHtml(box, ids.length ? ids.map((id) => {
    const row = cameras[id] || {};
    const zones = row.zones || {};
    const zoneRows = Object.keys(zones).map((zoneId) => {
      const zone = zones[zoneId] || {};
      const cameraOff = row.enabled === false || row.status === 'off';
      const last = zone.occupied === true ? 'có người ×' + (zone.count || 1)
        : (zone.occupied === false ? 'trống' : 'chưa rõ');
      // Camera tắt ⇒ trạng thái vùng là ảnh chụp cũ, không được khẳng định "có người".
      const state = cameraOff
        ? '<span class="badge gray">giữ trạng thái cũ: ' + escapeHtml(last) + '</span>'
        : (zone.occupied === true ? '<span class="badge green">có người ×' + (zone.count || 1) + '</span>'
          : (zone.occupied === false ? '<span class="badge gray">trống</span>' : '<span class="badge amber">chưa rõ</span>'));
      const wait = zone.pending_exit_s != null && !cameraOff ? ' · chờ xác nhận vắng ' + Number(zone.pending_exit_s).toFixed(1) + 's' : '';
      const forced = zone.forced ? ' · <b>đang mô phỏng</b>' : '';
      if (cameraOff) {
        return '<div class="diag-row"><span class="mono">' + escapeHtml(zoneId) + '</span>' + state +
          '<span class="small muted"> · không đánh giá (camera đang tắt)' + escapeHtml(forced) + '</span></div>';
      }
      return '<div class="diag-row"><span class="mono">' + escapeHtml(zoneId) + '</span>' + state +
        '<span class="small muted">' + escapeHtml(wait + forced) + '</span></div>';
    }).join('');
    // Tình trạng do máy chủ quyết định (row.status). Camera người dùng chủ động
    // TẮT không phải lỗi: trước đây nó hiện "mất hình" đỏ + viền đỏ, khiến người
    // dùng tưởng hệ thống hỏng trong khi mọi thứ bình thường.
    const CAM_STATUS = {
      off: { cls: 'gray', text: 'đang tắt' },
      ok: { cls: 'green', text: 'đang nhận hình' },
      stale: { cls: 'amber', text: 'hình bị treo' },
      lost: { cls: 'red', text: 'mất hình' },
      starting: { cls: 'amber', text: 'đang kết nối' },
    };
    const camStatus = row.status || (row.enabled === false ? 'off' : (row.connected ? 'ok' : 'lost'));
    const camInfo = CAM_STATUS[camStatus] || CAM_STATUS.lost;
    const camOff = camStatus === 'off';
    const bad = camStatus === 'lost' ? 'bad' : (camOff ? 'off' : '');
    return '<div class="diag-cam ' + bad + '">' +
      '<div class="diag-head"><b>' + escapeHtml(id) + '</b>' +
      '<span class="badge ' + camInfo.cls + '">' + camInfo.text + '</span>' +
      (row.sampled_frame === false && !camOff ? '<span class="badge amber">khung suy luận lỗi</span>' : '') +
      '</div>' +
      '<div class="diag-grid">' +
      '<span>FPS camera</span><b>' + (row.capture_fps || 0).toFixed(1) + '</b>' +
      '<span>Nguồn báo</span><b>' + (row.stream_fps || 0).toFixed(0) + ' fps</b>' +
      '<span>Tuổi khung hình</span><b class="' + diagLevel(row.frame_age_ms, 800, 2000) + '">' +
        Math.round(row.frame_age_ms || 0) + ' ms</b>' +
      '<span>Thời gian đọc</span><b>' + Math.round(row.read_ms || 0) + ' ms</b>' +
      '<span>FPS suy luận</span><b>' + (row.infer_fps || 0).toFixed(1) + ' / ' + (row.detect_fps_target || 0).toFixed(1) + '</b>' +
      '<span>Suy luận</span><b>' + Math.round(row.inference_ms || 0) + ' ms</b>' +
      '<span>Lần suy luận trước</span><b>' + (row.infer_age_s == null ? '–' : row.infer_age_s + ' s') + '</b>' +
      '<span>Lỗi suy luận</span><b class="' + (row.inference_errors ? 'warn' : '') + '">' + (row.inference_errors || 0) +
        (row.inference_errors_consecutive ? ' (liên tiếp ' + row.inference_errors_consecutive + ')' : '') + '</b>' +
      '<span>Mở lại kết nối</span><b>' + (row.reconnects || 0) + '</b>' +
      '</div>' +
      (camOff
        ? '<div class="diag-note">Camera đang tắt theo ý người dùng — không phải lỗi. Bật lại bằng công tắc ở tab Cài đặt.</div>'
        : (row.error ? '<div class="diag-err">' + escapeHtml(row.error + (row.hint ? ' — ' + row.hint : '')) + '</div>' : '')
          + (row.inference_last_error ? '<div class="diag-err">Suy luận: ' + escapeHtml(row.inference_last_error) + '</div>' : '')) +
      '<div class="diag-zones">' + (zoneRows || '<span class="small muted">chưa có vùng</span>') + '</div>' +
      '</div>';
  }).join('') : '<p class="small muted">Chưa có camera.</p>');

  const mqtt = diag.mqtt || {};
  const det = diag.detector || {};
  const res = diag.resources || {};
  const summary = $('#diag-summary');
  if (summary) {
    const disk = res.disk || {};
    const logs = res.logs || {};
    const lastCleanup = res.last_cleanup || {};
    const cleanupText = lastCleanup.at
      ? ('lần dọn gần nhất: ' + (lastCleanup.removed ? 'đã xoá ' + lastCleanup.removed + ' file cũ' : 'không có file cũ'))
      : 'chưa chạy';
    // Dòng tài nguyên: chạy 24/7 nên RAM và ổ đĩa là thứ người dùng cần thấy.
    const resLine = (res.rss_mb == null && !disk.ok) ? '' :
      'RAM: <b>' + (res.rss_mb == null ? '–' : Math.round(res.rss_mb) + ' MB') + '</b>' +
      ' · Ổ đĩa trống: <b class="' + (disk.free_gb != null && disk.free_gb < 2 ? 'bad'
        : (disk.free_gb != null && disk.free_gb < 10 ? 'warn' : '')) + '">' +
        (disk.free_gb == null ? '–' : disk.free_gb.toFixed(1) + ' GB') + '</b>' +
      ' · Nhật ký: <b>' + (logs.mb == null ? '–' : logs.mb.toFixed(1) + ' MB') + '</b>' +
      ' · giữ ' + (res.retention_days || 30) + ' ngày (' + escapeHtml(cleanupText) + ')';
    setHtml(summary, [
      'Model: <b>' + escapeHtml(det.name || '–') + '</b> · thiết bị <b>' + escapeHtml(det.device || '–') + '</b>',
      'MQTT: <b>' + (mqtt.connected ? 'đã nối' : 'mất kết nối') + '</b>' +
        (mqtt.queued ? ' · <b class="warn">' + mqtt.queued + ' lệnh đang xếp hàng</b>' : '') +
        ' · đã gửi ' + (mqtt.sent || 0) + ' / nhận ' + (mqtt.received || 0),
      'Lỗi suy luận tổng: <b>' + (det.errors_total || 0) + '</b>',
      resLine,
    ].filter(Boolean).map((line) => '<div class="diag-line">' + line + '</div>').join(''));
  }

  const decisions = $('#diag-decisions');
  if (decisions) {
    const rows = diag.decisions || [];
    setHtml(decisions, rows.length ? rows.slice().reverse().map((item) =>
      '<div class="diag-decision"><span class="mono">' + escapeHtml((item.time || '').slice(11)) + '</span>' +
      '<span class="' + (item.kind === 'command' ? 'ok' : '') + '">' + escapeHtml(item.message || '') + '</span></div>'
    ).join('') : '<p class="small muted">Chưa có quyết định nào.</p>');
  }

  const warnBox = $('#diag-warnings');
  if (warnBox) {
    const warnings = (S.state.warnings || []).concat(diag.warnings || []);
    setHtml(warnBox, warnings.length
      ? warnings.map((text) => '<div class="diag-err">⚠ ' + escapeHtml(text) + '</div>').join('')
      : '');
    warnBox.classList.toggle('show', warnings.length > 0);
  }
}

function renderHeader() {
  const st = S.state;
  const cameras = st.cameras || [];
  const anyOnline = cameras.some((c) => c.capture && c.capture.connected);
  // Camera bị người dùng tắt KHÔNG phải "Camera mất": nếu tất cả camera đều tắt
  // thì chấm xám + nhãn "Camera đang tắt" để không báo động sai.
  const allOff = cameras.length > 0 && cameras.every((c) => (c.status || (c.enabled === false ? 'off' : '')) === 'off');
  const camDot = $('#dot-camera');
  camDot.className = 'dot ' + (cameras.length === 0 ? '' : (anyOnline ? 'ok' : (allOff ? 'off' : 'bad')));
  $('#lbl-camera').textContent = cameras.length === 0 ? 'Chưa có camera'
    : (anyOnline ? 'Camera OK' : (allOff ? 'Camera đang tắt' : 'Camera mất'));

  const mqtt = st.mqtt || {};
  $('#dot-mqtt').className = 'dot ' + (mqtt.connected ? 'ok' : 'bad');
  $('#lbl-mqtt').textContent = mqtt.connected ? 'MQTT OK' : 'MQTT lỗi';

  const det = st.detector || {};
  $('#dot-detector').className = 'dot ' + (det.available ? 'ok' : 'warn');
  $('#lbl-detector').textContent = det.available ? ('Model ' + (det.device || '')) : 'Model chưa sẵn sàng';

  $('#uptime-label').textContent = 'hoạt động ' + fmtUptime(st.uptime_s);

  const toggle = $('#dry-run-toggle');
  toggle.checked = !!st.dry_run;
  document.body.classList.toggle('dry-run', !!st.dry_run);
}

function enabledCameras() {
  return ((S.state && S.state.cameras) || []).filter((c) => c.enabled !== false);
}

/* Camera đang chọn được nhớ trong localStorage và dùng chung cho tab Trực tiếp
 * và tab Vùng phát hiện, để hai tab không bao giờ nói về hai camera khác nhau. */
function setSelectedCamera(cameraId, options = {}) {
  const from = options.from || 'ui';
  if (!cameraId) return;
  S.liveCamera = cameraId;
  LS.set('liveCamera', cameraId);
  const exists = enabledCameras().some((c) => c.id === cameraId);
  if (exists && ZE.cameraId !== cameraId) {
    ZE.cameraId = cameraId;
    LS.set('zoneCamera', cameraId);
    if (from === 'zone' && S.config) {
      ZE.active = 0;
    }
  }
}

function currentLiveCamera() {
  const cameras = enabledCameras();
  if (!cameras.length) return null;
  let cam = cameras.find((c) => c.id === S.liveCamera);
  if (!cam) {
    // Camera đã nhớ bị xoá/tắt → về camera đầu tiên và nhớ luôn lựa chọn mới.
    cam = cameras[0];
    setSelectedCamera(cam.id, { from: 'auto' });
  }
  return cam;
}

function renderCameraSelects() {
  const cameras = enabledCameras();
  const options = cameras.map((c) =>
    '<option value="' + escapeHtml(c.id) + '">' + escapeHtml(c.name) +
    (c.test ? ' (TEST)' : '') + '</option>').join('');

  const live = $('#live-camera-select');
  if (live.dataset.signature !== options) {
    live.innerHTML = options || '<option value="">(chưa có camera)</option>';
    live.dataset.signature = options;
  }
  const liveCam = currentLiveCamera();
  if (liveCam && live.value !== liveCam.id) live.value = liveCam.id;

  const zsel = $('#zone-camera-select');
  if (zsel.dataset.signature !== options) {
    zsel.innerHTML = options || '<option value="">(chưa có camera)</option>';
    zsel.dataset.signature = options;
  }
  if (cameras.length) {
    const wanted = cameras.some((c) => c.id === ZE.cameraId) ? ZE.cameraId : cameras[0].id;
    if (zsel.value !== wanted) zsel.value = wanted;
  }
}

/* --------------------------------------------------- luồng MJPEG (watchdog)
 *
 * Trình duyệt chỉ xin luồng `<img src="/stream/...mjpg">` ĐÚNG MỘT LẦN cho mỗi
 * giá trị src. Khi máy chủ khởi động lại (hoặc mạng chớp), kết nối multipart
 * cũ chết nhưng thuộc tính src vẫn giữ nguyên → trình duyệt KHÔNG tự xin lại,
 * nên khung hình thành ĐEN trong khi số liệu (FPS camera, FPS suy luận) vẫn
 * chạy bình thường vì WebSocket đã tự nối lại. Đây chính là lỗi "mở lại app
 * thì mất hình".
 *
 * Cách chữa: (1) luôn thêm tham số chống đệm `?r=<thời điểm>` vào URL luồng,
 * (2) canh me mỗi 1,5 giây — nếu ảnh không có khung hình thì tự gắn lại src
 * (chờ 4s → 8s → 12s → tối đa 15s), (3) khi WebSocket vừa nối lại thì coi như
 * máy chủ vừa khởi động lại và xin luồng mới ngay.
 */
const STREAM_RETRY_BASE_MS = 4000;
const STREAM_RETRY_MAX_MS = 15000;

function streamUrl(cameraId, bust) {
  const base = '/stream/' + encodeURIComponent(cameraId) + '.mjpg' + tokenSuffix();
  if (!bust) return base;
  return base + (base.indexOf('?') >= 0 ? '&' : '?') + 'r=' + bust;
}

function showStreamNote(text) {
  const note = $('#live-stream-note');
  if (!note) return;
  note.textContent = text;
  note.classList.add('show');
}

function hideStreamNote() {
  const note = $('#live-stream-note');
  if (!note || !note.classList.contains('show')) return;
  note.classList.remove('show');
  note.textContent = '';
}

/** Gắn (hoặc gắn LẠI) luồng MJPEG cho #live-img. Luôn dùng URL mới. */
function armLiveStream(cam, reason) {
  const img = $('#live-img');
  if (!img || !cam) return false;
  S.streamCam = cam.id;
  img.dataset.cam = cam.id;
  img.src = streamUrl(cam.id, Date.now());
  if (reason === 'retry' || reason === 'reconnect' || reason === 'manual') {
    S.streamDeadSince = Date.now();
  }
  return true;
}

/** Ảnh đang có khung hình thật hay chưa (naturalWidth = 0 → chưa có gì để vẽ). */
function liveStreamHasPixels() {
  const img = $('#live-img');
  if (!img) return false;
  return !!(img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
}

/** Chạy mỗi 1,5 s: nếu luồng chết thì xin lại (kể cả khi máy chủ đã có hình). */
function checkLiveStream() {
  if (S.stopped) return;
  const img = $('#live-img');
  if (!img) return;
  if (liveStreamHasPixels()) {
    if (S.streamFails || S.streamDeadSince) {
      S.streamFails = 0;
      S.streamDeadSince = 0;
      hideStreamNote();
    }
    return;
  }
  const cam = currentLiveCamera();
  if (!cam) return;
  const now = Date.now();
  if (!img.getAttribute('src')) { armLiveStream(cam, 'missing'); return; }
  if (!S.streamDeadSince) { S.streamDeadSince = now; return; }
  const wait = Math.min(STREAM_RETRY_MAX_MS, STREAM_RETRY_BASE_MS * Math.max(1, S.streamFails));
  if (now - S.streamDeadSince < wait) return;
  S.streamFails += 1;
  armLiveStream(cam, 'retry');
  showStreamNote('Chưa nhận được hình — đang tự nối lại (lần ' + S.streamFails + ')…');
}

/**
 * Máy chủ vừa nối lại (WebSocket mở được sau khi đã rớt) → phục hồi mọi thứ
 * mà lần khởi động lại đã làm chết: luồng Live, ảnh tab Vùng, màn "đang dừng".
 */
function resumeAfterReconnect() {
  const screen = $('#stopped-screen');
  if (screen) {
    screen.remove();
    S.stopped = false;
    S.everWentDown = false;
  }
  const wasDownLong = S.wsDownAt && (Date.now() - S.wsDownAt > 3000);
  // WebSocket đã đóng hẳn (không phải đang mở/đang nối) thì mở lại.
  if (!S.stopped && (!S.ws || S.ws.readyState > 1)) connectWs();

  if (!liveStreamHasPixels()) {
    const cam = currentLiveCamera();
    if (cam) armLiveStream(cam, 'reconnect');
  }
  if (S.tab === 'zones' && !ZE.frozen) setZoneImageSource();
  if (wasDownLong) toast('Đã nối lại máy chủ — đang tải lại hình…', '');
}

function renderLive() {
  const cam = currentLiveCamera();
  renderCameraSelects();
  if (!cam) {
    $('#live-img').removeAttribute('src');
    $('#offline-msg').classList.add('show');
    $('#offline-msg').textContent = 'Chưa có camera — vào Cài đặt để thêm.';
    return;
  }
  const img = $('#live-img');
  if (img.dataset.cam !== cam.id || !img.getAttribute('src')) {
    armLiveStream(cam, 'pick');
  }

  const capture = cam.capture || {};
  const inference = cam.inference || {};
  $('#st-capture').textContent = (capture.capture_fps || 0).toFixed(1) + ' fps';
  $('#st-infer').textContent = (inference.fps || 0).toFixed(1) + ' fps';
  $('#st-ms').textContent = (inference.ms || 0).toFixed(0) + ' ms';
  // Frame age = how old the picture on screen is. A rising value means the
  // transport is buffering (backlog) — show it instead of leaving the user
  // guessing why the "live" view lags behind reality.
  const ageMs = Number(capture.frame_age_ms || 0);
  const readMs = Number(capture.read_ms || 0);
  const streamFps = Number(capture.stream_fps || 0);
  const latencyBox = $('#st-latency-box');
  if (latencyBox) {
    const value = $('#st-latency');
    if (!capture.connected) {
      value.textContent = '–';
      latencyBox.classList.remove('warn');
    } else if (ageMs >= 1000) {
      value.textContent = (ageMs / 1000).toFixed(1) + ' s';
      latencyBox.classList.add('warn');
    } else {
      value.textContent = Math.round(ageMs) + ' ms';
      latencyBox.classList.remove('warn');
    }
    latencyBox.title =
      'Tuổi khung hình đang hiển thị: ' + Math.round(ageMs) + ' ms' +
      (readMs ? ' · đọc ' + Math.round(readMs) + ' ms/khung' : '') +
      (streamFps ? ' · nguồn báo ' + streamFps.toFixed(0) + ' fps' : '');
  }
  const det = (S.state && S.state.detector) || {};
  $('#st-device').textContent = det.device || '–';
  if (S.tab === 'zones') renderZoneAnalysis();
  const zones = cam.zones || [];
  const occupied = zones.filter((z) => {
    const zs = (cam.zone_states || {})[z.id];
    return zs && zs.occupied === true;
  }).length;
  $('#st-occupied').textContent = occupied + '/' + zones.length;
  $('#live-cam-hint').textContent = cam.test ? '· camera TEST' : '';

  const offline = $('#offline-msg');
  if (!capture.connected || capture.stale) {
    offline.classList.add('show');
    offline.classList.remove('slow');
    offline.textContent = capture.error
      ? ('Mất hình: ' + capture.error + (capture.hint ? ' — ' + capture.hint : ''))
      : 'Mất tín hiệu camera — đang hiển thị hình cuối cùng.';
  } else if (ageMs >= 2000) {
    // Stream is alive but the picture is old: tell the user it is the
    // transport (Wi-Fi/RTSP buffer), not a dead camera.
    offline.classList.add('show', 'slow');
    offline.textContent =
      'Hình đang trễ ' + (ageMs / 1000).toFixed(1) + ' s — đường truyền đang đệm khung. ' +
      'Thử giảm độ phân giải camera hoặc đổi transport sang UDP.';
  } else {
    offline.classList.remove('show', 'slow');
  }

  const list = $('#live-zones');
  setHtml(list, zones.length ? zones.map((zone) => {
    const zs = (cam.zone_states || {})[zone.id] || {};
    const state = zs.occupied === true ? 'green' : (zs.occupied === false ? 'gray' : 'amber');
    const label = zs.occupied === true ? ('Có người ×' + (zs.count || 1)) :
      (zs.occupied === false ? 'Trống' : 'Đang xác định…');
    // Khi vùng trống, hiện LUÔN lý do (bao nhiêu đối tượng bị loại và vì sao)
    // để người dùng biết phải kéo vùng rộng ra hay đổi cách xác định.
    const analysis = zs.analysis || {};
    const meta = (zone.classes || []).join(', ');
    const why = zs.occupied === true ? ''
      : (analysis.reason ? ' · ' + analysis.reason : '');
    const detail = zs.occupied === true ? '' :
      (analysis.detections
        ? '<span class="zwarn">khung có ' + analysis.detections + ' đối tượng'
          + (analysis.outside ? ', ' + analysis.outside + ' ngoài vùng' : '')
          + (analysis.ignored_confidence ? ', ' + analysis.ignored_confidence + ' dưới ngưỡng' : '')
          + (analysis.ignored_class ? ', ' + analysis.ignored_class + ' khác lớp' : '')
          + '</span>'
        : '');
    return '<div class="zone-item"><span class="swatch" style="background:' + escapeHtml(zone.color || '#22c55e') + '"></span>' +
      '<div><div class="zname">' + escapeHtml(zone.name || zone.id) + '</div>' +
      '<div class="zmeta">' + escapeHtml(meta + why) + '</div>' + detail + '</div>' +
      '<div class="zright"><span class="badge ' + state + '">' + label + '</span></div></div>';
  }).join('') : '<p class="small muted">Camera này chưa có vùng nào. Sang tab “Vùng phát hiện” để vẽ.</p>');
}

function tokenSuffix() {
  const token = new URLSearchParams(location.search).get('token');
  return token ? '?token=' + encodeURIComponent(token) : '';
}

/* =============================== LOG TAB =============================== */
function logFilterPass(event) {
  const kind = $('#log-kind').value;
  const level = $('#log-level').value;
  if (kind && event.kind !== kind) return false;
  if (level && event.level !== level) return false;
  return true;
}

function appendLogLine(event) {
  if (S.tab !== 'logs') return;
  if (!logFilterPass(event)) return;
  const stream = $('#log-stream');
  const line = document.createElement('div');
  line.className = 'log-line ' + (event.level || 'info');
  line.innerHTML = '<span class="t">' + escapeHtml(event.time || fmtTime(event.ts)) + '</span>' +
    '<span class="k">[' + escapeHtml(event.kind) + ']</span>' +
    '<span class="m">' + escapeHtml(event.message) + '</span>';
  stream.appendChild(line);
  if (stream.childElementCount > 500) stream.removeChild(stream.firstChild);
  stream.scrollTop = stream.scrollHeight;
}

function appendEventRow(event) {
  if (!['command', 'zone_enter', 'zone_exit', 'camera', 'mqtt', 'config', 'error'].includes(event.kind)) return;
  if (S.tab !== 'logs') return;
  const tbody = $('#events-table tbody');
  const row = document.createElement('tr');
  const ok = event.data && typeof event.data.ok === 'boolean' ? event.data.ok : null;
  const dry = event.data && event.data.dry_run;
  row.innerHTML = '<td class="mono">' + escapeHtml(event.time || '') + '</td>' +
    '<td>' + escapeHtml(event.kind) + '</td>' +
    '<td>' + escapeHtml(event.message) + '</td>' +
    '<td>' + (ok === null ? '–' : (dry ? '<span class="badge amber">dry-run</span>' :
      (ok ? '<span class="badge green">OK</span>' : '<span class="badge red">Lỗi</span>'))) + '</td>';
  tbody.prepend(row);
  while (tbody.childElementCount > 200) tbody.removeChild(tbody.lastChild);
}

function renderLogs() {
  const stream = $('#log-stream');
  stream.innerHTML = '';
  S.logLines.filter(logFilterPass).slice(-300).forEach(appendLogLine);
  const tbody = $('#events-table tbody');
  tbody.innerHTML = '';
  S.events.slice(-100).reverse().forEach((event) => {
    const ok = event.data && typeof event.data.ok === 'boolean' ? event.data.ok : null;
    const dry = event.data && event.data.dry_run;
    const row = document.createElement('tr');
    row.innerHTML = '<td class="mono">' + escapeHtml(event.time || '') + '</td>' +
      '<td>' + escapeHtml(event.kind) + '</td>' +
      '<td>' + escapeHtml(event.message) + '</td>' +
      '<td>' + (ok === null ? '–' : (dry ? '<span class="badge amber">dry-run</span>' :
        (ok ? '<span class="badge green">OK</span>' : '<span class="badge red">Lỗi</span>'))) + '</td>';
    tbody.appendChild(row);
  });
}

async function loadEvents() {
  try {
    const data = await api('/api/events?limit=150');
    S.events = (data.events || []).slice();
    if (S.tab === 'logs') renderLogs();
  } catch (e) { /* offline: ignore */ }
}

function renderScenarioStatus(target) {
  if (!target) return;
  const scenarios = (S.state && S.state.scenarios) || [];
  if (!scenarios.length) {
    setHtml(target, '<p class="small muted">Chưa có kịch bản nào.</p>');
    return;
  }
  setHtml(target, scenarios.map((sc) => {
    const pending = (sc.pending || [])[0];
    const badge = sc.occupied === true ? '<span class="badge green">Có người</span>' :
      (sc.occupied === false ? '<span class="badge gray">Trống</span>' : '<span class="badge amber">Chưa rõ</span>');
    const paused = sc.paused_remaining_s > 0
      ? '<span class="badge amber">Tạm dừng ' + Math.ceil(sc.paused_remaining_s) + 's</span>' : '';
    const sched = sc.schedule_active === false ? '<span class="badge gray">Ngoài giờ</span>' : '';
    const disabled = sc.enabled === false ? '<span class="badge red">Đang tắt</span>' : '';
    let pendingHtml = '<span class="small muted">Không có bước chờ</span>';
    let width = '0%';
    if (pending) {
      const label = pending.label || pending.trigger || 'bước';
      const inS = Number(pending.in_s || 0);
      pendingHtml = '<span class="small" style="color:#fcd34d">' + escapeHtml(label) + ' sau ' +
        inS.toFixed(1) + 's → ' + escapeHtml(pending.topic || '') + '</span>';
      width = Math.max(4, Math.min(100, 100 - Math.min(100, inS))) + '%';
    }
    return '<div class="sc-card' + (pending ? ' pending' : '') + '">' +
      '<div class="sc-head"><b>' + escapeHtml(sc.name || sc.id) + '</b>' + badge + paused + sched + disabled +
      '<span class="small muted mono">' + escapeHtml(sc.id) + '</span></div>' +
      '<div style="margin-top:6px">' + pendingHtml + '</div>' +
      '<div class="bar"><i style="width:' + width + '"></i></div>' +
      (sc.reason ? '<div class="why">💡 ' + escapeHtml(sc.reason) + '</div>' : '') +
      '</div>';
  }).join(''));
}

/* ============================ SIMULATION TAB =========================== */
function renderSimulator() {
  const cameras = enabledCameras();
  const sim = (S.state && S.state.simulator) || { zones: {}, scale: 1 };
  const container = $('#sim-zones');
  const rows = [];
  cameras.forEach((cam) => {
    (cam.zones || []).forEach((zone) => {
      const ref = cam.id + '/' + zone.id;
      const forced = sim.zones[ref];
      const zs = (cam.zone_states || {})[zone.id] || {};
      const badge = forced
        ? '<span class="badge amber">Đang mô phỏng: ' + (forced.occupied ? 'có người' : 'trống') + '</span>'
        : (zs.occupied === true ? '<span class="badge green">Có người</span>' :
          (zs.occupied === false ? '<span class="badge gray">Trống</span>' : '<span class="badge gray">Chưa rõ</span>'));
      rows.push('<div class="zone-item"><span class="swatch" style="background:' + escapeHtml(zone.color || '#22c55e') + '"></span>' +
        '<div><div class="zname">' + escapeHtml(zone.name || zone.id) + '</div>' +
        '<div class="zmeta">' + escapeHtml(cam.name) + ' · ' + escapeHtml(cam.id + '/' + zone.id) + '</div></div>' +
        '<div class="zright">' + badge +
        '<button class="btn small" data-sim="occupy" data-zone="' + escapeHtml(ref) + '">Có người</button>' +
        '<button class="btn small" data-sim="vacate" data-zone="' + escapeHtml(ref) + '">Trống</button>' +
        '<button class="btn small ghost" data-sim="clear" data-zone="' + escapeHtml(ref) + '">Tắt</button>' +
        '</div></div>');
    });
  });
  setHtml(container, rows.length ? rows.join('') : '<p class="small muted">Chưa có vùng nào để mô phỏng.</p>');

  $$('#sim-speed .chip').forEach((chip) => {
    chip.classList.toggle('active', Number(chip.dataset.scale) === Number(sim.scale || 1));
  });
  $('#sim-speed-label').textContent = 'Đang chạy: x' + (sim.scale || 1);
}

async function simulateZone(ref, action) {
  try {
    if (action === 'clear') {
      await api('/api/simulate/clear', { method: 'POST', body: { zone: ref } });
      toast('Đã tắt mô phỏng cho ' + ref, 'ok');
    } else {
      const count = Number($('#sim-count').value || 1);
      await api('/api/simulate', {
        method: 'POST',
        body: { zone: ref, occupied: action === 'occupy', count },
      });
      toast('Mô phỏng ' + ref + ': ' + (action === 'occupy' ? 'có người' : 'trống'), 'ok');
    }
  } catch (e) { toast(e.message, 'err'); }
}

async function setSimulationScale(scale) {
  try {
    const result = await api('/api/simulate/scale', { method: 'POST', body: { scale } });
    $$('#sim-speed .chip').forEach((c) => c.classList.toggle('active', Number(c.dataset.scale) === Number(result.scale)));
    $('#sim-speed-label').textContent = 'Đang chạy: x' + result.scale;
  } catch (e) { toast(e.message, 'err'); }
}

/* ============================= MQTT TAB =============================== */
function renderMqttStatus() {
  const mqtt = (S.state && S.state.mqtt) || {};
  const badge = $('#mqtt-state-badge');
  badge.className = 'badge ' + (mqtt.connected ? 'green' : 'red');
  badge.textContent = mqtt.connected ? '● Đã kết nối' : '● Mất kết nối';
  $('#mqtt-broker').textContent = (mqtt.host || '') + ':' + (mqtt.port || '') +
    (mqtt.dry_run ? ' · chế độ dry-run' : '');
  $('#mqtt-sent').textContent = mqtt.sent ?? 0;
  $('#mqtt-recv').textContent = mqtt.received ?? 0;
  $('#mqtt-pending').textContent = mqtt.pending ?? 0;
  $('#mqtt-dropped').textContent = mqtt.dropped ?? 0;
  const error = $('#mqtt-error');
  if (mqtt.error) { error.style.display = 'block'; error.textContent = mqtt.error; }
  else { error.style.display = 'none'; }
}

async function loadMonitor() {
  const topic = $('#mon-topic').value.trim();
  const direction = $('#mon-direction').value;
  try {
    const data = await api('/api/mqtt/monitor?topic=' + encodeURIComponent(topic) +
      '&direction=' + encodeURIComponent(direction) + '&limit=150');
    const tbody = $('#mon-table tbody');
    setHtml(tbody, (data.entries || []).slice().reverse().map((entry) =>
      '<tr><td class="mono">' + escapeHtml(entry.time) + '</td>' +
      '<td>' + (entry.direction === 'tx' ? '↑ gửi' : '↓ nhận') + (entry.dry_run ? ' (dry)' : '') + '</td>' +
      '<td class="mono">' + escapeHtml(entry.topic) + '</td>' +
      '<td class="mono">' + escapeHtml(entry.payload) + '</td>' +
      '<td>' + (entry.ok ? '✔' : '✘') + '</td></tr>').join(''));
  } catch (e) { toast(e.message, 'err'); }
}

/* ============================== SETTINGS ============================== */
function renderSettingsCameras() {
  const container = $('#settings-cameras');
  const cameras = (S.state && S.state.cameras) || [];
  if (!cameras.length) {
    setHtml(container, '<p class="small muted">Chưa có camera nào.</p>');
    return;
  }
  setHtml(container, cameras.map((cam) => {
    const capture = cam.capture || {};
    const check = (S.camChecks || {})[cam.id];
    // Dòng phụ: lỗi + gợi ý sửa (hint) — trước đây chỉ hiện "error" nên người
    // dùng không biết phải làm gì.
    const camOff = cam.enabled === false || cam.status === 'off';
    let detail = escapeHtml(cam.id) + ' · ' + escapeHtml(cam.source) +
      ' · ' + escapeHtml(capture.source || '');
    // Camera chủ động tắt: ghi "camera đang tắt", không gắn ⚠ (không phải lỗi).
    if (camOff) detail += ' · tắt theo ý người dùng';
    else if (capture.error) detail += ' · ⚠ ' + escapeHtml(capture.error);
    const hintLine = (!camOff && capture.hint)
      ? '<div class="zmeta fix">💡 ' + escapeHtml(capture.hint) + '</div>' : '';
    const checkLine = check
      ? (check.pending
        ? '<div class="zmeta">⏳ Đang kiểm tra kết nối…</div>'
        : '<div class="zmeta ' + (check.ok ? 'fix ok' : 'fix bad') + '">' +
          (check.ok ? '✔ ' : '✖ ') + escapeHtml(check.message || '') +
          (check.hint ? '<br>💡 ' + escapeHtml(check.hint) : '') +
          (check.detail ? '<br><span class="mono muted">' + escapeHtml(check.detail) + '</span>' : '') +
          '</div>')
      : '';
    return '<div class="zone-item" style="margin-bottom:8px">' +
      '<span class="dot ' + (camOff ? 'off' : (capture.connected ? 'ok' : 'bad')) + '"></span>' +
      '<div style="min-width:0"><div class="zname">' + escapeHtml(cam.name) +
      (cam.test ? ' <span class="badge amber">TEST</span>' : '') +
      (cam.enabled === false ? ' <span class="badge red">đang tắt</span>' : '') + '</div>' +
      '<div class="zmeta mono">' + detail + '</div>' + hintLine + checkLine + '</div>' +
      '<div class="zright">' +
      '<button class="btn small" data-cam-check="' + escapeHtml(cam.id) +
        '" title="Thử phân giải tên miền, mở cổng và hỏi RTSP xem camera có trả lời không">' +
        '🔌 Kiểm tra</button>' +
      '<label class="switch" title="Bật/tắt camera này"><input type="checkbox" data-cam-enabled="' +
      escapeHtml(cam.id) + '"' + (cam.enabled === false ? '' : ' checked') +
      '><span class="track"></span></label>' +
      (cam.test ? '<button class="btn small" data-cam-promote="' + escapeHtml(cam.id) +
        '">Chuyển thành camera thật</button>' : '') +
      '<button class="btn small danger" data-cam-delete="' + escapeHtml(cam.id) + '">Xoá</button>' +
      '<button class="btn small ghost" data-cam-watch="' + escapeHtml(cam.id) + '">Xem</button>' +
      '</div></div>';
  }).join(''));
}

function renderSettings() {
  const cfg = S.config;
  if (!cfg) return;
  const det = cfg.detector || {};
  const mq = cfg.mqtt || {};
  const web = cfg.web || {};
  const auth = (web.auth) || {};

  $('#det-model').value = det.model || 'yolo11n.pt';
  $('#det-device').value = det.device || 'auto';
  $('#det-imgsz').value = det.imgsz || 640;
  $('#det-conf').value = Math.round((det.min_confidence ?? 0.45) * 100);
  $('#det-conf-val').textContent = Math.round((det.min_confidence ?? 0.45) * 100) + '%';
  $('#det-fps').value = det.detect_fps || 4;
  $('#det-motion').checked = !!det.motion_gating;

  $('#mq-host').value = mq.host || '';
  $('#mq-port').value = mq.port || 1883;
  $('#mq-client').value = mq.client_id || 'vision-automation';
  $('#mq-user').value = mq.username || '';
  $('#mq-pass').value = mq.password || '****';
  $('#mq-avail').value = mq.availability_topic || 'vision/status';

  $('#web-port').value = web.port || 8080;
  $('#auth-enabled').checked = !!auth.enabled;
  $('#auth-user').value = auth.username || 'admin';
  $('#auth-pass').value = auth.password || '****';

  const logCfg = cfg.logging || {};
  $('#log-retention').value = logCfg.retention_days || 30;
  $('#log-maxmb').value = Math.round(((logCfg.max_bytes || 5000000) / 1000000) * 10) / 10;
  $('#log-backups').value = logCfg.backup_count == null ? 3 : logCfg.backup_count;
  const res = (S.state && S.state.diagnostics && S.state.diagnostics.resources) || {};
  const usage = $('#log-usage');
  if (usage) {
    usage.textContent = res.logs
      ? ('Hiện dùng: ' + (res.logs.mb || 0).toFixed(1) + ' MB trong ' + (res.logs.files || 0) +
        ' file · ổ đĩa còn ' + ((res.disk && res.disk.free_gb) || '–') + ' GB')
      : '';
  }

  renderSettingsCameras();
}

function collectSettings() {
  const cfg = clone(S.config);
  cfg.detector = cfg.detector || {};
  cfg.detector.model = $('#det-model').value.trim() || 'yolo11n.pt';
  cfg.detector.device = $('#det-device').value;
  cfg.detector.imgsz = Number($('#det-imgsz').value || 640);
  cfg.detector.min_confidence = Number($('#det-conf').value) / 100;
  cfg.detector.detect_fps = Number($('#det-fps').value || 4);
  cfg.detector.motion_gating = $('#det-motion').checked;

  cfg.mqtt = cfg.mqtt || {};
  cfg.mqtt.host = $('#mq-host').value.trim();
  cfg.mqtt.port = Number($('#mq-port').value || 1883);
  cfg.mqtt.client_id = $('#mq-client').value.trim() || 'vision-automation';
  cfg.mqtt.username = $('#mq-user').value.trim();
  if ($('#mq-pass').value !== '****') cfg.mqtt.password = $('#mq-pass').value;
  cfg.mqtt.availability_topic = $('#mq-avail').value.trim();

  cfg.web = cfg.web || {};
  cfg.web.port = Number($('#web-port').value || 8080);
  cfg.web.auth = cfg.web.auth || {};
  cfg.web.auth.enabled = $('#auth-enabled').checked;
  cfg.web.auth.username = $('#auth-user').value.trim() || 'admin';
  if ($('#auth-pass').value !== '****') cfg.web.auth.password = $('#auth-pass').value;

  cfg.logging = cfg.logging || {};
  cfg.logging.retention_days = Math.min(365, Math.max(1, Number($('#log-retention').value || 30)));
  cfg.logging.max_bytes = Math.max(100000, Math.round(Number($('#log-maxmb').value || 5) * 1000000));
  cfg.logging.backup_count = Math.min(20, Math.max(0, Number($('#log-backups').value || 3)));
  return cfg;
}

async function saveSettings() {
  const status = $('#settings-status');
  status.textContent = 'đang lưu…';
  try {
    const cfg = collectSettings();
    await api('/api/config', { method: 'PUT', body: { config: cfg } });
    status.textContent = '✔ đã lưu ' + fmtTime(Date.now());
    toast('Đã lưu cài đặt — áp dụng ngay, không cần khởi động lại', 'ok');
    await loadConfig();
  } catch (e) {
    status.textContent = '';
    toast(e.message, 'err', 8000);
  }
}

function platformInfo() {
  return (S.state && S.state.platform) || {};
}

async function loadWebcams() {
  try {
    const data = await api('/api/webcams');
    const select = $('#webcam-select');
    const list = data.webcams || [];
    select.innerHTML = list.length ? list.map((cam) =>
      '<option value="' + escapeHtml(cam.name) + '">' + escapeHtml(cam.name) +
      (cam.recommended ? ' (khuyên dùng)' : '') + (cam.is_iphone ? ' — iPhone' : '') + '</option>').join('')
      : '<option value="">Không thấy webcam</option>';
    const p = platformInfo();
    const permBox = $('#webcam-permission-box');
    if (permBox) {
      // Trước đây hộp này luôn in hướng dẫn "hãy cấp quyền" kể cả khi đã cấp
      // quyền → người dùng tưởng app hỏng. Nay nói rõ TRẠNG THÁI thật.
      const state = p.webcam_permission || 'unknown';
      const label = {
        authorized: '✅ Đã cấp quyền camera cho ứng dụng này.',
        denied: '⛔ macOS/Windows đang CHẶN quyền camera của ứng dụng.',
        restricted: '⛔ Hệ điều hành giới hạn quyền camera (restricted).',
        not_determined: 'ℹ️ Chưa hỏi quyền camera lần nào — app sẽ hỏi khi mở webcam.',
        not_required: 'ℹ️ Hệ điều hành này không cần cấp quyền riêng cho webcam.',
      }[state] || 'ℹ️ Chưa rõ trạng thái quyền camera.';
      // Khi quyền ĐÃ cấp thì không in lại hướng dẫn "hãy cấp quyền" (vô lý).
      const granted = state === 'authorized' || state === 'not_required';
      permBox.textContent = granted
        ? label + ' Nếu hình vẫn đen: đóng ứng dụng khác đang dùng webcam rồi bấm “Kết nối lại”.'
        : label + ' ' + (p.webcam_hint || '');
      permBox.classList.toggle('ok', granted);
    }
    $('#testcam-info').textContent = list.length
      ? 'Tìm thấy ' + list.length + ' webcam trên ' + (p.label || 'máy này') +
        '. Camera test mở khi có người xem, tắt khi không dùng (đèn webcam tắt).'
      : 'Không tìm thấy webcam. ' + (p.webcam_hint || '');
  } catch (e) {
    $('#testcam-info').textContent = 'Không quét được webcam: ' + e.message;
  }
  const loopback = (S.state && S.state.loopback) || {};
  $('#loopback-info').textContent = loopback.running
    ? ('Đang phát tại ' + (loopback.url || ''))
    : (loopback.available === false
      ? ('Chưa cài ffmpeg/mediamtx. ' + (platformInfo().loopback_hint || ''))
      : 'Chưa chạy loopback.');
}

/* ============================ ZONE EDITOR ============================= */
function activeCameraConfig() {
  if (!S.config) return null;
  const cameras = (S.config.cameras || []).filter((c) => c.enabled !== false);
  return cameras.find((c) => c.id === ZE.cameraId) || cameras[0] || null;
}

async function loadZoneEditor(options = {}) {
  // Nạp lại từ server sẽ GHI ĐÈ dữ liệu đang sửa → phải hỏi trước.
  if (!options.force && !confirmDiscardZones('cấu hình vừa được tải lại')) return;
  if (!S.config) await loadConfig();
  const cameras = ((S.config && S.config.cameras) || []).filter((c) => c.enabled !== false);
  const remembered = LS.get('zoneCamera');
  if (!ZE.cameraId || !cameras.some((c) => c.id === ZE.cameraId)) {
    if (remembered && cameras.some((c) => c.id === remembered)) ZE.cameraId = remembered;
    else ZE.cameraId = cameras.length ? cameras[0].id : null;
  }
  if (ZE.cameraId) LS.set('zoneCamera', ZE.cameraId);
  if (!ZE.cameraId) {
    $('#zone-hint').textContent = 'Chưa có camera — thêm camera trong tab Cài đặt trước.';
    return;
  }
  const cam = activeCameraConfig();
  ZE.zones = clone((cam && cam.zones) || []);
  ZE.active = ZE.zones.length ? Math.min(ZE.active, ZE.zones.length - 1) : -1;
  ZE.history = []; ZE.future = []; ZE.draft = []; ZE.dirty = false;
  ZE.idRenames = {}; ZE.autoIds = new Set(); ZE.hover = null; ZE.selectedVertex = -1; ZE.idLocked = true;

  setZoneImageSource();
  renderZoneSidebar();
  renderZoneAttrs();
  updateDraftBar();
  updateDirtyBadge();
  drawZoneCanvas();
  loadClassList();
  refreshZonePreview();
  setZoneTool(ZE.mode);
}

/* Ảnh để vẽ vùng.
 *
 * Mặc định là ẢNH TĨNH (đóng băng khung hình hiện tại): vẽ trên hình đang chạy
 * thì người/đồ vật di chuyển làm mọi thao tác kéo-thả đều trượt. Bấm
 * “📷 Chụp khung hình” để lấy khung mới, “▶ Trực tiếp” để xem luồng thật.
 * Ảnh tĩnh cũng giúp nhả webcam (đèn tắt) khi không cần.
 */
function setZoneImageSource() {
  const img = $('#zone-img');
  if (!img || !ZE.cameraId) return;
  img.dataset.cam = ZE.cameraId;
  if (ZE.frozen && ZE.frozenSrc) {
    img.src = ZE.frozenSrc;
  } else {
    // Thêm ?r=<thời điểm> để trình duyệt luôn xin luồng MỚI — nếu không, sau khi
    // máy chủ khởi động lại, ảnh ở tab này sẽ đen vĩnh viễn (giữ kết nối đã chết).
    img.src = streamUrl(ZE.cameraId, Date.now());
  }
  updateFreezeBadge();
}

function updateFreezeBadge() {
  const badge = $('#zone-freeze-state');
  if (badge) {
    badge.textContent = ZE.frozen ? '⏸ ảnh tĩnh' : '▶ trực tiếp';
    badge.classList.toggle('live', !ZE.frozen);
  }
  const button = $('#zone-freeze');
  if (button) button.textContent = ZE.frozen ? '📷 Chụp lại' : '⏸ Đóng băng';
}

/* Chụp khung hình đang hiển thị thành ảnh tĩnh (data URL) để vẽ cho chắc tay. */
function freezeZoneFrame() {
  const img = $('#zone-img');
  if (!img || !img.naturalWidth) return false;
  try {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext('2d').drawImage(img, 0, 0);
    ZE.frozenSrc = canvas.toDataURL('image/jpeg', 0.85);
    ZE.frozen = true;
    img.src = ZE.frozenSrc;
    updateFreezeBadge();
    drawZoneCanvas();
    return true;
  } catch (e) {
    // Ảnh từ nguồn khác origin (rất hiếm) → giữ luồng trực tiếp, không làm hỏng UI.
    return false;
  }
}

function goLiveZoneFrame() {
  ZE.frozen = false;
  ZE.frozenSrc = null;
  setZoneImageSource();
  drawZoneCanvas();
}

/* Sau khi mở tab, chờ khung hình đầu tiên rồi tự đóng băng (thử trong ~5 s). */
function autoFreezeWhenReady() {
  if (ZE.frozen) { updateFreezeBadge(); return; }
  let tries = 0;
  const attempt = () => {
    tries += 1;
    if (S.tab !== 'zones' || ZE.frozen) return;
    const img = $('#zone-img');
    if (img && img.naturalWidth) {
      if (freezeZoneFrame()) return;
    }
    if (tries < 25) setTimeout(attempt, 200);
  };
  setTimeout(attempt, 250);
}

function snapshotZones() {
  ZE.history.push(JSON.stringify(ZE.zones));
  if (ZE.history.length > 60) ZE.history.shift();
  ZE.future = [];
  ZE.dirty = true;
}

function undoZones() {
  if (!ZE.history.length) return;
  ZE.future.push(JSON.stringify(ZE.zones));
  ZE.zones = JSON.parse(ZE.history.pop());
  ZE.active = Math.min(ZE.active, ZE.zones.length - 1);
  ZE.dirty = true;
  renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
}

function redoZones() {
  if (!ZE.future.length) return;
  ZE.history.push(JSON.stringify(ZE.zones));
  ZE.zones = JSON.parse(ZE.future.pop());
  ZE.dirty = true;
  renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
}

function renderZoneSidebar() {
  const select = $('#zone-select');
  select.innerHTML = ZE.zones.map((zone, index) =>
    '<option value="' + index + '">' + escapeHtml(zone.name || zone.id) + '</option>').join('')
    || '<option value="-1">(chưa có vùng)</option>';
  select.value = String(ZE.active);
  $('#zone-finish').disabled = ZE.draft.length < 3;
}

function renderZoneAttrs() {
  const zone = ZE.zones[ZE.active];
  const disabled = !zone;
  ['#zone-name', '#zone-id', '#zone-color', '#zone-anchor', '#zone-conf', '#zone-enabled',
    '#class-search', '#zone-shape', '#zone-ratio'].forEach((sel) => { $(sel).disabled = disabled; });
  if (!zone) {
    $('#zone-name').value = ''; $('#zone-id').value = '';
    $('#class-list').innerHTML = '<p class="small muted">Chưa có vùng.</p>';
    return;
  }
  $('#zone-name').value = zone.name || '';
  $('#zone-id').value = zone.id || '';
  const editableId = !ZE.idLocked || ZE.autoIds.has(zone.id);
  $('#zone-id').readOnly = !editableId;
  $('#zone-id-edit').textContent = ZE.idLocked ? '🔒' : '✎';
  $('#zone-id-edit').title = ZE.idLocked
    ? (ZE.autoIds.has(zone.id)
      ? 'Vùng mới: mã tự sinh theo tên. Sau khi lưu, mã sẽ khoá để kịch bản không bị lỗi.'
      : 'Mã vùng đang khoá (đổi tên không làm hỏng kịch bản). Bấm để đổi mã.')
    : 'Đang cho sửa mã — bấm 🔒 để khoá lại.';
  $('#zone-shape').value = zoneShapeOption(zone);
  $('#zone-color').value = zone.color || '#22c55e';
  const anchor = zone.anchor === 'overlap_ratio' ? 'inside_ratio' : (zone.anchor || 'bottom_center');
  $('#zone-anchor').value = anchor;
  const ratio = Math.round((zone.overlap_ratio == null ? 0.5 : zone.overlap_ratio) * 100);
  $('#zone-ratio').value = Math.max(5, Math.min(95, ratio));
  $('#zone-ratio-val').textContent = Math.round($('#zone-ratio').value) + '%';
  $('#zone-ratio-field').hidden = !(anchor === 'inside_ratio');
  const conf = zone.min_confidence == null ? 45 : Math.round(zone.min_confidence * 100);
  $('#zone-conf').value = conf;
  $('#zone-conf-val').textContent = conf + '%';
  $('#zone-enabled').checked = zone.enabled !== false;
  $('#zone-enabled-text').textContent = zone.enabled !== false ? 'Đang bật' : 'Đang tắt';
  renderZoneAnalysis();
  renderClassList();
}

/** Vì sao vùng đang trống / đang có người (lấy từ kết quả suy luận gần nhất). */
function renderZoneAnalysis() {
  const box = $('#zone-analysis');
  const zone = ZE.zones[ZE.active];
  if (!box || !zone) { if (box) box.hidden = true; return; }
  const cam = (S.state && S.state.cameras || []).find((item) => item.id === ZE.cameraId);
  const live = cam && cam.zone_states ? cam.zone_states[zone.id] : null;
  const analysis = live && live.analysis ? live.analysis : null;
  if (!analysis) {
    box.hidden = false;
    box.innerHTML = '<b>Phân tích trực tiếp</b><br><span class="small muted">Chưa có kết quả suy luận cho vùng này — hãy mở tab <b>Trực tiếp</b> để camera chạy.</span>';
    return;
  }
  const parts = [];
  parts.push('Đối tượng trong khung: <b>' + (analysis.detections || 0) + '</b>');
  parts.push('tính vào vùng: <b style="color:var(--green,#22c55e)">' + (analysis.matched || 0) + '</b>');
  if (analysis.outside) parts.push('nằm ngoài vùng: <b style="color:#fbbf24">' + analysis.outside + '</b>');
  if (analysis.ignored_class) parts.push('khác lớp: ' + analysis.ignored_class);
  if (analysis.ignored_confidence) parts.push('dưới ngưỡng tin cậy: ' + analysis.ignored_confidence);
  const anchorLabels = {
    bottom_center: 'điểm chân nằm trong vùng',
    center: 'tâm đối tượng nằm trong vùng',
    any_overlap: 'chạm vùng là tính',
    fully_inside: 'toàn bộ đối tượng trong vùng',
    inside_ratio: '≥ ' + Math.round((analysis.min_ratio || zone.overlap_ratio || 0.5) * 100) + '% đối tượng trong vùng',
    overlap_ratio: '≥ ' + Math.round((analysis.min_ratio || zone.overlap_ratio || 0.5) * 100) + '% đối tượng trong vùng',
  };
  const mode = anchorLabels[analysis.anchor] || analysis.anchor || '';
  box.hidden = false;
  box.innerHTML = '<b>Phân tích trực tiếp</b> <span class="small muted">(' + escapeHtml(mode) + ')</span><br>' +
    '<span class="small">' + parts.join(' · ') + '</span><br>' +
    '<span class="small" style="color:#fbbf24">Lý do: ' + escapeHtml(analysis.reason || '') + '</span>';
}

function renderClassList() {
  const zone = ZE.zones[ZE.active];
  const container = $('#class-list');
  if (!zone) { container.innerHTML = ''; return; }
  const query = ($('#class-search').value || '').toLowerCase().trim();
  const selected = new Set((zone.classes || ['person']).map((c) => c.toLowerCase()));
  let names = S.classList || [];
  if (!names.length) names = [{ id: 0, name: 'person' }];
  const filtered = names.filter((item) => !query || item.name.toLowerCase().includes(query));
  container.innerHTML = filtered.slice(0, 200).map((item) =>
    '<div class="class-row"><label><input type="checkbox" value="' + escapeHtml(item.name) + '"' +
    (selected.has(item.name.toLowerCase()) ? ' checked' : '') + '>' +
    '<span>' + escapeHtml(item.name) + '</span>' +
    '<span class="small muted">#' + item.id + '</span></label></div>').join('')
    || '<p class="small muted" style="padding:6px">Không thấy lớp nào khớp.</p>';
}

async function loadClassList() {
  try {
    const data = await api('/api/detector/classes');
    S.classList = data.classes || [];
    const info = data.info || {};
    $('#detector-badge').textContent = info.available
      ? ('model: ' + (info.model || '?') + ' · ' + (info.device || '?'))
      : 'model chưa sẵn sàng';
    renderClassList();
  } catch (e) { /* ignore */ }
}

/* --- canvas geometry ---------------------------------------------------- */
const VERTEX_HIT_PX = 22;   // bán kính bắt điểm (pixel màn hình) — rộng rãi cho điện thoại
const EDGE_HIT_PX = 14;     // bán kính bắt cạnh để chèn điểm mới / đổi kích thước
const EPS_RECT = 0.01;      // chừa 1% để kéo góc/cạnh không lật ngược hình chữ nhật
const DRAG_THRESHOLD_PX = 4; // phải kéo quá 4 px mới tính là kéo (dưới ngưỡng = bấm)
const MIN_VERTICES = 3;     // vùng cần ít nhất 3 điểm mới có diện tích
const MIN_AREA = 0.0008;    // ~0.08% khung hình: nhỏ hơn nữa là bấm nhầm
const DUP_EPS = 0.004;      // hai điểm gần nhau hơn 0.4% khung hình = điểm trùng

/* Con trỏ chuột cho từng thao tác. Dấu “+” vẽ bằng SVG để thấy rõ “bấm vào đây
 * là thêm điểm” (con trỏ `copy` mặc định của hệ điều hành rất khó nhận ra). */
const PLUS_CURSOR = 'url("data:image/svg+xml;utf8,'
  + '<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'26\' height=\'26\'>'
  + '<circle cx=\'13\' cy=\'13\' r=\'10\' fill=\'rgba(14,165,233,0.18)\' stroke=\'%230ea5e9\' stroke-width=\'2\'/>'
  + '<path d=\'M13 6.5v13M6.5 13h13\' stroke=\'%23e0f2fe\' stroke-width=\'2.6\' stroke-linecap=\'round\'/>'
  + '</svg>") 13 13, crosshair';

const CURSOR = {
  vertex: 'grab',
  vertexActive: 'grabbing',
  edgeAdd: PLUS_CURSOR,
  edgeResizeY: 'ns-resize',
  edgeResizeX: 'ew-resize',
  move: 'move',
  moveActive: 'grabbing',
  draw: 'crosshair',
};

function canvasSize(canvas, img) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width: rect.width, height: rect.height, image: imageBox(rect, img) };
}

/* Khung ảnh THẬT (theo CSS px) mà trình duyệt đang vẽ cho <img>, quy về hệ toạ
 * độ của canvas.
 *
 * Trước đây hàm này tự đoán khung ảnh bằng `min(rect.w/naturalW, rect.h/naturalH)`.
 * Công thức đó SAI khi ảnh nhỏ hơn khung: CSS `max-width/max-height: 100%` không
 * bao giờ phóng to ảnh, nên luồng 640×480 trong panel 1100 px vẫn chỉ rộng
 * 640 px, trong khi công thức tưởng là ~725 px ⇒ vùng vẽ bị lệch tới ~85 px và
 * toạ độ lưu xuống cũng sai. Dùng `getBoundingClientRect()` của chính <img> là
 * cách duy nhất đúng với mọi `object-fit`, mọi DPR và mọi kiểu layout.
 */
function imageBox(rect, img) {
  if (img && img.getBoundingClientRect) {
    const box = img.getBoundingClientRect();
    if (box.width > 0.5 && box.height > 0.5) {
      return { x: box.left - rect.left, y: box.top - rect.top, w: box.width, h: box.height };
    }
  }
  // Ảnh chưa tải / chưa có src: tạm dùng letterbox để không chia cho 0.
  const naturalW = (img && img.naturalWidth) || 16;
  const naturalH = (img && img.naturalHeight) || 9;
  const scale = Math.min(rect.width / naturalW, rect.height / naturalH) || 1;
  const w = naturalW * scale, h = naturalH * scale;
  return { x: (rect.width - w) / 2, y: (rect.height - h) / 2, w, h };
}

/* Đổi toạ độ con trỏ (clientX/clientY — hệ toạ độ MÀN HÌNH) sang hệ toạ độ của
 * canvas (góc trên-trái canvas = 0,0). Mọi phép so khoảng cách phải dùng CÙNG
 * một hệ toạ độ; trước đây `nearestVertex`/`hitEdge` so trực tiếp clientX với
 * toạ độ trong canvas nên lệch đúng bằng `rect.left/rect.top` — đó là lý do
 * "kéo điểm hoạt động kém".
 */
function canvasPointOf(clientPoint) {
  const canvas = $('#zone-canvas');
  if (!canvas) return [clientPoint[0], clientPoint[1]];
  const rect = canvas.getBoundingClientRect();
  return [clientPoint[0] - rect.left, clientPoint[1] - rect.top];
}

function polygonBounds(polygon) {
  const xs = (polygon || []).map((p) => p[0]), ys = (polygon || []).map((p) => p[1]);
  if (!xs.length) return { x1: 0.1, y1: 0.1, x2: 0.9, y2: 0.9 };
  return { x1: Math.min(...xs), y1: Math.min(...ys), x2: Math.max(...xs), y2: Math.max(...ys) };
}

function rectPolygon(bounds) {
  return [
    [bounds.x1, bounds.y1],
    [bounds.x2, bounds.y1],
    [bounds.x2, bounds.y2],
    [bounds.x1, bounds.y2],
  ];
}

function isAxisAlignedRect(polygon) {
  if (!polygon || polygon.length !== 4) return false;
  const [a, b, c, d] = polygon;
  const tol = 0.03;
  return Math.abs(a[1] - b[1]) < tol && Math.abs(b[0] - c[0]) < tol
    && Math.abs(c[1] - d[1]) < tol && Math.abs(d[0] - a[0]) < tol;
}

const clamp01 = (value) => Math.min(1, Math.max(0, value));

/* Kiểu vùng: ưu tiên `shape` người dùng đã chọn (lưu trong config); chỉ suy ra
 * từ hình học khi config cũ chưa có trường này. Nhờ vậy chuyển "Đa giác tự do"
 * → "Hình chữ nhật" là quyết định của người dùng, không bị đoán lại. */
function zoneShape(zone) {
  if (!zone) return 'polygon';
  if (zone.shape === 'rect') return 'rect';
  if (zone.shape === 'polygon') return 'polygon';
  return isAxisAlignedRect(zone.polygon || []) ? 'rect' : 'polygon';
}

function isRectZone(zone) {
  return zoneShape(zone) === 'rect';
}

/* Giá trị hiển thị cho dropdown "Kiểu vùng" ('full' = preset toàn khung hình). */
function zoneShapeOption(zone) {
  if (!zone) return 'polygon';
  const b = polygonBounds(zone.polygon || []);
  const isFull = isAxisAlignedRect(zone.polygon || [])
    && Math.abs(b.x1 - 0.02) < 0.03 && Math.abs(b.y1 - 0.02) < 0.03
    && Math.abs(b.x2 - 0.98) < 0.03 && Math.abs(b.y2 - 0.98) < 0.03;
  return isFull ? 'full' : zoneShape(zone);
}

/* Hình chữ nhật luôn lưu theo đúng thứ tự 4 góc: trên-trái, trên-phải,
 * dưới-phải, dưới-trái — để kéo góc/cạnh biết chính xác phải đổi toạ độ nào. */
function canonicalRect(polygon) {
  return rectPolygon(polygonBounds(polygon || []));
}

function samePolygon(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.every((p, i) => Math.abs(p[0] - b[i][0]) < 1e-6 && Math.abs(p[1] - b[i][1]) < 1e-6);
}

/* Diện tích đa giác (công thức shoelace, luôn dương). Dùng để bắt "vùng rỗng". */
function polygonArea(points) {
  if (!points || points.length < 3) return 0;
  let total = 0;
  for (let i = 0; i < points.length; i += 1) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    total += x1 * y2 - x2 * y1;
  }
  return Math.abs(total) / 2;
}

/* Bỏ điểm trùng liền kề (bấm đúp / bấm rung tay rất dễ sinh ra) và điểm cuối
 * trùng điểm đầu — cả hai đều làm đa giác "vô nghĩa" mà người dùng không thấy. */
function dedupePolygon(points, eps = DUP_EPS) {
  const cleaned = [];
  (points || []).forEach((point) => {
    const previous = cleaned[cleaned.length - 1];
    if (previous && Math.hypot(point[0] - previous[0], point[1] - previous[1]) < eps) return;
    cleaned.push([point[0], point[1]]);
  });
  while (cleaned.length > 1) {
    const first = cleaned[0], last = cleaned[cleaned.length - 1];
    if (Math.hypot(first[0] - last[0], first[1] - last[1]) >= eps) break;
    cleaned.pop();
  }
  return cleaned;
}

/* Hai đoạn thẳng a1a2 và b1b2 có cắt nhau (kể cả chạm) không. */
function segmentsIntersect(a1, a2, b1, b2) {
  const cross = (o, p, q) => (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0]);
  const d1 = cross(b1, b2, a1), d2 = cross(b1, b2, a2);
  const d3 = cross(a1, a2, b1), d4 = cross(a1, a2, b2);
  return ((d1 > 1e-12 && d2 < -1e-12) || (d1 < -1e-12 && d2 > 1e-12))
    && ((d3 > 1e-12 && d4 < -1e-12) || (d3 < -1e-12 && d4 > 1e-12));
}

/* Đa giác tự cắt chính nó (hình số 8) → kết quả "trong vùng" không xác định. */
function polygonSelfIntersects(points) {
  const count = (points || []).length;
  if (count < 4) return false;
  for (let i = 0; i < count; i += 1) {
    const a1 = points[i], a2 = points[(i + 1) % count];
    for (let j = i + 1; j < count; j += 1) {
      // hai cạnh kề nhau dùng chung một điểm thì bỏ qua
      if (j === i || (j + 1) % count === i || (i + 1) % count === j) continue;
      const b1 = points[j], b2 = points[(j + 1) % count];
      if (segmentsIntersect(a1, a2, b1, b2)) return true;
    }
  }
  return false;
}

/* Kiểm tra hình học trước khi lưu. Trả về chuỗi lỗi (tiếng Việt) hoặc '' nếu OK. */
function validatePolygon(points, label) {
  const cleaned = dedupePolygon(points || []);
  if (cleaned.length < MIN_VERTICES) {
    return 'Vùng “' + label + '” cần ít nhất ' + MIN_VERTICES + ' điểm (đang có ' + cleaned.length + ').';
  }
  if (polygonArea(cleaned) < MIN_AREA) {
    return 'Vùng “' + label + '” quá nhỏ hoặc các điểm nằm trên một đường thẳng — hãy kéo rộng ra.';
  }
  if (polygonSelfIntersects(cleaned)) {
    return 'Vùng “' + label + '” bị tự cắt nhau (các cạnh cắt chéo) — hãy sửa lại thứ tự/khoảng cách các điểm.';
  }
  return '';
}

/* Kéo 1 GÓC của hình chữ nhật → đổi chiều dài/rộng, 2 góc kề chạy theo, hình
 * luôn vuông góc và không bị lật (kẹp trong cạnh đối diện). */
function resizeRectCorner(polygon, index, point) {
  if (!polygon || polygon.length !== 4) return;
  const b = polygonBounds(polygon);
  const px = clamp01(point[0]), py = clamp01(point[1]);
  const x1 = Math.min(px, b.x2 - EPS_RECT);
  const x2 = Math.max(px, b.x1 + EPS_RECT);
  const y1 = Math.min(py, b.y2 - EPS_RECT);
  const y2 = Math.max(py, b.y1 + EPS_RECT);
  if (index === 0) { polygon[0] = [x1, y1]; polygon[1] = [b.x2, y1]; polygon[3] = [x1, b.y2]; }
  else if (index === 1) { polygon[1] = [x2, y1]; polygon[0] = [b.x1, y1]; polygon[2] = [x2, b.y2]; }
  else if (index === 2) { polygon[2] = [x2, y2]; polygon[1] = [x2, b.y1]; polygon[3] = [b.x1, y2]; }
  else { polygon[3] = [x1, y2]; polygon[0] = [x1, b.y1]; polygon[2] = [b.x2, y2]; }
}

/* Kéo CHẤM GIỮA CẠNH của hình chữ nhật → di chuyển riêng cạnh đó (đổi kích
 * thước), không chèn thêm điểm và không làm xô các cạnh khác. */
function resizeRectEdge(polygon, index, point) {
  if (!polygon || polygon.length !== 4) return;
  const b = polygonBounds(polygon);
  const px = clamp01(point[0]), py = clamp01(point[1]);
  if (index === 0) { const y = Math.min(py, b.y2 - EPS_RECT); polygon[0][1] = y; polygon[1][1] = y; }
  else if (index === 1) { const x = Math.max(px, b.x1 + EPS_RECT); polygon[1][0] = x; polygon[2][0] = x; }
  else if (index === 2) { const y = Math.max(py, b.y1 + EPS_RECT); polygon[2][1] = y; polygon[3][1] = y; }
  else { const x = Math.min(px, b.x2 - EPS_RECT); polygon[3][0] = x; polygon[0][0] = x; }
}

/* Cạnh 0/2 của hình chữ nhật nằm ngang (kéo lên-xuống), 1/3 thẳng đứng. */
function rectEdgeCursor(index) {
  return (index % 2 === 0) ? CURSOR.edgeResizeY : CURSOR.edgeResizeX;
}

function applyZoneShape(zone, shape) {
  if (!zone) return;
  if (shape === 'full') {
    zone.polygon = [[0.02, 0.02], [0.98, 0.02], [0.98, 0.98], [0.02, 0.98]];
    zone.shape = 'rect';
  } else if (shape === 'rect') {
    zone.polygon = canonicalRect(zone.polygon);
    zone.shape = 'rect';
  } else {
    // 'polygon' = giữ nguyên các điểm hiện có, chỉ mở khoá cho kéo tự do.
    zone.shape = 'polygon';
  }
}

function drawZoneCanvas() {
  updateDirtyBadge();
  const canvas = $('#zone-canvas');
  const img = $('#zone-img');
  if (!canvas) return;
  const { ctx, width, height, image } = canvasSize(canvas, img);
  ctx.clearRect(0, 0, width, height);

  ZE.zones.forEach((zone, index) => {
    const pts = (zone.polygon || []).map(([x, y]) => [image.x + x * image.w, image.y + y * image.h]);
    if (pts.length < 2) return;
    const isActive = index === ZE.active;
    const color = zone.color || '#22c55e';
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (pts.length >= 3) ctx.closePath();
    if (isActive && pts.length >= 3) {
      ctx.fillStyle = hexAlpha(color, 0.18);
      ctx.fill();
    }
    ctx.strokeStyle = zone.enabled === false ? '#64748b' : color;
    ctx.lineWidth = isActive ? 2.5 : 1.5;
    ctx.setLineDash(zone.enabled === false ? [6, 4] : []);
    ctx.stroke();
    ctx.setLineDash([]);

    if (pts.length) {
      ctx.fillStyle = zone.enabled === false ? '#64748b' : color;
      ctx.font = '600 12px system-ui, sans-serif';
      const label = zone.name || zone.id || '';
      ctx.fillText(label, pts[0][0] + 4, Math.max(14, pts[0][1] - 6));
    }

    // Điểm đang rê chuột tới (vùng nào cũng hiện) → sáng lên để biết sẽ kéo được.
    const hotVertex = (ZE.hover && ZE.hover.kind === 'vertex' && ZE.hover.zoneIndex === index)
      ? ZE.hover.index : -1;
    if (hotVertex >= 0 && pts[hotVertex]) {
      const [hx, hy] = pts[hotVertex];
      ctx.beginPath();
      ctx.arc(hx, hy, 11, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(251,191,36,0.85)';
      ctx.lineWidth = 3;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(hx, hy, 8, 0, Math.PI * 2);
      ctx.fillStyle = '#fbbf24';
      ctx.fill();
      ctx.strokeStyle = '#04121f';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    if (!isActive) return;
    const rectZone = isRectZone(zone) && pts.length === 4;

    // Cạnh đang rê chuột tới → tô sáng để biết "bấm vào đây là thêm điểm / đổi cỡ".
    if (ZE.hover && ZE.hover.kind === 'edge' && pts.length >= 2) {
      const a = pts[ZE.hover.index % pts.length];
      const b = pts[(ZE.hover.index + 1) % pts.length];
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.strokeStyle = '#fbbf24';
      ctx.lineWidth = 4;
      ctx.stroke();
    }

    // Chấm giữa cạnh: đa giác = "chèn điểm ở đây"; hình chữ nhật = "kéo để đổi cỡ".
    const edgeCount = pts.length >= 3 ? pts.length : pts.length - 1;
    for (let i = 0; i < edgeCount; i += 1) {
      const a = pts[i], b = pts[(i + 1) % pts.length];
      const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
      const hot = ZE.hover && ZE.hover.kind === 'edge' && ZE.hover.index === i;
      if (rectZone) {
        const horizontal = i % 2 === 0;
        const len = hot ? 11 : 8, thick = hot ? 5 : 4;
        ctx.beginPath();
        ctx.rect(mx - (horizontal ? len : thick) / 2, my - (horizontal ? thick : len) / 2,
          horizontal ? len : thick, horizontal ? thick : len);
        ctx.fillStyle = hot ? '#fbbf24' : hexAlpha(color, 0.7);
        ctx.fill();
        ctx.strokeStyle = '#04121f';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.arc(mx, my, hot ? 7 : 5, 0, Math.PI * 2);
        ctx.fillStyle = hot ? hexAlpha('#fbbf24', 0.9) : hexAlpha(color, 0.55);
        ctx.fill();
        ctx.strokeStyle = '#04121f';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }

    pts.forEach(([x, y], vi) => {
      const selected = vi === ZE.selectedVertex;
      ctx.beginPath();
      ctx.arc(x, y, selected ? 8 : 6, 0, Math.PI * 2);
      ctx.fillStyle = selected ? '#f59e0b' : '#0ea5e9';
      ctx.fill();
      ctx.strokeStyle = '#04121f';
      ctx.lineWidth = 2;
      ctx.stroke();
    });
  });

  // draft polygon being drawn
  if (ZE.draft.length) {
    const pts = ZE.draft.map(([x, y]) => [image.x + x * image.w, image.y + y * image.h]);
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    // Đường khép kín mờ để thấy trước hình sẽ được tạo.
    if (pts.length >= 3) {
      ctx.beginPath();
      ctx.moveTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
      ctx.lineTo(pts[0][0], pts[0][1]);
      ctx.strokeStyle = 'rgba(251,191,36,0.55)';
      ctx.stroke();
    }
    ctx.setLineDash([]);
    pts.forEach(([x, y], vi) => {
      ctx.beginPath();
      ctx.arc(x, y, vi === ZE.selectedVertex ? 7 : 5.5, 0, Math.PI * 2);
      ctx.fillStyle = vi === ZE.selectedVertex ? '#f59e0b' : '#fbbf24';
      ctx.fill();
    });
  }

  // rectangle rubber band
  if (ZE.rectStart && ZE.drag && ZE.drag.mode === 'rect') {
    const [sx, sy] = ZE.rectStart;
    const [ex, ey] = ZE.drag.current;
    const x1 = image.x + Math.min(sx, ex) * image.w, x2 = image.x + Math.max(sx, ex) * image.w;
    const y1 = image.y + Math.min(sy, ey) * image.h, y2 = image.y + Math.max(sy, ey) * image.h;
    ctx.strokeStyle = '#22d3ee';
    ctx.lineWidth = 2;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }
}

function hexAlpha(hex, alpha) {
  const text = (hex || '#22c55e').replace('#', '');
  const r = parseInt(text.slice(0, 2), 16), g = parseInt(text.slice(2, 4), 16), b = parseInt(text.slice(4, 6), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

function pointerToNormalized(event) {
  const canvas = $('#zone-canvas');
  const img = $('#zone-img');
  const rect = canvas.getBoundingClientRect();
  const image = imageBox(rect, img);
  const x = (event.clientX - rect.left - image.x) / image.w;
  const y = (event.clientY - rect.top - image.y) / image.h;
  return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
}

/* ``clientPoint`` = [clientX, clientY] (hệ màn hình). Hàm tự đổi sang hệ canvas
 * nên người gọi không phải nhớ; đổi ở ĐÚNG MỘT chỗ để không bị trừ 2 lần. */
function nearestVertex(clientPoint, polygon) {
  const canvas = $('#zone-canvas');
  const img = $('#zone-img');
  const rect = canvas.getBoundingClientRect();
  const image = imageBox(rect, img);
  const [px, py] = canvasPointOf(clientPoint);
  let best = null, bestDist = VERTEX_HIT_PX;
  (polygon || []).forEach((vertex, index) => {
    const vx = image.x + vertex[0] * image.w;
    const vy = image.y + vertex[1] * image.h;
    const dist = Math.hypot(vx - px, vy - py);
    if (dist < bestDist) { bestDist = dist; best = { index, dist }; }
  });
  return best;
}

function hitVertex(point, zone) {
  const hit = nearestVertex(point, (zone && zone.polygon) || []);
  return hit ? hit.index : -1;
}

/* Tìm điểm gần con trỏ nhất trong TẤT CẢ các vùng (điểm của vùng nào cũng kéo
 * được — bấm trúng điểm của vùng khác sẽ tự chuyển sang sửa vùng đó). */
function pickVertex(point) {
  let best = null;
  ZE.zones.forEach((zone, zoneIndex) => {
    const hit = nearestVertex(point, zone.polygon || []);
    if (hit && (!best || hit.dist < best.dist)) {
      best = { zoneIndex, index: hit.index, dist: hit.dist };
    }
  });
  return best;
}

/* Tìm cạnh gần con trỏ nhất. Trả về {index, point} với ``index`` là điểm bắt
 * đầu của cạnh — điểm mới sẽ được chèn vào vị trí ``index + 1``, đúng trên
 * đường cạnh (snap) để hình không bị méo khi bấm lệch tay. */
function hitEdge(clientPoint, polygon) {
  if (!polygon || polygon.length < 2) return null;
  const canvas = $('#zone-canvas');
  const img = $('#zone-img');
  const rect = canvas.getBoundingClientRect();
  const image = imageBox(rect, img);
  const [px, py] = canvasPointOf(clientPoint);
  const closed = polygon.length >= 3;
  const edgeCount = closed ? polygon.length : polygon.length - 1;
  let best = null, bestDist = EDGE_HIT_PX;
  for (let i = 0; i < edgeCount; i += 1) {
    const a = polygon[i], b = polygon[(i + 1) % polygon.length];
    const ax = image.x + a[0] * image.w, ay = image.y + a[1] * image.h;
    const bx = image.x + b[0] * image.w, by = image.y + b[1] * image.h;
    const vx = bx - ax, vy = by - ay;
    const len2 = vx * vx + vy * vy || 1e-9;
    let t = ((px - ax) * vx + (py - ay) * vy) / len2;
    t = Math.max(0.04, Math.min(0.96, t));
    const cx = ax + t * vx, cy = ay + t * vy;
    const dist = Math.hypot(cx - px, cy - py);
    if (dist < bestDist) {
      bestDist = dist;
      best = {
        index: i,
        point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t],
      };
    }
  }
  return best;
}

function pointInPolygon(point, polygon) {
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = polygon[i][0], yi = polygon[i][1];
    const xj = polygon[j][0], yj = polygon[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-12) + xi) inside = !inside;
  }
  return inside;
}

function newZone(polygon, shape) {
  const index = ZE.zones.length + 1;
  let kind = shape;
  if (!kind) kind = isAxisAlignedRect(polygon || []) ? 'rect' : 'polygon';
  return {
    id: 'vung_' + index,
    name: 'Vùng ' + index,
    polygon: polygon || [],
    shape: kind,
    classes: ['person'],
    color: PALETTE[(index - 1) % PALETTE.length],
    anchor: 'bottom_center',
    min_confidence: null,
    enter_confirm: { frames: 3, of: 5 },
    exit_grace_s: 3,
    enabled: true,
    publish_state: true,
  };
}

function finishDraftPolygon() {
  // Bấm đúp dễ sinh 1-2 điểm trùng ngay trên điểm cuối; bỏ chúng TRƯỚC khi đếm
  // để "bấm đúp để đóng" không bao giờ tạo ra đa giác 5-6 điểm trùng nhau.
  const cleaned = dedupePolygon(ZE.draft);
  if (cleaned.length < MIN_VERTICES) {
    toast('Cần ít nhất ' + MIN_VERTICES + ' điểm khác nhau để tạo vùng', 'warn');
    return;
  }
  const problem = validatePolygon(cleaned, 'vùng mới');
  if (problem) { toast(problem, 'err', 7000); return; }
  snapshotZones();
  ZE.zones.push(newZone(cleaned, 'polygon'));
  ZE.active = ZE.zones.length - 1;
  ZE.autoIds.add(ZE.zones[ZE.active].id);
  ZE.draft = [];
  ZE.selectedVertex = -1;
  ZE.hover = null;
  ZE.idLocked = true;
  updateDraftBar();
  renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
  toast('Đã tạo vùng — nhớ bấm “💾 Lưu vùng”', 'ok');
}

/* Đổi con trỏ + hiện gợi ý theo thứ đang nằm dưới chuột:
 *   điểm    → con trỏ “bàn tay”, điểm đó sáng lên, kéo để chỉnh
 *   cạnh     → con trỏ “+”, kéo để thêm điểm (đa giác) / đổi cỡ (hình chữ nhật)
 *   giữa vùng→ con trỏ “di chuyển”, kéo để dời cả vùng
 */
function applyZoneCursor(cursor) {
  const canvas = $('#zone-canvas');
  if (canvas) canvas.style.cursor = cursor || CURSOR.draw;
}

function zoneHoverLabel(hover) {
  if (!hover) return '';
  if (hover.kind === 'vertex') {
    const zone = ZE.zones[hover.zoneIndex];
    return zone ? ('Kéo để chỉnh điểm · ' + (zone.name || zone.id)) : 'Kéo để chỉnh điểm';
  }
  if (hover.kind === 'edge') {
    const zone = ZE.zones[hover.zoneIndex];
    return (zone && isRectZone(zone)) ? 'Kéo để đổi kích thước' : '＋ Bấm hoặc kéo để thêm điểm';
  }
  if (hover.kind === 'zone') {
    const zone = ZE.zones[hover.zoneIndex];
    return 'Kéo để di chuyển cả vùng' + (zone ? (' “' + (zone.name || zone.id) + '”') : '');
  }
  return '';
}

function updateZoneTip(clientPoint, hover) {
  const tip = $('#zone-tip');
  if (!tip) return;
  const label = zoneHoverLabel(hover);
  if (!label || !clientPoint) { tip.classList.remove('show'); return; }
  const wrap = $('#zone-wrap');
  const rect = wrap ? wrap.getBoundingClientRect() : { left: 0, top: 0 };
  tip.textContent = label;
  tip.style.left = (clientPoint[0] - rect.left + 16) + 'px';
  tip.style.top = (clientPoint[1] - rect.top + 14) + 'px';
  tip.classList.add('show');
}

function updateZoneHover(clientPoint) {
  const active = ZE.zones[ZE.active];
  let hover = null;
  let cursor = CURSOR.draw;
  if (ZE.draft.length) {
    const draftVertex = nearestVertex(clientPoint, ZE.draft);
    if (draftVertex) {
      cursor = CURSOR.vertex;
      hover = { kind: 'vertex', zoneIndex: -1, index: draftVertex.index };
    } else if (hitEdge(clientPoint, ZE.draft)) {
      cursor = CURSOR.edgeAdd;
    }
    if (!hover) {
      // Vẫn báo được "bấm vào điểm của vùng có sẵn là sửa được điểm đó".
      const picked = pickVertex(clientPoint);
      if (picked) {
        cursor = CURSOR.vertex;
        hover = { kind: 'vertex', zoneIndex: picked.zoneIndex, index: picked.index };
      }
    }
  } else {
    const picked = pickVertex(clientPoint);
    if (picked) {
      cursor = CURSOR.vertex;
      hover = { kind: 'vertex', zoneIndex: picked.zoneIndex, index: picked.index };
    } else if (active && (active.polygon || []).length >= 2) {
      const edge = hitEdge(clientPoint, active.polygon || []);
      if (edge) {
        hover = { kind: 'edge', zoneIndex: ZE.active, index: edge.index };
        cursor = (isRectZone(active) && active.polygon.length === 4)
          ? rectEdgeCursor(edge.index) : CURSOR.edgeAdd;
      } else if (pointInPolygon(pointerToNormalizedClient(clientPoint), active.polygon || [])) {
        hover = { kind: 'zone', zoneIndex: ZE.active };
        cursor = CURSOR.move;
      } else if (ZE.mode === 'rect') {
        cursor = CURSOR.draw;
      }
    }
  }
  const changed = JSON.stringify(hover) !== JSON.stringify(ZE.hover);
  ZE.hover = hover;
  applyZoneCursor(cursor);
  updateZoneTip(clientPoint, hover);
  if (changed) drawZoneCanvas();
}

/* Thanh báo "đang vẽ vùng mới" — để không ai bị kẹt ở chế độ vẽ mà không biết. */
function updateDraftBar() {
  const bar = $('#zone-draft-bar');
  if (!bar) return;
  const count = ZE.draft.length;
  bar.classList.toggle('show', count > 0);
  const countEl = $('#zone-draft-count');
  if (countEl) countEl.textContent = String(count);
}

function cancelDraft(reason) {
  if (!ZE.draft.length) return;
  ZE.draft = [];
  ZE.selectedVertex = -1;
  ZE.hover = null;
  updateDraftBar();
  renderZoneSidebar();
  drawZoneCanvas();
  if (reason) toast(reason, 'warn');
}

function pointerToNormalizedClient(clientPoint) {
  return pointerToNormalized({ clientX: clientPoint[0], clientY: clientPoint[1] });
}

function setZoneTool(mode) {
  ZE.mode = mode === 'rect' ? 'rect' : 'poly';
  const poly = $('#zone-mode-poly');
  const rect = $('#zone-mode-rect');
  if (poly) poly.classList.toggle('active', ZE.mode === 'poly');
  if (rect) rect.classList.toggle('active', ZE.mode === 'rect');
  const hint = $('#zone-hint');
  if (hint) {
    hint.innerHTML = ZE.mode === 'rect'
      ? '<b>Công cụ: Hình chữ nhật</b> — kéo chuột trên ảnh để tạo vùng mới. '
        + 'Trong hình chữ nhật: kéo <b>điểm góc</b> = đổi chiều dài/rộng, kéo <b>chấm giữa cạnh</b> = dời riêng cạnh đó, '
        + 'kéo giữa vùng = di chuyển cả vùng.'
      : '<b>Công cụ: Đa giác</b> — bấm từng điểm trên ảnh, bấm đúp (hoặc “✔ Hoàn tất vùng”) để đóng. '
        + 'Trong đa giác: kéo <b>điểm</b> = chỉ điểm đó chạy (2 cạnh dính vào nó xoay theo), '
        + 'kéo <b>chấm giữa cạnh</b> = thêm điểm mới, kéo giữa vùng = di chuyển cả vùng, chọn điểm rồi nhấn Delete để xoá điểm.';
  }
}

/* Chuyển vùng đang sửa (chỉ qua danh sách hoặc khi bấm trúng MỘT ĐIỂM của vùng
 * khác — không tự đổi khi bấm vào bên trong vùng khác, tránh "nhảy vùng"). */
function activateZone(index) {
  if (index < 0 || index >= ZE.zones.length || index === ZE.active) return;
  ZE.active = index;
  ZE.selectedVertex = -1;
  ZE.hover = null;
  ZE.idLocked = true;
  renderZoneSidebar();
  renderZoneAttrs();
}

/* Trước khi kéo, đưa hình chữ nhật về đúng 4 góc chuẩn (trên-trái, trên-phải,
 * dưới-phải, dưới-trái) để biết kéo góc/cạnh nào là đổi toạ độ nào. */
function ensureCanonicalRect(zone) {
  if (!zone || !isRectZone(zone)) return false;
  const canon = canonicalRect(zone.polygon);
  if (samePolygon(canon, zone.polygon)) return false;
  zone.polygon = canon;
  return true;
}

function startVertexDrag(index, draft) {
  ZE.selectedVertex = index;
  ZE.drag = { mode: 'vertex', index, draft: !!draft };
  applyZoneCursor(CURSOR.vertexActive);
}

function startRectEdgeDrag(index) {
  ZE.selectedVertex = -1;
  ZE.drag = { mode: 'edge', index };
  applyZoneCursor(rectEdgeCursor(index));
}

function startZoneMove(point) {
  const zone = ZE.zones[ZE.active];
  if (!zone || (zone.polygon || []).length < 3) return;
  snapshotZones();
  ZE.selectedVertex = -1;
  ZE.drag = { mode: 'polygon', start: point, original: clone(zone.polygon) };
  applyZoneCursor(CURSOR.moveActive);
}

function rectangleFromPoints(a, b) {
  return [
    [Math.min(a[0], b[0]), Math.min(a[1], b[1])],
    [Math.max(a[0], b[0]), Math.min(a[1], b[1])],
    [Math.max(a[0], b[0]), Math.max(a[1], b[1])],
    [Math.min(a[0], b[0]), Math.max(a[1], b[1])],
  ];
}

function wireZoneCanvas() {
  const canvas = $('#zone-canvas');
  if (!canvas) return;
  // Khung hình đầu tiên tới sau khi vẽ canvas → vẽ lại để toạ độ khớp ảnh thật.
  const zoneImg = $('#zone-img');
  if (zoneImg) zoneImg.addEventListener('load', () => drawZoneCanvas());

  const grabPointer = (event) => {
    try { canvas.setPointerCapture(event.pointerId); } catch (e) {}
  };

  /* Ghi lại điểm bắt đầu bấm. Chỉ khi con trỏ đi quá DRAG_THRESHOLD_PX thì mới
   * thật sự kéo — nhờ vậy bấm để CHỌN điểm không làm điểm chạy 1-2 px. */
  const rememberPress = (clientPoint) => {
    if (!ZE.drag) return;
    ZE.drag.press = clientPoint;
    ZE.drag.started = false;
  };

  canvas.addEventListener('pointerdown', (event) => {
    const clientPoint = [event.clientX, event.clientY];
    const point = pointerToNormalized(event);
    const zone = ZE.zones[ZE.active];

    // (1) Trúng ĐIỂM của bất kỳ vùng nào → chỉnh đúng điểm đó.
    //     (Điểm của vùng khác cũng kéo được: bấm vào là tự chuyển sang vùng đó.)
    const picked = pickVertex(clientPoint);
    if (picked) {
      if (ZE.draft.length) cancelDraft('Đã huỷ vẽ vùng mới — đang chỉnh điểm của vùng có sẵn');
      activateZone(picked.zoneIndex);
      const target = ZE.zones[ZE.active];
      if (target) {
        snapshotZones();
        if (ensureCanonicalRect(target)) {
          const rehit = nearestVertex(clientPoint, target.polygon);
          startVertexDrag(rehit ? rehit.index : picked.index, false);
        } else {
          startVertexDrag(picked.index, false);
        }
      }
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }

    // (2) Đang vẽ vùng mới → thêm/kéo điểm của bản nháp đó.
    if (ZE.draft.length) {
      const draftVertex = nearestVertex(clientPoint, ZE.draft);
      if (draftVertex) {
        startVertexDrag(draftVertex.index, true);
      } else {
        const draftEdge = hitEdge(clientPoint, ZE.draft);
        if (draftEdge) {
          ZE.draft.splice(draftEdge.index + 1, 0, draftEdge.point);
          startVertexDrag(draftEdge.index + 1, true);
        } else {
          ZE.draft.push(point);
          ZE.selectedVertex = ZE.draft.length - 1;
          updateDraftBar();
          renderZoneSidebar();
        }
      }
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }

    // (3) Chưa có vùng nào đang sửa → bắt đầu tạo vùng mới.
    if (!zone) {
      if (ZE.mode === 'rect') {
        ZE.rectStart = point;
        ZE.drag = { mode: 'rect', current: point };
      } else {
        ZE.draft.push(point);
        ZE.selectedVertex = 0;
        updateDraftBar();
        renderZoneSidebar();
      }
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }

    // (4) Trúng CẠNH của vùng đang sửa.
    let edge = hitEdge(clientPoint, zone.polygon || []);
    if (edge) {
      snapshotZones();
      if (ensureCanonicalRect(zone)) {
        // Vừa chuẩn hoá lại 4 góc → phải bắt lại cạnh theo toạ độ mới.
        edge = hitEdge(clientPoint, zone.polygon || []) || edge;
      }
      if (isRectZone(zone) && zone.polygon.length === 4) {
        // Hình chữ nhật: kéo cạnh = đổi kích thước, không chèn điểm lạ.
        startRectEdgeDrag(edge.index);
      } else {
        // Đa giác: chèn điểm mới đúng trên cạnh rồi kéo điểm đó.
        zone.polygon.splice(edge.index + 1, 0, edge.point);
        startVertexDrag(edge.index + 1, false);
      }
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }

    // (5) Bên trong vùng đang sửa → dời cả vùng.
    if ((zone.polygon || []).length >= 3 && pointInPolygon(point, zone.polygon)) {
      startZoneMove(point);
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }

    // (6) Bấm ra ngoài vùng: có thể vẽ hình chữ nhật mới, KHÔNG tự đổi vùng đang sửa.
    if (ZE.mode === 'rect') {
      ZE.rectStart = point;
      ZE.drag = { mode: 'rect', current: point };
      grabPointer(event);
      rememberPress(clientPoint);
      drawZoneCanvas();
      return;
    }
    ZE.selectedVertex = -1;
    const other = ZE.zones.findIndex((item, i) =>
      i !== ZE.active && (item.polygon || []).length >= 3 && pointInPolygon(point, item.polygon));
    if (other >= 0 && Date.now() - ZE.tipAt > 1500) {
      ZE.tipAt = Date.now();
      const name = zone.name || zone.id;
      toast('Đang sửa “' + name + '”. Muốn đổi vùng: bấm đúng vào một ĐIỂM của vùng kia, '
        + 'hoặc chọn trong danh sách “Vùng đang sửa”.', '', 5000);
    }
    grabPointer(event);
    drawZoneCanvas();
  });

  canvas.addEventListener('pointermove', (event) => {
    const clientPoint = [event.clientX, event.clientY];
    if (!ZE.drag) {
      updateZoneHover(clientPoint);
      return;
    }
    const point = pointerToNormalized(event);
    const zone = ZE.zones[ZE.active];
    const drag = ZE.drag;
    // Ngưỡng kéo: dưới 4 px coi như người dùng chỉ BẤM (chọn/thêm điểm), không
    // làm điểm chạy theo rung tay.
    if (drag.mode !== 'rect' && drag.press && !drag.started) {
      const moved = Math.hypot(clientPoint[0] - drag.press[0], clientPoint[1] - drag.press[1]);
      if (moved < DRAG_THRESHOLD_PX) return;
      drag.started = true;
    }
    if (drag.mode === 'vertex') {
      if (drag.draft) {
        if (ZE.draft[drag.index]) ZE.draft[drag.index] = point;
      } else if (zone && zone.polygon && zone.polygon[drag.index]) {
        if (isRectZone(zone) && zone.polygon.length === 4) {
          resizeRectCorner(zone.polygon, drag.index, point);
        } else {
          // Đa giác: CHỈ điểm đang kéo đổi toạ độ, hai cạnh dính vào nó xoay theo,
          // mọi điểm khác đứng yên.
          zone.polygon[drag.index] = point;
        }
      }
    } else if (drag.mode === 'edge') {
      if (zone && zone.polygon && zone.polygon.length === 4) {
        resizeRectEdge(zone.polygon, drag.index, point);
      }
    } else if (drag.mode === 'polygon' && zone) {
      const dx = point[0] - drag.start[0];
      const dy = point[1] - drag.start[1];
      zone.polygon = drag.original.map(([x, y]) => [clamp01(x + dx), clamp01(y + dy)]);
    } else if (drag.mode === 'rect') {
      drag.current = point;
    }
    drawZoneCanvas();
  });

  canvas.addEventListener('pointerleave', () => {
    const tip = $('#zone-tip');
    if (tip) tip.classList.remove('show');
    if (ZE.hover) { ZE.hover = null; drawZoneCanvas(); }
  });

  canvas.addEventListener('pointercancel', (event) => {
    if (ZE.drag) ZE.dirty = true;
    ZE.drag = null;
    ZE.rectStart = null;
    try { canvas.releasePointerCapture(event.pointerId); } catch (e) {}
    updateZoneHover([event.clientX, event.clientY]);
  });

  canvas.addEventListener('pointerup', (event) => {
    if (ZE.drag && ZE.drag.mode === 'rect' && ZE.rectStart) {
      const [sx, sy] = ZE.rectStart;
      const [ex, ey] = ZE.drag.current || pointerToNormalized(event);
      const dragged = Math.hypot((ex - sx) * 1000, (ey - sy) * 1000) > 1;
      if (dragged) {
        // Ô quá dẹt (kéo gần như theo một trục) trước đây bị BỎ IM LẶNG; nay nắn
        // cho nó tối thiểu 2% mỗi chiều để luôn tạo được vùng dùng được.
        const x1 = Math.min(sx, ex), x2 = Math.max(sx, ex);
        const y1 = Math.min(sy, ey), y2 = Math.max(sy, ey);
        const padX = Math.max(0, (0.02 - (x2 - x1)) / 2);
        const padY = Math.max(0, (0.02 - (y2 - y1)) / 2);
        const polygon = rectangleFromPoints(
          [clamp01(x1 - padX), clamp01(y1 - padY)],
          [clamp01(x2 + padX), clamp01(y2 + padY)],
        );
        snapshotZones();
        ZE.zones.push(newZone(polygon, 'rect'));
        ZE.active = ZE.zones.length - 1;
        ZE.idLocked = true;
        ZE.autoIds.add(ZE.zones[ZE.active].id);
        renderZoneSidebar(); renderZoneAttrs();
        toast('Đã tạo vùng hình chữ nhật — nhớ bấm “💾 Lưu vùng”', 'ok');
      }
    }
    if (ZE.drag) ZE.dirty = true;
    ZE.drag = null;
    ZE.rectStart = null;
    try { canvas.releasePointerCapture(event.pointerId); } catch (e) {}
    updateZoneHover([event.clientX, event.clientY]);
  });

  canvas.addEventListener('dblclick', (event) => {
    event.preventDefault();
    if (ZE.draft.length) finishDraftPolygon();
  });

  window.addEventListener('resize', debounce(() => { drawZoneCanvas(); }, 150));
  window.addEventListener('keydown', (event) => {
    if (S.tab !== 'zones') return;
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
    if (event.key === 'Delete' || event.key === 'Backspace') {
      if (ZE.selectedVertex >= 0) {
        const draft = ZE.draft.length ? ZE.draft : (ZE.zones[ZE.active] && ZE.zones[ZE.active].polygon);
        if (draft) {
          // Không cho xoá xuống dưới 3 điểm: vùng 2 điểm không có diện tích và
          // sẽ không lưu được (trước đây bấm Delete 2 lần là hỏng vùng).
          if (draft.length <= MIN_VERTICES) {
            toast('Vùng cần ít nhất ' + MIN_VERTICES + ' điểm — muốn bỏ vùng thì dùng “🗑 Xoá vùng”.', 'warn', 5000);
            event.preventDefault();
            return;
          }
          if (!ZE.draft.length) snapshotZones();
          draft.splice(ZE.selectedVertex, 1);
          ZE.selectedVertex = -1;
          ZE.dirty = true;
          drawZoneCanvas();
          event.preventDefault();
        }
      }
    } else if (event.key === 'Escape') {
      if (ZE.draft.length) {
        cancelDraft('Đã huỷ vẽ vùng mới');
      } else {
        ZE.selectedVertex = -1;
        drawZoneCanvas();
      }
    } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      if (event.shiftKey) redoZones(); else undoZones();
    }
  });
}

function uniqueZoneId(wanted) {
  let candidate = wanted;
  let n = 2;
  while (ZE.zones.some((z, i) => i !== ZE.active && z.id === candidate)) {
    candidate = wanted + '_' + n;
    n += 1;
  }
  return candidate;
}

/* Người dùng gõ tay vào ô “Mã vùng”. Vùng mới thì đổi luôn không cần migrate;
 * vùng đã lưu thì ghi vào ``idRenames`` để server cập nhật kịch bản. */
function applyZoneIdInput() {
  const zone = ZE.zones[ZE.active];
  if (!zone) return;
  const wasAuto = ZE.autoIds.has(zone.id);
  const wanted = slugify($('#zone-id').value.trim() || zone.name || zone.id);
  if (wanted && wanted !== zone.id) {
    snapshotZones();
    ZE.autoIds.delete(zone.id);
    const next = uniqueZoneId(wanted);
    if (!wasAuto) ZE.idRenames[zone.id] = next;
    zone.id = next;
    if (wasAuto) ZE.autoIds.add(next);
  }
  syncZoneAttributes();
}

function syncZoneAttributes() {
  const zone = ZE.zones[ZE.active];
  if (!zone) return;
  const name = $('#zone-name').value.trim() || ('Vùng ' + (ZE.active + 1));
  const previousId = zone.id;
  let id = previousId;
  // Mã vùng chỉ đổi khi người dùng CHỦ ĐỘNG mở khoá ô “Mã vùng” (hoặc vùng vừa
  // tạo, chưa lưu). Đổi tên KHÔNG đổi mã — đó chính là lỗi làm kịch bản mất
  // vùng trước đây: scenario trỏ tới 'khuvuc_test' trong khi vùng đã đổi mã.
  if (ZE.idLocked && ZE.autoIds.has(previousId)) {
    // Vùng mới: mã đi theo TÊN cho dễ đọc, chưa kịch bản nào trỏ tới nên
    // không cần ghi renames.
    const wanted = slugify(name);
    if (wanted && wanted !== previousId) {
      const next = uniqueZoneId(wanted);
      ZE.autoIds.delete(previousId);
      ZE.autoIds.add(next);
      id = next;
    }
  } else if (!ZE.idLocked) {
    const wanted = slugify($('#zone-id').value.trim() || name);
    if (wanted && wanted !== previousId) {
      id = uniqueZoneId(wanted);
      ZE.idRenames[previousId] = id;
    }
  }
  zone.name = name;
  zone.id = id;
  zone.color = $('#zone-color').value;
  const anchorValue = $('#zone-anchor').value;
  zone.anchor = anchorValue;
  zone.overlap_ratio = Math.max(0.05, Math.min(1, Number($('#zone-ratio').value || 50) / 100));
  $('#zone-ratio-field').hidden = anchorValue !== 'inside_ratio';
  zone.min_confidence = Number($('#zone-conf').value) / 100;
  zone.enabled = $('#zone-enabled').checked;
  const checked = $$('#class-list input:checked').map((input) => input.value);
  zone.classes = checked.length ? checked : ['person'];
  $('#zone-id').value = zone.id;
  $('#zone-id').readOnly = !(!ZE.idLocked || ZE.autoIds.has(zone.id));
  $('#zone-enabled-text').textContent = zone.enabled ? 'Đang bật' : 'Đang tắt';
  ZE.dirty = true;
  renderZoneSidebar();
  drawZoneCanvas();
}

/* Những kịch bản đang dùng vùng này — để cảnh báo trước khi xoá/đổi mã. */
function scenariosUsingZone(ref) {
  const scenarios = (S.config && S.config.scenarios) || [];
  return scenarios.filter((scenario) => scenario.zone === ref);
}

async function saveZones(extra = {}) {
  if (!ZE.cameraId) return;
  // Làm sạch + kiểm tra hình học TRƯỚC khi gửi: điểm trùng, vùng quá nhỏ, vùng
  // tự cắt nhau. Trước đây chỉ kiểm tra ">= 3 điểm" nên vùng hỏng vẫn lưu được.
  for (let index = 0; index < ZE.zones.length; index += 1) {
    const zone = ZE.zones[index];
    const cleaned = dedupePolygon(zone.polygon || []);
    const label = zone.name || zone.id || ('vùng ' + (index + 1));
    const problem = validatePolygon(cleaned, label);
    if (problem) {
      ZE.active = index;
      renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
      toast(problem, 'err', 9000);
      return;
    }
    zone.polygon = cleaned;
  }
  const present = new Set(ZE.zones.map((zone) => zone.id));
  const renames = {};
  Object.keys(ZE.idRenames).forEach((oldId) => {
    // Bỏ qua nếu mã cũ vẫn còn (người dùng đã hoàn tác) hoặc mã mới không tồn tại.
    if (present.has(oldId) || !present.has(ZE.idRenames[oldId])) return;
    renames[oldId] = ZE.idRenames[oldId];
  });
  try {
    const result = await api('/api/cameras/' + encodeURIComponent(ZE.cameraId) + '/zones', {
      method: 'POST',
      body: {
        zones: ZE.zones,
        renames,
        drop_orphan_scenarios: !!extra.dropOrphans,
      },
    });
    ZE.dirty = false;
    ZE.idRenames = {};
    updateDirtyBadge();
    const notes = [];
    if (result && result.renamed && Object.keys(result.renamed).length) {
      notes.push('đã cập nhật kịch bản theo mã vùng mới');
    }
    if (result && (result.dropped_scenarios || []).length) {
      notes.push('đã xoá ' + result.dropped_scenarios.length + ' kịch bản dùng vùng bị xoá');
    }
    toast('Đã lưu ' + ZE.zones.length + ' vùng cho ' + ZE.cameraId +
      (notes.length ? ' · ' + notes.join(' · ') : ''), 'ok', 6000);
    // Cảnh báo hình học (vùng tự cắt nhau…) do server kiểm tra lại.
    ((result && result.warnings) || []).forEach((warning) => toast(warning, 'warn', 12000));
    // loadConfig() tự gọi lại loadZoneEditor() khi đang ở tab Vùng phát hiện.
    await loadConfig();
  } catch (e) {
    const orphans = (e && e.data && e.data.orphans) || [];
    if (orphans.length && !extra.dropOrphans) {
      const names = orphans.map((item) => '“' + item.scenario_name + '”').join(', ');
      const ok = confirm(
        'Vùng đang bị xoá hoặc đổi mã nhưng kịch bản ' + names + ' vẫn dùng vùng đó.\n\n' +
        'Bấm OK để XOÁ LUÔN các kịch bản đó và lưu vùng.\n' +
        'Bấm Cancel để quay lại sửa (ví dụ: giữ nguyên mã vùng thay vì đổi).'
      );
      if (ok) { await saveZones({ dropOrphans: true }); return; }
      toast('Chưa lưu. Hãy đổi tên vùng thay vì xoá/đổi mã, hoặc xoá kịch bản trong tab Kịch bản.', 'warn', 8000);
      return;
    }
    toast(e.message, 'err', 9000);
  }
}

function refreshZonePreview() {
  if (!ZE.cameraId) return;
  const img = $('#zone-preview-img');
  img.src = '/snapshot/' + encodeURIComponent(ZE.cameraId) + '.jpg?t=' + Date.now() + tokenSuffix();
  img.onload = () => {
    const cam = (S.state && S.state.cameras || []).find((c) => c.id === ZE.cameraId);
    if (!cam) return;
    const detections = cam.detections || [];
    $('#preview-detect-info').textContent = detections.length
      ? (detections.length + ' đối tượng: ' + detections.map((d) => d.class + ' ' + Math.round((d.confidence || 0) * 100) + '%').join(', '))
      : 'Không thấy đối tượng nào trong khung hình.';
  };
}

/* =========================== SCENARIO BUILDER ========================= */
const TRIGGERS = [
  { type: 'enter', label: 'Có người vào vùng', help: 'Bắn khi vùng chuyển từ trống sang có người.' },
  { type: 'vacant', label: 'Vùng trống', help: 'Bắn khi vùng chuyển sang trống (sau thời gian chờ rời vùng).' },
  { type: 'count_ge', label: 'Số người ≥ N', help: 'Bắn khi số người trong vùng đạt ngưỡng.' },
  { type: 'count_le', label: 'Số người ≤ N', help: 'Bắn khi số người trong vùng giảm xuống ngưỡng.' },
  { type: 'dwell', label: 'Ở lâu hơn X giây', help: 'Bắn khi có người ở liên tục trong vùng đủ lâu.' },
  { type: 'class_present', label: 'Có lớp cụ thể', help: 'Ví dụ bật đèn khi thấy “cat”.' },
];

const PRESETS = {
  zigbee2mqtt: { topic: 'zigbee2mqtt/den_bep/set', payload: { state: 'ON', brightness: 254 } },
  ha: { topic: 'homeassistant/light/den_bep/set', payload: { state: 'ON', brightness: 255 } },
  tasmota: { topic: 'cmnd/tasmota/Backlog', payload: 'Power1 ON' },
  raw: { topic: 'nha/den_bep/lenh', payload: { lenh: 'bat', so_nguoi: '{{count}}' } },
};

function ensureScenarioDraft() {
  const scenarios = (S.config && S.config.scenarios) || [];
  if (!S.scenarioDraft) {
    S.scenarioDraft = scenarios.length ? clone(scenarios[0]) : newScenarioTemplate();
  }
  return S.scenarioDraft;
}

function newScenarioTemplate() {
  const cameras = (S.config && S.config.cameras) || [];
  const cam = cameras[0];
  const zone = cam && cam.zones && cam.zones[0];
  return {
    id: 'kich_ban_' + ((S.config && S.config.scenarios ? S.config.scenarios.length : 0) + 1),
    name: 'Kịch bản mới',
    description: '',
    enabled: true,
    zone: cam && zone ? (cam.id + '/' + zone.id) : '',
    cooldown_s: 2,
    on_enter: [{ after_s: 0, publish: { topic: 'zigbee2mqtt/den_bep/set', payload: { state: 'ON', brightness: 254 } } }],
    on_vacant: [],
    triggers: [],
  };
}

function triggerListOf(scenario) {
  const list = [];
  if ((scenario.on_enter || []).length) list.push({ type: 'enter', steps: scenario.on_enter });
  if ((scenario.on_vacant || []).length) list.push({ type: 'vacant', steps: scenario.on_vacant });
  (scenario.triggers || []).forEach((trigger) => {
    list.push({ type: trigger.type, steps: trigger.steps || [], source: trigger });
  });
  return list;
}

function renderScenarioPanel() {
  if (!S.config) return;
  const select = $('#scenario-select');
  const scenarios = S.config.scenarios || [];
  const options = scenarios.map((sc) => '<option value="' + escapeHtml(sc.id) + '">' + escapeHtml(sc.name) + '</option>').join('');
  if (select.dataset.signature !== options) {
    select.innerHTML = options || '<option value="">(chưa có kịch bản)</option>';
    select.dataset.signature = options;
  }
  const draft = ensureScenarioDraft();
  select.value = draft.id || '';

  $('#sc-name').value = draft.name || '';
  $('#sc-id').value = draft.id || '';
  $('#sc-desc').value = draft.description || '';
  $('#sc-cooldown').value = draft.cooldown_s ?? 2;
  $('#scenario-enabled').checked = draft.enabled !== false;

  const zoneOptions = [];
  const cameras = (S.config.cameras || []).filter((c) => c.enabled !== false);
  cameras.forEach((cam) => (cam.zones || []).forEach((zone) => {
    zoneOptions.push('<option value="' + escapeHtml(cam.id + '/' + zone.id) + '">' +
      escapeHtml(cam.name) + ' · ' + escapeHtml(zone.name || zone.id) + '</option>');
  }));
  const zoneSelect = $('#sc-zone');
  if (zoneSelect.dataset.signature !== zoneOptions.join('|')) {
    zoneSelect.innerHTML = zoneOptions.join('') || '<option value="">(chưa có vùng)</option>';
    zoneSelect.dataset.signature = zoneOptions.join('|');
  }
  zoneSelect.value = draft.zone || '';

  const schedule = draft.active_schedule;
  $('#sc-schedule').value = schedule ? 'custom' : '';
  $('#sc-sched-from').value = schedule ? schedule.from : '';
  $('#sc-sched-to').value = schedule ? schedule.to : '';

  renderTriggerChips(draft);
  renderStepTabs(draft);
  renderSteps(draft);
  renderTimeline(draft);
  renderPayloadPreview(draft);
}

function renderTriggerChips(draft) {
  const has = triggerListOf(draft).map((t) => t.type);
  $('#trigger-chips').innerHTML = TRIGGERS.map((trigger) =>
    '<span class="chip' + (has.includes(trigger.type) ? ' active' : '') + '" data-trigger="' + trigger.type + '">' +
    escapeHtml(trigger.label) + '</span>').join('');

  const parts = [];
  const needsCount = has.includes('count_ge') || has.includes('count_le');
  const needsSeconds = has.includes('dwell');
  const needsClass = has.includes('class_present');
  if (needsCount) {
    const existing = (draft.triggers || []).find((t) => t.type === 'count_ge' || t.type === 'count_le');
    parts.push('<label class="field"><span>Ngưỡng N</span><input type="number" min="1" max="50" id="trg-count" value="' +
      ((existing && existing.count) || 1) + '"></label>');
  }
  if (needsSeconds) {
    const existing = (draft.triggers || []).find((t) => t.type === 'dwell');
    parts.push('<label class="field"><span>Ở lâu (giây)</span><input type="number" min="1" max="3600" id="trg-seconds" value="' +
      ((existing && existing.seconds) || 60) + '"></label>');
  }
  if (needsClass) {
    const existing = (draft.triggers || []).find((t) => t.type === 'class_present');
    parts.push('<label class="field"><span>Lớp cần thấy</span><input type="text" id="trg-class" value="' +
      escapeHtml(((existing && existing.classes) || ['cat']).join(',')) + '" placeholder="cat"></label>');
  }
  $('#trigger-params').innerHTML = parts.join('');
  const chosen = TRIGGERS.filter((t) => has.includes(t.type));
  $('#trigger-help').textContent = chosen.length
    ? chosen.map((t) => t.label + ': ' + t.help).join(' • ')
    : 'Chọn ít nhất một điều kiện kích hoạt.';
}

const TRIGGER_LABELS = {
  enter: 'Có người vào',
  vacant: 'Vùng trống',
  count_ge: 'Số người ≥ N',
  count_le: 'Số người ≤ N',
  dwell: 'Ở lâu',
  class_present: 'Có lớp',
};

function activeTriggerType(draft) {
  const list = triggerListOf(draft);
  if (!list.length) return 'enter';
  if (S.scenarioTrigger && list.some((t) => t.type === S.scenarioTrigger)) return S.scenarioTrigger;
  S.scenarioTrigger = list[0].type;
  return S.scenarioTrigger;
}

function triggerIndex(draft, type) {
  return triggerListOf(draft).findIndex((t) => t.type === type);
}

function renderStepTabs(draft) {
  const list = triggerListOf(draft);
  const active = activeTriggerType(draft);
  const container = $('#step-trigger-tabs');
  if (!container) return;
  const chips = list.map((trigger) => {
    const count = (trigger.steps || []).length;
    return '<span class="chip' + (trigger.type === active ? ' active' : '') +
      '" data-step-trigger="' + escapeHtml(trigger.type) + '">' +
      escapeHtml(TRIGGER_LABELS[trigger.type] || trigger.type) + ' (' + count + ')</span>';
  });
  chips.push('<span class="small muted" style="align-self:center">chọn điều kiện để sửa các bước của nó</span>');
  container.innerHTML = chips.join('');
}

function stepsOf(draft, type) {
  if (type === 'enter') return (draft.on_enter = draft.on_enter || []);
  if (type === 'vacant') return (draft.on_vacant = draft.on_vacant || []);
  draft.triggers = draft.triggers || [];
  let trigger = draft.triggers.find((t) => t.type === type);
  if (!trigger) {
    trigger = { type, steps: [] };
    if (type === 'count_ge' || type === 'count_le') trigger.count = 1;
    if (type === 'dwell') trigger.seconds = 60;
    if (type === 'class_present') trigger.classes = ['cat'];
    draft.triggers.push(trigger);
  }
  return (trigger.steps = trigger.steps || []);
}

function renderSteps(draft) {
  const type = activeTriggerType(draft);
  const steps = stepsOf(draft, type);
  const container = $('#sc-steps');
  if (!steps.length) {
    container.innerHTML = '<p class="small muted">Chưa có bước nào cho điều kiện “' +
      escapeHtml(type) + '”. Bấm “Thêm bước”.</p>';
    return;
  }
  const currentTriggerIndex = triggerIndex(draft, type);
  container.innerHTML = steps.map((step, index) => {
    const publish = step.publish || { topic: '', payload: {} };
    return '<div class="step-editor">' +
      '<div class="step-head"><b>Bước ' + (index + 1) + '</b>' +
      '<span class="badge gray">' + escapeHtml(TRIGGER_LABELS[type] || type) + '</span>' +
      '<div class="spacer" style="flex:1"></div>' +
      '<button class="btn small ghost" data-step-test="' + index + '" data-step-trigger-idx="' +
      currentTriggerIndex + '">📤 Gửi thử</button>' +
      '<button class="btn small ghost" data-step-up="' + index + '">↑</button>' +
      '<button class="btn small ghost" data-step-down="' + index + '">↓</button>' +
      '<button class="btn small danger" data-step-del="' + index + '">✕</button></div>' +
      '<div class="grid3">' +
      '<label class="field"><span>Chờ (giây) trước khi gửi</span><input type="number" min="0" step="1" data-step-after="' + index + '" value="' + (step.after_s || 0) + '"></label>' +
      '<label class="field"><span>Topic</span><input type="text" data-step-topic="' + index + '" value="' + escapeHtml(publish.topic || '') + '"></label>' +
      '<label class="field"><span>Nhãn (không bắt buộc)</span><input type="text" data-step-label="' + index + '" value="' + escapeHtml(step.label || '') + '"></label>' +
      '</div>' +
      '<label class="field" style="margin-top:8px"><span>Payload (JSON hoặc text)</span>' +
      '<textarea data-step-payload="' + index + '" style="min-height:70px">' +
      escapeHtml(typeof publish.payload === 'string' ? publish.payload : JSON.stringify(publish.payload, null, 2)) +
      '</textarea></label>' +
      '<div class="row" style="margin-top:6px">' +
      '<label class="switch"><input type="checkbox" data-step-retain="' + index + '"' + (publish.retain ? ' checked' : '') + '><span class="track"></span><span>Retain</span></label>' +
      '<label class="field" style="max-width:110px"><span>QoS</span><select data-step-qos="' + index + '">' +
      [0, 1, 2].map((q) => '<option' + ((publish.qos ?? 1) === q ? ' selected' : '') + '>' + q + '</option>').join('') +
      '</select></label>' +
      '<div class="spacer" style="flex:1"></div>' +
      '<span class="small muted">Xem trước payload ở khung bên dưới</span>' +
      '</div></div>';
  }).join('');
}

function renderTimeline(draft) {
  const groups = triggerListOf(draft);
  const active = activeTriggerType(draft);
  if (!groups.length) {
    $('#sc-timeline').innerHTML = '<p class="small muted">Chọn điều kiện kích hoạt để thêm bước.</p>';
    return;
  }
  $('#sc-timeline').innerHTML = groups.map((group, triggerIdx) => {
    if (!(group.steps || []).length) return '';
    const head = '<div class="small" style="min-width:110px;color:' +
      (group.type === active ? '#86efac' : '#94a3b8') + ';font-weight:600;padding-top:14px">' +
      escapeHtml(TRIGGER_LABELS[group.type] || group.type) + '</div>';
    const steps = group.steps.map((step, index) => {
      const publish = step.publish || {};
      const payloadText = typeof publish.payload === 'string' ? publish.payload : JSON.stringify(publish.payload);
      return '<div class="tl-step">' +
        '<div class="tl-wait">' + (step.after_s ? 'chờ ' + step.after_s + 's' : 'ngay') + '</div>' +
        '<div class="tl-topic">' + escapeHtml(publish.topic || '(chưa có topic)') + '</div>' +
        '<div class="tl-payload">' + escapeHtml((payloadText || '').slice(0, 110)) + '</div>' +
        '<div class="tl-actions">' +
        '<button class="btn small ghost" data-step-test="' + index + '" data-step-trigger-idx="' +
        triggerIdx + '">Gửi thử</button>' +
        '<button class="btn small ghost" data-step-goto="' + index + '" data-step-goto-trigger="' +
        group.type + '">Sửa</button>' +
        '</div></div>';
    }).join('');
    return '<div style="display:flex;gap:6px;width:100%;margin-bottom:6px">' + head +
      '<div class="timeline" style="padding-top:8px;flex:1">' + steps + '</div></div>';
  }).join('') || '<p class="small muted">Thêm bước để xem dòng thời gian.</p>';
}

function renderPayloadPreview(draft) {
  const type = activeTriggerType(draft);
  const steps = stepsOf(draft, type);
  const payloads = steps.map((step) => step.publish && step.publish.payload).filter((p) => p !== undefined);
  const sample = {
    count: 2,
    zone: (draft.zone || 'cam/vung').split('/')[1] || 'vung',
    camera: (draft.zone || 'cam/vung').split('/')[0] || 'cam',
    timestamp: new Date().toISOString().slice(0, 19),
    class_counts: { person: 2 },
  };
  const rendered = payloads.map((payload) => renderTemplate(payload, sample));
  $('#sc-payload-preview').textContent = rendered.length
    ? JSON.stringify(rendered.length === 1 ? rendered[0] : rendered, null, 2)
    : '—';
}

function renderTemplate(value, context) {
  if (typeof value === 'string') {
    let out = value;
    Object.keys(context).forEach((key) => {
      const replacement = context[key];
      out = out.replaceAll('{{' + key + '}}', typeof replacement === 'object' ? JSON.stringify(replacement) : String(replacement));
    });
    return out;
  }
  if (Array.isArray(value)) return value.map((item) => renderTemplate(item, context));
  if (value && typeof value === 'object') {
    const result = {};
    Object.keys(value).forEach((key) => { result[key] = renderTemplate(value[key], context); });
    return result;
  }
  return value;
}

function collectScenarioDraft() {
  const draft = ensureScenarioDraft();
  draft.name = $('#sc-name').value.trim() || 'Kịch bản mới';
  const newId = slugify($('#sc-id').value.trim() || draft.name);
  draft.id = newId;
  draft.description = $('#sc-desc').value.trim();
  draft.cooldown_s = Number($('#sc-cooldown').value || 0);
  draft.zone = $('#sc-zone').value;
  draft.enabled = $('#scenario-enabled').checked;
  const scheduleMode = $('#sc-schedule').value;
  if (scheduleMode === 'custom') {
    draft.active_schedule = {
      from: $('#sc-sched-from').value.trim() || '00:00',
      to: $('#sc-sched-to').value.trim() || '23:59',
    };
  } else {
    draft.active_schedule = null;
  }
  // trigger parameters
  const type = activeTriggerType(draft);
  const countInput = $('#trg-count');
  if (countInput) {
    (draft.triggers || []).forEach((trigger) => {
      if (trigger.type === 'count_ge' || trigger.type === 'count_le') trigger.count = Number(countInput.value || 1);
    });
  }
  const secondsInput = $('#trg-seconds');
  if (secondsInput) {
    (draft.triggers || []).forEach((trigger) => {
      if (trigger.type === 'dwell') trigger.seconds = Number(secondsInput.value || 60);
    });
  }
  const classInput = $('#trg-class');
  if (classInput) {
    (draft.triggers || []).forEach((trigger) => {
      if (trigger.type === 'class_present') {
        trigger.classes = classInput.value.split(',').map((c) => c.trim()).filter(Boolean);
      }
    });
  }
  // steps: collect from DOM
  const steps = stepsOf(draft, type);
  $$('[data-step-topic]').forEach((input) => {
    const index = Number(input.dataset.stepTopic);
    if (!steps[index]) return;
    const payloadRaw = $('[data-step-payload="' + index + '"]').value;
    let payload = payloadRaw;
    try { payload = JSON.parse(payloadRaw); } catch (e) { payload = payloadRaw; }
    steps[index] = {
      after_s: Number(($('[data-step-after="' + index + '"]') || {}).value || 0),
      label: (($('[data-step-label="' + index + '"]') || {}).value || '').trim() || undefined,
      publish: {
        topic: input.value.trim(),
        payload,
        qos: Number((($('[data-step-qos="' + index + '"]') || {}).value) || 1),
        retain: !!($('[data-step-retain="' + index + '"]') || {}).checked,
      },
    };
  });
  // clean empty trigger arrays so config stays tidy
  if (!(draft.on_enter || []).length) delete draft.on_enter;
  if (!(draft.on_vacant || []).length) delete draft.on_vacant;
  draft.triggers = (draft.triggers || []).filter((trigger) => (trigger.steps || []).length);
  return draft;
}

async function saveScenario() {
  try {
    const draft = collectScenarioDraft();
    if (!draft.zone) { toast('Chọn vùng áp dụng trước', 'err'); return; }
    const steps = triggerListOf(draft).flatMap((t) => t.steps || []);
    if (!steps.length) { toast('Thêm ít nhất một bước hành động', 'err'); return; }
    const cfg = clone(S.config);
    cfg.scenarios = cfg.scenarios || [];
    const index = cfg.scenarios.findIndex((sc) => sc.id === draft.id);
    if (index >= 0) cfg.scenarios[index] = draft; else cfg.scenarios.push(draft);
    await api('/api/config', { method: 'PUT', body: { config: cfg } });
    toast('Đã lưu kịch bản “' + draft.name + '”', 'ok');
    await loadConfig();
    S.scenarioDraft = null;
    renderScenarioPanel();
  } catch (e) { toast(e.message, 'err', 9000); }
}

async function sendStepTest(triggerIdx, stepIdx) {
  const draft = ensureScenarioDraft();
  try {
    const data = await api('/api/scenarios/' + encodeURIComponent(draft.id) + '/test', {
      method: 'POST',
      body: { trigger: Math.max(0, Number(triggerIdx) || 0), step: Math.max(0, Number(stepIdx) || 0) },
    });
    if (data.ok) {
      toast('Đã gửi thử tới ' + (data.topic || '') + (data.dry_run ? ' (dry-run)' : ''), 'ok');
    } else {
      toast(data.message || 'Không gửi được', 'err');
    }
  } catch (e) { toast(e.message + ' — lưu kịch bản trước khi gửi thử', 'err', 7000); }
}

/* ------------------------------- YAML tab ------------------------------- */
async function loadYaml() {
  try {
    const response = await fetch('/api/config/yaml', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Không tải được YAML');
    $('#yaml-editor').value = await response.text();
    $('#yaml-status').textContent = '';
  } catch (e) { toast(e.message, 'err'); }
}

async function validateYaml() {
  const status = $('#yaml-status');
  status.textContent = 'đang kiểm tra…';
  try {
    const response = await fetch('/api/config/validate', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'text/plain' },
      body: $('#yaml-editor').value,
    });
    const data = await response.json();
    status.textContent = data.ok ? '✔ ' + (data.message || 'Hợp lệ') : '';
    if (!data.ok) { status.textContent = ''; toast(data.error || 'YAML không hợp lệ', 'err', 9000); }
  } catch (e) { status.textContent = ''; toast(e.message, 'err'); }
}

async function saveYaml() {
  try {
    await api('/api/config/yaml', {
      method: 'PUT',
      headers: { 'Content-Type': 'text/plain' },
      body: $('#yaml-editor').value,
    });
    toast('Đã lưu config.yaml', 'ok');
    await loadConfig();
  } catch (e) { toast(e.message, 'err', 9000); }
}

/* =============================== CONFIG =============================== */
async function loadConfig() {
  try {
    const data = await api('/api/config');
    S.config = data.config;
    S.configVersion = data.version;
    if (S.tab === 'zones') { await loadZoneEditor(); }
    if (S.tab === 'scenarios') { S.scenarioDraft = null; renderScenarioPanel(); }
    if (S.tab === 'settings') { renderSettings(); }
  } catch (e) {
    if (e.status !== 401) toast('Không tải được cấu hình: ' + e.message, 'err');
  }
}

/* ================================ WIZARD =============================== */
const WIZ = { step: 1, cameraId: null };

function openWizard() {
  $('#wizard').classList.add('show');
  wizardGo(1);
}

function closeWizard() {
  $('#wizard').classList.remove('show');
  // stop the preview stream so the camera is released (LED off if unused)
  const preview = $('#wiz-preview');
  if (preview) preview.removeAttribute('src');
}

function wizardGo(step) {
  WIZ.step = Math.min(3, Math.max(1, step));
  $$('.wizard-page').forEach((page, index) => page.classList.toggle('active', index === WIZ.step - 1));
  ['#w-step-1', '#w-step-2', '#w-step-3'].forEach((sel, index) => {
    const el = $(sel);
    el.className = 'ws' + (index < WIZ.step - 1 ? ' done' : (index === WIZ.step - 1 ? ' now' : ''));
  });
  $('#wiz-back').disabled = WIZ.step === 1;
  $('#wiz-next').textContent = WIZ.step === 3 ? 'Hoàn tất' : 'Tiếp →';
}

async function wizardTestCamera() {
  const url = $('#wiz-rtsp').value.trim();
  const status = $('#wiz-cam-status');
  if (!url) { toast('Nhập URL RTSP trước', 'warn'); return; }
  status.textContent = 'đang thêm camera và thử kết nối…';
  try {
    const cfg = clone(S.config || {});
    cfg.cameras = cfg.cameras || [];
    const id = slugify($('#wiz-rtsp').value.split('/').pop() || 'camera_ip') || 'camera_ip';
    const cameraId = cfg.cameras.some((c) => c.id === id) ? id + '_2' : id;
    cfg.cameras.push({
      id: cameraId,
      name: 'Camera ' + cameraId,
      source: 'rtsp',
      url,
      transport: $('#wiz-transport').value,
      zones: [{
        id: 'toan_khung',
        name: 'Toàn khung hình',
        polygon: [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
        classes: ['person'],
        color: '#22c55e',
      }],
    });
    await api('/api/config', { method: 'PUT', body: { config: cfg } });
    WIZ.cameraId = cameraId;
    await loadConfig();
    status.textContent = 'đã thêm camera ' + cameraId + ' — chờ hình ảnh…';
    setTimeout(() => {
      $('#wiz-preview').src = '/stream/' + encodeURIComponent(cameraId) + '.mjpg' + tokenSuffix();
    }, 800);
    setTimeout(async () => {
      const cam = (S.state && S.state.cameras || []).find((c) => c.id === cameraId);
      if (cam && cam.capture && cam.capture.connected) {
        status.textContent = '✔ Camera hoạt động';
        toast('Camera kết nối thành công', 'ok');
      } else {
        status.textContent = 'chưa có hình — kiểm tra mật khẩu/URL, hoặc thử transport UDP';
      }
    }, 6000);
  } catch (e) {
    status.textContent = '';
    toast('Không thêm được camera: ' + e.message, 'err', 9000);
  }
}

async function wizardTestMqtt() {
  const status = $('#wiz-mqtt-status');
  status.textContent = 'đang kiểm tra…';
  try {
    const cfg = clone(S.config || {});
    cfg.mqtt = cfg.mqtt || {};
    cfg.mqtt.host = $('#wiz-mq-host').value.trim() || cfg.mqtt.host;
    cfg.mqtt.port = Number($('#wiz-mq-port').value || 1883);
    cfg.mqtt.username = $('#wiz-mq-user').value.trim();
    if ($('#wiz-mq-pass').value) cfg.mqtt.password = $('#wiz-mq-pass').value;
    await api('/api/config', { method: 'PUT', body: { config: cfg } });
    await loadConfig();
    const result = await api('/api/mqtt/test', { method: 'POST' });
    status.textContent = result.ok ? '✔ ' + result.message : '';
    toast(result.message, result.ok ? 'ok' : 'err', 8000);
  } catch (e) {
    status.textContent = '';
    toast(e.message, 'err', 8000);
  }
}

async function wizardFinish() {
  try {
    const cfg = clone(S.config || {});
    const cameraId = WIZ.cameraId || ((cfg.cameras || [])[0] || {}).id;
    const camera = (cfg.cameras || []).find((c) => c.id === cameraId);
    const zone = camera && camera.zones && camera.zones[0];
    if (camera && zone) {
      const existing = (cfg.scenarios || []).some((sc) => sc.id === 'den_bep');
      // A test camera already ships with its own "Test: đèn bếp (giả lập)"
      // scenario; creating a second one on the same zone would only confuse.
      const isTestCamera = camera.test === true;
      if (!existing && !isTestCamera) {
        const topic = $('#wiz-sc-topic').value.trim() || 'zigbee2mqtt/den_bep/set';
        cfg.scenarios = cfg.scenarios || [];
        cfg.scenarios.push({
          id: 'den_bep',
          name: $('#wiz-sc-name').value.trim() || 'Đèn bếp tự động',
          description: 'Bật đèn khi có người, mờ sau 30 giây, tắt sau 2 phút',
          enabled: true,
          zone: camera.id + '/' + zone.id,
          cooldown_s: 2,
          on_enter: [{ after_s: 0, publish: { topic, payload: { state: 'ON', brightness: 254, transition: 1 } } }],
          on_vacant: [
            { after_s: 30, publish: { topic, payload: { state: 'ON', brightness: 80, transition: 3 } } },
            { after_s: 120, publish: { topic, payload: { state: 'OFF', transition: 2 } } },
          ],
          manual_override: { state_topic: topic.replace(/\/set$/, ''), pause_minutes: 10 },
        });
      }
      cfg.setup_completed = true;
      await api('/api/config', { method: 'PUT', body: { config: cfg } });
      await api('/api/wizard/complete', { method: 'POST' });
      toast(
        isTestCamera && !existing
          ? 'Xong! Camera test đã có sẵn kịch bản “Test: đèn bếp” (chạy thử).'
          : 'Thiết lập xong! Bật Dry-run rồi bấm “Mô phỏng” để thử.',
        'ok',
        8000,
      );
    } else {
      await api('/api/wizard/complete', { method: 'POST' });
      toast('Đã lưu. Hãy thêm camera trong tab Cài đặt.', 'warn', 7000);
    }
    await loadConfig();
    closeWizard();
  } catch (e) { toast(e.message, 'err', 9000); }
}

/* ================================= INIT =============================== */
function wireTabs() {
  $$('#tabs button').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
}

function wireHeader() {
  $('#dry-run-toggle').addEventListener('change', async (event) => {
    const value = event.target.checked;
    document.body.classList.toggle('dry-run', value);
    try {
      await api('/api/dry-run', { method: 'POST', body: { value } });
      toast('Dry-run ' + (value ? 'BẬT — không gửi lệnh thật' : 'TẮT — lệnh sẽ gửi thật'), value ? 'warn' : 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });
  const shutdownBtn = $('#btn-shutdown');
  if (shutdownBtn) shutdownBtn.addEventListener('click', askShutdown);
  $('#btn-help').addEventListener('click', () => $('#help-modal').classList.add('show'));
  $('#help-close').addEventListener('click', () => $('#help-modal').classList.remove('show'));
}

function wireLive() {
  $('#live-camera-select').addEventListener('change', (event) => {
    S.liveCamera = event.target.value;
    $('#live-img').dataset.cam = '';
    S.streamFails = 0;
    S.streamDeadSince = 0;
    hideStreamNote();
    renderLive();
  });
  $('#btn-snapshot').addEventListener('click', () => {
    const cam = currentLiveCamera();
    if (!cam) return;
    window.open('/snapshot/' + encodeURIComponent(cam.id) + '.jpg' + tokenSuffix(), '_blank');
  });
  $('#btn-reconnect-cam').addEventListener('click', () => {
    const cam = currentLiveCamera();
    if (!cam) return;
    S.streamFails = 0;
    S.streamDeadSince = 0;
    armLiveStream(cam, 'manual');
    toast('Đang kết nối lại ' + cam.id, '');
  });

  // Có khung hình trở lại (hoặc lỗi) thì cập nhật bộ đếm để watchdog thôi thử lại.
  const img = $('#live-img');
  if (img) {
    img.addEventListener('load', () => {
      S.streamFails = 0;
      S.streamDeadSince = 0;
      hideStreamNote();
    });
    img.addEventListener('error', () => {
      if (!S.streamDeadSince) S.streamDeadSince = Date.now();
    });
  }
  setInterval(checkLiveStream, 1500);
}

function wireZoneEditor() {
  $('#zone-camera-select').addEventListener('change', (event) => {
    ZE.cameraId = event.target.value;
    ZE.active = 0;
    LS.set('zoneCamera', ZE.cameraId);
    setSelectedCamera(ZE.cameraId, { from: 'zone' });
    loadZoneEditor();
  });
  $('#zone-select').addEventListener('change', (event) => {
    cancelDraft('Đã huỷ vẽ vùng mới (bạn chọn vùng khác để sửa)');
    ZE.active = Number(event.target.value);
    ZE.selectedVertex = -1;
    ZE.hover = null;
    ZE.drag = null;
    ZE.idLocked = true;
    renderZoneAttrs(); drawZoneCanvas();
  });
  $('#zone-mode-poly').addEventListener('click', () => {
    setZoneTool('poly');
    toast('Vẽ đa giác: bấm từng điểm, bấm đúp để đóng vùng', '');
  });
  $('#zone-mode-rect').addEventListener('click', () => {
    setZoneTool('rect');
    toast('Vẽ hình chữ nhật: kéo chuột trên ảnh để tạo vùng', '');
  });
  const shapeSelect = $('#zone-shape');
  if (shapeSelect) {
    shapeSelect.addEventListener('change', (event) => {
      const zone = ZE.zones[ZE.active];
      if (!zone) return;
      const shape = event.target.value;
      if (shape === zoneShapeOption(zone)) return;
      snapshotZones();
      applyZoneShape(zone, shape);
      ZE.selectedVertex = -1;
      renderZoneAttrs();
      drawZoneCanvas();
      const labels = { rect: 'Hình chữ nhật', full: 'Toàn khung hình', polygon: 'Đa giác tự do' };
      toast('Đã chuyển vùng sang: ' + (labels[shape] || shape) + ' — kéo điểm/giữa cạnh để chỉnh', 'ok');
    });
  }
  const idEdit = $('#zone-id-edit');
  if (idEdit) {
    idEdit.addEventListener('click', () => {
      ZE.idLocked = !ZE.idLocked;
      renderZoneAttrs();
      if (!ZE.idLocked) {
        $('#zone-id').focus();
        $('#zone-id').select();
        toast('Đang cho sửa mã vùng — kịch bản sẽ được cập nhật theo mã mới khi bạn bấm Lưu', 'warn', 7000);
      }
    });
  }
  $('#zone-undo').addEventListener('click', undoZones);
  $('#zone-redo').addEventListener('click', redoZones);
  $('#zone-finish').addEventListener('click', finishDraftPolygon);
  $('#zone-new').addEventListener('click', () => {
    ZE.draft = [];
    ZE.selectedVertex = -1;
    ZE.hover = null;
    ZE.active = -1;
    ZE.idLocked = true;
    ZE.drag = null;
    updateDraftBar();
    renderZoneSidebar();
    renderZoneAttrs();
    drawZoneCanvas();
    toast(ZE.mode === 'rect'
      ? 'Kéo chuột trên ảnh để vẽ vùng hình chữ nhật mới'
      : 'Nhấn lên ảnh để bắt đầu vẽ vùng mới', '');
  });
  $('#zone-delete').addEventListener('click', () => {
    if (ZE.active < 0 || !ZE.zones[ZE.active]) return;
    const zone = ZE.zones[ZE.active];
    const used = scenariosUsingZone(ZE.cameraId + '/' + zone.id);
    let message = 'Xoá vùng “' + (zone.name || zone.id) + '”?';
    if (used.length) {
      message += '\n\nKịch bản đang dùng vùng này: ' +
        used.map((sc) => '“' + sc.name + '”').join(', ') +
        '.\nKhi bấm “Lưu vùng”, app sẽ hỏi có xoá luôn các kịch bản đó không.';
    }
    if (!confirm(message)) return;
    snapshotZones();
    ZE.zones.splice(ZE.active, 1);
    ZE.active = ZE.zones.length ? 0 : -1;
    ZE.idLocked = true;
    renderZoneSidebar(); renderZoneAttrs(); drawZoneCanvas();
  });
  $('#zone-save').addEventListener('click', () => {
    if (ZE.draft.length) {
      toast('Bản nháp ' + ZE.draft.length + ' điểm chưa được tạo thành vùng nên không được lưu '
        + '— bấm “✔ Hoàn tất vùng” trước khi lưu.', 'warn', 6000);
    }
    saveZones();
  });
  $('#zone-ratio').addEventListener('input', () => {
    $('#zone-ratio-val').textContent = Math.round($('#zone-ratio').value) + '%';
    syncZoneAttributes();   // cập nhật ngay, không cần thả chuột
  });
  ['#zone-color', '#zone-anchor', '#zone-enabled', '#zone-ratio'].forEach((sel) => {
    $(sel).addEventListener('change', syncZoneAttributes);
  });
  $('#zone-id').addEventListener('change', applyZoneIdInput);
  $('#zone-name').addEventListener('input', debounce(syncZoneAttributes, 300));
  $('#zone-conf').addEventListener('input', (event) => {
    $('#zone-conf-val').textContent = event.target.value + '%';
    syncZoneAttributes();
  });
  $('#class-search').addEventListener('input', debounce(renderClassList, 200));
  $('#class-list').addEventListener('change', syncZoneAttributes);
  $('#class-reload').addEventListener('click', loadClassList);
  $('#btn-refresh-preview').addEventListener('click', refreshZonePreview);
  const freezeBtn = $('#zone-freeze');
  if (freezeBtn) {
    freezeBtn.addEventListener('click', () => {
      if (ZE.frozen) {
        // Đang xem ảnh tĩnh: bấm = chụp LẠI khung mới nhất (mở luồng trong giây lát).
        ZE.frozen = false;
        ZE.frozenSrc = null;
        setZoneImageSource();
        const img = $('#zone-img');
        const once = () => { img.removeEventListener('load', once); freezeZoneFrame(); };
        img.addEventListener('load', once);
        toast('Đang lấy khung hình mới…', '');
      } else {
        if (freezeZoneFrame()) toast('Đã đóng băng khung hình để vẽ cho chắc tay', 'ok');
        else toast('Chưa có khung hình để chụp — thử lại sau 1-2 giây', 'warn');
      }
    });
  }
  const liveBtn = $('#zone-live');
  if (liveBtn) {
    liveBtn.addEventListener('click', () => {
      goLiveZoneFrame();
      toast('Đang xem luồng trực tiếp — vẽ trên hình động sẽ khó canh hơn', 'warn', 5000);
    });
  }
  wireZoneCanvas();
}

function wireScenarioBuilder() {
  $('#scenario-select').addEventListener('change', (event) => {
    const scenarios = (S.config && S.config.scenarios) || [];
    const found = scenarios.find((sc) => sc.id === event.target.value);
    if (found) { S.scenarioDraft = clone(found); S.scenarioTrigger = null; renderScenarioPanel(); }
  });
  $('#scenario-new').addEventListener('click', () => {
    S.scenarioTrigger = null;
    S.scenarioDraft = newScenarioTemplate();
    renderScenarioPanel();
  });
  $('#scenario-delete').addEventListener('click', async () => {
    const draft = ensureScenarioDraft();
    if (!confirm('Xoá kịch bản “' + draft.name + '”?')) return;
    try {
      const cfg = clone(S.config);
      cfg.scenarios = (cfg.scenarios || []).filter((sc) => sc.id !== draft.id);
      await api('/api/config', { method: 'PUT', body: { config: cfg } });
      S.scenarioDraft = null;
      toast('Đã xoá kịch bản', 'ok');
      await loadConfig();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#trigger-chips').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const draft = collectScenarioDraft();
    const type = chip.dataset.trigger;
    const list = triggerListOf(draft);
    const exists = list.some((t) => t.type === type);
    if (exists) {
      if (type === 'enter') draft.on_enter = [];
      else if (type === 'vacant') draft.on_vacant = [];
      else draft.triggers = (draft.triggers || []).filter((t) => t.type !== type);
    } else {
      if (type === 'enter') draft.on_enter = draft.on_enter || [{ after_s: 0, publish: clone(PRESETS.zigbee2mqtt) }];
      else if (type === 'vacant') draft.on_vacant = draft.on_vacant || [{ after_s: 30, publish: clone(PRESETS.zigbee2mqtt) }];
      else {
        draft.triggers = draft.triggers || [];
        draft.triggers.push({ type, steps: [{ after_s: 0, publish: clone(PRESETS.zigbee2mqtt) }] });
      }
    }
    S.scenarioTrigger = exists ? activeTriggerType(draft) : type;
    S.scenarioDraft = draft;
    renderScenarioPanel();
  });
  $('#sc-add-step').addEventListener('click', () => {
    const draft = collectScenarioDraft();
    const steps = stepsOf(draft, activeTriggerType(draft));
    steps.push({ after_s: steps.length ? 30 : 0, publish: clone(PRESETS.zigbee2mqtt) });
    S.scenarioDraft = draft;
    renderScenarioPanel();
  });
  $('#sc-steps').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    const draft = collectScenarioDraft();
    const steps = stepsOf(draft, activeTriggerType(draft));
    if (button.dataset.stepTest !== undefined) {
      sendStepTest(Number(button.dataset.stepTriggerIdx || 0), Number(button.dataset.stepTest));
      return;
    }
    if (button.dataset.stepDel !== undefined) {
      steps.splice(Number(button.dataset.stepDel), 1);
    } else if (button.dataset.stepUp !== undefined) {
      const index = Number(button.dataset.stepUp);
      if (index > 0) [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]];
    } else if (button.dataset.stepDown !== undefined) {
      const index = Number(button.dataset.stepDown);
      if (index < steps.length - 1) [steps[index + 1], steps[index]] = [steps[index], steps[index + 1]];
    }
    S.scenarioDraft = draft;
    renderScenarioPanel();
  });
  $('#sc-timeline').addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.stepTest !== undefined) {
      sendStepTest(Number(button.dataset.stepTriggerIdx || 0), Number(button.dataset.stepTest));
    }
    if (button.dataset.stepGoto !== undefined) {
      S.scenarioTrigger = button.dataset.stepGotoTrigger || S.scenarioTrigger;
      S.scenarioDraft = collectScenarioDraft();
      renderScenarioPanel();
      const editor = $('[data-step-topic="' + button.dataset.stepGoto + '"]');
      if (editor) editor.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  });

  $('#step-trigger-tabs').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-step-trigger]');
    if (!chip) return;
    S.scenarioDraft = collectScenarioDraft();
    S.scenarioTrigger = chip.dataset.stepTrigger;
    renderScenarioPanel();
  });
  $('#sc-save').addEventListener('click', saveScenario);
  $('#sc-schedule').addEventListener('change', () => renderScenarioPanel());
  $$('[data-preset]').forEach((button) => {
    button.addEventListener('click', () => {
      const preset = PRESETS[button.dataset.preset];
      if (!preset) return;
      const draft = collectScenarioDraft();
      const steps = stepsOf(draft, activeTriggerType(draft));
      if (!steps.length) steps.push({ after_s: 0, publish: clone(preset) });
      else steps[steps.length - 1].publish = clone(preset);
      S.scenarioDraft = draft;
      renderScenarioPanel();
      toast('Đã áp mẫu ' + button.dataset.preset, 'ok');
    });
  });
  $('#yaml-validate').addEventListener('click', validateYaml);
  $('#yaml-save').addEventListener('click', saveYaml);
  $('#yaml-reload').addEventListener('click', loadYaml);
}

function wireSimulator() {
  $('#sim-zones').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-sim]');
    if (!button) return;
    simulateZone(button.dataset.zone, button.dataset.sim);
  });
  $('#sim-stop-all').addEventListener('click', async () => {
    try {
      await api('/api/simulate/clear', { method: 'POST', body: {} });
      toast('Đã tắt toàn bộ mô phỏng', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#sim-speed').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    setSimulationScale(Number(chip.dataset.scale));
  });
}

function wireLogs() {
  $('#log-kind').addEventListener('change', renderLogs);
  $('#log-level').addEventListener('change', renderLogs);
  $('#log-clear').addEventListener('click', () => { $('#log-stream').innerHTML = ''; });
}

function wireMqtt() {
  $('#mqtt-test').addEventListener('click', async () => {
    const badge = $('#mqtt-state-badge');
    badge.textContent = 'đang kiểm tra…';
    try {
      const result = await api('/api/mqtt/test', { method: 'POST' });
      toast(result.message, result.ok ? 'ok' : 'err', 8000);
    } catch (e) { toast(e.message, 'err'); }
    renderMqttStatus();
  });
  $('#pub-send').addEventListener('click', async () => {
    try {
      const result = await api('/api/mqtt/publish', {
        method: 'POST',
        body: {
          topic: $('#pub-topic').value.trim(),
          payload: $('#pub-payload').value,
          qos: Number($('#pub-qos').value),
          retain: $('#pub-retain').checked,
        },
      });
      toast(result.sent ? 'Đã gửi' : (result.dry_run ? 'Dry-run: không gửi thật' : 'Đã xếp hàng chờ kết nối'), 'ok');
      loadMonitor();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#mon-refresh').addEventListener('click', loadMonitor);
  $('#mon-topic').addEventListener('input', debounce(loadMonitor, 400));
  $('#mon-direction').addEventListener('change', loadMonitor);
}

function wireSettings() {
  $('#settings-save').addEventListener('click', saveSettings);
  $('#settings-reload').addEventListener('click', async () => { await loadConfig(); toast('Đã tải lại cấu hình', 'ok'); });
  $('#det-conf').addEventListener('input', (event) => {
    $('#det-conf-val').textContent = event.target.value + '%';
  });
  $('#add-camera').addEventListener('click', async () => {
    const url = $('#new-cam-url').value.trim();
    const id = slugify($('#new-cam-id').value.trim() || 'camera');
    if (!url) { toast('Nhập URL RTSP', 'err'); return; }
    if (!url.startsWith('rtsp://')) { toast('URL phải bắt đầu bằng rtsp://', 'err'); return; }
    try {
      await api('/api/config', { method: 'PUT', body: { config: collectSettings() } });
      const cfg = clone(S.config);
      cfg.cameras = cfg.cameras || [];
      if (cfg.cameras.some((c) => c.id === id)) { toast('Mã camera đã tồn tại', 'err'); return; }
      cfg.cameras.push({
        id,
        name: $('#new-cam-name').value.trim() || ('Camera ' + id),
        source: 'rtsp',
        url,
        transport: $('#new-cam-transport').value,
        zones: [],
      });
      await api('/api/config', { method: 'PUT', body: { config: cfg } });
      toast('Đã thêm camera ' + id, 'ok');
      $('#new-cam-url').value = ''; $('#new-cam-id').value = ''; $('#new-cam-name').value = '';
      await loadConfig();
    } catch (e) { toast(e.message, 'err', 9000); }
  });
  $('#settings-cameras').addEventListener('click', async (event) => {
    const promote = event.target.closest('[data-cam-promote]');
    const remove = event.target.closest('[data-cam-delete]');
    const watch = event.target.closest('[data-cam-watch]');
    const check = event.target.closest('[data-cam-check]');
    try {
      if (check) {
        const cameraId = check.dataset.camCheck;
        S.camChecks[cameraId] = { pending: true };
        renderSettingsCameras();
        try {
          S.camChecks[cameraId] = await api(
            '/api/cameras/' + encodeURIComponent(cameraId) + '/test-connection',
            { method: 'POST' });
        } catch (error) {
          S.camChecks[cameraId] = { ok: false, message: error.message };
        }
        renderSettingsCameras();
        return;
      }
      if (promote) {
        await api('/api/test-camera/' + encodeURIComponent(promote.dataset.camPromote) + '/promote', { method: 'POST' });
        toast('Đã chuyển thành camera thật', 'ok');
        await loadConfig();
      } else if (remove) {
        if (!confirm('Xoá camera này và TẤT CẢ kịch bản của nó?')) return;
        await api('/api/cameras/' + encodeURIComponent(remove.dataset.camDelete) + '/delete', { method: 'POST' });
        toast('Đã xoá camera', 'ok');
        await loadConfig();
      } else if (watch) {
        S.liveCamera = watch.dataset.camWatch;
        switchTab('live');
      }
    } catch (e) { toast(e.message, 'err'); }
  });
  $('#settings-cameras').addEventListener('change', async (event) => {
    const toggle = event.target.closest('[data-cam-enabled]');
    if (!toggle) return;
    const cameraId = toggle.dataset.camEnabled;
    try {
      await api('/api/cameras/' + encodeURIComponent(cameraId) + '/enabled', {
        method: 'POST',
        body: { value: toggle.checked },
      });
      toast((toggle.checked ? 'Đã bật ' : 'Đã tắt ') + cameraId, 'ok');
      await loadConfig();
    } catch (e) {
      toggle.checked = !toggle.checked;
      toast(e.message, 'err');
    }
  });
  $('#webcam-refresh').addEventListener('click', loadWebcams);
  $('#testcam-add').addEventListener('click', async () => {
    try {
      const result = await api('/api/test-camera/add', { method: 'POST' });
      toast(result.created ? 'Đã thêm camera test' : 'Camera test đã có sẵn', 'ok');
      await loadConfig();
    } catch (e) { toast(e.message, 'err', 9000); }
  });
  $('#loopback-start').addEventListener('click', async () => {
    toast('Đang khởi động loopback…', '');
    try {
      const result = await api('/api/loopback/start', { method: 'POST' });
      toast(result.message || (result.ok ? 'Loopback chạy' : 'Không chạy được'), result.ok ? 'ok' : 'err', 9000);
      await loadConfig();
      loadWebcams();
    } catch (e) { toast(e.message, 'err', 9000); }
  });
  $('#loopback-stop').addEventListener('click', async () => {
    try {
      await api('/api/loopback/stop', { method: 'POST' });
      toast('Đã tắt loopback', 'ok');
      loadWebcams();
    } catch (e) { toast(e.message, 'err'); }
  });
}

function wireWizard() {
  $('#wiz-next').addEventListener('click', () => {
    if (WIZ.step < 3) wizardGo(WIZ.step + 1);
    else wizardFinish();
  });
  $('#wiz-back').addEventListener('click', () => wizardGo(WIZ.step - 1));
  $('#wiz-skip').addEventListener('click', async () => {
    try { await api('/api/wizard/complete', { method: 'POST' }); } catch (e) {}
    closeWizard();
  });
  $('#wiz-test-cam').addEventListener('click', wizardTestCamera);
  $('#wiz-test-mqtt').addEventListener('click', wizardTestMqtt);
}

async function boot() {
  installPointerGuard();
  wireTabs();
  wireHeader();
  wireLive();
  wireZoneEditor();
  wireScenarioBuilder();
  wireSimulator();
  wireLogs();
  wireMqtt();
  wireSettings();
  wireWizard();

  // initial HTTP state (WS will take over)
  try {
    S.state = await api('/api/state');
    noteAssetVersion(S.state);
    renderAll();
  } catch (e) {
    if (e.status === 401) { location.href = '/login'; return; }
    toast('Không tải được trạng thái: ' + e.message, 'err');
  }
  await loadConfig();
  loadEvents();
  if (S.tab === 'scenarios') loadYaml();
  connectWs();

  // first-run wizard
  const setupDone = S.state && S.state.setup_completed;
  if (!setupDone) setTimeout(openWizard, 600);

  setInterval(() => { if (!S.state) return; }, 5000);
}

/* Cầu nối nhỏ để gỡ lỗi trong Console trình duyệt và để test tự động kiểm tra
 * được "panel có bị dựng lại không" — không ảnh hưởng chức năng của app. */
window.__VO__ = {
  get state() { return S.state; },
  get config() { return S.config; },
  get tab() { return S.tab; },
  renderAll,
  setState(next) { S.state = next; noteAssetVersion(next); renderAll(); },
  // --- luồng hình (dùng cho test tự động + gỡ lỗi "mất hình") ---
  get stream() {
    const img = $('#live-img');
    const note = $('#live-stream-note');
    return {
      src: img ? img.getAttribute('src') : null,
      hasPixels: liveStreamHasPixels(),
      fails: S.streamFails,
      stopped: S.stopped,
      noteShown: !!(note && note.classList.contains('show')),
      note: note ? note.textContent : '',
      stoppedScreen: !!$('#stopped-screen'),
    };
  },
  armLiveStream(reason) { return armLiveStream(currentLiveCamera(), reason || 'manual'); },
  checkLiveStream,
  resumeAfterReconnect,
  showStoppedScreen,
  markServerWasDown() { S.everWentDown = true; },
};

document.addEventListener('DOMContentLoaded', boot);

// --------------------------------------------------------------------------
// Tắt ứng dụng an toàn từ giao diện (nút ⏻ cạnh logo)
//
// Vì sao cần: máy chạy 24/7 không có màn hình; trước đây muốn dừng phải mở
// Terminal (Ctrl-C) hoặc chạy Dung-lai.bat. Nút này gọi đúng endpoint mà
// Dung-lai.bat dùng, nên hành vi giống hệt Ctrl-C: gửi MQTT "offline", đóng
// camera, lưu config.yaml rồi thoát.
// --------------------------------------------------------------------------
function askShutdown() {
  if (S.stopped) return;
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop show';
  backdrop.id = 'shutdown-modal';
  backdrop.innerHTML =
    '<div class="modal" style="max-width:520px">' +
      '<h2>⏻ Tắt ứng dụng?</h2>' +
      '<p>Ứng dụng sẽ dừng <b>an toàn</b>:</p>' +
      '<ul class="small" style="margin:8px 0 8px 18px;line-height:1.7">' +
        '<li>gửi trạng thái <b>offline</b> cho MQTT (báo hệ thống đã dừng),</li>' +
        '<li>đóng mọi luồng camera (đèn webcam tắt),</li>' +
        '<li>lưu <code>config.yaml</code> (kèm <code>.bak</code>).</li>' +
      '</ul>' +
      '<p class="small muted">Đèn/thiết bị đang bật sẽ <b>giữ nguyên trạng thái</b> khi app tắt ' +
        '(an toàn, không tự tắt oan) — muốn tắt thì tắt trên app điều khiển như bình thường.</p>' +
      '<p class="small muted">Muốn chạy lại: bấm <b>Khoi-dong.bat</b> (Windows) hoặc chạy ' +
        '<code>python run.py</code> trên máy này. Trình duyệt/phần mềm khác không bị ảnh hưởng.</p>' +
      '<div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">' +
        '<button class="btn ghost" id="shutdown-cancel">Huỷ</button>' +
        '<button class="btn danger" id="shutdown-confirm">⏻ Tắt ngay</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  $('#shutdown-cancel').addEventListener('click', close);
  backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
  $('#shutdown-confirm').addEventListener('click', async () => {
    const btn = $('#shutdown-confirm');
    btn.disabled = true;
    btn.textContent = 'Đang dừng…';
    try {
      await api('/api/system/shutdown', { method: 'POST' });
      close();
      showStoppedScreen();
    } catch (e) {
      close();
      toast('Không tắt được: ' + e.message, 'err');
    }
  });
}

function showStoppedScreen() {
  S.stopped = true;
  try { if (S.ws) S.ws.close(1000); } catch (e) {}
  ['#live-img', '#zone-img'].forEach((sel) => {
    const img = $(sel);
    if (img) img.removeAttribute('src');    // ngắt luồng MJPEG
  });
  const screen = document.createElement('div');
  screen.id = 'stopped-screen';
  screen.className = 'stopped-screen';
  screen.innerHTML =
    '<div class="stopped-box">' +
      '<div class="stopped-icon">⏻</div>' +
      '<h2>Ứng dụng đang dừng</h2>' +
      '<p class="small" id="stopped-text">Đang gửi MQTT offline, đóng camera và lưu cấu hình…</p>' +
      '<p class="small muted">Khi dòng trên đổi thành “đã dừng”, bạn có thể đóng tab này.<br>' +
      'Chạy lại: <b>Khoi-dong.bat</b> (Windows) hoặc <code>python run.py</code>.</p>' +
    '</div>';
  document.body.appendChild(screen);

  // Chờ máy chủ thật sự ngừng trả lời rồi báo "đã dừng" (không đoán).
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    let alive = false;
    try {
      const ctrl = new AbortController();
      const kill = setTimeout(() => ctrl.abort(), 1200);
      const res = await fetch('/api/state/public', { cache: 'no-store', signal: ctrl.signal });
      clearTimeout(kill);
      alive = res.ok;
    } catch (e) { alive = false; }
    const text = $('#stopped-text');
    if (!alive) {
      S.everWentDown = true;
      if (text && !text.classList.contains('ok')) {
        text.innerHTML = '✅ Ứng dụng <b>đã dừng</b> — đã lưu cấu hình, đã gửi MQTT offline.';
        text.className = 'small ok';
      }
      return;   // vẫn tiếp tục canh: nếu người dùng chạy lại app thì tự phục hồi
    }
    if (S.everWentDown) {
      // Máy chủ đã tắt hẳn rồi sống lại ⇒ KHÔNG bắt người dùng bấm F5:
      // gỡ màn hình chờ, nối lại WebSocket và xin luồng hình mới.
      clearInterval(timer);
      S.wsDownAt = Date.now();
      resumeAfterReconnect();
      toast('Ứng dụng đã chạy lại — đang kết nối lại…', 'ok');
      return;
    }
    if (attempts > 40 && text) {
      clearInterval(timer);
      text.innerHTML = 'Ứng dụng chưa dừng hẳn sau 40 giây. Kiểm tra log <code>logs/vision-automation.log</code> ' +
        'hoặc tắt tiến trình python trong Task Manager.';
      text.className = 'small warn';
    }
  }, 1000);
}
