// Hệ thống thông báo nổi Toast Notification
function showToast(msg, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position:fixed; top:24px; right:24px; z-index:999999; display:flex; flex-direction:column; gap:8px; pointer-events:none;';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  const bg = type === 'warning' ? '#f59e0b' : (type === 'error' ? '#ef4444' : '#10b981');
  toast.style.cssText = `background:${bg}; color:#fff; font-weight:700; padding:12px 20px; border-radius:8px; box-shadow:0 8px 24px rgba(0,0,0,0.5); font-size:13.5px; pointer-events:auto; transition:all 0.25s ease; transform:translateX(20px); opacity:0; white-space:pre-line;`;
  toast.textContent = String(msg ?? '');
  container.appendChild(toast);
  setTimeout(() => { toast.style.transform = 'translateX(0)'; toast.style.opacity = '1'; }, 10);
  setTimeout(() => {
    toast.style.transform = 'translateX(20px)';
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 250);
  }, 3500);
}

let gChannels = [];
let gGroups = [];
let gProfiles = [];
let gOptions = [];
let gFFmpegPresets = [];
let gRenderPresets = [];
let gSelectedIds = new Set();
let gQueueItems = [];
let gPipelineRunning = false;
let gPendingBannerDismissed = false;
let gPendingQueueIds = [];
let gLastQueueProject = null;
let gApiToken = null;
let gApiTokenPromise = null;
let gLiveRefreshInFlight = false;
let gLiveRefreshTimer = null;
let gMonitorLifecycleTimer = null;
let gApiRetryTimer = null;

function selectedMultiValues(selectEl) {
  return selectEl ? Array.from(selectEl.selectedOptions || []).map(option => option.value).filter(Boolean) : [];
}

function groupNamesFromItem(item) {
  let names = [];
  try {
    const raw = item && item.target_group_names_json;
    const parsed = typeof raw === 'string' ? JSON.parse(raw || '[]') : raw;
    if (Array.isArray(parsed)) names = parsed.map(x => String(x || '').trim()).filter(Boolean);
  } catch (_) {}
  if (!names.length && item && item.target_group_name) names = [String(item.target_group_name).trim()];
  return [...new Set(names)];
}

function setGroupSelectOptions(selectEl, groups, defaultGroup = false) {
  if (!selectEl) return;
  const prior = selectedMultiValues(selectEl);
  const values = (groups || []).map(group => String(group.name || '')).filter(Boolean);
  selectEl.innerHTML = values.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
  const chosen = new Set(prior.filter(name => values.includes(name)));
  if (!prior.length && defaultGroup && values.includes('Mặc định')) chosen.add('Mặc định');
  Array.from(selectEl.options).forEach(option => { option.selected = chosen.has(option.value); });
}

function selectGroupValues(selectEl, names, availableGroups = gGroups) {
  if (!selectEl) return;
  const selected = [...new Set((names || []).map(name => String(name || '').trim()).filter(Boolean))];
  const known = new Set((availableGroups || []).map(group => String(group.name || '')));
  selected.filter(name => !known.has(name)).forEach(name => {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = `⚠️ ${name} (nhóm không còn tồn tại)`;
    option.dataset.missing = 'true';
    selectEl.appendChild(option);
  });
  Array.from(selectEl.options).forEach(option => { option.selected = selected.includes(option.value); });
}

function switchTab(tabId, btnEl) {
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  const target = document.getElementById(tabId);
  if (target) target.classList.add('active');
  if (btnEl) btnEl.classList.add('active');
}

function switchSubTab(subtab) {
  const scanBtn = document.getElementById('btn-subtab-scan');
  const bulkBtn = document.getElementById('btn-subtab-bulk');
  const scanPane = document.getElementById('subtab-pane-scan');
  const bulkPane = document.getElementById('subtab-pane-bulk');
  if (subtab === 'scan') {
    if (scanBtn) scanBtn.classList.add('active');
    if (bulkBtn) bulkBtn.classList.remove('active');
    if (scanPane) scanPane.classList.add('active');
    if (bulkPane) bulkPane.classList.remove('active');
  } else {
    if (bulkBtn) bulkBtn.classList.add('active');
    if (scanBtn) scanBtn.classList.remove('active');
    if (bulkPane) bulkPane.classList.add('active');
    if (scanPane) scanPane.classList.remove('active');
  }
}

function toggleAdvScan() {
  const panel = document.getElementById('adv-scan-panel');
  const arrow = document.getElementById('adv-arrow');
  if (!panel) return;
  panel.classList.toggle('open');
  if (arrow) {
    arrow.textContent = panel.classList.contains('open') ? '▲' : '▼';
  }
}

function toggleSidebar() {
  const grid = document.querySelector('.workspace-grid');
  const btn = document.getElementById('btn-toggle-sidebar');
  if (!grid) return;
  grid.classList.toggle('collapsed-sidebar');
  const isCollapsed = grid.classList.contains('collapsed-sidebar');
  if (btn) {
    btn.innerHTML = isCollapsed ? '▶ Mở Rộng Cột Trái' : '◀ Thu Gọn Cột Trái';
  }
}

function filterQueueTable(query) {
  const q = (query || '').toLowerCase().trim();
  const rows = document.querySelectorAll('#queue-tbody tr');
  rows.forEach(r => {
    if (!q) {
      r.style.display = '';
      return;
    }
    const text = r.textContent.toLowerCase();
    r.style.display = text.includes(q) ? '' : 'none';
  });
  updateQueueSelectionSummary();
}

function onDragOverVideo(e) {
  e.preventDefault();
  e.stopPropagation();
  const dz = document.getElementById('upload-dropzone');
  if (dz) dz.classList.add('dragover');
}

function onDragLeaveVideo(e) {
  e.preventDefault();
  e.stopPropagation();
  const dz = document.getElementById('upload-dropzone');
  if (dz) dz.classList.remove('dragover');
}

function onDropVideo(e) {
  e.preventDefault();
  e.stopPropagation();
  const dz = document.getElementById('upload-dropzone');
  if (dz) dz.classList.remove('dragover');
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    uploadVideoFiles(e.dataTransfer.files);
  }
}

function handleBrowserFiles(files) {
  if (files && files.length > 0) {
    uploadVideoFiles(files);
  }
}

async function uploadVideoFiles(files) {
  if (!files || !files.length) return;
  const proj = document.getElementById('sel-project').value || 'Default';
  const uploadTo = document.getElementById('scan-upload-to').value || 'Shorts';
  const renderMode = document.getElementById('scan-render-mode').value || 'shorts_9_16';
  const res = document.getElementById('scan-resolution') ? document.getElementById('scan-resolution').value : '1080p';
  const vis = document.getElementById('scan-visibility') ? document.getElementById('scan-visibility').value : 'public';
  const scheduleTime = vis === 'schedule' && document.getElementById('scan-sched-time')
    ? document.getElementById('scan-sched-time').value : '';
  const scheduleInterval = document.getElementById('scan-sched-interval')
    ? parseInt(document.getElementById('scan-sched-interval').value || '60', 10) : 60;
  const isPremiere = vis === 'schedule' && document.getElementById('scan-is-premiere')?.checked ? 1 : 0;
  const madeForKids = document.getElementById('scan-made-for-kids')
    ? parseInt(document.getElementById('scan-made-for-kids').value || '0', 10)
    : 0;
  const ageRestriction = document.getElementById('scan-age-restriction')
    ? parseInt(document.getElementById('scan-age-restriction').value || '0', 10)
    : 0;
  const alteredContent = document.getElementById('scan-altered-content')
    ? parseInt(document.getElementById('scan-altered-content').value || '0', 10)
    : 0;
  const emulateMobile = document.getElementById('scan-mobile')
    ? (document.getElementById('scan-mobile').checked ? 1 : 0)
    : 0;

  if (madeForKids && ageRestriction) {
    alert('Không thể vừa chọn "Dành cho trẻ em" vừa đặt "Giới hạn trên 18 tuổi". Hãy chọn lại đối tượng người xem.');
    return;
  }
  if (vis === 'schedule' && (!scheduleTime || new Date(scheduleTime) <= new Date())) {
    alert('Thời gian lên lịch phải được nhập và ở tương lai.');
    return;
  }

  const statusBox = document.getElementById('upload-status-box');
  const statusTitle = document.getElementById('upload-status-title');
  const statusPct = document.getElementById('upload-status-pct');
  const progBar = document.getElementById('upload-prog-bar');

  if (statusBox) statusBox.style.display = 'block';
  if (statusTitle) statusTitle.textContent = `Đang tải lên ${files.length} video vào dự án [${proj}]...`;
  if (statusPct) statusPct.textContent = '0%';
  if (progBar) progBar.style.width = '0%';

  const formData = new FormData();
  formData.append('project_name', proj);
  formData.append('upload_to', uploadTo);
  formData.append('render_mode', renderMode);
  formData.append('resolution', res);
  formData.append('visibility', vis);
  formData.append('schedule_time', scheduleTime);
  formData.append('schedule_interval_minutes', String(scheduleInterval));
  formData.append('is_premiere', String(isPremiere));
  formData.append('target_group_names', JSON.stringify(selectedMultiValues(document.getElementById('scan-group'))));
  formData.append('made_for_kids', String(madeForKids));
  formData.append('age_restriction', String(ageRestriction));
  formData.append('altered_content', String(alteredContent));
  formData.append('emulate_mobile', String(emulateMobile));

  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }

  let token;
  try {
    token = await ensureApiToken(true);
  } catch (e) {
    alert('Không lấy được API token cục bộ: ' + e.message);
    if (statusBox) statusBox.style.display = 'none';
    return;
  }

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload-video-files', true);
  xhr.setRequestHeader('X-DHB-Token', token);

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      if (statusPct) statusPct.textContent = `${pct}%`;
      if (progBar) progBar.style.width = `${pct}%`;
    }
  };

  xhr.onload = async () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const resp = JSON.parse(xhr.responseText || '{}');
      if (statusTitle) statusTitle.textContent = `✅ Đã nạp thành công ${resp.added} video!`;
      if (statusPct) statusPct.textContent = '100%';
      if (progBar) progBar.style.width = '100%';
      setTimeout(() => {
        if (statusBox) statusBox.style.display = 'none';
      }, 2500);
      const fileInput = document.getElementById('file-upload-input');
      if (fileInput) fileInput.value = '';
      await loadQueue();
    } else {
      alert('Lỗi tải file lên: ' + xhr.responseText);
      if (statusBox) statusBox.style.display = 'none';
    }
  };

  xhr.onerror = () => {
    alert('Lỗi kết nối khi tải file lên máy chủ!');
    if (statusBox) statusBox.style.display = 'none';
  };

  xhr.send(formData);
}

async function browseNativeFiles() {
  const proj = document.getElementById('sel-project').value || 'Default';
  const uploadTo = document.getElementById('scan-upload-to').value || 'Shorts';
  const renderMode = document.getElementById('scan-render-mode').value || 'shorts_9_16';
  const res = document.getElementById('scan-resolution') ? document.getElementById('scan-resolution').value : '1080p';
  const vis = document.getElementById('scan-visibility') ? document.getElementById('scan-visibility').value : 'public';
  const scheduleTime = vis === 'schedule' && document.getElementById('scan-sched-time')
    ? document.getElementById('scan-sched-time').value : '';
  const scheduleInterval = document.getElementById('scan-sched-interval')
    ? parseInt(document.getElementById('scan-sched-interval').value || '60', 10) : 60;
  const isPremiere = vis === 'schedule' && document.getElementById('scan-is-premiere')?.checked ? 1 : 0;

  try {
    const r = await apiFetch('/api/browse-native-files', { method: 'POST' });
    if (!r.files || !r.files.length) return;

    const addRes = await apiFetch('/api/add-local-paths', {
      method: 'POST',
      body: JSON.stringify({
        paths: r.files,
        project_name: proj,
        upload_to: uploadTo,
        render_mode: renderMode,
        resolution: res,
        visibility: vis,
        schedule_time: scheduleTime,
        schedule_interval_minutes: scheduleInterval,
        is_premiere: isPremiere,
        made_for_kids: parseInt(document.getElementById('scan-made-for-kids')?.value || '0', 10),
        age_restriction: parseInt(document.getElementById('scan-age-restriction')?.value || '0', 10),
        altered_content: parseInt(document.getElementById('scan-altered-content')?.value || '0', 10),
        target_group_names: selectedMultiValues(document.getElementById('scan-group')),
      }),
    });

    if (!addRes.added && addRes.skipped) {
      alert(`ℹ️ ${addRes.skipped} video đã có trong dự án [${proj}] — không thêm trùng.`);
    } else {
      const skipNote = addRes.skipped ? ` (bỏ qua ${addRes.skipped} video đã có)` : '';
      alert(`✅ Đã thêm trực tiếp ${addRes.added} video từ máy tính vào dự án [${proj}]!${skipNote}`);
    }
    await loadQueue();
  } catch (e) {
    alert('Lỗi chọn file: ' + e.message);
  }
}

async function browseNativeFolder() {
  try {
    const r = await apiFetch('/api/browse-native-folder', { method: 'POST' });
    if (!r.folder) return;

    const scanEl = document.getElementById('scan-urls');
    if (scanEl) {
      scanEl.value = (scanEl.value.trim() ? scanEl.value.trim() + '\n' : '') + r.folder;
    }
    await scanAndAddVideos();
  } catch (e) {
    alert('Lỗi chọn thư mục: ' + e.message);
  }
}

// ===== KẾT NỐI THƯ MỤC MÁY TÍNH (EXPLORER TRONG TOOL) =====
function openBrowseFolderModal() {
  document.getElementById('modal-browse-folder').style.display = 'flex';
  bfNavigate('');
}

function closeBrowseFolderModal() {
  document.getElementById('modal-browse-folder').style.display = 'none';
}

async function bfNavigate(path) {
  const tbody = document.getElementById('bf-tbody');
  const sumEl = document.getElementById('bf-summary');
  if (tbody) tbody.innerHTML = `<tr><td style="padding:14px; text-align:center; color:var(--text-muted);">⏳ Đang đọc thư mục...</td></tr>`;
  try {
    const wantRecursiveCount = path ? 0 : 0; // đếm đệ quy chỉ khi bấm nút "Thêm cả thư mục con"
    const r = await apiFetch(`/api/browse-disk?path=${encodeURIComponent(path || '')}&recursive_count=0`);

    const driveSel = document.getElementById('bf-drive');
    if (driveSel) {
      const cur = (r.current || '').slice(0, 2).toUpperCase() + '\\';
      driveSel.innerHTML = (r.drives || []).map(d => `<option value="${escapeHtml(d)}" ${d.toUpperCase() === cur ? 'selected' : ''}>💾 ${escapeHtml(d)}</option>`).join('');
    }
    document.getElementById('bf-path').value = r.current || '';

    let rows = '';
    if (r.parent) {
      rows += `<tr style="cursor:pointer;" onclick="bfNavigate(${escapeHtml(JSON.stringify(r.parent))})">
        <td style="padding:7px 12px; color:#38bdf8;">⬆️ .. (Thư mục cha)</td><td></td>
      </tr>`;
    }
    for (const d of (r.dirs || [])) {
      rows += `<tr>
        <td style="padding:7px 12px; cursor:pointer;" onclick="bfNavigate(${escapeHtml(JSON.stringify(d.path))})">📁 <b>${escapeHtml(d.name)}</b></td>
        <td style="width:60px; text-align:right; padding-right:10px;">
          <button class="btn btn-success btn-sm" onclick="event.stopPropagation(); bfAddPath(${escapeHtml(JSON.stringify(d.path))}, false)" title="Lấy video trong thư mục này">➕</button>
        </td>
      </tr>`;
    }
    for (const v of (r.videos || [])) {
      rows += `<tr>
        <td style="padding:6px 12px; padding-left:28px;">🎬 ${escapeHtml(v.name)} <span style="color:var(--text-muted); font-size:11px;">(${v.size_mb} MB)</span></td>
        <td></td>
      </tr>`;
    }
    if (!rows) {
      rows = `<tr><td style="padding:14px; text-align:center; color:var(--text-muted);">Thư mục trống (không có thư mục con / video).</td></tr>`;
    }
    if (tbody) tbody.innerHTML = rows;

    if (sumEl) {
      sumEl.innerHTML =
        `📍 <b>${escapeHtml(r.current)}</b><br>` +
        `🎬 <b>${(r.videos || []).length}</b> video trực tiếp tại đây` +
        ` · Bấm "🌳 Thêm Cả Thư Mục Con" để quét đệ quy toàn bộ cây bên trong.`;
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = `<tr><td style="padding:14px; color:#f87171;">Lỗi đọc thư mục: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function bfGetRecursiveCount(path) {
  try {
    const r = await apiFetch(`/api/browse-disk?path=${encodeURIComponent(path)}&recursive_count=1`);
    return r.recursive_video_count || 0;
  } catch (e) {
    return 0;
  }
}

async function bfAddCurrent(recursive) {
  const path = document.getElementById('bf-path').value;
  if (!path) return;
  if (recursive) {
    const n = await bfGetRecursiveCount(path);
    if (!n) {
      alert('Không tìm thấy video nào trong cây thư mục này (kể cả thư mục con).');
      return;
    }
    if (!confirm(`Quét đệ quy toàn bộ cây thư mục:\n${path}\n\nPhát hiện ~${n} video. Thêm tất cả vào hàng đợi?`)) return;
  }
  await bfAddPath(path, recursive);
}

async function bfAddPath(path, recursive) {
  const proj = document.getElementById('sel-project').value || 'Default';
  const payload = {
    paths: [path],
    project_name: proj,
    upload_to: document.getElementById('scan-upload-to').value || 'Shorts',
    render_mode: document.getElementById('scan-render-mode').value || 'shorts_9_16',
    resolution: document.getElementById('scan-resolution') ? document.getElementById('scan-resolution').value : '1080p',
    visibility: document.getElementById('scan-visibility') ? document.getElementById('scan-visibility').value : 'public',
    schedule_time: document.getElementById('scan-visibility')?.value === 'schedule'
      ? (document.getElementById('scan-sched-time')?.value || '') : '',
    schedule_interval_minutes: parseInt(document.getElementById('scan-sched-interval')?.value || '60', 10),
    is_premiere: document.getElementById('scan-visibility')?.value === 'schedule' && document.getElementById('scan-is-premiere')?.checked ? 1 : 0,
    made_for_kids: parseInt(document.getElementById('scan-made-for-kids')?.value || '0', 10),
    age_restriction: parseInt(document.getElementById('scan-age-restriction')?.value || '0', 10),
    altered_content: parseInt(document.getElementById('scan-altered-content')?.value || '0', 10),
    target_group_names: selectedMultiValues(document.getElementById('scan-group')),
    recursive: !!recursive,
  };
  try {
    const r = await apiFetch('/api/add-local-paths', { method: 'POST', body: JSON.stringify(payload) });
    if (r.added > 0) {
      const skipNote = r.skipped ? `, bỏ qua ${r.skipped} video đã có` : '';
      showToast(`➕ Đã thêm ${r.added} video từ thư mục vào dự án [${proj}]${skipNote}!`, 'info');
      await loadQueue();
      await loadOverview();
    } else if (r.skipped > 0) {
      alert(`ℹ️ ${r.skipped} video trong thư mục đã có trong dự án [${proj}] — không thêm trùng.`);
    } else {
      alert('Không tìm thấy file video nào trong thư mục này.');
    }
  } catch (e) {
    alert('Lỗi thêm thư mục: ' + e.message);
  }
}

function resetApiToken() {
  gApiToken = '';
  gApiTokenPromise = null;
}

async function ensureApiToken(force = false) {
  if (force) resetApiToken();
  if (gApiToken) return gApiToken;
  if (!gApiTokenPromise) {
    gApiTokenPromise = (async () => {
      const res = await fetch('/api/auth/token', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Không lấy được API token cục bộ (HTTP ${res.status}).`);
      const data = await res.json();
      if (!data.token) throw new Error('API token cục bộ rỗng.');
      gApiToken = data.token;
      return gApiToken;
    })().catch((err) => {
      gApiTokenPromise = null;
      throw err;
    });
  }
  return gApiTokenPromise;
}

