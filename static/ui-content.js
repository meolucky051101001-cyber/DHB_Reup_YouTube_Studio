/* AI and render views. Presentation only; mutations remain in app.js. */
(() => {
  'use strict';

  let initialized = false;
  let aiBusy = false;
  let aiRequest = null;
  let renderBaseline = '';
  let renderTouched = false;
  const scopes = [];
  const providers = {gemini: 'Google Gemini', openai: 'OpenAI', deepseek: 'DeepSeek', google_translate: 'Google Translate'};
  const byId = id => document.getElementById(id);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (text, handler, className = 'btn btn-outline') => {
    const node = element('button', className, text);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  };

  function makeScope(section) {
    const bar = element('div', 'content-scope');
    const summary = element('p', 'content-scope-summary');
    const link = button('Chọn video trong hàng đợi', () => {
      const navigation = document.querySelector('[data-tab="tab-workspace"]');
      if (typeof switchTab === 'function') switchTab('tab-workspace', navigation);
      byId('queue-search')?.focus();
    }, 'btn btn-outline btn-sm');
    bar.append(summary, link);
    const heading = section.querySelector(':scope > .ui-page-heading');
    if (heading) heading.after(bar);
    else section.prepend(bar);
    scopes.push(summary);
  }

  function context() {
    const items = typeof gQueueItems !== 'undefined' ? gQueueItems : [];
    const current = new Set(items.map(item => Number(item.id)));
    const selected = typeof gSelectedIds !== 'undefined'
      ? [...gSelectedIds].map(Number).filter(id => current.has(id)) : [];
    const visible = typeof getVisibleQueueIds === 'function' ? getVisibleQueueIds() : [];
    const project = byId('sel-project');
    const name = project?.selectedOptions[0]?.textContent || project?.value || 'Chưa tải dự án';
    return {project: name, count: selected.length || visible.length, selected: selected.length,
      hidden: selected.filter(id => !visible.includes(id)).length, visible: visible.length};
  }

  function updateScope() {
    const scope = context();
    const detail = scope.selected
      ? `Đã chọn ${scope.selected} video${scope.hidden ? ` · ${scope.hidden} đang ẩn bởi bộ lọc` : ''}`
      : `${scope.visible} video theo bộ lọc hiện tại · Chưa chọn thủ công`;
    scopes.forEach(node => { node.textContent = `${scope.project} — ${detail}`; });
    const ai = document.querySelector('#tab-ai button[onclick="runAIProcess(\'ai\')"]');
    const edit = document.querySelector('#tab-ai button[onclick="runAIProcess(\'batch_edit\')"]');
    if (ai) ai.textContent = aiBusy ? 'Đang xử lý…' : `Chạy AI cho ${scope.count} video`;
    if (edit) edit.textContent = aiBusy ? 'Đang xử lý…' : `Biên tập ${scope.count} video`;
    const apply = document.querySelector('#tab-render button[onclick="applyRenderStudioToSelected()"]');
    if (apply) apply.textContent = `Áp dụng cấu hình cho ${scope.count} video`;
    const run = byId('content-render-run');
    if (run) run.textContent = `Chỉ render ${scope.count} video`;
  }

  function makeTabs(host, prefix, entries) {
    const list = element('div', 'content-tabs');
    list.setAttribute('role', 'tablist');
    list.setAttribute('aria-label', prefix === 'content-ai' ? 'Công cụ nội dung' : 'Nhóm cấu hình render');
    const controls = entries.map(([label, panel], index) => {
      const id = `${prefix}-${index}`;
      panel.id ||= `${id}-panel`;
      panel.classList.add('content-tab-panel');
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', id);
      const tab = button(label, () => activate(index));
      tab.id = id;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', panel.id);
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % entries.length;
        if (event.key === 'ArrowLeft') next = (index + entries.length - 1) % entries.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = entries.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        activate(next);
        controls[next].focus();
      });
      list.append(tab);
      return tab;
    });
    function activate(index) {
      entries.forEach(([, panel], current) => {
        panel.hidden = current !== index;
        controls[current].setAttribute('aria-selected', String(current === index));
        controls[current].tabIndex = current === index ? 0 : -1;
      });
    }
    host.prepend(list);
    activate(0);
    return activate;
  }

  function buildAI() {
    const section = byId('tab-ai');
    if (!section) return;
    section.classList.add('content-enhanced');
    const grid = section.querySelector('.grid-2');
    if (!grid) return;
    grid.classList.add('content-editor-layout');
    const cards = [...grid.children].filter(node => node.classList.contains('card'));
    if (cards.length < 2) return;
    cards[0].querySelector('.card-title').textContent = 'AI nội dung';
    cards[1].querySelector('.card-title').textContent = 'Biên tập văn bản';
    makeTabs(grid, 'content-ai', [['AI nội dung', cards[0]], ['Sửa văn bản', cards[1]]]);
    const note = element('p', 'content-help', 'Kết quả được lưu vào hàng đợi khi chạy. Kiểm tra phạm vi video trước khi thực hiện.');
    cards[0].append(note);
    makeScope(section);
    const result = element('section', 'card content-result');
    result.id = 'content-ai-results';
    result.setAttribute('aria-labelledby', 'content-ai-result-title');
    const heading = element('h2', 'content-section-title', 'Kết quả lần xử lý');
    heading.id = 'content-ai-result-title';
    const status = element('p', 'content-result-status', 'Chưa có kết quả trong phiên này.');
    status.id = 'content-ai-result-status';
    status.setAttribute('role', 'status');
    const body = element('div', 'content-result-body');
    body.id = 'content-ai-result-body';
    result.append(heading, status, body);
    section.append(result);
    const select = byId('ed-op');
    const showEditFields = () => {
      const operation = select?.value;
      const find = byId('ed-find')?.closest('.form-group');
      const replace = byId('ed-replace')?.closest('.form-group');
      if (find) find.hidden = operation !== 'replace';
      if (replace) replace.hidden = !['replace', 'append_first', 'append_end'].includes(operation);
    };
    select?.addEventListener('change', showEditFields);
    showEditFields();
    byId('ai-mode')?.querySelector('option[value="rewrite_seo"]')?.replaceChildren(document.createTextNode('Viết lại tiêu đề, mô tả và tags'));
    byId('ai-mode')?.querySelector('option[value="shorts_hook"]')?.replaceChildren(document.createTextNode('Viết tiêu đề cho Shorts'));
    byId('ai-mode')?.querySelector('option[value="translate"]')?.replaceChildren(document.createTextNode('Dịch tiêu đề và mô tả'));
  }

  function setAIBusy(busy, payload) {
    aiBusy = !!busy;
    if (busy && payload) {
      aiRequest = {project: payload.project_name, mode: payload.mode, count: payload.ids?.length || 0};
    }
    const section = byId('tab-ai');
    section?.setAttribute('aria-busy', String(aiBusy));
    const status = byId('content-ai-result-status');
    if (busy && status) status.textContent = `Đang xử lý ${aiRequest?.count || 0} video. Kết quả sẽ xuất hiện tại đây.`;
    updateScope();
  }

  function showAIResult(data, payload = {}) {
    const host = byId('content-ai-result-body');
    if (!host) return false;
    const failures = Array.isArray(data.failed) ? data.failed : [];
    const count = Number(data.updated || 0);
    const request = {project: payload.project_name || aiRequest?.project || '', mode: payload.mode || aiRequest?.mode || ''};
    host.replaceChildren();
    const stamp = new Date().toLocaleTimeString('vi-VN', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    byId('content-ai-result-status').textContent = `${stamp}${request.project ? ` · ${request.project}` : ''} · Đã cập nhật ${count} video · Không cập nhật ${failures.length} video`;
    const metrics = element('div', 'content-result-metrics');
    metrics.append(element('span', 'content-result-success', `${count} đã cập nhật`));
    if (failures.length) metrics.append(element('span', 'content-result-warning', `${failures.length} không cập nhật`));
    host.append(metrics);
    if (request.mode !== 'batch_edit' && data.provider_requested) {
      const actual = Object.entries(data.provider_counts || {})
        .map(([name, amount]) => `${providers[name] || name}: ${amount}`).join(' · ');
      host.append(element('p', 'content-help', `Yêu cầu: ${providers[data.provider_requested] || data.provider_requested}. Thực tế: ${actual || 'Không có video được cập nhật'}.`));
      if (Number(data.fallback_count) > 0) host.append(element('p', 'content-help', `${data.fallback_count} video dùng Google Translate thay thế nhà cung cấp yêu cầu.`));
      if (Number(data.links_preserved) > 0) host.append(element('p', 'content-help', `Đã giữ ${data.links_preserved} liên kết từ mô tả nguồn.`));
    }
    if (failures.length) {
      host.append(element('p', 'content-help', 'Các video dưới đây không được ghi kết quả trong lần xử lý này. Kiểm tra trạng thái hiện tại trước khi thực hiện lại.'));
      const wrap = element('div', 'content-result-table-wrap');
      const table = element('table', 'content-result-table');
      const caption = element('caption', '', 'Video không được cập nhật');
      const head = element('thead');
      const header = element('tr');
      ['Video', 'Mã kết quả', 'Chi tiết'].forEach(label => {
        const cell = element('th', '', label); cell.scope = 'col'; header.append(cell);
      });
      head.append(header);
      const body = element('tbody');
      failures.forEach(item => {
        const row = element('tr');
        row.append(element('td', '', `#${item.id}`), element('td', '', item.code || 'failed'),
          element('td', '', item.error || 'Không cập nhật được video.'));
        body.append(row);
      });
      table.append(caption, head, body); wrap.append(table); host.append(wrap);
    }
    return true;
  }

  function showAIError(message) {
    const host = byId('content-ai-result-body');
    if (!host) return false;
    byId('content-ai-result-status').textContent = 'Không hoàn tất yêu cầu. Kiểm tra thông báo bên dưới.';
    const error = element('p', 'content-inline-error', String(message));
    error.setAttribute('role', 'alert');
    // Keep the previous successful result visible if refreshing the queue failed afterwards.
    host.querySelector('.content-inline-error')?.remove();
    host.prepend(error);
    return true;
  }

  function group(label, description) {
    const fieldset = element('fieldset', 'content-render-group');
    fieldset.append(element('legend', '', label));
    if (description) fieldset.append(element('p', 'content-help', description));
    const fields = element('div', 'content-fields');
    fieldset.append(fields);
    return {fieldset, fields};
  }

  function moveField(id, target) {
    const input = byId(id);
    if (!input) return;
    let holder = input.closest('.row-inline > div');
    if (!holder) holder = input.closest('.form-group');
    if (!holder) return;
    holder.classList.add('content-field');
    target.append(holder);
  }

  function renderSignature() {
    const section = byId('tab-render');
    return JSON.stringify([...section.querySelectorAll('input[id^="rc-"],select[id^="rc-"],textarea[id^="rc-"],#rp-name,#rp-note')]
      .map(node => [node.id, node.type === 'checkbox' ? node.checked : node.value]));
  }

  function refreshRender(options = {}) {
    if (!byId('content-render-summary')) return;
    const signature = renderSignature();
    if (options.clean || !renderTouched) { renderBaseline = signature; renderTouched = false; }
    const dirty = signature !== renderBaseline;
    const indicator = byId('content-render-dirty');
    indicator.textContent = dirty ? 'Có thay đổi chưa lưu vào preset' : 'Cấu hình trong form';
    indicator.classList.toggle('is-dirty', dirty);
    const mode = byId('rc-mode');
    const values = [mode?.selectedOptions[0]?.textContent || 'Chưa chọn chế độ',
      `Tốc độ ${byId('rc-speed')?.value || '1'}×`, `Âm lượng ${byId('rc-volume')?.value || '1'}×`,
      byId('rc-bg-music')?.value ? `Nhạc: ${byId('rc-bg-music').value}` : 'Âm thanh gốc'];
    const summary = byId('content-render-summary');
    summary.replaceChildren(...values.map(value => element('span', '', value)));
  }

  function buildResources(card) {
    card.classList.add('content-resource-card');
    card.querySelector('.card-title').textContent = 'Tài nguyên render';
    const controls = element('div', 'content-resource-controls');
    const searchLabel = element('label', '', 'Tìm tài nguyên');
    searchLabel.htmlFor = 'content-resource-search';
    const search = element('input');
    search.type = 'search'; search.id = 'content-resource-search'; search.placeholder = 'Tên nhạc, video, font…';
    const typeLabel = element('label', '', 'Loại tài nguyên');
    typeLabel.htmlFor = 'content-resource-type';
    const type = element('select'); type.id = 'content-resource-type';
    [['', 'Tất cả'], ['0', 'Nhạc nền'], ['1', 'Video nền'], ['2', 'Intro'], ['3', 'Outro'], ['4', 'Filter'], ['5', 'Font']]
      .forEach(([value, label]) => { const option = element('option', '', label); option.value = value; type.append(option); });
    const searchGroup = element('div'); searchGroup.append(searchLabel, search);
    const typeGroup = element('div'); typeGroup.append(typeLabel, type);
    controls.append(searchGroup, typeGroup);
    card.querySelector('.card-header').after(controls);
    const status = element('p', 'content-help', 'Đang tải danh sách tài nguyên…');
    status.id = 'content-resource-status'; status.setAttribute('role', 'status'); card.append(status);
    card.append(button('Tải lại tài nguyên', async () => {
      try { await loadRenderResources(); }
      catch (error) { showResourcesError(error.message); }
    }, 'btn btn-outline btn-sm'));
    const list = byId('dhb-resources-summary');
    const filter = () => {
      const query = search.value.trim().toLocaleLowerCase('vi');
      let found = 0;
      [...list.children].forEach((section, index) => {
        let visible = 0;
        const selectedType = !type.value || String(index) === type.value;
        section.querySelectorAll('.badge').forEach(item => {
          item.hidden = !selectedType || !item.textContent.toLocaleLowerCase('vi').includes(query);
          if (!item.hidden) visible++;
        });
        section.hidden = !selectedType || visible === 0;
        found += visible;
      });
      status.textContent = found ? `${found} tài nguyên phù hợp` : 'Không có tài nguyên phù hợp. Thử đổi loại hoặc từ khóa.';
    };
    search.addEventListener('input', filter); type.addEventListener('change', filter);
    new MutationObserver(filter).observe(list, {childList: true, subtree: true});
    if (list.childElementCount) filter();
  }

  function showResourcesError(message) {
    const status = byId('content-resource-status');
    if (!status) return;
    status.textContent = `Không tải được tài nguyên. Danh sách có thể chưa được cập nhật. ${message || ''}`;
  }

  function buildRender() {
    const section = byId('tab-render');
    if (!section) return;
    section.classList.add('content-enhanced');
    const config = byId('rc-mode')?.closest('.card');
    const preset = byId('rp-name')?.closest('.card');
    const resources = byId('dhb-resources-summary')?.closest('.card');
    const grid = section.querySelector('.grid-2');
    if (!config || !preset || !resources || !grid) return;
    grid.before(preset);
    preset.classList.add('content-preset-card');
    preset.querySelector('.card-title').textContent = 'Preset render';
    const selectRow = byId('rp-select').closest('.row-inline');
    preset.querySelector('.card-header').after(selectRow);
    const saveDetails = element('details', 'content-preset-save');
    saveDetails.append(element('summary', '', 'Lưu preset và ghi chú'));
    saveDetails.append(byId('rp-name').closest('.row-inline'), byId('rp-note').closest('.form-group'));
    selectRow.after(saveDetails);
    const load = preset.querySelector('button[onclick="loadPresetToForm()"]');
    if (load) load.textContent = 'Nạp cấu hình';
    const help = preset.lastElementChild;
    if (help?.id !== 'rp-editing') help.textContent = 'Nạp preset → chỉnh thông số → lưu preset hoặc áp dụng cho video. Nạp preset chưa thay đổi hàng đợi.';
    const dirty = element('span', 'content-render-dirty', 'Cấu hình trong form');
    dirty.id = 'content-render-dirty';
    preset.querySelector('.card-header').append(dirty);
    makeScope(section);
    grid.classList.add('content-render-layout');
    config.classList.add('content-render-config');
    config.querySelector('.card-title').textContent = 'Thông số render';
    const summary = element('div', 'content-render-summary'); summary.id = 'content-render-summary';
    config.querySelector('.card-header').after(summary);
    const basic = group('Cơ bản', 'Chọn chế độ và tốc độ trước khi tinh chỉnh các nhóm khác.');
    const frame = group('Khung hình');
    const audio = group('Âm thanh');
    const branding = group('Branding');
    const advanced = group('Nâng cao', 'Lệnh FFmpeg tùy chỉnh chỉ chạy khi chọn chế độ tương ứng.');
    const panels = element('div', 'content-render-panels');
    [basic, frame, audio, branding, advanced].forEach(item => panels.append(item.fieldset));
    summary.after(panels);
    ['rc-mode', 'rc-speed', 'rc-shorts-enforce', 'rc-bg-video', 'rc-bg-random'].forEach(id => moveField(id, basic.fields));
    ['rc-brightness', 'rc-hflip', 'rc-shorts-visual'].forEach(id => moveField(id, frame.fields));
    ['rc-volume', 'rc-bg-music', 'rc-pitch', 'rc-audio-orig'].forEach(id => moveField(id, audio.fields));
    const edit = byId('edit-panel');
    const brandingPanel = byId('branding-panel');
    config.querySelector('button[onclick="toggleEditPanel()"]')?.remove();
    config.querySelector('button[onclick="toggleBrandingPanel()"]')?.remove();
    if (edit) { edit.classList.add('content-unfolded'); frame.fields.append(edit); }
    if (brandingPanel) { brandingPanel.classList.add('content-unfolded'); branding.fields.append(brandingPanel); }
    moveField('rc-ffmpeg-preset', advanced.fields);
    config.querySelectorAll('.row-inline').forEach(row => { if (!row.children.length) row.remove(); });
    makeTabs(panels, 'content-render', [['Cơ bản', basic.fieldset], ['Khung hình', frame.fieldset], ['Âm thanh', audio.fieldset], ['Branding', branding.fieldset], ['Nâng cao', advanced.fieldset]]);
    const apply = config.querySelector('button[onclick="applyRenderStudioToSelected()"]');
    const footer = element('div', 'content-render-actions');
    if (apply) { apply.removeAttribute('style'); footer.append(apply); }
    const run = button('Chỉ render', () => {
      if (typeof startPipeline === 'function') startPipeline(false, true, false);
    });
    run.id = 'content-render-run';
    run.title = 'Chạy với cấu hình đã lưu trên từng video. Áp dụng cấu hình trước nếu vừa chỉnh form.';
    footer.append(run, element('p', 'content-help', 'Áp dụng chỉ lưu cấu hình. Chỉ render dùng cấu hình đã lưu trên từng video.'));
    config.append(footer);
    const modeNames = {shorts_9_16: 'Dọc 9:16 · nền mờ', pip_thu_nho: 'Video thu nhỏ trên nền', full_16_9: 'Ngang 16:9', square_1_1: 'Vuông 1:1', audio_effect: 'Hiệu ứng âm thanh', md5_cut: 'Cắt video và xóa metadata', custom_ffmpeg: 'FFmpeg tùy chỉnh'};
    [...byId('rc-mode').options].forEach(option => { if (modeNames[option.value]) option.textContent = modeNames[option.value]; });
    const labels = {'rc-bg-video': 'Video nền', 'rc-bg-music': 'Nhạc nền', 'rc-ffmpeg-preset': 'Mẫu lệnh FFmpeg', 'rc-pitch': 'Thay đổi cao độ âm thanh'};
    Object.entries(labels).forEach(([id, label]) => {
      const field = byId(id);
      const target = field?.closest('label') || field?.parentElement.querySelector('label');
      if (!target) return;
      if (target.contains(field)) {
        [...target.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).forEach(node => node.remove());
        target.append(document.createTextNode(` ${label}`));
      } else { target.textContent = label; target.htmlFor = id; }
    });
    const fieldNames = {
      'rc-pad-size': 'Độ dày viền (px)', 'rc-pad-color': 'Màu viền HEX',
      'rc-wm-size': 'Kích thước watermark ảnh (W:H)', 'rc-wm-pos': 'Vị trí watermark ảnh (X:Y)',
      'rc-wm-opacity': 'Độ mờ watermark ảnh', 'rc-logo-size': 'Kích thước logo video (W:H)',
      'rc-logo-pos': 'Vị trí logo video (X:Y)', 'rc-logo-chroma': 'Màu nền chroma của logo',
      'rc-wm-text-size': 'Cỡ chữ watermark', 'rc-wm-text-pos': 'Vị trí chữ watermark (X:Y)',
      'rc-wm-text-color': 'Màu chữ watermark', 'rc-scroll-size': 'Cỡ chữ chạy',
      'rc-scroll-speed': 'Tốc độ chữ chạy (pixel/giây)', 'rc-scroll-color': 'Màu chữ chạy',
      'rc-intro-random': 'Chọn intro ngẫu nhiên', 'rc-outro-random': 'Chọn outro ngẫu nhiên',
      'rc-intro': 'Video intro', 'rc-outro': 'Video outro', 'rc-audio-orig': 'Âm thanh gốc (%)',
      'rc-audio-bg': 'Nhạc nền (%)', 'rc-custom-cmd': 'Lệnh FFmpeg tùy chỉnh',
    };
    Object.entries(fieldNames).forEach(([id, name]) => byId(id)?.setAttribute('aria-label', name));
    section.addEventListener('input', event => {
      if (event.target.id?.startsWith('rc-') || ['rp-name', 'rp-note'].includes(event.target.id)) {
        renderTouched = true; refreshRender();
      }
    });
    section.addEventListener('change', event => {
      if (event.target.id?.startsWith('rc-')) { renderTouched = true; refreshRender(); }
    });
    buildResources(resources);
    refreshRender({clean: true});
  }

  function init() {
    if (initialized) return;
    initialized = true;
    buildAI(); buildRender(); updateScope();
  }

  window.StudioContent = {init, updateScope, setAIBusy, showAIResult, showAIError, refreshRender, showResourcesError};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();
