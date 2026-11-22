/* Administrative views: presentation only; mutation handlers remain in app.js. */
(() => {
  'use strict';
  const views = new Map();
  const editors = new Map();
  const state = { initialized: false, settingsDirty: false, settingsBusy: false,
    lockedSettings: [], logsPaused: false, logsUpdatedAt: null, logsCount: 0,
    logsError: '', settings: null, diagnostics: null, monitors: null, monitorsUpdatedAt: null };
  const byId = id => document.getElementById(id);
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  };
  const button = (label, action, className = 'btn btn-outline') => {
    const element = node('button', className, label);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  };
  const cardOf = id => byId(id)?.closest('.card');
  const title = (card, value) => {
    const heading = card?.querySelector('.card-title');
    if (heading) heading.textContent = value;
  };
  const hint = text => node('p', 'admin-hint', text);

  function enhanceFields(root) {
    root.querySelectorAll('label:not([for])').forEach(label => {
      if (label.querySelector('input,select,textarea')) return;
      const next = label.nextElementSibling;
      if (next?.matches('input[id],select[id],textarea[id]')) label.htmlFor = next.id;
    });
    root.querySelectorAll('table').forEach(table => {
      table.querySelectorAll('thead th').forEach(th => th.scope = 'col');
      const wrap = table.closest('.table-wrap');
      if (wrap) {
        wrap.tabIndex = 0;
        wrap.setAttribute('role', 'region');
        wrap.setAttribute('aria-label', `${table.closest('.card')?.querySelector('.card-title')?.textContent || 'Bảng dữ liệu'} — có thể cuộn`);
      }
    });
  }

  function tabset(section, items) {
    const nav = node('div', 'admin-tabs');
    nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', section.id === 'tab-settings' ? 'Các mục hệ thống' : 'Quản lý kênh và profile');
    const panels = new Map();
    for (const item of items) {
      const panel = node('div', 'admin-panel');
      panel.id = `${section.id}-${item.key}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', `${panel.id}-tab`);
      const tab = button(item.label, () => showPanel(section.id, item.key), 'admin-tab');
      tab.id = `${panel.id}-tab`;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panel.id);
      item.nodes.filter(Boolean).forEach(child => panel.append(child));
      nav.append(tab);
      panels.set(item.key, { tab, panel });
    }
    section.append(nav, ...Array.from(panels.values(), value => value.panel));
    views.set(section.id, panels);
    nav.addEventListener('keydown', event => {
      const keys = Array.from(panels.keys());
      const active = Array.from(panels.values()).findIndex(value => value.tab === event.target);
      if (active < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1
        : (active + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length;
      showPanel(section.id, keys[index], true);
    });
    showPanel(section.id, items[0].key);
    return panels;
  }

  function showPanel(sectionId, key, focus = false) {
    const panels = views.get(sectionId);
    if (!panels?.has(key)) return;
    panels.forEach((value, current) => {
      const selected = current === key;
      value.panel.hidden = !selected;
      value.tab.setAttribute('aria-selected', String(selected));
      value.tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) value.tab.focus();
    });
  }

  function disclosure(key, content, label, fieldId) {
    const details = node('details', 'admin-editor');
    details.id = `admin-${key}-editor`;
    details.append(node('summary', 'admin-editor-title', label));
    content.classList.add('admin-editor-body');
    details.append(content);
    editors.set(key, { details, fieldId });
    return details;
  }

  function openEditor(key) {
    const editor = editors.get(key);
    if (!editor) return;
    editor.details.open = true;
    if (key === 'channel') showPanel('tab-channels', 'channels');
    if (key === 'option') showPanel('tab-channels', 'options');
    const field = byId(editor.fieldId);
    field?.focus({ preventScroll: true });
    editor.details.scrollIntoView({ block: 'nearest', behavior: 'instant' });
  }

  function syncChannelMode(prefix = 'ch') {
    const mode = byId(`${prefix}-mode`);
    if (!mode) return;
    const isAPI = mode.value === 'youtube_api';
    const oauth = byId(`${prefix}-oauth`)?.closest('.form-group');
    const category = byId(`${prefix}-studio-category`)?.closest('.form-group');
    if (oauth) oauth.hidden = !isAPI;
    if (category) category.hidden = isAPI;
  }

  function organizeChannels() {
    const section = byId('tab-channels');
    if (!section) return;
    const form = cardOf('ch-name');
    const list = cardOf('channels-tbody');
    const profiles = cardOf('profiles-tbody');
    const options = cardOf('options-tbody');
    const capabilities = cardOf('upload-capabilities-tbody');
    if (![form, list, profiles, options, capabilities].every(Boolean)) return;
    const originalGrid = form.parentElement;
    title(form, 'Thông tin kênh');
    title(list, 'Kênh YouTube');
    title(profiles, 'Profile trình duyệt');
    title(options, 'Bộ tùy chọn đăng');
    form.prepend(hint('Lưu kênh chỉ thêm cấu hình. Mở đăng nhập, kiểm tra và đăng video là các thao tác riêng.'));
    const channelEditor = disclosure('channel', form, 'Thêm kênh YouTube', 'ch-name');
    list.querySelector('.card-header').append(button('+ Thêm kênh', () => openEditor('channel'), 'btn btn-accent'));
    list.querySelector('.card-header').after(hint('Đăng nhập và Channel ID là hai điều kiện riêng. Xem trạng thái từng kênh trước khi chuẩn bị lô đăng.'));
    const optionGrid = byId('opt-name').closest('.grid-2');
    const optionForm = byId('opt-name').parentElement.parentElement;
    const optionTable = byId('options-tbody').closest('.table-wrap');
    const optionEditor = disclosure('option', optionForm, 'Tạo hoặc sửa bộ tùy chọn', 'opt-name');
    optionGrid.replaceWith(optionTable, optionEditor);
    options.addEventListener('click', event => {
      const action = event.target.closest('button')?.getAttribute('onclick') || '';
      if (/^(editOptionGroup|resetOptionForm)\(/.test(action.trim())) openEditor('option');
    }, true);
    const panels = tabset(section, [
      { key: 'channels', label: 'Kênh', nodes: [list, channelEditor] },
      { key: 'profiles', label: 'Profile', nodes: [profiles] },
      { key: 'options', label: 'Bộ tùy chọn', nodes: [options] },
      { key: 'capabilities', label: 'Khả năng đăng', nodes: [capabilities] },
    ]);
    if (!originalGrid.children.length) originalGrid.remove();
    panels.get('profiles').panel.prepend(hint('Mở đăng nhập hoặc quét kênh sẽ sử dụng trình duyệt của profile. Thay đổi mục đang xem không mở phiên trình duyệt.'));
    capabilities.querySelector('details').open = true;
    for (const prefix of ['ch', 'ec']) {
      byId(`${prefix}-mode`)?.addEventListener('change', () => syncChannelMode(prefix));
    }
    syncChannelMode('ch');
    enhanceFields(section);
  }

  function groupRows(card, groups) {
    const container = node('div', 'admin-form-groups');
    const moved = new Set();
    groups.forEach(([label, ids, description]) => {
      const fieldset = node('fieldset', 'admin-fieldset');
      fieldset.append(node('legend', '', label));
      if (description) fieldset.append(hint(description));
      ids.forEach(id => {
        let child = byId(id);
        if (!child || !card.contains(child)) return;
        while (child.parentElement !== card) child = child.parentElement;
        if (moved.has(child)) return;
        moved.add(child);
        fieldset.append(child);
      });
      container.append(fieldset);
    });
    card.querySelector('.card-header')?.after(container);
    return container;
  }

  function organizeMonitor() {
    const section = byId('tab-monitor');
    const form = cardOf('mon-name');
    const list = cardOf('monitors-tbody');
    if (!section || !form || !list) return;
    const grid = form.parentElement;
    title(form, 'Cấu hình luồng theo dõi');
    title(list, 'Luồng theo dõi nguồn');
    groupRows(form, [
      ['1. Nguồn', ['mon-name', 'mon-urls'], 'Mỗi dòng là một nguồn theo dõi.'],
      ['2. Đích đăng', ['mon-channel', 'mon-visibility'], 'Chọn kênh cụ thể hoặc phân phối theo nhóm. Các giá trị kế thừa sẽ được kiểm tra trước khi bật luồng.'],
      ['3. Xử lý', ['mon-render', 'mon-ai-action']],
      ['4. Chu kỳ', ['mon-interval'], 'Lưu cấu hình chưa khởi chạy. Bấm Bật nguồn trong danh sách để kiểm tra và bắt đầu.'],
    ]);
    form.querySelector('button[onclick="addMonitor()"]')?.classList.add('admin-save-action');
    const editor = disclosure('monitor', form, 'Tạo luồng theo dõi', 'mon-name');
    list.querySelector('.card-header').append(button('+ Tạo luồng', () => openEditor('monitor'), 'btn btn-accent'));
    const summary = node('p', 'admin-hint');
    summary.id = 'admin-monitor-summary';
    summary.textContent = 'Đang tải danh sách luồng…';
    summary.setAttribute('role', 'status');
    list.querySelector('.card-header').after(summary);
    const retry = button('Tải lại trạng thái', () => window.loadMonitors?.().catch(() => {}));
    retry.id = 'admin-monitor-retry';
    retry.hidden = true;
    summary.after(retry);
    section.append(list, editor);
    if (!grid.children.length) grid.remove();
    enhanceFields(section);
    if (state.monitors) onMonitorsLoaded(state.monitors);
  }

  function settingField(id) {
    const control = byId(id);
    if (!control) return null;
    if (control.parentElement.tagName === 'LABEL') return control.parentElement.closest('.form-group');
    return control.parentElement;
  }

  function organizeSettings(card) {
    title(card, 'Cài đặt');
    const groups = [
      ['Kết nối AI', ['set-gemini-key', 'set-gemini-model', 'set-openai-key', 'set-deepseek-key'], 'Để trống ô khóa để giữ giá trị đã lưu. Các khóa mới chỉ được gửi khi bạn bấm Lưu.'],
      ['Worker & tải nguồn', ['set-threads', 'set-render-threads', 'set-tiktok-res', 'set-download-cookies'], 'Giới hạn số tác vụ chạy đồng thời và cấu hình tải nguồn.'],
      ['Đăng video', ['set-delay', 'set-checks-timeout', 'set-monitor-gap', 'set-max-uploads', 'set-thumb-source', 'set-hide-browser'], 'Đơn vị và giới hạn của từng trường được áp dụng cho toàn hệ thống.'],
      ['Lưu trữ & sao lưu', ['set-del-temp', 'set-external-backup-dir', 'set-external-backup-keep']],
    ];
    const body = node('div', 'admin-settings-groups');
    groups.forEach(([label, ids, description]) => {
      const group = node('fieldset', 'admin-fieldset');
      group.append(node('legend', '', label));
      if (description) group.append(hint(description));
      const fields = node('div', 'admin-fields');
      ids.forEach(id => {
        const wrapper = settingField(id);
        if (wrapper) { wrapper.classList.add('admin-field'); fields.append(wrapper); }
      });
      group.append(fields);
      body.append(group);
    });
    const backupAction = card.querySelector('button[onclick="exportBackupNow()"]')?.parentElement;
    if (backupAction) body.lastElementChild.append(backupAction);
    const save = card.querySelector('button[onclick="saveAppSettings()"]');
    const footer = node('div', 'admin-settings-footer');
    const status = node('p', 'admin-save-status', 'Đang tải cài đặt…');
    status.id = 'admin-settings-status';
    status.setAttribute('role', 'status');
    footer.append(status);
    if (save) {
      save.id = 'admin-settings-save';
      save.textContent = 'Lưu toàn bộ cài đặt';
      save.style.removeProperty('width');
      footer.append(save);
    }
    const header = card.querySelector('.card-header');
    // Preserve any future fields not assigned to a group; remove only empty wrappers.
    Array.from(card.children).forEach(child => {
      if (child !== header && !child.querySelector('input, select, textarea, button')) child.remove();
    });
    card.append(body, footer);
    const markDirty = event => {
      if (!event.target.matches('input,select,textarea') || state.settingsBusy) return;
      state.settingsDirty = true;
      status.textContent = 'Có thay đổi chưa lưu · Áp dụng cho toàn bộ cài đặt';
      status.dataset.state = 'dirty';
    };
    card.addEventListener('input', markDirty);
    card.addEventListener('change', markDirty);
    for (const id of ['set-gemini-key', 'set-openai-key', 'set-deepseek-key']) {
      const input = byId(id);
      input.autocomplete = 'new-password';
      const configured = node('span', 'admin-secret-state', 'Chưa tải trạng thái khóa');
      configured.id = `${id}-state`;
      input.after(configured);
      input.setAttribute('aria-describedby', configured.id);
    }
    if (state.settings) onSettingsLoaded(state.settings);
  }

  function organizeLogs(card) {
    title(card, 'Nhật ký hoạt động');
    const header = card.querySelector('.card-header');
    const clear = card.querySelector('button[onclick="clearLogs()"]');
    const actions = node('div', 'admin-log-actions');
    const pause = button('Tạm dừng cập nhật', () => {
      state.logsPaused = !state.logsPaused;
      renderLogStatus();
      if (!state.logsPaused) window.loadLogs?.(true);
    });
    pause.id = 'admin-logs-pause';
    pause.setAttribute('aria-pressed', 'false');
    actions.append(pause, button('Tải lại', () => window.loadLogs?.(true)));
    if (clear) {
      const more = node('details', 'admin-log-more');
      more.append(node('summary', '', 'Tùy chọn'));
      clear.textContent = 'Xóa toàn bộ nhật ký…';
      clear.classList.add('btn-danger');
      more.append(clear);
      actions.append(more);
    }
    header.append(actions);
    const status = node('p', 'admin-hint');
    status.id = 'admin-log-status';
    status.setAttribute('role', 'status');
    header.after(status);
    const exportButton = card.querySelector('button[onclick="exportLogs()"]');
    if (exportButton) {
      exportButton.textContent = 'Xuất CSV theo bộ lọc';
      exportButton.title = 'Xuất tối đa 20.000 log gần nhất khớp bộ lọc từ máy chủ; không giới hạn ở 80 dòng đang xem.';
    }
    renderLogStatus();
  }

  function organizeDiagnostics(card) {
    title(card, 'Chẩn đoán hệ thống');
    const raw = byId('diagnostics-summary');
    const summary = node('div', 'admin-diagnostics');
    summary.id = 'admin-diagnostics-cards';
    summary.append(hint('Đang kiểm tra hệ thống…'));
    const details = node('details', 'admin-diagnostic-details');
    details.append(node('summary', '', 'Chi tiết kỹ thuật'), raw);
    card.querySelector('.card-header').after(summary, details);
    if (state.diagnostics) onDiagnosticsLoaded(state.diagnostics);
  }

  function organizeSystem() {
    const section = byId('tab-settings');
    const settings = cardOf('set-gemini-key');
    const logs = cardOf('logs-tbody');
    const storage = cardOf('storage-summary');
    const diagnostics = cardOf('diagnostics-summary');
    if (!section || ![settings, logs, storage, diagnostics].every(Boolean)) return;
    const originalGrid = settings.parentElement;
    organizeSettings(settings);
    organizeLogs(logs);
    organizeDiagnostics(diagnostics);
    title(storage, 'Lưu trữ & file tạm');
    tabset(section, [
      { key: 'settings', label: 'Cài đặt', nodes: [settings] },
      { key: 'activity', label: 'Hoạt động', nodes: [logs] },
      { key: 'storage', label: 'Lưu trữ', nodes: [storage] },
      { key: 'diagnostics', label: 'Chẩn đoán', nodes: [diagnostics] },
    ]);
    if (!originalGrid.children.length) originalGrid.remove();
    enhanceFields(section);
  }

  function onSettingsLoaded(settings) {
    state.settings = settings;
    const fields = { 'set-gemini-key': 'gemini_api_key_configured', 'set-openai-key': 'openai_api_key_configured', 'set-deepseek-key': 'deepseek_api_key_configured' };
    Object.entries(fields).forEach(([id, key]) => {
      const status = byId(`${id}-state`);
      if (status) status.textContent = settings[key] ? 'Đã cấu hình · Để trống để giữ nguyên' : 'Chưa cấu hình';
    });
    const status = byId('admin-settings-status');
    if (status && !state.settingsDirty && !state.settingsBusy) {
      status.textContent = 'Cài đặt đã tải · Chưa có thay đổi';
      status.dataset.state = 'saved';
    }
  }

  function onSettingsSaving() {
    if (state.settingsBusy) return false;
    state.settingsBusy = true;
    const card = cardOf('set-gemini-key');
    state.lockedSettings = Array.from(card?.querySelectorAll('input, select, textarea, button') || []).map(control => [control, control.disabled]);
    state.lockedSettings.forEach(([control]) => { control.disabled = true; });
    card?.setAttribute('aria-busy', 'true');
    const status = byId('admin-settings-status');
    if (status) { status.textContent = 'Đang lưu cài đặt…'; status.dataset.state = 'saving'; }
    return true;
  }

  function unlockSettings() {
    state.lockedSettings.forEach(([control, disabled]) => { control.disabled = disabled; });
    state.lockedSettings = [];
    state.settingsBusy = false;
    cardOf('set-gemini-key')?.setAttribute('aria-busy', 'false');
  }

  function onSettingsSaved() {
    unlockSettings();
    state.settingsDirty = false;
    const status = byId('admin-settings-status');
    if (status) { status.textContent = 'Đã lưu toàn bộ cài đặt'; status.dataset.state = 'saved'; }
  }

  function onSettingsError(error, partial = false) {
    unlockSettings();
    state.settingsDirty = true;
    const status = byId('admin-settings-status');
    if (status) {
      status.textContent = `${partial ? 'Một phần cài đặt đã được lưu. ' : ''}Chưa hoàn tất: ${String(error?.message || error || 'Hãy thử lại.')} Nội dung đang nhập được giữ lại.`;
      status.dataset.state = 'error';
    }
  }

  function renderLogStatus() {
    const status = byId('admin-log-status');
    const pause = byId('admin-logs-pause');
    if (pause) {
      pause.textContent = state.logsPaused ? 'Tiếp tục cập nhật' : 'Tạm dừng cập nhật';
      pause.setAttribute('aria-pressed', String(state.logsPaused));
    }
    if (!status) return;
    const timestamp = state.logsUpdatedAt ? `Cập nhật lúc ${state.logsUpdatedAt.toLocaleTimeString('vi-VN')}` : 'Chưa tải được nhật ký';
    status.textContent = state.logsError
      ? `Không cập nhật được nhật ký: ${state.logsError}. ${timestamp}. Dữ liệu đang xem có thể đã cũ.`
      : `${state.logsPaused ? 'Đã tạm dừng tự cập nhật. ' : ''}${timestamp} · ${state.logsCount} / tối đa 80 dòng gần nhất theo bộ lọc.`;
    status.dataset.state = state.logsError ? 'error' : state.logsPaused ? 'paused' : 'ready';
  }

  function onLogsLoaded(logs) {
    state.logsUpdatedAt = new Date();
    state.logsCount = Array.isArray(logs) ? logs.length : 0;
    state.logsError = '';
    renderLogStatus();
  }

  function onLogsError(error) {
    state.logsError = String(error?.message || error || 'Lỗi không xác định');
    renderLogStatus();
  }

  function onDiagnosticsLoaded(data) {
    state.diagnostics = data;
    const host = byId('admin-diagnostics-cards');
    if (!host) return;
    const workers = data.workers || {};
    const values = [
      ['Bản dựng', data.version || 'Chưa rõ', data.runtime?.restart_required ? 'Cần khởi động lại để áp dụng mã mới' : 'Thông tin tại lần kiểm tra gần nhất'],
      ['Worker', data.batch_state === 'stopping' ? 'Đang dừng' : workers.pipeline_running ? 'Đang chạy' : 'Không chạy pipeline', `${workers.active_batch_threads ?? '—'} luồng batch · ${workers.monitor_workers ?? '—'} worker theo dõi`],
      ['Cần đối soát', data.needs_review_count ?? '—', 'Mở hàng đợi để xem bằng chứng từng video'],
      ['Dung lượng trống', typeof window.storageFormatBytes === 'function' && data.storage?.free_bytes != null ? window.storageFormatBytes(data.storage.free_bytes) : 'Chưa rõ', data.database?.available ? 'Database sẵn có' : 'Chưa xác nhận database'],
    ];
    if (data.scheduled_uploads?.configured === true) {
      const sched = data.scheduled_uploads;
      const rawDrift = sched.folder_drift;
      const drift = rawDrift && typeof rawDrift === 'object' && !Array.isArray(rawDrift) ? rawDrift : {};
      const validCount = value => Number.isSafeInteger(value) && value >= 0;
      const malformedDrift = rawDrift != null && (rawDrift !== drift
        || ['detected', 'next_source_exists'].some(key => drift[key] !== undefined && typeof drift[key] !== 'boolean')
        || ['missing_future_count', 'new_files_count'].some(key => drift[key] !== undefined && !validCount(drift[key])));
      const missingCount = validCount(drift.missing_future_count) ? drift.missing_future_count : 0;
      const newCount = validCount(drift.new_files_count) ? drift.new_files_count : 0;
      const nextMissing = drift.next_source_exists === false;
      const sourceChanged = drift.detected === true || missingCount > 0 || newCount > 0 || nextMissing;
      const statusText = nextMissing ? 'Cần xử lý nguồn' : (sourceChanged || malformedDrift ? 'Cần kiểm tra nguồn' : sched.status);
      const prog = Number.isInteger(sched.completed_count) && Number.isInteger(sched.total_count)
        ? `${sched.completed_count}/${sched.total_count} video · ${statusText}`
        : String(statusText || 'Đã cấu hình');
      const nextText = sched.next_due_at
        ? `Kế tiếp: ${new Date(sched.next_due_at).toLocaleString('vi-VN')}`
        : (sched.message || 'Xem trong Quy trình đăng tự động');
      const sourceDetails = [];
      if (nextMissing) sourceDetails.push('File kế tiếp không còn trên đĩa');
      if (missingCount) sourceDetails.push(`${missingCount} file chờ không còn trên đĩa`);
      if (newCount) sourceDetails.push(`${newCount} file mới chưa nạp vào lịch`);
      let hint = sched.action_required ? `Cần kiểm tra: ${sched.message || sched.status}` : nextText;
      if (sourceChanged || malformedDrift) {
        hint = sourceDetails.length ? sourceDetails.join(' · ') + '. ' : '';
        hint += malformedDrift ? 'Dữ liệu kiểm tra nguồn không hợp lệ; tải lại trạng thái trước khi đồng bộ hoặc chạy tiếp.'
          : 'Mở Quy trình đăng tự động để kiểm tra nguồn trước khi đồng bộ.';
        if (newCount) hint += ' File mới không mặc nhiên là file đổi tên; đối chiếu nội dung để tránh đăng trùng.';
      }
      values.push(['Lịch đăng Private', prog, hint]);
    }
    host.replaceChildren();
    values.forEach(([label, value, description]) => {
      const card = node('div', 'admin-metric');
      card.append(node('span', 'admin-metric-label', label), node('strong', '', String(value)), node('span', 'admin-hint', description));
      host.append(card);
    });
  }

  function onDiagnosticsError(error) {
    const host = byId('admin-diagnostics-cards');
    if (!host) return;
    const message = hint(`Không đọc được trạng thái hệ thống: ${String(error?.message || error)}. Hãy kiểm tra lại.`);
    message.dataset.state = 'error';
    host.replaceChildren(message);
  }

  function onMonitorsLoaded(list) {
    state.monitors = list;
    state.monitorsUpdatedAt = new Date();
    const summary = byId('admin-monitor-summary');
    if (summary) {
      summary.textContent = `${list.length} luồng · ${list.filter(item => item.is_running).length} đang bật · Cập nhật lúc ${state.monitorsUpdatedAt.toLocaleTimeString('vi-VN')}. Trạng thái và quyền xóa do máy chủ xác nhận.`;
      summary.dataset.state = 'ready';
    }
    const retry = byId('admin-monitor-retry');
    if (retry) retry.hidden = true;
    if (!list.length) {
      const tbody = byId('monitors-tbody');
      if (tbody) {
        const row = node('tr');
        const cell = node('td', 'admin-empty', 'Chưa có luồng theo dõi. Chọn “Tạo luồng” để cấu hình nguồn và đích đăng.');
        cell.colSpan = 6;
        row.append(cell);
        tbody.replaceChildren(row);
      }
    }
  }

  function onMonitorsError(error) {
    const summary = byId('admin-monitor-summary');
    if (summary) {
      const last = state.monitorsUpdatedAt
        ? `Cập nhật thành công lần cuối lúc ${state.monitorsUpdatedAt.toLocaleTimeString('vi-VN')}. Dữ liệu đang xem có thể đã cũ.`
        : 'Chưa xác nhận được trạng thái của các luồng.';
      summary.textContent = `Không cập nhật được Monitor: ${String(error?.message || error)}. ${last}`;
      summary.dataset.state = 'error';
    }
    const retry = byId('admin-monitor-retry');
    if (retry) retry.hidden = false;
  }

  function confirmClearLogs() {
    if (document.getElementById('admin-clear-logs-dialog')?.open) return Promise.resolve(false);
    return new Promise(resolve => {
      const previousFocus = document.activeElement;
      const dialog = node('dialog', 'workspace-dialog admin-confirm-dialog');
      dialog.id = 'admin-clear-logs-dialog';
      dialog.setAttribute('aria-labelledby', 'admin-clear-logs-title');
      const heading = node('h2', '', 'Xóa toàn bộ nhật ký?');
      heading.id = 'admin-clear-logs-title';
      const explanation = node('p', '', 'Thao tác này xóa toàn bộ nhật ký hệ thống, kể cả các dòng ngoài bộ lọc và 80 dòng đang xem. Nhật ký đã xóa không thể khôi phục qua giao diện.');
      const actions = node('div', 'admin-confirm-actions');
      const cancel = button('Giữ nhật ký', () => dialog.close('cancel'));
      const confirm = button('Xóa toàn bộ nhật ký', () => dialog.close('delete'), 'btn btn-danger');
      actions.append(cancel, confirm);
      dialog.append(heading, explanation, actions);
      dialog.addEventListener('close', () => {
        const accepted = dialog.returnValue === 'delete';
        dialog.remove();
        if (previousFocus?.isConnected) previousFocus.focus();
        resolve(accepted);
      }, { once: true });
      document.body.append(dialog);
      dialog.showModal();
      cancel.focus();
    });
  }

  function init() {
    if (state.initialized) return;
    state.initialized = true;
    organizeChannels();
    organizeMonitor();
    organizeSystem();
  }

  window.StudioAdmin = {
    init, showPanel, syncChannelMode,
    openChannelForm: () => openEditor('channel'),
    openOptionForm: () => openEditor('option'),
    openMonitorForm: () => openEditor('monitor'),
    onSettingsLoaded, onSettingsSaving, onSettingsSaved, onSettingsError,
    hasUnsavedSettings: () => state.settingsDirty,
    shouldLoadLogs: (force = false) => force || !state.logsPaused,
    onLogsLoaded, onLogsError, onDiagnosticsLoaded, onDiagnosticsError,
    onMonitorsLoaded, onMonitorsError, confirmClearLogs,
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