async function apiFetch(url, options = {}) {
  const doFetch = async () => {
    const token = await ensureApiToken();
    const headers = new Headers(options.headers || {});
    headers.set('X-DHB-Token', token);
    if (!(options.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    return fetch(url, { ...options, headers });
  };
  const safeFetch = async () => {
    try { return await doFetch(); }
    catch (cause) {
      const error = new Error('Không kết nối được API cục bộ. Kiểm tra server rồi thử lại.');
      error.cause = cause;
      throw error;
    }
  };
  let res = await safeFetch();
  if (res.status === 401) {
    // Token đổi theo tiến trình server: tab đang mở phải tự lấy token mới thay vì Ctrl+F5.
    resetApiToken();
    res = await safeFetch();
  }
  if (!res.ok) {
    let errText = await res.text();
    try {
      const j = JSON.parse(errText);
      errText = j.detail || j.message || errText;
    } catch (_) {}
    const error = new Error(`HTTP ${res.status}: ${errText}`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}

async function downloadWithToken(url, fallbackName) {
  try {
    const doFetch = async () => {
      const token = await ensureApiToken();
      return fetch(url, { headers: { 'X-DHB-Token': token } });
    };
    const safeFetch = async () => {
      try { return await doFetch(); }
      catch (cause) {
        const error = new Error('Không kết nối được API cục bộ. Kiểm tra server rồi thử lại.');
        error.cause = cause;
        throw error;
      }
    };
    let res = await safeFetch();
    if (res.status === 401) {
      resetApiToken();
      res = await safeFetch();
    }
    if (!res.ok) {
      let errText = await res.text();
      try {
        const j = JSON.parse(errText);
        errText = j.detail || j.message || errText;
      } catch (_) {}
      const error = new Error(`HTTP ${res.status}: ${errText}`);
      error.status = res.status;
      throw error;
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/i);
    const link = document.createElement('a');
    const objectUrl = URL.createObjectURL(blob);
    link.href = objectUrl;
    link.download = match ? match[1] : fallbackName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 10000);
  } catch (e) {
    alert('Lỗi xuất file: ' + e.message);
  }
}

async function refreshLivePanels() {
  if (document.hidden || gLiveRefreshInFlight) return;
  gLiveRefreshInFlight = true;
  try {
    const results = await Promise.allSettled([
      loadOverview(), loadQueue(true), loadMonitors(), loadLogs(),
    ]);
    const failed = results.filter(result => result.status === 'rejected').length;
    if (failed) console.warn(`${failed} phần của bảng điều khiển chưa cập nhật được.`);
  } catch (e) {
    console.warn('Không cập nhật được bảng điều khiển:', e);
  } finally {
    gLiveRefreshInFlight = false;
  }
}

async function initApp() {
  try {
    await ensureApiToken();
  } catch (e) {
    showToast('Không kết nối được API cục bộ; sẽ thử lại sau vài giây.', 'warning');
    if (!gApiRetryTimer) {
      gApiRetryTimer = setTimeout(() => {
        gApiRetryTimer = null;
        initApp();
      }, 5000);
    }
    return;
  }
  const sections = [
    loadOverview(), loadProjects(), loadGroups(), loadProfiles(), loadChannels(),
    loadOptions(), loadRenderResources(), loadRenderPresets(), loadMonitors(),
    loadAppSettings(), loadLogs(), loadLogModules(),
  ];
  const results = await Promise.allSettled(sections);
  const followup = await Promise.allSettled([loadDiagnostics(), loadQueue()]);
  const failed = results.concat(followup).filter(result => result.status === 'rejected').length;
  if (failed) showToast(`${failed} phần không tải được; các phần còn lại vẫn hoạt động.`, 'warning');

  // Polling tự động mỗi 2.5 giây; tạm dừng khi tab ẩn để không tải DB/log vô ích
  if (!gLiveRefreshTimer) gLiveRefreshTimer = setInterval(refreshLivePanels, 2500);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshLivePanels();
  });
}

async function loadOverview() {
  try {
    const data = await apiFetch('/api/overview');
    gPipelineRunning = !!data.pipeline_running;
    document.getElementById('st-dhb').textContent = data.dhb_connected ? 'Đã kết nối ổ C' : 'Chưa kết nối';
    document.getElementById('st-channels').textContent = data.channels_count;
    document.getElementById('st-videos').textContent = data.total_videos;
    document.getElementById('st-completed').textContent = data.completed_videos;
    document.getElementById('st-monitors').textContent = data.monitors_running;
  } catch (e) {}
}

async function syncFromDHB() {
  try {
    const res = await apiFetch('/api/sync-dhb', { method: 'POST' });
    alert(`Đã cập nhật cài đặt API và nạp ${res.stats.profiles} Profile Chrome từ máy tính.\n(Đã tự động loại trừ các dự án Facebook).`);
    await initApp();
  } catch (e) {
    alert('Lỗi nạp cài đặt: ' + e.message);
  }
}

async function loadProjects() {
  const projects = await apiFetch('/api/projects');
  const sel = document.getElementById('sel-project');
  const current = sel.value || (projects[0] ? projects[0].name : 'Dự án YouTube #1');
  sel.innerHTML = projects
    .map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} (${p.video_count} video)</option>`)
    .join('');
  if ([...sel.options].some(o => o.value === current)) {
    sel.value = current;
  }
}

async function createNewProject() {
  const name = prompt('Nhập tên Dự Án mới:');
  if (!name || !name.trim()) return;
  await apiFetch('/api/projects', {
    method: 'POST',
    body: JSON.stringify({ name: name.trim() }),
  });
  await loadProjects();
  document.getElementById('sel-project').value = name.trim();
  await loadQueue();
}

async function deleteCurrentProject() {
  const sel = document.getElementById('sel-project');
  if (!sel.value) return;
  if (sel.options.length <= 1) {
    alert('Cần giữ lại ít nhất 1 dự án để làm việc.');
    return;
  }
  if (!confirm(`Bạn có chắc muốn xóa dự án "${sel.value}" và toàn bộ video trong dự án này?`)) return;
  try {
    await apiFetch(`/api/projects/${encodeURIComponent(sel.value)}`, { method: 'DELETE' });
    await loadProjects();
    await loadQueue();
  } catch (e) {
    alert(e.message);
  }
}

async function loadGroups() {
  gGroups = await apiFetch('/api/groups');
  setGroupSelectOptions(document.getElementById('scan-group'), gGroups, true);
  setGroupSelectOptions(document.getElementById('ch-group'), gGroups, true);
  setGroupSelectOptions(document.getElementById('ec-group'), gGroups, false);
  setGroupSelectOptions(document.getElementById('ed-vid-groups'), gGroups, false);
  const monGrp = document.getElementById('mon-group');
  if (monGrp) {
    const prior = monGrp.value;
    monGrp.innerHTML = gGroups.map(g => `<option value="${escapeHtml(g.name)}">${escapeHtml(g.name)}</option>`).join('');
    if (gGroups.some(g => g.name === prior)) monGrp.value = prior;
  }
  const bulkGrp = document.getElementById('bulk-group');
  if (bulkGrp) setGroupSelectOptions(bulkGrp, gGroups, false);
}

async function loadProfiles() {
  try {
    gProfiles = await apiFetch('/api/profiles');
    const profOpts = gProfiles.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} (${escapeHtml(p.source || 'DHB')})</option>`).join('');

    const bulkProf = document.getElementById('bulk-profile');
    if (bulkProf) bulkProf.innerHTML = `<option value="">-- Giữ nguyên Profile --</option>` + profOpts;

    const edProf = document.getElementById('ed-vid-profile');
    if (edProf) edProf.innerHTML = `<option value="">-- Mặc định (Tự chọn) --</option>` + profOpts;

    const filterProf = document.getElementById('filter-channel-profile');
    if (filterProf) filterProf.innerHTML = `<option value="">-- Tất cả Profile --</option>` + profOpts;

    const chProfSelect = document.getElementById('ch-prof-select');
    if (chProfSelect) chProfSelect.innerHTML = `<option value="">-- Tự nhập tên profile mới --</option>` + profOpts;

    renderProfilesTable();
    refreshAntidetectStatus();
  } catch (e) {
    console.error("Lỗi loadProfiles:", e);
  }
}

async function refreshAntidetectStatus() {
  const el = document.getElementById('antidetect-status');
  if (!el) return;
  try {
    const st = await apiFetch('/api/antidetect/status');
    if (st.connected) {
      el.innerHTML = `<span style="color:#34d399;">🟢 Antidetect: ${Number(st.profile_count || 0)} profile</span>`;
      el.title = st.base_url || '';
    } else {
      el.innerHTML = '<span style="color:#fb7185;">🔴 Antidetect: chưa kết nối</span>';
      el.title = st.error || '';
    }
  } catch (e) {
    el.innerHTML = '<span style="color:#fb7185;">🔴 Antidetect: chưa kết nối</span>';
    el.title = e.message;
  }
}

async function syncAntidetectProfiles() {
  showToast('🔗 Đang đồng bộ Profile từ Antidetect...', 'info');
  try {
    const res = await apiFetch('/api/antidetect/sync', { method: 'POST' });
    let msg = `✅ Đồng bộ Antidetect xong:\n- Thêm mới: ${res.added}\n- Cập nhật: ${res.updated}\n- Bỏ qua: ${res.skipped}`;
    if (res.missing && res.missing.length) {
      msg += `\n\n⚠️ ${res.missing.length} Profile đã đồng bộ trước đây không còn trong Antidetect:\n${res.missing.join(', ')}`;
    }
    alert(msg);
    await loadProfiles();
    await refreshAntidetectStatus();
  } catch (e) {
    alert('Lỗi đồng bộ Antidetect: ' + e.message);
  }
}

async function syncAntidetectChannels() {
  // Đồng bộ Antidetect với YouTube Studio: quét từng profile để đọc đăng nhập + kênh.
  const NL_CHAR = String.fromCharCode(10);
  try {
    const st = await apiFetch('/api/antidetect/status');
    if (!st.connected) {
      alert(`Chưa kết nối được Antidetect Browser Manager. Hãy mở Antidetect trước rồi thử lại.

${st.error || ''}`);
      return;
    }
  } catch (e) {
    alert('Chưa kết nối được Antidetect Browser Manager: ' + e.message);
    return;
  }
  const ok = confirm(`Quét Kênh YouTube Studio từ các Profile Antidetect?

- Mỗi profile mất khoảng 15-30 giây (mở trình duyệt qua Antidetect → đọc Studio → đóng tab do tool tạo).
- Kênh tìm được sẽ về tab Quản Lý Kênh với Channel ID đầy đủ, sẵn sàng để đăng.
- Theo dõi tiến độ chi tiết ở tab Nhật Ký.`);
  if (!ok) return;
  showToast('🧲 Đang quét kênh Studio từ các Profile Antidetect...', 'info');
  try {
    const res = await apiFetch('/api/antidetect/sync', {
      method: 'POST',
      body: JSON.stringify({ scan_channels: true }),
    });
    const s = res.studio || { scanned: 0, logged_in: 0, channels_added: 0, channels_updated: 0, profiles: [] };
    const lines = (s.profiles || []).map(p => {
      if (p.error) return `• ${p.name}: ⚠️ ${p.error}`;
      if (!p.logged_in) return `• ${p.name}: chưa đăng nhập Google (mở profile để đăng nhập rồi quét lại)`;
      return `• ${p.name}: ${p.channels} kênh (+${p.added} mới, ${p.updated} cập nhật)`;
    });
    alert(`🧲 Quét Studio xong:

- Profile đã quét: ${s.scanned}
- Đã đăng nhập: ${s.logged_in}
- Kênh mới: ${s.channels_added}
- Kênh cập nhật: ${s.channels_updated}

${lines.join(NL_CHAR) || '(không có profile Antidetect nào)'}`);
    await loadProfiles();
    await loadChannels();
    await refreshAntidetectStatus();
  } catch (e) {
    alert('Lỗi quét kênh Studio: ' + e.message);
  }
}

function escapeJs(str) {
  return String(str ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '&quot;');
}

function renderProfilesTable() {
  const tbody = document.getElementById('profiles-tbody');
  if (!tbody) return;
  if (!gProfiles.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:18px; color:var(--text-muted)">Chưa có Profile Chrome nào. Hãy thêm kênh ở trên hoặc bấm "🔄 Nạp Cài Đặt & Profile" ở góc trên bên phải.</td></tr>`;
    return;
  }
  tbody.innerHTML = gProfiles.map(p => {
    const st = p.login_status || '';
    let stBadge = '<span class="badge badge-wait">Chưa kiểm tra</span>';
    if (st === 'Đã đăng nhập') stBadge = '<span class="badge badge-ok">✅ Đã đăng nhập</span>';
    else if (st === 'Chưa đăng nhập') stBadge = '<span class="badge badge-err">❌ Chưa đăng nhập</span>';

    const isAd = (p.source === 'Antidetect') || !!p.antidetect_profile_id;
    const sourceLabel = p.source || (isAd ? 'Antidetect' : 'DHB');
    const sourceClass = sourceLabel === 'Antidetect' ? 'badge-purple' : (sourceLabel === 'DHB' ? 'badge-video' : 'badge-shorts');
    const pathLine = isAd
      ? `ID: ${escapeHtml(p.antidetect_profile_id || '')}`
      : escapeHtml(p.path || '');
    const proxyCell = isAd
      ? (p.proxy
          ? `<span style="color:#38bdf8;">${escapeHtml(p.proxy)}</span><div style="font-size:10px; color:var(--text-muted);">Antidetect quản lý</div>`
          : '<span style="color:var(--text-muted);">Theo Antidetect</span>')
      : (p.proxy ? escapeHtml(p.proxy) : '<span style="color:var(--text-muted)">Trực tiếp</span>');
    const proxyBtn = isAd
      ? '<button class="btn btn-outline btn-sm" disabled title="Proxy do Antidetect quản lý — sửa trong Antidetect">🔒</button>'
      : `<button class="btn btn-outline btn-sm" onclick="profileEditProxy('${escapeJs(p.name)}')" title="Sửa Proxy của Profile">⚙️</button>`;

    return `
      <tr>
        <td><b>${escapeHtml(p.name)}</b><div style="font-size:10.5px; color:var(--text-muted);">${pathLine}</div></td>
        <td><span class="badge ${sourceClass}">${escapeHtml(sourceLabel)}</span></td>
        <td style="font-size:11px; color:#38bdf8;">${proxyCell}</td>
        <td>${p.channel_count || 0}</td>
        <td>${stBadge}${p.last_login_check ? `<div style="font-size:10px; color:var(--text-muted)">${escapeHtml(p.last_login_check)}</div>` : ''}</td>
        <td>
          <button class="btn btn-success btn-sm" onclick="profileOpenBrowser('${escapeJs(p.name)}')" title="Mở Chrome để đăng nhập Google thủ công">🌐 Mở Đăng Nhập</button>
          <button class="btn btn-accent btn-sm" onclick="profileCheckLogin('${escapeJs(p.name)}')" title="Tự kiểm tra phiên đăng nhập Google của Profile">🔍 Kiểm Tra</button>
          <button class="btn btn-purple btn-sm" onclick="profileRefreshChannels('${escapeJs(p.name)}')" title="Quét danh sách kênh YouTube thật trong Profile">🔄 Quét Kênh</button>
          ${proxyBtn}
          <button class="btn btn-outline btn-sm" style="color:#f87171; border-color:rgba(248,113,113,0.3);" onclick="profileDelete('${escapeJs(p.name)}')" title="Xóa Profile khỏi hệ thống (không xóa thư mục)">🗑️</button>
        </td>
      </tr>
    `;
  }).join('');
}

async function profileOpenBrowser(name) {
  try {
    showToast('🌐 Đang mở Profile để đăng nhập...', 'info');
    const res = await apiFetch(`/api/profiles/${encodeURIComponent(name)}/open-browser`, { method: 'POST' });
    if (res.antidetect) {
      alert(res.message);
    } else {
      alert(res.message + '\n\nHãy đăng nhập tài khoản Google/YouTube trên cửa sổ Chrome vừa mở (kèm Proxy nếu có), sau đó đóng lại và bấm "🔍 Kiểm Tra".');
    }
  } catch (e) {
    alert('Lỗi mở trình duyệt: ' + e.message);
  }
}

async function profileCheckLogin(name) {
  showToast(`🔍 Đang kiểm tra đăng nhập Profile "${name}" (~10-20 giây)...`, 'info');
  try {
    const res = await apiFetch(`/api/profiles/${encodeURIComponent(name)}/check-login`, { method: 'POST' });
    if (res.logged_in) {
      const nCh = (res.channels || []).length;
      alert(
        `✅ Profile "${name}" ĐÃ ĐĂNG NHẬP Google.` +
        (res.email ? `\n📧 Tài khoản: ${res.email}` : '') +
        (nCh ? `\n📺 Phát hiện ${nCh} kênh YouTube — bấm "🔄 Quét Kênh" để cập nhật vào hệ thống.` : '')
      );
    } else {
      alert(`❌ Profile "${name}" CHƯA đăng nhập Google.\n\nHãy bấm "🌐 Mở Đăng Nhập" để đăng nhập trước khi chạy tự động.`);
    }
    await loadProfiles();
    await loadChannels();
  } catch (e) {
    alert('Lỗi kiểm tra đăng nhập: ' + e.message);
  }
}

async function profileRefreshChannels(name) {
  showToast(`🔄 Đang quét kênh YouTube trong Profile "${name}"...`, 'info');
  try {
    const res = await apiFetch(`/api/profiles/${encodeURIComponent(name)}/refresh-channels`, { method: 'POST' });
    if (!res.logged_in) {
      alert(`❌ Profile "${name}" chưa đăng nhập Google nên không quét được kênh.\nHãy bấm "🌐 Mở Đăng Nhập" trước.`);
    } else {
      alert(
        `✅ Quét xong Profile "${name}":\n` +
        `- Thêm mới: ${res.added} kênh\n` +
        `- Cập nhật: ${res.updated} kênh` +
        (res.email ? `\n- Tài khoản Google: ${res.email}` : '')
      );
    }
    await loadProfiles();
    await loadChannels();
    await loadOverview();
  } catch (e) {
    alert('Lỗi quét kênh: ' + e.message);
  }
}

async function profileEditProxy(name) {
  const p = gProfiles.find(x => x.name === name);
  if (p && (p.source === 'Antidetect' || p.antidetect_profile_id)) {
    alert('Proxy của Profile này do Antidetect quản lý. Hãy sửa proxy trong Antidetect rồi bấm "🔗 Đồng Bộ Antidetect".');
    return;
  }
  const cur = p ? (p.proxy || '') : '';
  const proxy = prompt(
    `Proxy cho Profile "${name}"\nĐịnh dạng: IP:PORT hoặc IP:PORT:USER:PASS\nĐể trống = dùng mạng trực tiếp.\n(Lưu ý: cả đăng nhập lẫn đăng tự động sẽ đi qua Proxy này)`,
    cur
  );
  if (proxy === null) return;
  try {
    await apiFetch(`/api/profiles/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify({ proxy: proxy.trim() }),
    });
    showToast('⚙️ Đã cập nhật Proxy và đồng bộ cho các kênh thuộc Profile.', 'info');
    await loadProfiles();
  } catch (e) {
    alert('Lỗi cập nhật Proxy: ' + e.message);
  }
}

async function profileDelete(name) {
  if (!confirm(`Xóa Profile "${name}" khỏi hệ thống?\n(Chỉ xóa bản ghi trong phần mềm — KHÔNG xóa thư mục Chrome trên ổ đĩa)`)) return;
  try {
    await apiFetch(`/api/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
    showToast('🗑️ Đã xóa Profile khỏi hệ thống.', 'warning');
    await loadProfiles();
    await loadChannels();
  } catch (e) {
    alert('Lỗi xóa Profile: ' + e.message);
  }
}

async function checkChannelLogin(channelId) {
  const ch = gChannels.find(x => x.id === channelId);
  if (!ch) return;
  if (!ch.profile_name) {
    alert('Kênh này chưa gắn với Profile Chrome nào.');
    return;
  }
  await profileCheckLogin(ch.profile_name);
}

async function onBulkProfileChange() {
  const profName = document.getElementById('bulk-profile').value;
  const chSel = document.getElementById('bulk-channel');
  if (!profName) {
    chSel.innerHTML = `<option value="">-- Giữ nguyên kênh --</option>` +
      gChannels.map(c => `<option value="${c.id}">${escapeHtml(c.name)} (${escapeHtml(c.group_name)})</option>`).join('');
    return;
  }
  try {
    const subChannels = await apiFetch(`/api/profiles/${encodeURIComponent(profName)}/channels`);
    if (subChannels.length > 0) {
      chSel.innerHTML = `<option value="">-- Chọn kênh thuộc [${escapeHtml(profName)}] --</option>` +
        subChannels.map(c => `<option value="${c.id}">📺 ${escapeHtml(c.name)} (${escapeHtml(c.handle || '')})</option>`).join('');
    } else {
      chSel.innerHTML = `<option value="">(Chưa có kênh nào gán cho ${escapeHtml(profName)})</option>`;
    }
  } catch (e) {
    console.error("Lỗi onBulkProfileChange:", e);
  }
}

async function onModalProfileChange() {
  const profName = document.getElementById('ed-vid-profile').value;
  const chSel = document.getElementById('ed-vid-channel');
  if (!profName) {
    chSel.innerHTML = `<option value="">-- Theo nhóm mặc định --</option>` +
      gChannels.map(c => `<option value="${c.id}">${escapeHtml(c.name)} (${escapeHtml((c.group_names || [c.group_name]).filter(Boolean).join(', '))}</option>`).join('');
    return;
  }
  try {
    const subChannels = await apiFetch(`/api/profiles/${encodeURIComponent(profName)}/channels`);
    if (subChannels.length > 0) {
      chSel.innerHTML = `<option value="">-- Chọn kênh của [${escapeHtml(profName)}] --</option>` +
        subChannels.map(c => `<option value="${c.id}">📺 ${escapeHtml(c.name)} (${escapeHtml(c.handle || '')})</option>`).join('');
    } else {
      chSel.innerHTML = `<option value="">(Chưa có kênh nào gán cho ${escapeHtml(profName)})</option>`;
    }
  } catch (e) {
    console.error("Lỗi onModalProfileChange:", e);
  }
}

function _applyChannelProfileMode(found) {
  const isAd = !!(found && (found.source === 'Antidetect' || found.antidetect_profile_id));
  const modeEl = document.getElementById('ch-mode');
  const proxyEl = document.getElementById('ch-proxy');
  if (modeEl) {
    modeEl.disabled = isAd;
    if (isAd) modeEl.value = 'playwright';
    modeEl.title = isAd ? 'Profile Antidetect chỉ hỗ trợ đăng qua trình duyệt Playwright' : '';
  }
  if (proxyEl) {
    proxyEl.disabled = isAd;
    if (isAd) {
      proxyEl.value = '';
      proxyEl.placeholder = 'Theo Antidetect (tự quản lý proxy/phiên)';
    } else {
      proxyEl.placeholder = 'Để trống nếu dùng mạng trực tiếp';
    }
  }
  return isAd;
}

function onProfileNameTyped() {
  const name = (document.getElementById('ch-prof-name').value || '').trim();
  _applyChannelProfileMode(gProfiles.find(p => p.name === name));
}

function onSelectExistingProfile(profName) {
  const found = gProfiles.find(p => p.name === profName);
  _applyChannelProfileMode(found);
  if (!profName) return;
  document.getElementById('ch-prof-name').value = profName;
  if (found && found.path) {
    document.getElementById('ch-prof-path').value = found.path;
  }
  if (found && (found.source === 'Antidetect' || found.antidetect_profile_id)) {
    showToast('🔗 Profile Antidetect: tool sẽ dùng chung trình duyệt, proxy và fingerprint của Antidetect.', 'info');
  }
}

function filterChannelsByProfile(profName) {
  const filtered = profName ? gChannels.filter(c => c.profile_name === profName) : gChannels;
  renderChannelTable(filtered);
}

function renderChannelTable(list) {
  const tbody = document.getElementById('channels-tbody');
  if (!tbody) return;
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:18px; color:var(--text-muted)">Không có kênh YouTube nào hiển thị.</td></tr>`;
    return;
  }
  tbody.innerHTML = list.map(c => {
    let stBadge;
    if (c.status === 'Đã đăng nhập') {
      stBadge = '<span class="badge badge-ok" style="font-size:10px;">✅ Đăng nhập</span>';
    } else if (c.status === 'Chưa đăng nhập') {
      stBadge = '<span class="badge badge-err" style="font-size:10px;">❌ Chưa ĐN</span>';
    } else {
      stBadge = `<span class="badge badge-wait" style="font-size:10px;">${escapeHtml(c.status || 'Ready')}</span>`;
    }
    return `
      <tr>
        <td>#${c.id}</td>
        <td><b>${escapeHtml(c.name)}</b><br/><span style="color:var(--text-muted);font-size:11px">${escapeHtml(c.handle || '')}</span>${c.email ? `<br/><span style="color:var(--text-muted);font-size:10.5px">📧 ${escapeHtml(c.email)}</span>` : ''}${c.channel_id ? `<br/><span style="color:var(--text-muted);font-size:10.5px">🆔 ${escapeHtml(c.channel_id)}</span>` : '<br/><span class="badge badge-err" style="font-size:10px;" title="Bắt buộc có Channel ID để xác minh đúng kênh trước khi đăng">⚠️ Thiếu Channel ID</span>'}</td>
        <td><span class="badge badge-video">${escapeHtml((c.group_names || [c.group_name]).filter(Boolean).join(', '))}</span></td>
        <td>${c.upload_mode === 'youtube_api' ? '🔑 YouTube API v3' : '🌐 Playwright Studio'}</td>
        <td style="font-size:11.5px;color:#38bdf8;"><b>👤 ${escapeHtml(c.profile_name || 'Mặc định')}</b></td>
        <td>${stBadge}</td>
        <td>
          <button class="btn btn-accent btn-sm" onclick="openChannelBrowser(${c.id})" title="Mở Chrome để đăng nhập Google cho Profile này">🌐 Đăng Nhập</button>
          <button class="btn btn-outline btn-sm" onclick="checkChannelLogin(${c.id})" title="Kiểm tra phiên đăng nhập Google của Profile">🔍</button>
          <button class="btn btn-outline btn-sm" onclick="openChannelEdit(${c.id})" title="Sửa chi tiết kênh (tên, handle, Channel ID, profile, OAuth token...)">✏️</button>
          <button class="btn btn-outline btn-sm" onclick="deleteChannel(${c.id})" title="Xóa kênh khỏi hệ thống">🗑️</button>
        </td>
      </tr>
    `;
  }).join('');
}

async function loadChannels() {
  gChannels = await apiFetch('/api/channels');
  const allChOpts = gChannels.map(c => `<option value="${c.id}">${escapeHtml(c.name)} (${escapeHtml((c.group_names || [c.group_name]).filter(Boolean).join(', '))})${c.channel_id ? '' : ' ⚠️ thiếu Channel ID'}</option>`).join('');
  const chOpts = `<option value="">-- Chia đều theo Nhóm Kênh --</option>` + allChOpts;
  const bulkCh = document.getElementById('bulk-channel');
  if (bulkCh) bulkCh.innerHTML = `<option value="">-- Giữ nguyên kênh --</option>` + allChOpts;
  const monCh = document.getElementById('mon-channel');
  if (monCh) monCh.innerHTML = chOpts;

  const activeProfFilter = document.getElementById('filter-channel-profile') ? document.getElementById('filter-channel-profile').value : '';
  if (activeProfFilter) {
    filterChannelsByProfile(activeProfFilter);
  } else {
    renderChannelTable(gChannels);
  }
}

async function openChannelBrowser(channelId) {
  try {
    const res = await apiFetch(`/api/channels/${channelId}/open-browser`, { method: 'POST' });
    alert(res.message + '\n\nHãy đăng nhập tài khoản Google/YouTube Studio trên cửa sổ Chrome vừa mở, sau đó đóng trình duyệt lại trước khi bấm chạy tự động.');
  } catch (e) {
    alert('Lỗi mở trình duyệt: ' + e.message);
  }
}

async function addChannel() {
  const groupNames = selectedMultiValues(document.getElementById('ch-group'));
  const payload = {
    name: document.getElementById('ch-name').value,
    handle: document.getElementById('ch-handle').value,
    group_name: groupNames[0] || 'Mặc định',
    group_names: groupNames.length ? groupNames : ['Mặc định'],
    upload_mode: document.getElementById('ch-mode').value,
    profile_name: document.getElementById('ch-prof-name').value,
    profile_path: document.getElementById('ch-prof-path').value,
    proxy: document.getElementById('ch-proxy').value,
    default_category_id: document.getElementById('ch-cat').value,
    oauth_token_json: document.getElementById('ch-oauth').value,
    use_hidden_desktop: document.getElementById('ch-hidden').checked,
  };
  try {
    await apiFetch('/api/channels', { method: 'POST', body: JSON.stringify(payload) });
    document.getElementById('ch-name').value = '';
    await loadChannels();
    await loadOverview();
  } catch (e) {
    alert(e.message);
  }
}

async function deleteChannel(id) {
  if (!confirm('Xóa kênh YouTube này?')) return;
  await apiFetch(`/api/channels/${id}`, { method: 'DELETE' });
  await loadChannels();
}

// ==========================================================
// BỘ TÙY CHỌN ĐĂNG (AFFILIATE SHOPEE / PIN COMMENT / HASHTAGS)
// ==========================================================
async function loadOptions() {
  try {
    gOptions = (await apiFetch('/api/options')) || [];
  } catch (e) {
    console.error('Lỗi tải bộ tùy chọn đăng:', e);
    gOptions = [];
  }
  renderOptionsTable();

  const optsHtml = gOptions
    .map(o => `<option value="${o.id}">${escapeHtml(o.name)} (#${o.id})</option>`)
    .join('');
  const targets = [
    ['scan-option-group', '-- Không dùng bộ tùy chọn --'],
    ['mon-option-group', '-- Không dùng bộ tùy chọn --'],
    ['bulk-option-group', '-- Giữ nguyên --'],
    ['ed-vid-option-group', '-- Không dùng bộ tùy chọn --'],
  ];
  targets.forEach(([elId, emptyLabel]) => {
    const el = document.getElementById(elId);
    if (!el) return;
    const cur = el.value;
    el.innerHTML = `<option value="">${emptyLabel}</option>` + optsHtml;
    el.value = cur;
    if (el.value !== cur) el.value = '';
  });
}

function renderOptionsTable() {
  const tbody = document.getElementById('options-tbody');
  if (!tbody) return;
  if (!gOptions.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:18px; color:var(--text-muted)">Chưa có bộ tùy chọn đăng nào. Hãy tạo ở khung bên trái.</td></tr>`;
    return;
  }
  tbody.innerHTML = gOptions.map(o => `
    <tr>
      <td>#${o.id}</td>
      <td><b>${escapeHtml(o.name)}</b>${o.category_id ? `<div style="font-size:10.5px; color:var(--text-muted)">Danh mục ${escapeHtml(String(o.category_id))}</div>` : ''}</td>
      <td>${o.affiliate_enabled ? `<span class="badge badge-ok" style="font-size:10px;">✅ Bật</span><div style="font-size:10.5px; color:#38bdf8; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(o.affiliate_url || '')}</div>` : '<span style="color:var(--text-muted)">—</span>'}</td>
      <td>${o.pin_comment_enabled ? '<span class="badge badge-ok" style="font-size:10px;">✅ Ghim</span>' : '<span style="color:var(--text-muted)">—</span>'}</td>
      <td style="font-size:11px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(o.append_hashtags || '')}">${escapeHtml(o.append_hashtags || '')}</td>
      <td>
        <button class="btn btn-outline btn-sm" onclick="editOptionGroup(${o.id})" title="Sửa bộ tùy chọn">✏️</button>
        <button class="btn btn-outline btn-sm" style="color:#f87171; border-color:rgba(248,113,113,0.3);" onclick="deleteOptionGroup(${o.id})" title="Xóa bộ tùy chọn">🗑️</button>
      </td>
    </tr>
  `).join('');
}

function resetOptionForm() {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  set('opt-id', '');
  set('opt-name', '');
  const aff = document.getElementById('opt-affiliate-enabled');
  if (aff) aff.checked = false;
  set('opt-affiliate-url', '');
  set('opt-affiliate-cta', '👉 Mua ngay sản phẩm chính hãng tại đây:');
  const pin = document.getElementById('opt-pin-enabled');
  if (pin) pin.checked = false;
  set('opt-pin-text', '');
  set('opt-hashtags', '#shorts #trending #viral');
  set('opt-default-tags', '');
  set('opt-category', '22');
  set('opt-made-for-kids', '0');
  const ai = document.getElementById('opt-ai-label');
  if (ai) ai.checked = false;
}

function editOptionGroup(id) {
  const o = gOptions.find(x => x.id === id);
  if (!o) return;
  const set = (elId, val) => { const el = document.getElementById(elId); if (el) el.value = val ?? ''; };
  set('opt-id', o.id);
  set('opt-name', o.name || '');
  const aff = document.getElementById('opt-affiliate-enabled');
  if (aff) aff.checked = !!o.affiliate_enabled;
  set('opt-affiliate-url', o.affiliate_url || '');
  set('opt-affiliate-cta', o.affiliate_CTA || '👉 Mua ngay sản phẩm chính hãng tại đây:');
  const pin = document.getElementById('opt-pin-enabled');
  if (pin) pin.checked = !!o.pin_comment_enabled;
  set('opt-pin-text', o.pin_comment_text || '');
  set('opt-hashtags', o.append_hashtags || '');
  set('opt-default-tags', o.default_tags || '');
  set('opt-category', o.category_id || '22');
  set('opt-made-for-kids', o.made_for_kids ? '1' : '0');
  const ai = document.getElementById('opt-ai-label');
  if (ai) ai.checked = !!o.ai_label;
  const nameEl = document.getElementById('opt-name');
  if (nameEl) nameEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveOptionGroup() {
  const idRaw = document.getElementById('opt-id').value;
  const name = document.getElementById('opt-name').value.trim();
  if (!name) {
    alert('Vui lòng nhập tên bộ tùy chọn đăng.');
    return;
  }
  const payload = {
    name,
    affiliate_enabled: document.getElementById('opt-affiliate-enabled').checked,
    affiliate_url: document.getElementById('opt-affiliate-url').value.trim(),
    affiliate_CTA: document.getElementById('opt-affiliate-cta').value.trim(),
    pin_comment_enabled: document.getElementById('opt-pin-enabled').checked,
    pin_comment_text: document.getElementById('opt-pin-text').value.trim(),
    append_hashtags: document.getElementById('opt-hashtags').value.trim(),
    default_tags: document.getElementById('opt-default-tags').value.trim(),
    category_id: document.getElementById('opt-category').value,
    made_for_kids: document.getElementById('opt-made-for-kids').value === '1',
    ai_label: document.getElementById('opt-ai-label').checked,
  };
  if (payload.affiliate_enabled && !payload.affiliate_url) {
    alert('Đã bật Affiliate nhưng chưa nhập link Shopee.');
    return;
  }
  try {
    if (idRaw) {
      await apiFetch(`/api/options/${parseInt(idRaw, 10)}`, { method: 'PUT', body: JSON.stringify(payload) });
    } else {
      await apiFetch('/api/options', { method: 'POST', body: JSON.stringify(payload) });
    }
    resetOptionForm();
    await loadOptions();
    showToast('💾 Đã lưu bộ tùy chọn đăng.', 'success');
  } catch (e) {
    alert('Lỗi lưu bộ tùy chọn: ' + e.message);
  }
}

async function deleteOptionGroup(id) {
  const o = gOptions.find(x => x.id === id);
  if (!confirm(`Xóa bộ tùy chọn "${o ? o.name : '#' + id}"? Các video đang gán bộ này sẽ dùng cấu hình mặc định của kênh.`)) return;
  try {
    await apiFetch(`/api/options/${id}`, { method: 'DELETE' });
    if (document.getElementById('opt-id').value === String(id)) resetOptionForm();
    await loadOptions();
  } catch (e) {
    alert('Lỗi xóa bộ tùy chọn: ' + e.message);
  }
}

// ==========================================================
// SỬA CHI TIẾT KÊNH YOUTUBE & KIỂM TRA OAUTH TOKEN
// ==========================================================
async function openChannelEdit(id) {
  const c = gChannels.find(x => x.id === id);
  if (!c) {
    alert('Không tìm thấy kênh YouTube này.');
    return;
  }
  const set = (elId, val) => { const el = document.getElementById(elId); if (el) el.value = val ?? ''; };
  const check = (elId, val) => { const el = document.getElementById(elId); if (el) el.checked = !!val; };

  document.getElementById('ec-id').textContent = c.id;
  set('ec-id-val', c.id);
  set('ec-name', c.name || '');
  set('ec-handle', c.handle || '');
  set('ec-channel-id', c.channel_id || '');
  set('ec-email', c.email || '');
  set('ec-mode', c.upload_mode || 'playwright');
  set('ec-profile-name', c.profile_name || '');
  set('ec-profile-path', c.profile_path || '');
  set('ec-proxy', c.proxy || '');
  set('ec-privacy', c.default_privacy || 'public');
  set('ec-note', c.note || '');
  const oauthEl = document.getElementById('ec-oauth');
  if (oauthEl) {
    oauthEl.value = '';
    oauthEl.placeholder = c.has_oauth_token
      ? 'Đã lưu token — để trống nếu không thay đổi'
      : '{"client_id":"...","client_secret":"...","refresh_token":"..."}';
  }
  set('ec-made-for-kids', c.made_for_kids ? '1' : '0');
  set('ec-altered-content', c.altered_content ? '1' : '0');
  check('ec-emulate-mobile', c.emulate_mobile);
  check('ec-hidden', c.use_hidden_desktop);

  const grpEl = document.getElementById('ec-group');
  if (grpEl) {
    setGroupSelectOptions(grpEl, gGroups, false);
    selectGroupValues(grpEl, c.group_names && c.group_names.length ? c.group_names : [c.group_name || 'Mặc định']);
  }
  const catEl = document.getElementById('ec-category');
  if (catEl) {
    const cat = String(c.default_category_id || '22');
    if (!Array.from(catEl.options).some(op => op.value === cat)) {
      const op = document.createElement('option');
      op.value = cat;
      op.textContent = `${cat} - (hiện tại)`;
      catEl.appendChild(op);
    }
    catEl.value = cat;
  }
  const profList = document.getElementById('ec-profile-list');
  if (profList) {
    profList.innerHTML = (gProfiles || []).map(p => `<option value="${escapeHtml(p.name)}"></option>`).join('');
  }

  document.getElementById('modal-edit-channel').style.display = 'flex';
  await loadChannelOauthStatus(id);
}

function closeChannelEdit() {
  const m = document.getElementById('modal-edit-channel');
  if (m) m.style.display = 'none';
}

function renderOauthStatusHtml(s) {
  if (!s || !s.configured) {
    return '<span style="color:#f87171">❌ Chưa cấu hình OAuth token JSON cho kênh này.</span>';
  }
  const parts = [];
  parts.push(s.has_refresh_token ? '✅ Có refresh_token' : '❌ Thiếu refresh_token');
  parts.push(s.has_client_credentials ? '✅ Có client_id/client_secret' : '❌ Thiếu client_id/client_secret');
  if (s.expires_at) {
    parts.push(`${s.expired ? '⚠️ Access token đã hết hạn lúc' : '🕒 Access token hết hạn lúc'} <b>${escapeHtml(s.expires_at)}</b>`);
  }
  let html = parts.join(' · ');
  if (s.error) html += `<div style="margin-top:4px; color:#fbbf24;">⚠️ ${escapeHtml(s.error)}</div>`;
  return html;
}

async function loadChannelOauthStatus(id) {
  const box = document.getElementById('ec-oauth-status');
  if (!box) return;
  box.textContent = 'Đang đọc trạng thái token...';
  try {
    const s = await apiFetch(`/api/channels/${id}/oauth-status`);
    box.innerHTML = renderOauthStatusHtml(s);
  } catch (e) {
    box.innerHTML = `<span style="color:#f87171">Không đọc được trạng thái token: ${escapeHtml(e.message)}</span>`;
  }
}

async function checkChannelOauth(id) {
  const box = document.getElementById('ec-oauth-status');
  if (box) box.textContent = '🔄 Đang làm mới token và xác minh kênh với Google...';
  try {
    const r = await apiFetch(`/api/channels/${id}/check-oauth`, { method: 'POST' });
    if (r.ok) {
      alert(
        `✅ OAuth token HỢP LỆ.\n\nKênh token đại diện: ${r.identity.name}\nChannel ID: ${r.identity.channel_id}\n` +
        `Khớp kênh đích trong tool: ${r.matched ? 'CÓ ✅' : 'KHÔNG ❌ — upload sẽ bị chặn để tránh đăng nhầm kênh.'}`
      );
    } else {
      alert('❌ OAuth token KHÔNG dùng được:\n\n' + (r.error || 'Không rõ lỗi.'));
    }
    await loadChannelOauthStatus(id);
  } catch (e) {
    alert('Lỗi kiểm tra OAuth token: ' + e.message);
    await loadChannelOauthStatus(id);
  }
}

async function saveChannelEdit() {
  const id = parseInt(document.getElementById('ec-id-val').value, 10);
  if (!id) return;
  const name = document.getElementById('ec-name').value.trim();
  if (!name) {
    alert('Tên kênh không được để trống.');
    return;
  }
  const payload = {
    name,
    handle: document.getElementById('ec-handle').value.trim(),
    channel_id: document.getElementById('ec-channel-id').value.trim(),
    email: document.getElementById('ec-email').value.trim(),
    group_name: selectedMultiValues(document.getElementById('ec-group'))[0] || 'Mặc định',
    group_names: selectedMultiValues(document.getElementById('ec-group')),
    upload_mode: document.getElementById('ec-mode').value,
    profile_name: document.getElementById('ec-profile-name').value.trim(),
    profile_path: document.getElementById('ec-profile-path').value.trim(),
    proxy: document.getElementById('ec-proxy').value.trim(),
    default_category_id: document.getElementById('ec-category').value,
    default_privacy: document.getElementById('ec-privacy').value,
    note: document.getElementById('ec-note').value.trim(),
    made_for_kids: document.getElementById('ec-made-for-kids').value === '1',
    altered_content: document.getElementById('ec-altered-content').value === '1',
    emulate_mobile: document.getElementById('ec-emulate-mobile').checked,
    use_hidden_desktop: document.getElementById('ec-hidden').checked,
  };
  const oauthVal = document.getElementById('ec-oauth').value.trim();
  if (oauthVal) payload.oauth_token_json = oauthVal;
  if (!payload.profile_name) {
    alert('Tên Profile Chrome không được để trống — tool cần Profile để đăng nhập Google.');
    return;
  }
  try {
    await apiFetch(`/api/channels/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
    closeChannelEdit();
    await loadChannels();
    await loadProfiles();
    showToast('💾 Đã lưu thông tin kênh YouTube.', 'success');
  } catch (e) {
    alert('Lỗi lưu kênh: ' + e.message);
  }
}

async function addGroup() {
  const name = document.getElementById('grp-new-name').value.trim();
  if (!name) return;
  await apiFetch('/api/groups', { method: 'POST', body: JSON.stringify({ name }) });
  document.getElementById('grp-new-name').value = '';
  await loadGroups();
}

async function renameGroup() {
  const choices = (gGroups || []).map(group => group.name).filter(name => name !== 'Mặc định');
  if (!choices.length) return alert('Chưa có nhóm nào có thể đổi tên.');
  const oldName = prompt(`Nhập chính xác tên nhóm cần đổi tên:\n${choices.join('\n')}`);
  if (!oldName || !choices.includes(oldName.trim())) return;
  const newName = prompt(`Tên mới cho nhóm "${oldName.trim()}":`, oldName.trim());
  if (!newName || !newName.trim()) return;
  try {
    await apiFetch(`/api/groups/${encodeURIComponent(oldName.trim())}`, {
      method: 'PUT', body: JSON.stringify({ name: newName.trim() }),
    });
    await loadGroups();
    await loadChannels();
    showToast('Đã đổi tên nhóm. Video còn trỏ tới tên cũ sẽ cần chọn lại đích.', 'warning');
  } catch (e) { alert('Lỗi đổi tên nhóm: ' + e.message); }
}

async function deleteGroup() {
  const choices = (gGroups || []).map(group => group.name).filter(name => name !== 'Mặc định');
  if (!choices.length) return alert('Chưa có nhóm nào có thể xóa.');
  const name = prompt(`Nhập chính xác tên nhóm cần xóa:\n${choices.join('\n')}`);
  if (!name || !choices.includes(name.trim())) return;
  if (!confirm(`Xóa nhóm "${name.trim()}"? Video đang chờ trỏ tới nhóm này sẽ cần chọn lại đích trước khi chạy.`)) return;
  try {
    await apiFetch(`/api/groups/${encodeURIComponent(name.trim())}`, { method: 'DELETE' });
    await loadGroups();
    await loadChannels();
    showToast('Đã xóa nhóm. Video còn trỏ tới nhóm này sẽ cần chọn lại đích.', 'warning');
  } catch (e) { alert('Lỗi xóa nhóm: ' + e.message); }
}

const RUNNABLE_QUEUE_PREFIXES = ['pending', 'downloaded', 'rendered', 'downloading', 'rendering', 'chờ giãn cách'];

function isRunnableQueueItem(item) {
  const status = (item.status || '').trim();
  const lower = status.toLowerCase();
  if ((item.published_url || '').trim() || status === 'Completed') return false;
  if (lower.startsWith('uploading') || lower.startsWith('đang đăng')) return false;
  if (!lower) return true;
  return RUNNABLE_QUEUE_PREFIXES.some(prefix => lower.startsWith(prefix));
}

function updatePendingQueueBanner(items) {
  const banner = document.getElementById('queue-pending-banner');
  if (!banner) return;
  const pending = (items || []).filter(isRunnableQueueItem);
  gPendingQueueIds = pending.map(v => v.id);
  const countEl = document.getElementById('queue-pending-count');
  if (countEl) countEl.textContent = String(pending.length);
  const shouldShow = pending.length > 0 && !gPendingBannerDismissed && !gPipelineRunning;
  banner.style.display = shouldShow ? 'flex' : 'none';
}

function hidePendingBanner() {
  gPendingBannerDismissed = true;
  const banner = document.getElementById('queue-pending-banner');
  if (banner) banner.style.display = 'none';
}

async function runPendingQueueNow() {
  if (!gPendingQueueIds.length) {
    alert('Không có video nào đang chờ trong dự án này.');
    return;
  }
  await startPipeline(true, true, true, gPendingQueueIds);
}

async function loadQueue(preserveSelection = false) {
  // Đang mở modal sửa/xem video: tạm bỏ qua re-render để không giật form người dùng đang thao tác
  const editModal = document.getElementById('modal-edit-video');
  const previewModal = document.getElementById('modal-preview-video');
  if ((editModal && editModal.style.display === 'flex') || (previewModal && previewModal.style.display === 'flex')) {
    return;
  }

  const proj = document.getElementById('sel-project').value || 'Default';
  if (gLastQueueProject !== proj) {
    gLastQueueProject = proj;
    gPendingBannerDismissed = false;
  }
  const items = await apiFetch(`/api/queue?project=${encodeURIComponent(proj)}`);
  gQueueItems = items || [];
  updatePendingQueueBanner(gQueueItems);
  if (!preserveSelection) {
    gSelectedIds.clear();
  }

  const chMap = {};
  gChannels.forEach(c => { chMap[c.id] = c; });

  const tbody = document.getElementById('queue-tbody');
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; padding:28px; color:var(--text-muted)">Chưa có video nào trong dự án này. Hãy nhập Link hoặc Thư mục video ở khung bên trái để quét!</td></tr>`;
    updateQueueSelectionSummary();
    return;
  }

  tbody.innerHTML = items.map(item => {
    const checked = gSelectedIds.has(item.id) ? 'checked' : '';
    const publishedHref = safePublishedVideoUrl(item.published_url);
    let profLabel = item.target_profile_name ? `👤 ${escapeHtml(item.target_profile_name)}` : '';
    let chLabel = item.assigned_channel_id && chMap[item.assigned_channel_id]
      ? `📺 ${escapeHtml(chMap[item.assigned_channel_id].name)} (đã chốt)`
      : item.target_channel_id && chMap[item.target_channel_id]
      ? `📺 ${escapeHtml(chMap[item.target_channel_id].name)}`
      : `📂 Nhóm: ${escapeHtml(groupNamesFromItem(item).join(' + ') || item.target_group_name || 'Mặc định')}`;

    let statusBadge = `<span class="badge badge-wait">${escapeHtml(item.status)}</span>`;
    if (item.status === 'Completed') {
      statusBadge = `<span class="badge badge-ok">✅ Đã đăng</span>`;
    } else if (item.status === 'NeedsReview') {
      statusBadge = `<span class="badge" style="background:rgba(245,158,11,0.2); color:#f59e0b; border:1px solid #f59e0b; font-weight:700;" title="${escapeHtml(item.error_message || 'Hãy kiểm tra YouTube Studio trước khi chạy lại')}">🔎 Cần đối soát</span>`;
    } else if (item.status === 'Stopped' || (item.status && item.status.includes('Dừng'))) {
      statusBadge = `<span class="badge" style="background:rgba(245,158,11,0.2); color:#f59e0b; border:1px solid #f59e0b; font-weight:700;" title="${escapeHtml(item.error_message || 'Đã dừng theo yêu cầu')}">⏹️ Đã dừng</span>`;
    } else if (item.status && (item.status.includes('Render') || item.status.includes('Đang Render'))) {
      statusBadge = `<span class="badge" style="background:rgba(59,130,246,0.2); color:#38bdf8; border:1px solid #38bdf8; font-weight:700;">⚙️ ${escapeHtml(item.status)}</span>`;
    } else if (item.status && (item.status.includes('Bản Quyền') || item.status.includes('Copyright') || item.status.includes('Hủy') || item.status.includes('Cancelled'))) {
      statusBadge = `<span class="badge" style="background:rgba(239,68,68,0.2); color:#ef4444; border:1px solid #ef4444; font-weight:700;" title="${escapeHtml(item.error_message || 'Bị cảnh báo bản quyền nên đã hủy')}">🛑 Đã Hủy (Dính Bản Quyền)</span>`;
    } else if (item.status === 'Error') {
      statusBadge = `<span class="badge badge-err" title="${escapeHtml(item.error_message)}">❌ Lỗi</span>`;
    }

    const res = (item.resolution || '1080p').toLowerCase();
    const resBadge = res === '4k' ? 'badge-ok' : (res === '2k' ? 'badge-purple' : 'badge-video');
    const mobileBadge = item.emulate_mobile !== 0
      ? '<span title="Đăng giả lập điện thoại di động (Mobile User-Agent & Touch)" style="font-size:11px; color:#38bdf8; font-weight:600;">📱 Mobile</span>'
      : '<span title="Đăng chế độ máy tính (Native Desktop - Khuyên dùng an toàn chống Checkpoint)" style="font-size:11px; color:#10b981; font-weight:600;">🖥️ Desktop (An toàn)</span>';

    let ytModeBadge = '';
    const vis = (item.visibility || 'public').toLowerCase();
    if (vis === 'schedule' || item.schedule_time) {
      const schedTimeShort = item.schedule_time ? item.schedule_time.slice(5, 16) : 'Chưa đặt';
      ytModeBadge = `<span class="badge" style="background:rgba(245,158,11,0.15); color:#f59e0b; border:1px solid #f59e0b; font-size:10.5px;" title="Lên lịch phát hành">⏰ Lịch: ${escapeHtml(schedTimeShort)}${item.is_premiere ? ' (Premiere 🎬)' : ''}</span>`;
    } else if (vis === 'public') {
      ytModeBadge = `<span class="badge badge-ok" style="font-size:10.5px;" title="Đăng ngay - Công khai">🚀 Công khai${item.is_premiere ? ' (Premiere 🎬)' : ''}</span>`;
    } else if (vis === 'unlisted') {
      ytModeBadge = `<span class="badge" style="background:rgba(148,163,184,0.15); color:#94a3b8; border:1px solid #64748b; font-size:10.5px;" title="Đăng ngay - Không công khai">🔒 K.công khai</span>`;
    } else if (vis === 'private') {
      ytModeBadge = `<span class="badge" style="background:rgba(244,63,94,0.15); color:#f43f5e; border:1px solid #f43f5e; font-size:10.5px;" title="Đăng ngay - Riêng tư">🛡️ Riêng tư</span>`;
    }

    let audBadges = '';
    if (item.made_for_kids) {
      audBadges += `<span class="badge" style="background:rgba(234,179,8,0.15); color:#eab308; border:1px solid #eab308; font-size:10px;" title="Nội dung dành cho trẻ em">👶 Kids</span>`;
    }
    if (item.age_restriction) {
      audBadges += `<span class="badge" style="background:rgba(239,68,68,0.15); color:#ef4444; border:1px solid #ef4444; font-size:10px;" title="Giới hạn độ tuổi 18+">🔞 18+</span>`;
    }
    if (item.altered_content) {
      audBadges += `<span class="badge badge-purple" style="font-size:10px;" title="Nội dung do AI tạo hoặc chỉnh sửa">🤖 AI</span>`;
    }

    return `
      <tr>
        <td><input type="checkbox" class="row-chk" data-id="${item.id}" ${checked} onchange="onRowCheck(${item.id}, this.checked)" /></td>
        <td>#${item.id}</td>
        <td>
          <span class="badge ${item.upload_to === 'Shorts' ? 'badge-shorts' : 'badge-video'}">${escapeHtml(item.upload_to)}</span>
          <div style="font-size:11px; color:var(--text-muted); margin-top:3px;">${escapeHtml(item.platform)}</div>
        </td>
        <td>
          <div style="font-weight:600; color:#fff;">${escapeHtml(item.title)}</div>
          <div style="font-size:11px; color:var(--text-muted); max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
            ${escapeHtml(item.description || item.source_url)}
          </div>
          <div style="margin-top:4px; display:flex; gap:4px; flex-wrap:wrap; align-items:center;">
            ${ytModeBadge}
            ${audBadges}
          </div>
        </td>
        <td>
          <div><span class="badge ${resBadge}">${res.toUpperCase()}</span></div>
          <div style="margin-top:3px;">${mobileBadge}</div>
        </td>
        <td><span class="badge badge-wait">${escapeHtml(item.render_mode)}</span></td>
        <td>
          ${profLabel ? `<div style="font-size:11px; color:#38bdf8; font-weight:600; margin-bottom:2px;">${profLabel}</div>` : ''}
          <div style="font-size:11.5px; font-weight:600;">${chLabel}</div>
        </td>
        <td>
          <div class="prog-bar">
            <div class="prog-fill prog-dl" style="width:${item.download_progress || 0}%"></div>
            <div class="prog-label">${item.download_progress || 0}%</div>
          </div>
        </td>
        <td>
          <div class="prog-bar">
            <div class="prog-fill prog-rd" style="width:${item.render_progress || 0}%"></div>
            <div class="prog-label">${item.render_progress || 0}%</div>
          </div>
        </td>
        <td>
          <div class="prog-bar">
            <div class="prog-fill prog-up" style="width:${item.upload_progress || 0}%"></div>
            <div class="prog-label">${item.upload_progress || 0}%</div>
          </div>
        </td>
        <td>
          ${statusBadge}
          ${publishedHref ? `<div style="margin-top:4px;"><a href="${escapeHtml(publishedHref)}" target="_blank" rel="noopener noreferrer" style="color:#34d399; font-size:11.5px; font-weight:600;">🔗 Xem trên YT</a></div>` : (item.published_url ? '<div title="URL không phải địa chỉ HTTPS của YouTube" style="margin-top:4px; color:var(--text-muted); font-size:11px;">🔗 URL không hợp lệ</div>' : '')}
          ${item.schedule_time ? `<div style="font-size:11px; color:#fbbf24; margin-top:3px; font-weight:600;" title="Thời gian hẹn giờ đăng">📅 ${escapeHtml(item.schedule_time.slice(5, 16))}</div>` : ''}
          ${item.error_message ? `<div style="font-size:10.5px; color:#fb7185; margin-top:2px;">${escapeHtml(item.error_message.slice(0, 65))}</div>` : ''}
        </td>
        <td>
          <div style="display:flex; gap:4px; align-items:center;">
            <button class="btn btn-accent btn-sm" onclick="openPreviewModal(${item.id})" title="Xem thử video trực tiếp">▶️</button>
            <button class="btn btn-outline btn-sm" onclick="openEditModal(${item.id})" title="Chỉnh sửa chi tiết tiêu đề, mô tả, chất lượng...">✏️</button>
            ${item.status === 'Completed' ? `<button class="btn btn-outline btn-sm" onclick="manualReuploadQueueItem(${item.id}, 'reupload')" title="Ghi lý do và cho phép đăng lại riêng video này">🔁</button>` : ''}
            ${item.status === 'NeedsReview' ? `<button class="btn btn-outline btn-sm" onclick="manualReuploadQueueItem(${item.id}, 'reupload')" title="Xác nhận chưa đăng rồi cho phép chạy lại video này">🔁</button><button class="btn btn-outline btn-sm" onclick="manualReuploadQueueItem(${item.id}, 'confirmed_published')" title="Xác nhận đã đăng và lưu URL vào lịch sử">✅</button>` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  const searchInput = document.getElementById('queue-search');
  if (searchInput && searchInput.value) {
    filterQueueTable(searchInput.value);
  }
  updateQueueSelectionSummary();
}

function onRowCheck(id, checked) {
  if (checked) gSelectedIds.add(id);
  else gSelectedIds.delete(id);
  updateQueueSelectionSummary();
}

function toggleSelectAll(checked) {
  document.querySelectorAll('.row-chk').forEach(cb => {
    const row = cb.closest('tr');
    if (!row || row.style.display === 'none') return;
    cb.checked = checked;
    const id = parseInt(cb.getAttribute('data-id'), 10);
    if (checked) gSelectedIds.add(id);
    else gSelectedIds.delete(id);
  });
  updateQueueSelectionSummary();
}

function getVisibleQueueIds() {
  return Array.from(document.querySelectorAll('.row-chk'))
    .filter(cb => cb.closest('tr') && cb.closest('tr').style.display !== 'none')
    .map(cb => parseInt(cb.getAttribute('data-id'), 10))
    .filter(Number.isFinite);
}

function updateQueueSelectionSummary() {
  const summary = document.getElementById('queue-selection-summary');
  const currentIds = new Set((gQueueItems || []).map(item => Number(item.id)));
  const selected = Array.from(gSelectedIds).filter(id => currentIds.has(Number(id)));
  const visible = getVisibleQueueIds();
  const hiddenCount = selected.filter(id => !visible.includes(Number(id))).length;
  if (summary) {
    summary.textContent = selected.length
      ? `Đã chọn ${selected.length} video${hiddenCount ? ` (${hiddenCount} dòng đang ẩn)` : ''}; thao tác dùng đủ số đã chọn.`
      : `Chưa chọn thủ công; thao tác mặc định áp dụng ${visible.length} dòng đang hiển thị.`;
  }
  const selectAll = document.getElementById('chk-all');
  if (selectAll) {
    const selectedVisible = visible.filter(id => gSelectedIds.has(id)).length;
    selectAll.checked = visible.length > 0 && selectedVisible === visible.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
  }
}

function getTargetIdsOrAlert() {
  const currentIds = new Set((gQueueItems || []).map(item => Number(item.id)));
  const selected = Array.from(gSelectedIds).filter(id => currentIds.has(Number(id)));
  return selected.length ? selected : getVisibleQueueIds();
}

function toggleScanScheduleFields() {
  const vis = document.getElementById('scan-visibility') ? document.getElementById('scan-visibility').value : 'public';
  const wrap = document.getElementById('scan-schedule-wrap');
  if (wrap) wrap.style.display = (vis === 'schedule') ? 'block' : 'none';
  if (vis !== 'schedule') {
    const timeEl = document.getElementById('scan-sched-time');
    const premEl = document.getElementById('scan-is-premiere');
    if (timeEl) timeEl.value = '';
    if (premEl) premEl.checked = false;
  }
}

function toggleBulkScheduleFields() {
  const vis = document.getElementById('bulk-visibility') ? document.getElementById('bulk-visibility').value : '';
  const wrap = document.getElementById('bulk-schedule-wrap');
  if (wrap) wrap.style.display = (vis === 'schedule') ? 'block' : 'none';
  if (vis !== 'schedule') {
    const timeEl = document.getElementById('bulk-sched-time');
    const premEl = document.getElementById('bulk-is-premiere');
    if (timeEl) timeEl.value = '';
    if (premEl) premEl.checked = false;
  }
}

function toggleModalScheduleFields() {
  const vis = document.getElementById('ed-vid-visibility') ? document.getElementById('ed-vid-visibility').value : 'public';
  const wrap = document.getElementById('ed-vid-schedule-wrap');
  if (wrap) wrap.style.display = (vis === 'schedule') ? 'block' : 'none';
  if (vis !== 'schedule') {
    const timeEl = document.getElementById('ed-vid-schedule-time');
    const premEl = document.getElementById('ed-vid-is-premiere');
    if (timeEl) timeEl.value = '';
    if (premEl) premEl.checked = false;
  }
}

async function scanAndAddVideos() {
  const btn = document.getElementById('btn-scan');
  const urls = document.getElementById('scan-urls').value;
  if (!urls.trim()) {
    alert('Vui lòng nhập ít nhất 1 Link kênh/video hoặc đường dẫn thư mục!');
    return;
  }
  const visVal = document.getElementById('scan-visibility') ? document.getElementById('scan-visibility').value : 'public';
  const schedTimeVal = document.getElementById('scan-sched-time') ? document.getElementById('scan-sched-time').value : '';
  if (visVal === 'schedule' && !schedTimeVal) {
    alert('Bạn đã chọn "Lên lịch phát hành" nhưng chưa nhập thời gian bắt đầu. Hệ thống sẽ không tự chuyển sang Công khai.');
    return;
  }
  if (visVal === 'schedule' && new Date(schedTimeVal) <= new Date()) {
    alert('Thời gian bắt đầu lên lịch phải ở TƯƠNG LAI.');
    return;
  }
  btn.disabled = true;
  btn.textContent = '⏳ Đang quét dữ liệu...';
  try {
    const payload = {
      project_name: document.getElementById('sel-project').value || 'Default',
      urls,
      max_items: parseInt(document.getElementById('scan-max').value || '20', 10),
      min_views: parseInt(document.getElementById('scan-min-views').value || '0', 10),
      min_comments: document.getElementById('scan-min-comments') ? parseInt(document.getElementById('scan-min-comments').value || '0', 10) : 0,
      recursive: document.getElementById('scan-recursive') ? document.getElementById('scan-recursive').checked : false,
      upload_to: document.getElementById('scan-upload-to').value,
      resolution: document.getElementById('scan-resolution') ? document.getElementById('scan-resolution').value : '1080p',
      render_mode: document.getElementById('scan-render-mode').value,
      target_group_names: selectedMultiValues(document.getElementById('scan-group')),
      emulate_mobile: document.getElementById('scan-mobile') ? document.getElementById('scan-mobile').checked : false,
      visibility: visVal,
      made_for_kids: document.getElementById('scan-made-for-kids') ? parseInt(document.getElementById('scan-made-for-kids').value || '0', 10) : 0,
      schedule_time: visVal === 'schedule' ? schedTimeVal : '',
      schedule_interval_minutes: document.getElementById('scan-sched-interval') ? parseInt(document.getElementById('scan-sched-interval').value || '60', 10) : 60,
      is_premiere: visVal === 'schedule' && document.getElementById('scan-is-premiere') ? (document.getElementById('scan-is-premiere').checked ? 1 : 0) : 0,
      age_restriction: document.getElementById('scan-age-restriction') ? parseInt(document.getElementById('scan-age-restriction').value || '0', 10) : 0,
      altered_content: document.getElementById('scan-altered-content') ? parseInt(document.getElementById('scan-altered-content').value || '0', 10) : 0,
      option_group_id: (() => {
        const el = document.getElementById('scan-option-group');
        return el && el.value ? parseInt(el.value, 10) : null;
      })(),
    };
    const res = await apiFetch('/api/queue/scan', { method: 'POST', body: JSON.stringify(payload) });
    if (!res.added && res.skipped) {
      alert(`ℹ️ ${res.skipped} video đã có trong dự án — không thêm trùng.`);
    } else {
      const skipNote = res.skipped ? `\nBỏ qua ${res.skipped} video đã có trong dự án.` : '';
      alert(`Đã quét và thêm thành công ${res.added} video vào bảng!${skipNote}`);
    }
    await loadProjects();
    await loadQueue();
  } catch (e) {
    alert('Lỗi khi quét nguồn: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🚀 Quét & Đưa Vào Bảng Dự Án';
  }
}

async function applyBulkConfig() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Chưa có video nào trong bảng.');
    return;
  }
  const updates = {};
  const profVal = document.getElementById('bulk-profile') ? document.getElementById('bulk-profile').value : '';
  const chVal = document.getElementById('bulk-channel') ? document.getElementById('bulk-channel').value : '';
  const grpVals = selectedMultiValues(document.getElementById('bulk-group'));
  const optVal = document.getElementById('bulk-option-group') ? document.getElementById('bulk-option-group').value : '';
  const resVal = document.getElementById('bulk-res') ? document.getElementById('bulk-res').value : '';
  const rdVal = document.getElementById('bulk-render') ? document.getElementById('bulk-render').value : '';
  const titleVal = document.getElementById('bulk-title') ? document.getElementById('bulk-title').value.trim() : '';
  const descVal = document.getElementById('bulk-desc') ? document.getElementById('bulk-desc').value.trim() : '';
  const cutStart = document.getElementById('bulk-cut-start') ? document.getElementById('bulk-cut-start').value.trim() : '';
  const cutEnd = document.getElementById('bulk-cut-end') ? document.getElementById('bulk-cut-end').value.trim() : '';
  const mobileEl = document.getElementById('bulk-mobile');
  const visVal = document.getElementById('bulk-visibility') ? document.getElementById('bulk-visibility').value : '';
  const mfkVal = document.getElementById('bulk-made-for-kids') ? document.getElementById('bulk-made-for-kids').value : '';
  const ageVal = document.getElementById('bulk-age-restriction') ? document.getElementById('bulk-age-restriction').value : '';
  const altVal = document.getElementById('bulk-altered-content') ? document.getElementById('bulk-altered-content').value : '';
  const schedTimeVal = document.getElementById('bulk-sched-time') ? document.getElementById('bulk-sched-time').value : '';
  const schedIntRaw = document.getElementById('bulk-sched-interval') ? parseInt(document.getElementById('bulk-sched-interval').value || '60', 10) : 60;
  const schedIntVal = Math.max(30, isNaN(schedIntRaw) ? 60 : schedIntRaw);
  const isPremEl = document.getElementById('bulk-is-premiere');

  if (visVal === 'schedule' && !schedTimeVal) {
    alert('Bạn đã chọn "Lên lịch phát hành" nhưng chưa nhập thời gian bắt đầu.');
    return;
  }
  if (schedTimeVal && new Date(schedTimeVal) <= new Date()) {
    alert('Thời gian bắt đầu phải ở TƯƠNG LAI để kịp tải/render/đăng video.');
    return;
  }

  if (chVal) {
    const chId = parseInt(chVal, 10);
    updates.target_channel_id = chId;
    updates.target_type = 'CHANNEL';
    const pickedChannel = gChannels.find(c => c.id === chId);
    if (pickedChannel && pickedChannel.profile_name) updates.target_profile_name = pickedChannel.profile_name;
    else if (profVal) updates.target_profile_name = profVal;
  } else if (grpVals.length) {
    updates.target_type = 'GROUP';
    updates.target_group_names = grpVals;
    updates.target_channel_id = null;
    updates.target_profile_name = '';
  } else if (profVal) {
    updates.target_profile_name = profVal;
    updates.target_channel_id = null;
    updates.target_type = 'PROFILE';
  }
  if (optVal) updates.option_group_id = parseInt(optVal, 10);
  if (resVal) updates.resolution = resVal;
  if (rdVal) updates.render_mode = rdVal;
  if (titleVal) updates.title = titleVal;
  if (descVal) updates.description = descVal;
  if (cutStart) updates.cut_start = cutStart;
  if (cutEnd) updates.cut_end = cutEnd;
  if (mobileEl) updates.emulate_mobile = mobileEl.checked ? 1 : 0;
  if (visVal) updates.visibility = visVal;
  if (mfkVal !== '') updates.made_for_kids = parseInt(mfkVal, 10);
  if (ageVal !== '') updates.age_restriction = parseInt(ageVal, 10);
  if (altVal !== '') updates.altered_content = parseInt(altVal, 10);
  if (visVal === 'schedule') {
    updates.schedule_time = schedTimeVal ? schedTimeVal.replace('T', ' ') + (schedTimeVal.length === 16 ? ':00' : '') : '';
    updates.schedule_interval_minutes = schedIntVal;
    if (isPremEl) updates.is_premiere = isPremEl.checked ? 1 : 0;
  }

  try {
    await apiFetch('/api/queue/bulk-update', {
      method: 'POST',
      body: JSON.stringify({ ids, updates }),
    });
    alert(`Đã áp dụng cấu hình thành công cho ${ids.length} video!`);
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi cập nhật hàng loạt: ' + e.message);
  }
}

async function openEditModal(id) {
  const item = gQueueItems.find(x => x.id === id);
  if (!item) return;

  document.getElementById('ed-vid-id').textContent = item.id;
  document.getElementById('ed-vid-id-val').value = item.id;
  document.getElementById('ed-vid-title').value = item.title || '';
  document.getElementById('ed-vid-desc').value = item.description || '';
  document.getElementById('ed-vid-res').value = item.resolution || '1080p';
  document.getElementById('ed-vid-render').value = item.render_mode || 'shorts_9_16';
  document.getElementById('ed-vid-cut-start').value = item.cut_start || '';
  document.getElementById('ed-vid-cut-end').value = item.cut_end || '';
  document.getElementById('ed-vid-mobile').checked = item.emulate_mobile !== 0;
  const optGroupEl = document.getElementById('ed-vid-option-group');
  if (optGroupEl) optGroupEl.value = item.option_group_id ? String(item.option_group_id) : '';

  const visEl = document.getElementById('ed-vid-visibility');
  if (visEl) visEl.value = item.visibility || 'public';
  const mfkEl = document.getElementById('ed-vid-made-for-kids');
  if (mfkEl) mfkEl.value = item.made_for_kids ? '1' : '0';
  const ageEl = document.getElementById('ed-vid-age-restriction');
  if (ageEl) ageEl.value = item.age_restriction ? '1' : '0';
  const altEl = document.getElementById('ed-vid-altered-content');
  if (altEl) altEl.value = item.altered_content ? '1' : '0';
  const premEl = document.getElementById('ed-vid-is-premiere');
  if (premEl) premEl.checked = !!item.is_premiere;

  const schedEl = document.getElementById('ed-vid-schedule-time');
  if (schedEl) {
    if (item.schedule_time) {
      schedEl.value = item.schedule_time.replace(' ', 'T').slice(0, 16);
    } else {
      schedEl.value = '';
    }
  }

  toggleModalScheduleFields();

  const edProf = document.getElementById('ed-vid-profile');
  if (edProf) {
    const profileValue = String(item.target_profile_name || '');
    if (profileValue && !Array.from(edProf.options).some(option => option.value === profileValue)) {
      const missing = document.createElement('option');
      missing.value = profileValue;
      missing.textContent = `⚠️ ${profileValue} (Profile không còn trong danh sách)`;
      missing.dataset.missing = 'true';
      edProf.appendChild(missing);
    }
    edProf.value = profileValue;
  }

  await onModalProfileChange();
  const groupSelect = document.getElementById('ed-vid-groups');
  if (groupSelect) {
    selectGroupValues(
      groupSelect,
      String(item.target_type || '').toUpperCase() === 'GROUP' ? groupNamesFromItem(item) : [],
    );
  }
  if (item.target_channel_id) {
    const chSel = document.getElementById('ed-vid-channel');
    if (chSel) {
      const idValue = String(item.target_channel_id);
      if (!Array.from(chSel.options).some(option => option.value === idValue)) {
        const missing = document.createElement('option');
        missing.value = idValue;
        missing.textContent = `⚠️ Kênh #${idValue} (không còn trong danh sách)`;
        missing.dataset.missing = 'true';
        chSel.appendChild(missing);
      }
      chSel.value = idValue;
    }
  }

  document.getElementById('modal-edit-video').style.display = 'flex';
}

function closeEditModal() {
  document.getElementById('modal-edit-video').style.display = 'none';
}

async function saveEditModal() {
  const id = parseInt(document.getElementById('ed-vid-id-val').value, 10);
  const chVal = document.getElementById('ed-vid-channel').value;
  const profVal = document.getElementById('ed-vid-profile').value;
  const schedVal = document.getElementById('ed-vid-schedule-time') ? document.getElementById('ed-vid-schedule-time').value : '';
  const visVal = document.getElementById('ed-vid-visibility') ? document.getElementById('ed-vid-visibility').value : 'public';
  const isSchedule = visVal === 'schedule';
  const groupNames = selectedMultiValues(document.getElementById('ed-vid-groups'));

  if (isSchedule && !schedVal) {
    alert('Bạn đã chọn "Lên lịch phát hành" nhưng chưa nhập thời gian.');
    return;
  }
  if (isSchedule && new Date(schedVal) <= new Date()) {
    alert('Thời gian đặt lịch phải ở TƯƠNG LAI.');
    return;
  }

  const payload = {
    id,
    title: document.getElementById('ed-vid-title').value.trim(),
    description: document.getElementById('ed-vid-desc').value.trim(),
    resolution: document.getElementById('ed-vid-res').value,
    render_mode: document.getElementById('ed-vid-render').value,
    option_group_id: document.getElementById('ed-vid-option-group') && document.getElementById('ed-vid-option-group').value
      ? parseInt(document.getElementById('ed-vid-option-group').value, 10)
      : null,
    emulate_mobile: document.getElementById('ed-vid-mobile').checked,
    cut_start: document.getElementById('ed-vid-cut-start').value.trim(),
    cut_end: document.getElementById('ed-vid-cut-end').value.trim(),
    visibility: visVal,
    made_for_kids: document.getElementById('ed-vid-made-for-kids') ? parseInt(document.getElementById('ed-vid-made-for-kids').value || '0', 10) : 0,
    age_restriction: document.getElementById('ed-vid-age-restriction') ? parseInt(document.getElementById('ed-vid-age-restriction').value || '0', 10) : 0,
    altered_content: document.getElementById('ed-vid-altered-content') ? parseInt(document.getElementById('ed-vid-altered-content').value || '0', 10) : 0,
    schedule_time: isSchedule && schedVal ? schedVal.replace('T', ' ') + (schedVal.length === 16 ? ':00' : '') : '',
    is_premiere: isSchedule && document.getElementById('ed-vid-is-premiere') ? (document.getElementById('ed-vid-is-premiere').checked ? 1 : 0) : 0,
  };
  if (chVal) {
    payload.target_channel_id = parseInt(chVal, 10);
    payload.target_profile_name = profVal || '';
  } else if (groupNames.length) {
    payload.target_group_names = groupNames;
  } else if (profVal) {
    payload.target_profile_name = profVal;
  }

  try {
    await apiFetch('/api/queue/edit-metadata', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeEditModal();
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi lưu thông tin video: ' + e.message);
  }
}


async function openPreviewModal(id) {
  const item = gQueueItems.find(x => x.id === id);
  if (!item) return;

  document.getElementById('pv-vid-id').textContent = item.id;
  document.getElementById('pv-vid-title').textContent = item.title || 'Video #' + item.id;
  const player = document.getElementById('pv-video-player');
  try {
    const token = await ensureApiToken(true);
    player.src = `/api/queue/${item.id}/stream?token=${encodeURIComponent(token)}`;
    player.play().catch(() => {});
  } catch (e) {
    alert('Không mở được video: ' + e.message);
    return;
  }

  const infoEl = document.getElementById('pv-video-info');
  const pInfo = item.output_video_path ? `🎬 Video Render Thành Phẩm (${item.resolution || '1080p'})` : (item.input_video_path ? `⬇️ Video Gốc Đã Tải` : `Chưa tải xong`);
  infoEl.textContent = `${pInfo} | Thời lượng: ${item.duration || 'N/A'}`;

  document.getElementById('modal-preview-video').style.display = 'flex';
}

function closePreviewModal() {
  const player = document.getElementById('pv-video-player');
  if (player) {
    player.pause();
    player.src = '';
  }
  document.getElementById('modal-preview-video').style.display = 'none';
}

// ===== LÊN LỊCH GIÃN CÁCH SỐ LƯỢNG LỚN (50+ VIDEO) =====
function promptAutoSchedule() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Chưa có video nào để lên lịch.');
    return;
  }
  const startEl = document.getElementById('as-start-time');
  if (startEl && !startEl.value) {
    const d = new Date(Date.now() + 60 * 60 * 1000);
    const pad = n => String(n).padStart(2, '0');
    startEl.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  updateAutoScheduleSummary();
  document.getElementById('modal-auto-schedule').style.display = 'flex';
}

function closeAutoScheduleModal() {
  document.getElementById('modal-auto-schedule').style.display = 'none';
}

function updateAutoScheduleSummary() {
  const sumEl = document.getElementById('as-summary');
  if (!sumEl) return;
  const ids = getTargetIdsOrAlert();
  const n = ids.length;
  if (!n) {
    sumEl.innerHTML = '⚠️ Chưa tích chọn video nào trong bảng.';
    return;
  }
  const startVal = document.getElementById('as-start-time').value;
  let interval = parseInt(document.getElementById('as-interval').value || '60', 10);
  if (isNaN(interval) || interval < 30) interval = 30;
  const start = startVal ? new Date(startVal) : new Date(Date.now() + 60 * 60 * 1000);
  const end = new Date(start.getTime() + (n - 1) * interval * 60000);
  const fmt = d => d.toLocaleString('vi-VN', { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit', year: 'numeric' });
  const spanDays = Math.round((end - start) / 86400000 * 10) / 10;
  sumEl.innerHTML =
    `📺 <b>${n}</b> video sẽ được phát hành liên tiếp:<br>` +
    `⏰ Video đầu: <b>${fmt(start)}</b><br>` +
    `🏁 Video cuối: <b>${fmt(end)}</b> (kéo dài ~${spanDays} ngày)<br>` +
    `⏳ Giãn cách <b>${interval}</b> phút/video — an toàn chống spam`;
}

async function applyAutoSchedule() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Hãy tích chọn các video cần lên lịch trước.');
    return;
  }
  const startVal = document.getElementById('as-start-time').value;
  let interval = parseInt(document.getElementById('as-interval').value || '60', 10);
  if (isNaN(interval) || interval < 30) interval = 30;
  if (startVal && new Date(startVal).getTime() <= Date.now()) {
    alert('Thời gian bắt đầu phải ở TƯƠNG LAI (để kịp upload video trước giờ phát hành).');
    return;
  }
  try {
    const res = await apiFetch('/api/queue/auto-schedule', {
      method: 'POST',
      body: JSON.stringify({
        ids,
        start_time: startVal ? startVal.replace('T', ' ') : '',
        interval_minutes: interval,
        is_premiere: document.getElementById('as-premiere').checked ? 1 : 0,
      }),
    });
    closeAutoScheduleModal();
    alert(
      `📅 Đã lên lịch thành công cho ${res.scheduled} video!\n` +
      `⏰ Video đầu: ${res.first_time.slice(0, 16).replace('T', ' ')}\n` +
      `🏁 Video cuối: ${res.last_time.slice(0, 16).replace('T', ' ')}\n` +
      `⏳ Giãn cách ${res.interval_minutes} phút/video (YouTube sẽ tự phát đúng giờ).`
    );
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi lên lịch: ' + e.message);
  }
}

async function deleteSelectedQueue() {
  const currentIds = new Set((gQueueItems || []).map(item => Number(item.id)));
  const ids = Array.from(gSelectedIds).filter(id => currentIds.has(Number(id)));
  if (!ids.length) {
    alert('Hãy tích chọn các dòng video muốn xóa.');
    return;
  }
  const visibleIds = new Set(getVisibleQueueIds());
  const hiddenCount = ids.filter(id => !visibleIds.has(Number(id))).length;
  const scope = hiddenCount ? ` (bao gồm ${hiddenCount} dòng đang ẩn theo bộ lọc)` : '';
  if (!confirm(`Xóa ${ids.length} video đã chọn${scope}?`)) return;
  await apiFetch('/api/queue/delete', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  });
  gSelectedIds.clear();
  await loadProjects();
  await loadQueue();
}

async function manualReuploadQueueItem(itemId, action) {
  const item = gQueueItems.find(row => Number(row.id) === Number(itemId));
  if (!item) return;

  const reason = prompt('Nhập lý do/ghi chú đối soát (bắt buộc, tối thiểu 5 ký tự):');
  if (!reason || reason.trim().length < 5) return;

  const payload = { action, reason: reason.trim() };
  if (action === 'reupload') {
    if (item.status === 'NeedsReview') {
      if (!confirm('Bạn đã kiểm tra YouTube Studio và xác nhận lượt trước CHƯA đăng video này?')) return;
      payload.confirm_not_published = true;
    } else if (!confirm('Cho phép đăng lại riêng video này? Thao tác có thể tạo video trùng trên kênh.')) {
      return;
    }
  } else {
    const url = prompt('Dán URL video đã xác minh trên YouTube:');
    if (!url || !url.trim()) return;
    payload.published_url = url.trim();
  }

  try {
    const result = await apiFetch(`/api/queue/${itemId}/manual-reupload`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showToast(result.message || 'Đã lưu quyết định cho video.', 'success');
    await loadQueue(true);
  } catch (e) {
    alert('Không thể lưu quyết định: ' + e.message);
  }
}

function exportExcel() {
  const proj = document.getElementById('sel-project').value || 'Default';
  const ids = Array.from(gSelectedIds);
  let url = `/api/queue/export-excel?project=${encodeURIComponent(proj)}`;
  if (ids.length) url += `&ids=${ids.join(',')}`;
  downloadWithToken(url, 'YouTube_Queue.xlsx');
}

function triggerImportExcel() {
  const fileInput = document.getElementById('file-import-excel');
  if (fileInput) {
    fileInput.value = '';
    fileInput.click();
  }
}

async function handleImportExcel(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const proj = document.getElementById('sel-project').value || 'Default';
  const selected = Array.from(gSelectedIds);
  const sendImport = (previewOnly) => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('project_name', proj);
    formData.append('preview_only', previewOnly ? 'true' : 'false');
    if (selected.length) formData.append('ids', selected.join(','));
    return apiFetch('/api/queue/import-excel', { method: 'POST', body: formData });
  };

  const describePreview = (data) => {
    const lines = [
      `Dự kiến cập nhật: ${data.matched || 0} dòng`,
      `Bỏ qua: ${data.skipped || 0} dòng`,
      `Lỗi: ${data.error_count || 0} dòng`,
    ];
    (data.rows || []).filter(row => row.state === 'error' || row.state === 'skipped').slice(0, 8).forEach(row => {
      lines.push(`Dòng ${row.line}: ${(row.reasons || []).join('; ')}`);
    });
    if ((data.rows || []).length > 8) lines.push('… xem trước tối đa 8 dòng có vấn đề.');
    if (data.warnings && data.warnings.length) lines.push('', 'Cảnh báo:', ...data.warnings.slice(0, 5));
    return lines.join('\n');
  };

  try {
    showToast('⏳ Đang kiểm tra trước file Excel...', 'info');
    const preview = await sendImport(true);
    const summary = describePreview(preview);
    if (!(preview.matched > 0) || !confirm(`${summary}\n\nÁp dụng các dòng hợp lệ?`)) return;

    showToast('⏳ Đang nhập Excel...', 'info');
    const data = await sendImport(false);

    let msg = `✅ Đã cập nhật ${data.updated || 0}/${data.total_rows || 0} dòng Excel vào dự án [${proj}].`;
    if (data.skipped) msg += `\nBỏ qua ${data.skipped} dòng không khớp.`;
    if (data.error_count) msg += `\nCó ${data.error_count} dòng lỗi, không được áp dụng.`;
    if (data.adjusted_schedules) msg += `\n⏰ Đã tự dời ${data.adjusted_schedules} lịch để giãn cách tối thiểu 30 phút.`;
    if (data.warnings && data.warnings.length) {
      msg += '\n\n⚠️ Lưu ý:\n' + data.warnings.slice(0, 8).map(w => `• ${w}`).join('\n');
      if (data.warning_count > 8) msg += `\n… và ${data.warning_count - 8} cảnh báo khác (xem Nhật ký).`;
    }
    alert(msg);
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi nhập Excel: ' + e.message);
  } finally {
    event.target.value = '';
  }
}

let gBulkDescriptionIds = [];
let gBulkDescriptionMode = 'manual';
let gBulkDescriptionProject = '';
let gBulkDescriptionBusy = false;
let gBulkDescriptionPending = null;

function openBulkDescriptionModal() {
  if (gBulkDescriptionBusy) return;
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Không có video nào để ghi mô tả.');
    return;
  }
  const byId = new Map((gQueueItems || []).map(item => [Number(item.id), item]));
  const items = ids.map(id => byId.get(Number(id))).filter(Boolean);
  if (!items.length) {
    alert('Không tìm thấy dữ liệu các video đã chọn. Hãy tải lại hàng đợi.');
    return;
  }

  gBulkDescriptionIds = items.map(item => Number(item.id));
  gBulkDescriptionProject = document.getElementById('sel-project').value;
  document.getElementById('bulk-desc-target').textContent =
    `${items.length} video ${gSelectedIds.size ? 'đã chọn' : 'đang hiển thị'}. Chỉ sửa mô tả trong hàng đợi; không cập nhật video đã đăng trên YouTube. Tiêu đề, kênh và lịch đăng được giữ nguyên.`;
  const container = document.getElementById('bulk-desc-manual-rows');
  container.replaceChildren();
  items.forEach(item => {
    const wrapper = document.createElement('div');
    const label = document.createElement('label');
    label.textContent = `#${item.id} — ${item.title || 'Không có tiêu đề'}`;
    const input = document.createElement('textarea');
    input.rows = 3;
    input.className = 'bulk-desc-input';
    input.id = `bulk-description-${item.id}`;
    label.htmlFor = input.id;
    input.oninput = invalidateBulkDescriptionPreview;
    input.dataset.id = String(item.id);
    input.value = item.description || '';
    input.maxLength = 5000;
    wrapper.appendChild(label);
    wrapper.appendChild(input);
    container.appendChild(wrapper);
  });
  document.getElementById('bulk-desc-excel-file').value = '';
  document.getElementById('bulk-desc-shared').value = '';
  setBulkDescriptionMode('manual');
  document.getElementById('modal-bulk-description').style.display = 'flex';
  document.getElementById('bulk-desc-shared').focus();
}

function closeBulkDescriptionModal() {
  if (gBulkDescriptionBusy) return;
  invalidateBulkDescriptionPreview();
  document.getElementById('modal-bulk-description').style.display = 'none';
}

function invalidateBulkDescriptionPreview() {
  gBulkDescriptionPending = null;
  document.getElementById('bulk-desc-apply').disabled = true;
  document.getElementById('bulk-desc-preview').replaceChildren();
}

function fillSharedBulkDescription() {
  const value = document.getElementById('bulk-desc-shared').value.trim();
  if (!value) return alert('Hãy nhập mô tả dùng chung.');
  document.querySelectorAll('#bulk-desc-manual-rows textarea').forEach(input => { input.value = value; });
  invalidateBulkDescriptionPreview();
}

function setBulkDescriptionBusy(busy) {
  gBulkDescriptionBusy = busy;
  document.getElementById('modal-bulk-description').setAttribute('aria-busy', String(busy));
  document.querySelectorAll('#modal-bulk-description button, #modal-bulk-description input, #modal-bulk-description textarea')
    .forEach(element => { element.disabled = busy; });
  document.getElementById('bulk-desc-apply').disabled = busy || !gBulkDescriptionPending;
}

function setBulkDescriptionMode(mode) {
  if (gBulkDescriptionBusy) return;
  gBulkDescriptionMode = mode === 'excel' ? 'excel' : 'manual';
  const isExcel = gBulkDescriptionMode === 'excel';
  document.getElementById('bulk-desc-manual-panel').style.display = isExcel ? 'none' : 'block';
  document.getElementById('bulk-desc-excel-panel').style.display = isExcel ? 'block' : 'none';
  document.getElementById('bulk-desc-manual-tab').className = isExcel ? 'btn btn-outline btn-sm' : 'btn btn-primary btn-sm';
  document.getElementById('bulk-desc-excel-tab').className = isExcel ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
  invalidateBulkDescriptionPreview();
}

function createBulkDescriptionSender() {
  const projectName = gBulkDescriptionProject;
  const ids = gBulkDescriptionIds.slice();
  if (gBulkDescriptionMode === 'excel') {
    const fileInput = document.getElementById('bulk-desc-excel-file');
    const file = fileInput.files && fileInput.files[0];
    if (!file) throw new Error('Hãy chọn file Excel có cột ID và Mô tả.');
    return (previewOnly, revision = '') => {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('project_name', projectName);
      formData.append('ids', JSON.stringify(ids));
      formData.append('preview_only', previewOnly ? 'true' : 'false');
      formData.append('preview_revision', revision);
      return apiFetch('/api/queue/import-descriptions', { method: 'POST', body: formData });
    };
  }

  const items = Array.from(document.querySelectorAll('#bulk-desc-manual-rows .bulk-desc-input'))
    .map(input => ({
      id: Number(input.dataset.id),
      description: input.value.trim(),
    }))
    .filter(item => item.description);
  if (!items.length) throw new Error('Hãy nhập ít nhất một mô tả.');
  return (previewOnly, revision = '') => apiFetch('/api/queue/bulk-descriptions', {
    method: 'POST',
    body: JSON.stringify({
      project_name: projectName,
      ids,
      items,
      preview_only: previewOnly,
      preview_revision: revision,
    }),
  });
}

function renderBulkDescriptionPreview(data) {
  const container = document.getElementById('bulk-desc-preview');
  container.replaceChildren();
  const summary = document.createElement('div');
  summary.style.marginBottom = '8px';
  summary.textContent = data.preview_only
    ? `Xem trước: ${data.pending || 0} dòng sẽ đổi, ${data.unchanged || 0} dòng giữ nguyên, ${data.skipped || 0} dòng bỏ qua, ${data.error_count || 0} dòng lỗi.`
    : `Kết quả: đã cập nhật ${data.updated || 0}, giữ nguyên ${data.unchanged || 0}, bỏ qua ${data.skipped || 0}, lỗi ${data.error_count || 0}.`;
  container.appendChild(summary);
  const note = document.createElement('div');
  note.textContent = 'Xem nhanh tối đa 100 dòng, 160 ký tự mỗi mô tả; nội dung đầy đủ sẽ được lưu.';
  note.style.color = 'var(--text-muted)';
  container.appendChild(note);

  const rows = data.rows || [];
  if (!rows.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  wrap.style.maxHeight = '260px';
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['ID', 'Trạng thái', 'Mô tả / lý do'].forEach(text => {
    const cell = document.createElement('th');
    cell.textContent = text;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement('tbody');
  const labels = { ready: 'Sẽ cập nhật', updated: 'Đã cập nhật', unchanged: 'Giữ nguyên', skipped: 'Bỏ qua', error: 'Lỗi' };
  rows.slice(0, 100).forEach(row => {
    const tr = document.createElement('tr');
    [row.queue_id ? `#${row.queue_id}` : `Dòng ${row.line}`, labels[row.state] || row.state, row.reason || row.preview || ''].forEach(text => {
      const td = document.createElement('td');
      td.textContent = String(text);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  wrap.appendChild(table);
  container.appendChild(wrap);
}

async function previewBulkDescriptions() {
  if (gBulkDescriptionBusy) return;
  invalidateBulkDescriptionPreview();
  setBulkDescriptionBusy(true);
  try {
    const send = createBulkDescriptionSender();
    const preview = await send(true);
    renderBulkDescriptionPreview(preview);
    if (preview.pending > 0) gBulkDescriptionPending = { send, preview };
    document.getElementById('bulk-desc-preview').scrollIntoView({ block: 'nearest' });
  } catch (e) {
    alert('Lỗi xem trước mô tả: ' + e.message);
  } finally {
    setBulkDescriptionBusy(false);
  }
}

async function saveBulkDescriptions() {
  if (gBulkDescriptionBusy || !gBulkDescriptionPending) return;
  const { send, preview } = gBulkDescriptionPending;
  const message = `Sẽ cập nhật ${preview.pending} mô tả; giữ nguyên ${preview.unchanged || 0}, bỏ qua ${preview.skipped || 0}, lỗi ${preview.error_count || 0}.\n\nXác nhận chỉ lưu mô tả trong hàng đợi?`;
  if (!confirm(message)) return;
  setBulkDescriptionBusy(true);
  try {
    const result = await send(false, preview.preview_revision);
    gBulkDescriptionPending = null;
    renderBulkDescriptionPreview(result);
    await loadQueue(true);
    const current = new Map(gQueueItems.map(item => [Number(item.id), item.description || '']));
    document.querySelectorAll('#bulk-desc-manual-rows textarea').forEach(input => {
      if (current.has(Number(input.dataset.id))) input.value = current.get(Number(input.dataset.id));
    });
    showToast(`Đã cập nhật ${result.updated || 0} mô tả.`, 'success');
  } catch (e) {
    invalidateBulkDescriptionPreview();
    alert('Lỗi ghi mô tả hàng loạt: ' + e.message);
  } finally {
    setBulkDescriptionBusy(false);
  }
}

function exportCsv() {
  const proj = document.getElementById('sel-project').value || 'Default';
  downloadWithToken(`/api/queue/export-csv?project=${encodeURIComponent(proj)}`, 'YouTube_Queue.csv');
}

function triggerImportCsv() {
  const fileInput = document.getElementById('file-import-csv');
  if (fileInput) {
    fileInput.value = '';
    fileInput.click();
  }
}

async function handleImportCsv(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;

  const proj = document.getElementById('sel-project').value || 'Default';
  const reader = new FileReader();
  reader.onload = async (e) => {
    try {
      const csvText = e.target.result;
      const sendImport = (preview) => apiFetch('/api/queue/import-csv', {
        method: 'POST',
        body: JSON.stringify({ project_name: proj, csv_text: csvText, preview }),
      });
      const preview = await sendImport(true);
      const lines = [
        `Hợp lệ: ${preview.valid_count || 0}`,
        `Trùng: ${preview.duplicate_count || 0}`,
        `Lỗi: ${preview.error_count || 0}`,
      ];
      (preview.rows || []).filter(row => row.state !== 'valid').slice(0, 8).forEach(row => {
        lines.push(`Dòng ${row.line}: ${(row.reasons || []).join('; ')}`);
      });
      if (!(preview.valid_count > 0) || !confirm(`${lines.join('\n')}\n\nNhập các dòng hợp lệ?`)) return;
      const res = await sendImport(false);
      alert(`Đã nhập ${res.added || 0} video, trùng ${res.duplicate_count || 0}, lỗi ${res.error_count || 0} dòng trong dự án [${proj}].`);
      await loadProjects();
      await loadQueue();
    } catch (err) {
      alert('Lỗi nhập CSV: ' + err.message);
    }
  };
  reader.readAsText(file, 'UTF-8');
}

// Cùng thuật toán với backend (_resolve_target_channel): xác định kênh đích thực tế
// của từng video để chặn sớm khi kênh thiếu Channel ID (backend vẫn fail-closed).
function findChannelIdBlockers(ids, forceReupload) {
  const channels = Array.isArray(gChannels) ? gChannels.slice() : [];
  if (!channels.length) return [];

  const byId = new Map();
  const byProfile = new Map();
  const byGroup = new Map();
  channels.forEach(c => {
    byId.set(Number(c.id), c);
    const prof = String(c.profile_name || '');
    if (!byProfile.has(prof)) byProfile.set(prof, []);
    byProfile.get(prof).push(c);
    (c.group_names || [c.group_name]).filter(Boolean).forEach(grp => {
      const name = String(grp || '');
      if (!byGroup.has(name)) byGroup.set(name, []);
      byGroup.get(name).push(c);
    });
  });
  // Backend quay vòng theo ORDER BY id ASC; /api/channels trả id DESC nên phải sắp lại.
  [byProfile, byGroup].forEach(m => m.forEach(list => list.sort((a, b) => Number(a.id) - Number(b.id))));

  const blockers = [];
  ids.forEach(id => {
    const item = gQueueItems.find(v => Number(v.id) === Number(id));
    if (!item) return;
    const status = String(item.status || '').trim();
    const published = String(item.published_url || '').trim();
    // Các video backend sẽ bỏ qua trước khi chọn kênh thì không chặn oan.
    if (!forceReupload && (status === 'Completed' || published)) return;
    if (!forceReupload && (status.startsWith('Uploading') || status.startsWith('Đang đăng'))) return;

    let target = null;
    if (item.assigned_channel_id) {
      target = byId.get(Number(item.assigned_channel_id)) || null;
    } else if (item.target_channel_id) {
      target = byId.get(Number(item.target_channel_id)) || null;
    } else if (String(item.target_profile_name || '').trim()) {
      const list = byProfile.get(String(item.target_profile_name).trim()) || [];
      if (list.length === 1) target = list[0];
    } else {
      const groups = groupNamesFromItem(item);
      const list = [];
      const seen = new Set();
      groups.forEach(group => (byGroup.get(group) || []).forEach(channel => {
        if (!seen.has(Number(channel.id))) { seen.add(Number(channel.id)); list.push(channel); }
      }));
      list.sort((a, b) => Number(a.id) - Number(b.id));
      if (list.length) {
        const key = Number(item.id);
        target = list[(Number.isFinite(key) ? Math.abs(key) : 0) % list.length];
      }
    }
    if (target && !String(target.channel_id || '').trim()) {
      blockers.push({ item, target });
    }
  });
  return blockers;
}

async function startPipeline(doDownload, doRender, doUpload, overrideIds = null) {
  let previewToken = '';
  let ids;
  if (Array.isArray(overrideIds) && overrideIds.length > 0) {
    ids = overrideIds.slice();
  } else {
    ids = getTargetIdsOrAlert();
    if (!ids.length) {
      alert('Chưa có video nào để chạy.');
      return;
    }
    if (!gSelectedIds.size && !confirm(`Bạn chưa chọn thủ công. Hệ thống sẽ chạy ${ids.length} video đang hiển thị theo bộ lọc hiện tại. Tiếp tục?`)) return;
  }

  if (doUpload) {
    const published = gQueueItems.filter(v => ids.includes(v.id)
      && (v.status === 'Completed' || (v.published_url || '').trim()));
    if (published.length > 0) {
      alert(
        `${published.length} video đã đăng cần được cho phép đăng lại riêng từng dòng bằng nút 🔁. ` +
        `Các video đó sẽ bị bỏ qua trong lượt chạy này.`
      );
      const publishedIds = new Set(published.map(v => Number(v.id)));
      ids = ids.filter(id => !publishedIds.has(Number(id)));
      if (!ids.length) return;
    }
    try {
      const preview = await apiFetch('/api/queue/preview-distribution', {
        method: 'POST',
        body: JSON.stringify({ ids, do_download: doDownload, do_render: doRender, do_upload: doUpload }),
      });
      const previewText = formatDistributionPreview(preview);
      if (preview.blocked) {
        alert(`⛔ Có ${preview.blocked} video chưa đủ điều kiện. Sửa các dòng bị chặn rồi xem lại preview.\n\n${previewText}`);
        return;
      }
      if (!preview.preview_id || !await showDistributionDialog(previewText, true)) return;
      previewToken = preview.preview_id;
    } catch (e) {
      alert('Không xem được preview phân phối: ' + e.message);
      return;
    }
  }

  try {
    const res = await apiFetch('/api/pipeline/start', {
      method: 'POST',
      body: JSON.stringify({
        ids,
        do_download: doDownload,
        do_render: doRender,
        do_upload: doUpload,
        force_reupload: false,
        preview_id: previewToken,
      }),
    });
    if (!res.ok) {
      alert(res.message);
      return;
    }
    if (res.replayed && res.state === 'interrupted') {
      alert('Lượt trước đã bị ngắt. Hãy kiểm tra hàng đợi và xem trước lại các video chưa đăng.');
    }
    if (res.skipped && res.skipped.length) {
      showToast(res.message || `Đã bỏ qua ${res.skipped.length} video đã đăng/đang upload dở.`, 'warning');
    }
  } catch (e) {
    alert('Lỗi khởi chạy: ' + e.message);
  }
}

function formatDistributionPreview(preview) {
  const rows = (preview && preview.items) || [];
  const profiles = new Map();
  const channels = new Map();
  const lines = rows.map(item => {
    const target = item.channel_name ? `${item.channel_name} (${item.youtube_channel_id || '#' + item.channel_id})` : 'chưa có kênh';
    const profile = [item.profile_name, item.antidetect_profile_id].filter(Boolean).join(' / ') || 'profile mặc định';
    if (!item.blocked) {
      profiles.set(profile, (profiles.get(profile) || 0) + 1);
      channels.set(target, (channels.get(target) || 0) + 1);
    }
    const groups = item.groups && item.groups.length ? ` · nhóm ${item.groups.join(' + ')}` : '';
    const schedule = item.visibility === 'schedule' ? ` · ⏰ ${item.schedule_time || 'thiếu lịch'}` : ` · ${item.visibility}`;
    const prefix = item.blocked ? '⛔' : '•';
    const suffix = [...(item.reasons || []), ...(item.warnings || []).map(w => `cảnh báo: ${w}`)];
    return `${prefix} #${item.id} ${item.title || ''} → ${target} · ${profile}${groups}${schedule}${suffix.length ? `\n    ${suffix.join('; ')}` : ''}`;
  });
  const totals = entries => Array.from(entries, ([name, count]) => `${name}: ${count}`).join('\n');
  return `Theo profile\n${totals(profiles)}\n\nTheo kênh\n${totals(channels)}\n\n${lines.join('\n')}`;
}

function showDistributionDialog(text, canStart = false) {
  const dialog = document.createElement('dialog');
  dialog.setAttribute('aria-label', 'Xem trước phân công YouTube');
  dialog.style.cssText = 'max-width: min(1000px, 92vw); padding: 1rem;';
  const content = document.createElement('pre');
  content.style.cssText = 'white-space: pre-wrap; overflow: auto; max-height: 65vh;';
  content.textContent = text;
  const actions = document.createElement('form');
  actions.method = 'dialog';
  const close = document.createElement('button');
  close.value = 'close';
  close.textContent = 'Đóng';
  actions.append(close);
  if (canStart) {
    const start = document.createElement('button');
    start.value = 'start';
    start.textContent = 'Xác nhận phân công và chạy';
    actions.append(start);
  }
  dialog.append(content, actions);
  document.body.append(dialog);
  return new Promise(resolve => {
    dialog.addEventListener('close', () => {
      resolve(dialog.returnValue === 'start');
      dialog.remove();
    }, { once: true });
    dialog.showModal();
  });
}

async function previewSelectedDistribution() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) return alert('Chưa có video nào để preview.');
  try {
    const preview = await apiFetch('/api/queue/preview-distribution', {
      method: 'POST', body: JSON.stringify({ ids }),
    });
    await showDistributionDialog(`Preview ${preview.total} video · bị chặn ${preview.blocked} · cảnh báo ${preview.warning_count}\n\n${formatDistributionPreview(preview)}`);
  } catch (e) { alert('Không xem được preview: ' + e.message); }
}

async function stopPipeline() {
  try {
    showToast('⏳ Đang phát lệnh dừng khẩn cấp...', 'warning');
    const res = await apiFetch('/api/pipeline/stop', { method: 'POST' });
    showToast('🛑 Đã dừng toàn bộ tiến trình render và upload!', 'warning');
    await loadQueue(true);
    await loadLogs();
  } catch (e) {
    showToast('❌ Lỗi dừng tiến trình: ' + e.message, 'error');
  }
}

async function reviewUploadSlot() {
  try {
    const slot = await apiFetch('/api/pipeline/upload-slot');
    if (slot.state === 'recovery_required') {
      const confirmed = confirm(
        `Lượt upload của video #${slot.queue_id} cần phục hồi.\n\n` +
        'Hãy kiểm tra Studio và đóng tab của lượt upload cũ trước khi xác nhận. ' +
        'Sau khi xác nhận, tool chờ 30 phút. Video cần đối soát vẫn giữ nguyên trạng thái.\n\n' +
        'Bạn đã kiểm tra và phiên upload cũ đã kết thúc?'
      );
      if (!confirmed) return;
      await apiFetch('/api/pipeline/upload-slot/acknowledge', {
        method: 'POST', body: JSON.stringify({ recovery_id: slot.recovery_id, previous_session_closed: true }),
      });
      showToast('Đã xác nhận phục hồi. Lượt tiếp theo chờ ít nhất 30 phút.');
      return;
    }
    if (slot.state === 'active') {
      alert(`Video #${slot.queue_id} đang giữ lượt upload.`);
    } else if (slot.state === 'cooldown') {
      alert('Đang kiểm tra khoảng nghỉ 30 phút trước lượt tiếp theo.');
    } else {
      alert('Lượt upload đang trống.');
    }
  } catch (e) {
    alert('Không đọc được trạng thái lượt upload: ' + e.message);
  }
}

async function runAIProcess(type) {
  const ids = getTargetIdsOrAlert();
  const payload = {
    ids,
    project_name: document.getElementById('sel-project').value || 'Default',
    mode: type === 'batch_edit' ? 'batch_edit' : document.getElementById('ai-mode').value,
    provider: document.getElementById('ai-provider').value,
    target_lang: document.getElementById('ai-lang').value,
    custom_prompt: document.getElementById('ai-prompt').value,
    edit_field: document.getElementById('ed-field').value,
    edit_op: document.getElementById('ed-op').value,
    find_str: document.getElementById('ed-find').value,
    replace_str: document.getElementById('ed-replace').value,
  };
  try {
    const res = await apiFetch('/api/queue/ai-process', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (payload.mode === 'batch_edit') {
      alert(`Đã biên tập ${res.updated} video.`);
    } else {
      const actual = Object.entries(res.provider_counts || {}).map(([name, count]) => `${name}: ${count}`).join(', ') || 'không có video';
      const fallback = Number(res.fallback_count || 0);
      const failed = res.failed || [];
      const failureMessage = failed.length
        ? `\nKhông cập nhật được ${failed.length} video (ID: ${failed.map(item => item.id).join(', ')}); metadata gốc được giữ.`
        : '';
      alert(`Đã cập nhật ${res.updated} video.\nYêu cầu: ${res.provider_requested || payload.provider}\nThực tế: ${actual}${fallback ? `\n${fallback} video chuyển sang Google Translate.` : ''}${res.links_preserved ? `\nĐã giữ ${res.links_preserved} link từ mô tả gốc.` : ''}${failureMessage}`);
    }
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi xử lý AI/Biên tập: ' + e.message);
  }
}

async function loadRenderResources() {
  const data = await apiFetch('/api/render-resources');
  const res = data.resources || {};
  gFFmpegPresets = res.ffmpeg_presets || [];

  document.getElementById('rc-bg-video').innerHTML =
    (res.background_video || []).map(v => `<option value="${escapeHtml(v.name)}">${escapeHtml(v.name)}</option>`).join('');
  document.getElementById('rc-bg-music').innerHTML =
    `<option value="">-- Giữ âm thanh gốc (Không chèn nhạc nền) --</option>` +
    (res.background_music || []).map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}</option>`).join('');

  // Kho tài nguyên cho lớp phủ branding (kế thừa DHB)
  const wmSel = document.getElementById('rc-wm-image');
  if (wmSel) wmSel.innerHTML =
    `<option value="">-- Không dùng watermark ảnh --</option>` +
    (res.images || []).map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}</option>`).join('');
  const logoSel = document.getElementById('rc-logo');
  if (logoSel) logoSel.innerHTML =
    `<option value="">-- Không dùng logo video --</option>` +
    (res.logo_videos || []).map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}</option>`).join('');
  const introSel = document.getElementById('rc-intro');
  if (introSel) introSel.innerHTML =
    (res.intros || []).map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}</option>`).join('');
  const outroSel = document.getElementById('rc-outro');
  if (outroSel) outroSel.innerHTML =
    (res.outros || []).map(m => `<option value="${escapeHtml(m.name)}">${escapeHtml(m.name)}</option>`).join('');

  document.getElementById('rc-ffmpeg-preset').innerHTML =
    `<option value="">-- Chọn mẫu lệnh FFmpeg có sẵn từ DHB --</option>` +
    gFFmpegPresets.map((p, idx) => `<option value="${idx}">${escapeHtml(p.name)}</option>`).join('');

  const summaryEl = document.getElementById('dhb-resources-summary');
  const sections = [
    ['🎵 Nhạc Nền (_Backgroud Music)', res.background_music || []],
    ['🌌 Video Nền (_Backgroud Video)', res.background_video || []],
    ['🎬 Video Mở Đầu (_Intro)', res.intros || []],
    ['🏁 Video Kết Thúc (_Outro)', res.outros || []],
    ['🖼️ Lớp Phủ Filter (_Filter)', res.filters || []],
    ['🔤 Phông Chữ (_Font)', res.fonts || []],
  ];
  summaryEl.innerHTML = sections.map(([title, list]) => `
    <div style="margin-bottom:12px;">
      <div style="font-weight:700; color:#fff; margin-bottom:4px;">${title} (${list.length})</div>
      <div style="display:flex; flex-wrap:wrap; gap:6px;">
        ${list.map(item => `<span class="badge badge-wait">${escapeHtml(item.name)}</span>`).join('')}
      </div>
    </div>
  `).join('');
}

function onSelectFFmpegPreset(idxStr) {
  if (idxStr === '') return;
  const p = gFFmpegPresets[parseInt(idxStr, 10)];
  if (p) {
    document.getElementById('rc-custom-cmd').value = p.command;
    document.getElementById('rc-mode').value = 'custom_ffmpeg';
  }
}

async function applyRenderStudioToSelected() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Chưa có video nào trong dự án để áp dụng.');
    return;
  }
  const renderSpeed = readRenderSpeedOrAlert();
  if (renderSpeed === null) return;
  const mode = document.getElementById('rc-mode').value;
  const cfg = {
    speed: renderSpeed,
    volume: parseFloat(document.getElementById('rc-volume').value || '1.0'),
    brightness: parseFloat(document.getElementById('rc-brightness').value || '0.0'),
    bg_video_name: document.getElementById('rc-bg-video').value,
    bg_music_name: document.getElementById('rc-bg-music').value,
    hflip: document.getElementById('rc-hflip').checked,
    audio_pitch_shift: document.getElementById('rc-pitch').checked,
    enforce_shorts_duration: document.getElementById('rc-shorts-enforce') ? document.getElementById('rc-shorts-enforce').checked : true,
    shorts_visual_pop: document.getElementById('rc-shorts-visual') ? document.getElementById('rc-shorts-visual').checked : true,
    custom_command: document.getElementById('rc-custom-cmd').value,
  };
  collectBrandingConfig(cfg);
  collectEditConfig(cfg);
  await apiFetch('/api/queue/bulk-update', {
    method: 'POST',
    body: JSON.stringify({
      ids,
      updates: {
        render_mode: mode,
        render_config_json: JSON.stringify(cfg),
      },
    }),
  });
  alert(`Đã lưu cấu hình Render (${mode}) cho ${ids.length} video!`);
  await loadQueue(true);
}

// ===== LỚP PHỦ BRANDING (kế thừa DHB) =====
function toggleBrandingPanel() {
  const panel = document.getElementById('branding-panel');
  const arrow = document.getElementById('branding-arrow');
  if (!panel) return;
  panel.classList.toggle('open');
  if (arrow) arrow.textContent = panel.classList.contains('open') ? '▲' : '▼';
}

function toggleEditPanel() {
  const panel = document.getElementById('edit-panel');
  const arrow = document.getElementById('edit-arrow');
  if (!panel) return;
  panel.classList.toggle('open');
  if (arrow) arrow.textContent = panel.classList.contains('open') ? '▲' : '▼';
}

function collectEditConfig(cfg) {
  const val = (id, fallback) => {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
  };
  const chk = (id) => {
    const el = document.getElementById(id);
    return el ? el.checked : false;
  };
  cfg.crop_enabled = chk('rc-crop-enable');
  cfg.crop_center = chk('rc-crop-center');
  cfg.crop_size = val('rc-crop-size', '900:506');
  cfg.crop_location = val('rc-crop-pos', '0:0');
  cfg.rotation_angle = parseInt(val('rc-rotation', '0') || '0', 10);
  cfg.pad_enabled = chk('rc-pad-enable');
  cfg.pad_color = val('rc-pad-color', 'FF0000');
  cfg.pad_size = parseInt(val('rc-pad-size', '6') || '0', 10);
  cfg.move_camera = chk('rc-move-camera');
  cfg.move_camera_speed = parseFloat(val('rc-move-speed', '1.0') || '1.0');
}

function fillEditForm(cfg) {
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  };
  setChk('rc-crop-enable', cfg.crop_enabled);
  setChk('rc-crop-center', cfg.crop_center !== false);
  set('rc-crop-size', cfg.crop_size || '900:506');
  set('rc-crop-pos', cfg.crop_location || '0:0');
  set('rc-rotation', cfg.rotation_angle || 0);
  setChk('rc-pad-enable', cfg.pad_enabled);
  set('rc-pad-color', cfg.pad_color || 'FF0000');
  set('rc-pad-size', cfg.pad_size !== undefined ? cfg.pad_size : 6);
  setChk('rc-move-camera', cfg.move_camera);
  set('rc-move-speed', cfg.move_camera_speed !== undefined ? cfg.move_camera_speed : 1.0);
}

function collectBrandingConfig(cfg) {
  const val = (id, fallback) => {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
  };
  const chk = (id) => {
    const el = document.getElementById(id);
    return el ? el.checked : false;
  };
  cfg.watermark_image_name = val('rc-wm-image', '');
  cfg.watermark_image_size = val('rc-wm-size', '240:120');
  cfg.watermark_image_pos = val('rc-wm-pos', '10:10');
  cfg.watermark_image_opacity = parseFloat(val('rc-wm-opacity', '1.0'));
  cfg.logo_video_name = val('rc-logo', '');
  cfg.logo_video_size = val('rc-logo-size', '300:150');
  cfg.logo_video_pos = val('rc-logo-pos', '10:10');
  cfg.logo_video_chroma = val('rc-logo-chroma', '13FE06');
  cfg.logo_video_similarity = 0.4;
  cfg.logo_video_blend = 0.1;
  cfg.watermark_text = val('rc-wm-text', '');
  cfg.watermark_text_size = parseInt(val('rc-wm-text-size', '50'), 10);
  cfg.watermark_text_pos = val('rc-wm-text-pos', '350:80');
  cfg.watermark_text_color = val('rc-wm-text-color', 'white');
  cfg.text_scroll = val('rc-scroll-text', '');
  cfg.text_scroll_size = parseInt(val('rc-scroll-size', '40'), 10);
  cfg.text_scroll_speed = parseInt(val('rc-scroll-speed', '10'), 10);
  cfg.text_scroll_color = val('rc-scroll-color', 'yellow');
  cfg.text_scroll_height = 60;
  cfg.intro_enabled = chk('rc-intro-enable');
  cfg.intro_random = chk('rc-intro-random');
  cfg.intro_name = val('rc-intro', '');
  cfg.outro_enabled = chk('rc-outro-enable');
  cfg.outro_random = chk('rc-outro-random');
  cfg.outro_name = val('rc-outro', '');
  cfg.audio_original_percent = parseInt(val('rc-audio-orig', '100'), 10);
  cfg.bg_music_percent = parseInt(val('rc-audio-bg', '25'), 10);
  cfg.bg_video_random = chk('rc-bg-random');
}

function fillBrandingForm(cfg) {
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  };
  set('rc-wm-image', cfg.watermark_image_name || '');
  set('rc-wm-size', cfg.watermark_image_size || '240:120');
  set('rc-wm-pos', cfg.watermark_image_pos || '10:10');
  set('rc-wm-opacity', cfg.watermark_image_opacity !== undefined ? cfg.watermark_image_opacity : 1.0);
  set('rc-logo', cfg.logo_video_name || '');
  set('rc-logo-size', cfg.logo_video_size || '300:150');
  set('rc-logo-pos', cfg.logo_video_pos || '10:10');
  set('rc-logo-chroma', cfg.logo_video_chroma || '13FE06');
  set('rc-wm-text', cfg.watermark_text || '');
  set('rc-wm-text-size', cfg.watermark_text_size || 50);
  set('rc-wm-text-pos', cfg.watermark_text_pos || '350:80');
  set('rc-wm-text-color', cfg.watermark_text_color || 'white');
  set('rc-scroll-text', cfg.text_scroll || '');
  set('rc-scroll-size', cfg.text_scroll_size || 40);
  set('rc-scroll-speed', cfg.text_scroll_speed || 10);
  set('rc-scroll-color', cfg.text_scroll_color || 'yellow');
  setChk('rc-intro-enable', cfg.intro_enabled);
  setChk('rc-intro-random', cfg.intro_random);
  set('rc-intro', cfg.intro_name || '');
  setChk('rc-outro-enable', cfg.outro_enabled);
  setChk('rc-outro-random', cfg.outro_random);
  set('rc-outro', cfg.outro_name || '');
  set('rc-audio-orig', cfg.audio_original_percent !== undefined ? cfg.audio_original_percent : 100);
  set('rc-audio-bg', cfg.bg_music_percent !== undefined ? cfg.bg_music_percent : 25);
  setChk('rc-bg-random', cfg.bg_video_random);
}

// ===== PRESET RENDER CÓ TÊN (học quy trình preset JSON của DHB) =====
async function loadRenderPresets() {
  try {
    gRenderPresets = await apiFetch('/api/render-presets');
    const sel = document.getElementById('rp-select');
    if (sel) {
      sel.innerHTML = gRenderPresets.length
        ? gRenderPresets.map(p => `<option value="${p.id}">${escapeHtml(p.name)} (${escapeHtml(p.render_mode)})</option>`).join('')
        : `<option value="">-- Chưa có preset nào --</option>`;
    }
  } catch (e) {
    console.error('Lỗi loadRenderPresets:', e);
  }
}

function onSelectRenderPreset() {}

async function saveRenderPreset() {
  const name = document.getElementById('rp-name').value.trim();
  if (!name) {
    alert('Nhập tên preset trước khi lưu!');
    return;
  }
  const renderSpeed = readRenderSpeedOrAlert();
  if (renderSpeed === null) return;
  const mode = document.getElementById('rc-mode').value;
  const cfg = {
    speed: renderSpeed,
    volume: parseFloat(document.getElementById('rc-volume').value || '1.0'),
    brightness: parseFloat(document.getElementById('rc-brightness').value || '0.0'),
    bg_video_name: document.getElementById('rc-bg-video').value,
    bg_music_name: document.getElementById('rc-bg-music').value,
    hflip: document.getElementById('rc-hflip').checked,
    audio_pitch_shift: document.getElementById('rc-pitch').checked,
    enforce_shorts_duration: document.getElementById('rc-shorts-enforce').checked,
    shorts_visual_pop: document.getElementById('rc-shorts-visual').checked,
    custom_command: document.getElementById('rc-custom-cmd').value,
  };
  collectBrandingConfig(cfg);
  collectEditConfig(cfg);
  try {
    await apiFetch('/api/render-presets', {
      method: 'POST',
      body: JSON.stringify({ name, render_mode: mode, config: cfg }),
    });
    showToast(`💾 Đã lưu Preset Render "${name}"!`, 'info');
    document.getElementById('rp-name').value = '';
    await loadRenderPresets();
  } catch (e) {
    alert('Lỗi lưu preset: ' + e.message);
  }
}

function loadPresetToForm() {
  const id = parseInt(document.getElementById('rp-select').value, 10);
  const p = gRenderPresets.find(x => x.id === id);
  if (!p) {
    alert('Chưa chọn preset hợp lệ.');
    return;
  }
  document.getElementById('rc-mode').value = p.render_mode || 'shorts_9_16';
  const cfg = p.config || {};
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  };
  set('rc-speed', cfg.speed !== undefined ? cfg.speed : 1.0);
  set('rc-volume', cfg.volume !== undefined ? cfg.volume : 1.0);
  set('rc-brightness', cfg.brightness !== undefined ? cfg.brightness : 0.0);
  set('rc-bg-video', cfg.bg_video_name || '');
  set('rc-bg-music', cfg.bg_music_name || '');
  setChk('rc-hflip', cfg.hflip);
  setChk('rc-pitch', cfg.audio_pitch_shift);
  setChk('rc-shorts-enforce', cfg.enforce_shorts_duration !== false);
  setChk('rc-shorts-visual', cfg.shorts_visual_pop !== false);
  set('rc-custom-cmd', cfg.custom_command || '');
  fillBrandingForm(cfg);
  fillEditForm(cfg);
  showToast(`📋 Đã nạp preset "${p.name}" vào form — bấm "Áp Dụng" để gán cho video.`, 'info');
}

async function deleteRenderPreset() {
  const id = parseInt(document.getElementById('rp-select').value, 10);
  const p = gRenderPresets.find(x => x.id === id);
  if (!p) {
    alert('Chưa chọn preset để xóa.');
    return;
  }
  if (!confirm(`Xóa preset "${p.name}"?`)) return;
  await apiFetch(`/api/render-presets/${id}`, { method: 'DELETE' });
  showToast('🗑️ Đã xóa preset.', 'warning');
  await loadRenderPresets();
}

async function applyShortsBooster() {
  const ids = getTargetIdsOrAlert();
  if (!ids.length) {
    alert('Vui lòng chọn ít nhất 1 video hoặc nạp video vào hàng đợi để tối ưu.');
    return;
  }
  const confirmed = confirm(
    `⚡ KÍCH HOẠT SHORTS BOOSTER CHO ${ids.length} VIDEO:\n` +
    `- Tự động chuyển chế độ: Render Dọc 9:16 (1080x1920 Full HD)\n` +
    `- Khống chế thời lượng <= 58.5s (Tự tăng tốc mượt nếu 60-72s để 100% lọt Shorts Feed)\n` +
    `- Bật bộ lọc màu OLED di động & độ nét cao (Visual Pop)\n` +
    `- Chụp ảnh đại diện tại điểm vàng (25% thời lượng video)\n` +
    `- Tự động thêm hashtag #shorts vào tiêu đề\n` +
    `- Đăng ở chế độ máy tính (Native Desktop) — an toàn chống Checkpoint\n\n` +
    `Bạn có muốn áp dụng ngay không?`
  );
  if (!confirmed) return;

  try {
    const res = await apiFetch('/api/queue/shorts-boost', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    });
    alert(`🎉 Đã tối ưu hóa chuẩn Shorts 100% cho ${res.updated} video!`);
    await loadQueue(true);
  } catch (e) {
    alert('Lỗi áp dụng Shorts Booster: ' + e.message);
  }
}

async function loadMonitors() {
  const list = await apiFetch('/api/monitors');
  const tbody = document.getElementById('monitors-tbody');
  tbody.innerHTML = list.map(m => {
    const transitioning = ['Starting', 'Stopping'].includes(m.lifecycle_state);
    const actionLabel = m.lifecycle_state === 'Stopping' ? '⏳ Đang dừng...' :
      m.lifecycle_state === 'Starting' ? '⏳ Đang khởi động...' :
      m.is_running ? '⏹️ Dừng' : '▶️ Chạy 24/7';
    return `
    <tr>
      <td>#${m.id}</td>
      <td><b>${escapeHtml(m.name)}</b><br/><span style="font-size:11px;color:var(--text-muted)">${escapeHtml((m.source_urls || '').split('\n')[0])}</span></td>
      <td><span class="badge badge-video">${escapeHtml(m.target_group_name || 'Mặc định')}</span></td>
      <td>${m.check_interval_minutes} phút</td>
      <td>
        <span class="badge ${m.is_running ? 'badge-ok' : 'badge-wait'}">${escapeHtml(m.status)}</span>
        ${m.last_checked_at ? `<div style="font-size:10.5px;color:var(--text-muted)">${escapeHtml(m.last_checked_at)}</div>` : ''}
      </td>
      <td>
        <button class="btn ${m.is_running ? 'btn-warning' : 'btn-success'} btn-sm" onclick="toggleMonitor(${m.id})" ${transitioning ? 'disabled' : ''}>
          ${actionLabel}
        </button>
        <button class="btn btn-outline btn-sm" onclick="deleteMonitor(${m.id})" ${m.can_delete ? '' : 'disabled'} title="${m.can_delete ? 'Xóa monitor' : 'Dừng monitor và đợi worker kết thúc trước khi xóa'}">🗑️</button>
      </td>
    </tr>
  `;
  }).join('');
  if (gMonitorLifecycleTimer) clearTimeout(gMonitorLifecycleTimer);
  gMonitorLifecycleTimer = null;
  if (list.some(m => ['Starting', 'Stopping'].includes(m.lifecycle_state))) {
    gMonitorLifecycleTimer = setTimeout(() => {
      gMonitorLifecycleTimer = null;
      loadMonitors().catch(error => console.error('Không thể cập nhật trạng thái Monitor:', error));
    }, 1000);
  }
}

async function addMonitor() {
  const payload = {
    name: document.getElementById('mon-name').value,
    source_urls: document.getElementById('mon-urls').value,
    target_channel_id: document.getElementById('mon-channel').value ? parseInt(document.getElementById('mon-channel').value, 10) : null,
    target_group_name: document.getElementById('mon-group').value || 'Mặc định',
    render_mode: document.getElementById('mon-render').value,
    option_group_id: (() => {
      const el = document.getElementById('mon-option-group');
      return el && el.value ? parseInt(el.value, 10) : null;
    })(),
    ai_Action: document.getElementById('mon-ai-action').value,
    ai_target_lang: document.getElementById('mon-ai-lang').value,
    check_interval_minutes: parseInt(document.getElementById('mon-interval').value || '30', 10),
    max_videos_per_scan: parseInt(document.getElementById('mon-max').value || '3', 10),
  };
  try {
    await apiFetch('/api/monitors', { method: 'POST', body: JSON.stringify(payload) });
    document.getElementById('mon-name').value = '';
    await loadMonitors();
  } catch (e) {
    alert(e.message);
  }
}

async function toggleMonitor(id) {
  try {
    const result = await apiFetch(`/api/monitors/${id}/toggle`, { method: 'POST' });
    if (result.message) showToast(result.message, result.ok ? 'success' : 'warning');
  } catch (e) {
    alert(e.message);
  } finally {
    await loadMonitors();
  }
}

async function deleteMonitor(id) {
  if (!confirm('Xóa luồng Auto Monitor này?')) return;
  try {
    await apiFetch(`/api/monitors/${id}`, { method: 'DELETE' });
    await loadMonitors();
  } catch (e) {
    alert(e.message);
  }
}

async function loadAppSettings() {
  const s = await apiFetch('/api/settings');
  const setSecretInput = (id, configured) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = configured ? '•••••••• (đã lưu — để trống nếu không đổi)' : 'Chưa cấu hình';
  };
  setSecretInput('set-gemini-key', s.gemini_api_key_configured);
  setSecretInput('set-openai-key', s.openai_api_key_configured);
  setSecretInput('set-deepseek-key', s.deepseek_api_key_configured);
  document.getElementById('set-gemini-model').value = s.gemini_model || 'gemini-2.5-flash';
  document.getElementById('set-threads').value = s.max_concurrent_threads || 2;
  document.getElementById('set-render-threads').value = s.max_concurrent_renders || 2;
  document.getElementById('set-delay').value = s.upload_delay_seconds || 10;
  document.getElementById('set-checks-timeout').value = s.checks_timeout_seconds || 120;
  document.getElementById('set-monitor-gap').value = s.monitor_upload_gap_minutes !== undefined ? s.monitor_upload_gap_minutes : 30;
  document.getElementById('set-max-uploads').value = s.max_uploads_per_run !== undefined ? s.max_uploads_per_run : 0;
  document.getElementById('set-thumb-source').value = s.thumbnail_source || 'reup';
  document.getElementById('set-tiktok-res').value = s.download_resolution_tiktok || '720p';
  document.getElementById('set-download-cookies').value = s.download_cookies_file || '';
  document.getElementById('set-hide-browser').checked = !!s.hide_browser;
  document.getElementById('set-del-temp').checked = s.delete_temp_after_upload !== false;
  document.getElementById('set-external-backup-dir').value = s.external_backup_dir || '';
  document.getElementById('set-external-backup-keep').value = s.external_backup_keep || 10;
  await loadStorageOverview();
}

async function loadDiagnostics() {
  const panel = document.getElementById('diagnostics-summary');
  if (!panel) return;
  panel.textContent = 'Đang kiểm tra phiên bản, database, worker và FFmpeg…';
  try {
    const data = await apiFetch('/api/diagnostics');
    const db = data.database || {};
    const workers = data.workers || {};
    const storage = data.storage || {};
    const heartbeat = data.heartbeat || {};
    const schemaText = (db.schemas || []).map(item => `${item.key}: v${item.version}`).join(', ') || 'chưa có bản ghi schema';
    const toolsText = (data.tools || []).map(tool =>
      `${tool.name}: ${tool.available ? 'OK' : 'LỖI'}${tool.version ? ` — ${tool.version}` : ''}${tool.error ? ` (${tool.error})` : ''}`
    ).join('\n');
    panel.textContent = [
      `Bản dựng: ${data.version || 'không rõ'}`,
      `Database: ${db.available ? 'OK' : 'không tìm thấy'} · ${storageFormatBytes(db.bytes || 0)} · user_version ${db.user_version ?? 0}`,
      `Schema: ${schemaText}`,
      `Worker batch: ${workers.pipeline_running ? 'đang chạy' : 'dừng'} · active ${workers.active_batch_threads || 0}`,
      `Monitor: ${workers.monitor_workers || 0} worker · ${workers.monitor_heartbeats || 0} heartbeat · supervisor ${workers.monitor_supervisor_alive ? 'sống' : 'dừng'}`,
      `NeedsReview: ${data.needs_review_count || 0} · heartbeat mới nhất: ${heartbeat.latest || 'chưa có'} (${heartbeat.running_monitors || 0} monitor bật)`,
      `Ổ đĩa trống: ${storageFormatBytes(storage.free_bytes || 0)} · temp ${storageFormatBytes(storage.temp_bytes || 0)} · output ${storageFormatBytes(storage.output_bytes || 0)} · artifact ${storage.artifact_count || 0}`,
      toolsText,
    ].filter(Boolean).join('\n');
  } catch (error) {
    panel.textContent = `Không đọc được diagnostics: ${error.message}`;
  }
}

async function saveAppSettings() {
  const payload = {
    gemini_model: document.getElementById('set-gemini-model').value,
    max_concurrent_threads: parseInt(document.getElementById('set-threads').value || '2', 10),
    max_concurrent_renders: parseInt(document.getElementById('set-render-threads').value || '2', 10),
    upload_delay_seconds: parseInt(document.getElementById('set-delay').value || '10', 10),
    checks_timeout_seconds: parseInt(document.getElementById('set-checks-timeout').value || '120', 10),
    monitor_upload_gap_minutes: parseInt(document.getElementById('set-monitor-gap').value || '30', 10),
    max_uploads_per_run: parseInt(document.getElementById('set-max-uploads').value || '0', 10),
    thumbnail_source: document.getElementById('set-thumb-source').value || 'reup',
    download_resolution_tiktok: document.getElementById('set-tiktok-res').value || '720p',
    download_cookies_file: document.getElementById('set-download-cookies').value.trim(),
    hide_browser: document.getElementById('set-hide-browser').checked,
    delete_temp_after_upload: document.getElementById('set-del-temp').checked,
    external_backup_dir: document.getElementById('set-external-backup-dir').value.trim(),
    external_backup_keep: parseInt(document.getElementById('set-external-backup-keep').value || '10', 10),
  };
  // Ô secret để trống = giữ nguyên giá trị đã lưu; chỉ gửi khi người dùng nhập key mới.
  const secretFields = {
    gemini_api_key: 'set-gemini-key',
    openai_api_key: 'set-openai-key',
    deepseek_api_key: 'set-deepseek-key',
  };
  const secretPayload = {};
  for (const [key, id] of Object.entries(secretFields)) {
    const value = document.getElementById(id).value.trim();
    if (value) secretPayload[key] = value;
  }
  await apiFetch('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
  if (Object.keys(secretPayload).length) {
    await apiFetch('/api/settings/secrets', { method: 'POST', body: JSON.stringify(secretPayload) });
  }
  await loadAppSettings();
  await loadDiagnostics();
  alert('Đã lưu cấu hình hệ thống thành công!');
}

function readRenderSpeedOrAlert() {
  const speed = Number(document.getElementById('rc-speed').value || '1.0');
  if (!Number.isFinite(speed) || speed < 0.5 || speed > 2.0) {
    alert('Tốc độ render phải nằm trong khoảng 0.5–2.0 để giữ đồng bộ âm thanh và video.');
    return null;
  }
  return speed;
}

function storageFormatBytes(value) {
  let bytes = Math.max(0, Number(value) || 0);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit += 1;
  }
  return `${bytes.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function storageRetentionDays() {
  const days = Number(document.getElementById('storage-retention-days')?.value || 30);
  return Number.isInteger(days) ? Math.max(1, Math.min(365, days)) : 30;
}

function renderStorageSummary(data) {
  const summary = document.getElementById('storage-summary');
  if (!summary) return;
  summary.textContent = `Còn trống: ${storageFormatBytes(data.free_bytes)} · ` +
    `Temp: ${storageFormatBytes(data.temp?.bytes)} (${data.temp?.files || 0} file) · ` +
    `Output: ${storageFormatBytes(data.output?.bytes)} (${data.output?.files || 0} file) · ` +
    `Có thể xem xét: ${data.candidate_count || 0} file / ${storageFormatBytes(data.candidate_bytes)} · ` +
    `Được giữ vì hàng đợi đang tham chiếu: ${data.protected_references || 0} · ` +
    `Log: ${data.logs_count || 0}/${data.logs_retained_limit || 5000} dòng (giới hạn bảo trì)`;
}

function updateStorageCleanupButton() {
  const button = document.getElementById('storage-cleanup-button');
  if (button) button.disabled = !document.querySelector('#storage-preview input[type="checkbox"]:checked');
}

function renderStorageCandidates(data) {
  const host = document.getElementById('storage-preview');
  if (!host) return;
  host.replaceChildren();
  if (!data.candidates?.length) {
    const note = document.createElement('div');
    note.className = 'form-group';
    note.textContent = 'Không có artifact đủ tuổi đang chờ dọn; file chưa có manifest không được đưa vào danh sách.';
    host.appendChild(note);
    updateStorageCleanupButton();
    return;
  }
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const headerRow = document.createElement('tr');
  ['Chọn', 'Tên file', 'Loại', 'Đăng ký lúc', 'Dung lượng'].forEach(label => {
    const th = document.createElement('th');
    th.textContent = label;
    headerRow.appendChild(th);
  });
  head.appendChild(headerRow);
  table.appendChild(head);
  const body = document.createElement('tbody');
  data.candidates.forEach(item => {
    const row = document.createElement('tr');
    const selectCell = document.createElement('td');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.dataset.artifactId = String(item.id);
    checkbox.addEventListener('change', updateStorageCleanupButton);
    selectCell.appendChild(checkbox);
    row.appendChild(selectCell);
    [item.name, item.kind, item.registered_at, storageFormatBytes(item.size_bytes)].forEach(value => {
      const cell = document.createElement('td');
      cell.textContent = String(value ?? '');
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  table.appendChild(body);
  host.appendChild(table);
  updateStorageCleanupButton();
}

async function loadStorageOverview() {
  try {
    const days = storageRetentionDays();
    document.getElementById('storage-retention-days').value = String(days);
    const data = await apiFetch(`/api/storage/overview?retention_days=${days}`);
    renderStorageSummary(data);
  } catch (e) {
    const summary = document.getElementById('storage-summary');
    if (summary) summary.textContent = `Không đọc được thông tin dung lượng: ${e.message}`;
  }
}

async function previewStorageCleanup() {
  try {
    const data = await apiFetch('/api/storage/cleanup-preview', {
      method: 'POST', body: JSON.stringify({ retention_days: storageRetentionDays() }),
    });
    renderStorageSummary(data);
    renderStorageCandidates(data);
  } catch (e) {
    alert(`Không tạo được bản xem trước dọn file: ${e.message}`);
  }
}

async function cleanupSelectedStorage() {
  const selected = [...document.querySelectorAll('#storage-preview input[type="checkbox"]:checked')];
  const artifactIds = selected.map(input => Number(input.dataset.artifactId)).filter(Number.isInteger);
  if (!artifactIds.length) return;
  if (!confirm(`Dọn ${artifactIds.length} artifact đã chọn? Các đường dẫn và hàng đợi sẽ được kiểm tra lại trước khi xóa.`)) return;
  try {
    const result = await apiFetch('/api/storage/cleanup', {
      method: 'POST', body: JSON.stringify({ artifact_ids: artifactIds, retention_days: storageRetentionDays() }),
    });
    alert(`Đã dọn ${result.deleted_count} file (${storageFormatBytes(result.deleted_bytes)}). Bị giữ lại: ${result.skipped.length}.`);
    await previewStorageCleanup();
  } catch (e) {
    alert(`Dọn artifact thất bại: ${e.message}`);
  }
}

async function exportBackupNow() {
  const dir = document.getElementById('set-external-backup-dir').value.trim();
  if (!dir) {
    alert('Nhập thư mục export CSV trước (và bấm Lưu Cài Đặt nếu vừa thay đổi).');
    return;
  }
  const keep = parseInt(document.getElementById('set-external-backup-keep').value || '10', 10);
  try {
    await apiFetch('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ external_backup_dir: dir, external_backup_keep: keep }),
    });
    const res = await apiFetch('/api/backup/export-now', { method: 'POST', body: JSON.stringify({}) });
    alert(`Đã export ${res.tables} bảng (${res.rows} dòng) ra:\n${res.path}`);
  } catch (e) {
    alert('Export thất bại: ' + e.message);
  }
}

let gLogReloadTimer = null;

function scheduleLogsReload() {
  clearTimeout(gLogReloadTimer);
  gLogReloadTimer = setTimeout(loadLogs, 400);
}

function getLogFilters() {
  return {
    module: document.getElementById('log-filter-module')?.value || '',
    status: document.getElementById('log-filter-status')?.value || '',
    q: document.getElementById('log-filter-q')?.value.trim() || '',
  };
}

function buildLogQuery(extra = {}) {
  const f = getLogFilters();
  const params = new URLSearchParams();
  if (f.module) params.set('module', f.module);
  if (f.status) params.set('status', f.status);
  if (f.q) params.set('q', f.q);
  Object.entries(extra).forEach(([k, v]) => params.set(k, v));
  return params.toString();
}

async function loadLogModules() {
  try {
    const modules = await apiFetch('/api/logs/modules');
    const sel = document.getElementById('log-filter-module');
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">Tất cả phân hệ</option>' +
      modules.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
    sel.value = current;
  } catch (e) {}
}

async function loadLogs() {
  try {
    const logs = await apiFetch('/api/logs?' + buildLogQuery({ limit: 80 }));
    const tbody = document.getElementById('logs-tbody');
    if (!tbody) return;
    tbody.innerHTML = logs.length ? logs.map(l => {
      const context = [
        l.run_id ? `run ${l.run_id}` : '',
        l.queue_id != null ? `queue #${l.queue_id}` : '',
        l.attempt_id != null ? `attempt #${l.attempt_id}` : '',
        l.phase || '',
        l.error_code || '',
      ].filter(Boolean).map(escapeHtml).join(' · ');
      return `
      <tr>
        <td style="color:var(--text-muted); font-size:11.5px;">${escapeHtml(l.created_at)}</td>
        <td><b>${escapeHtml(l.module)}</b></td>
        <td><span class="badge ${l.status === 'ERROR' ? 'badge-err' : (l.status === 'SUCCESS' ? 'badge-ok' : 'badge-wait')}">${escapeHtml(l.status)}</span></td>
        <td>${context ? `<div style="font-size:10px; color:var(--text-muted); margin-bottom:3px;">${context}</div>` : ''}${escapeHtml(l.message)}</td>
      </tr>
    `;
    }).join('') : `
      <tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:18px;">Không có log phù hợp bộ lọc.</td></tr>
    `;
  } catch (e) {}
}

function exportLogs() {
  downloadWithToken('/api/logs/export?' + buildLogQuery(), 'dhb_logs.csv');
}

async function clearLogs() {
  await apiFetch('/api/logs', { method: 'DELETE' });
  await loadLogs();
  await loadLogModules();
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safePublishedVideoUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const host = url.hostname.toLowerCase();
    const allowedHosts = new Set(['youtu.be', 'www.youtu.be', 'youtube.com', 'www.youtube.com', 'm.youtube.com']);
    if (url.protocol !== 'https:' || !allowedHosts.has(host) || (url.port && url.port !== '443') || url.username || url.password) return '';
    const parts = url.pathname.split('/').filter(Boolean);
    let videoId = '';
    if (host.endsWith('youtu.be') && parts.length === 1) videoId = parts[0];
    else if (parts.length === 1 && parts[0] === 'watch') videoId = url.searchParams.get('v') || '';
    else if (parts.length === 2 && ['shorts', 'live', 'embed'].includes(parts[0].toLowerCase())) videoId = parts[1];
    return /^[A-Za-z0-9_-]{11}$/.test(videoId) ? url.href : '';
  } catch (_) {
    return '';
  }
}

window.addEventListener('DOMContentLoaded', initApp);
