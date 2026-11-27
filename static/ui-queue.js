/* Queue presentation. Selection, approval and mutations remain owned by app.js. */
(() => {
  'use strict';
  let ready = false;
  let lastLoaded = '';
  let lastProject = '';
  let rowsById = new Map();
  let channelSignature = '';
  const controls = {};
  const statuses = [
    ['all', 'Tất cả trạng thái'], ['waiting', 'Chờ xử lý / đã tải'],
    ['active', 'Đang xử lý'], ['rendered', 'Đã render'], ['review', 'Cần đối soát'],
    ['error', 'Có lỗi / đã hủy'], ['completed', 'Đã đăng'], ['stopped', 'Đã dừng'],
    ['other', 'Trạng thái khác'],
  ];
  const renderNames = {shorts_9_16:'Dọc 9:16',pip_thu_nho:'Video trong khung',full_16_9:'Ngang 16:9',square_1_1:'Vuông 1:1',audio_effect:'Hiệu ứng âm thanh',md5_cut:'Cắt & metadata',none:'Giữ bản gốc',custom_ffmpeg:'FFmpeg tùy chỉnh'};
  const visibilityNames = {public:'Công khai',private:'Riêng tư',unlisted:'Không công khai',schedule:'Lên lịch'};
  const $ = id => document.getElementById(id);
  const esc = value => escapeHtml(String(value ?? ''));
  const number = value => Number.isFinite(Number(value)) ? Math.max(0, Math.min(100, Number(value))) : 0;
  const actionButton = (label, action, className = 'btn btn-outline btn-sm') => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = className; button.textContent = label;
    button.addEventListener('click', action);
    return button;
  };
  function statusOf(item) {
    const status = String(item.status || '').trim();
    if (item.requires_review || status === 'NeedsReview') return {key:'review', label:'Cần đối soát'};
    if (status === 'Completed') return {key:'completed', label:'Đã đăng'};
    if (status === 'Error' || /copyright|bản quyền|cancelled|hủy/i.test(status)) return {key:'error',label:status === 'Error' ? 'Có lỗi' : 'Đã hủy'};
    if (status === 'Stopped' || /dừng/i.test(status)) return {key:'stopped',label:'Đã dừng'};
    if (/^(Downloading|Rendering|Uploading|Đang tải|Đang Render|Đang đăng|Chờ giãn cách|Chờ lượt upload)/i.test(status) || item.is_claimed) {
      const label = /^Downloading|^Đang tải/i.test(status) ? 'Đang tải' : /^Rendering|^Đang render/i.test(status) ? 'Đang render' : /^Uploading|^Đang đăng/i.test(status) ? 'Đang đăng' : /^Chờ (giãn cách|lượt upload)/i.test(status) ? 'Chờ lượt đăng' : 'Đang xử lý';
      return {key:'active',label};
    }
    if (status === 'Rendered') return {key:'rendered',label:'Đã render'};
    if (!status || /^(Pending|Downloaded)$/i.test(status)) return {key:'waiting',label:status === 'Downloaded' ? 'Đã tải' : 'Chờ xử lý'};
    return {key:'other',label:status};
  }
  function destination(item) {
    const id = Number(item.assigned_channel_id || item.target_channel_id);
    const channel = (gChannels || []).find(row => Number(row.id) === id);
    return {id: id || '', name:channel?.name || (id ? `Kênh #${id} không còn trong danh sách` : item.target_profile_name ? `Profile: ${item.target_profile_name}` : `Nhóm: ${groupNamesFromItem(item).join(' + ') || 'Mặc định'}`),
      profile:item.target_profile_name || channel?.profile_name || '', assigned:!!item.assigned_channel_id};
  }
  function measuredPercent(value) {
    return value == null || String(value).trim() === '' || !Number.isFinite(Number(value)) ? null : number(value);
  }
  function progressOf(item) {
    const state = statusOf(item), status = String(item.status || '');
    if (state.key === 'review') return {label:'Chờ đối soát',value:null};
    if (state.key === 'error' || state.key === 'stopped') return {label:state.label,value:null};
    if (/^Uploading|^Đang đăng/i.test(status) || state.key === 'completed') return {label:state.key === 'completed' ? 'Đăng hoàn tất' : 'Đang đăng',value:measuredPercent(item.upload_progress)};
    if (/^Rendering|^Đang render/i.test(status) || state.key === 'rendered') return {label:state.key === 'rendered' ? 'Render hoàn tất' : 'Đang render',value:measuredPercent(item.render_progress)};
    if (/^Downloading|^Đang tải/i.test(status) || status === 'Downloaded') return {label:status === 'Downloaded' ? 'Tải hoàn tất' : 'Đang tải',value:measuredPercent(item.download_progress)};
    return {label:state.label,value:null};
  }
  function statusText(item, state, progress) {
    if (state.key !== 'active') return state.label;
    if (state.label === 'Chờ lượt đăng' && Number(item.render_progress) === 100) {
      return 'Chờ lượt đăng · Đã render 100%';
    }
    if (progress.value !== null && progress.label === state.label) {
      return `${state.label} ${Math.round(progress.value)}%`;
    }
    return state.label;
  }
  function warningsOf(item) {
    try { const rows = JSON.parse(item.post_upload_warnings_json || '[]'); return Array.isArray(rows) ? rows.filter(row => row && typeof row === 'object') : []; } catch (_) { return []; }
  }
  function canEdit(item, state) {
    return !item.is_claimed && !item.published_url && !['review','completed','active'].includes(state.key);
  }
  function rowMarkup(item) {
    const id = Number(item.id), state = statusOf(item), dest = destination(item), progress = progressOf(item);
    const href = safePublishedVideoUrl(item.published_url), warnings = warningsOf(item);
    const editable = canEdit(item, state);
    const schedule = item.schedule_time ? String(item.schedule_time).replace('T', ' ') : visibilityNames[item.visibility] || item.visibility || 'Chưa đặt';
    return `<td class="uq-check"><input type="checkbox" class="row-chk" data-id="${id}" aria-label="Chọn video #${id}: ${esc(item.title)}"></td>
      <td class="uq-video"><button type="button" class="uq-title" data-queue-action="details">${esc(item.title || 'Chưa có tiêu đề')}</button><small>#${id} · ${esc(item.upload_to || 'Video')} · ${esc(item.platform || 'Nguồn chưa rõ')}</small>
        ${editable ? '<button type="button" class="btn btn-success btn-sm" data-queue-action="publish">Đăng</button>' : state.key === 'active' ? `<small class="uq-inline-progress">${esc(statusText(item,state,progress))}</small>` : state.key === 'review' ? '<button type="button" class="btn btn-outline btn-sm" data-queue-action="details">Đối soát</button>' : ''}</td>
      <td class="uq-channel">${editable ? `<button type="button" class="uq-channel-link" data-queue-action="edit" aria-label="Đổi kênh: ${esc(dest.name)}">${esc(dest.name)}</button>` : `<span>${esc(dest.name)}</span>`}<small>${esc(dest.profile)}${dest.assigned ? ' · Đã chốt' : ''}</small></td>
      <td class="uq-config"><span>${esc(schedule)}</span><small>${esc(renderNames[item.render_mode] || item.render_mode || 'Chưa đặt render')} · ${esc(item.resolution || 'Gốc')}</small></td>
      <td class="uq-progress"><small>${esc(progress.label)}</small>${progress.value === null ? '<span class="uq-muted">—</span>' : `<div class="uq-progress-line"><progress max="100" value="${progress.value}" aria-label="${esc(progress.label)} video #${id}"></progress><span>${Math.round(progress.value)}%</span></div>`}</td>
      <td class="uq-status"><span class="uq-badge uq-${state.key}">${esc(statusText(item,state,progress))}</span>${item.error_message ? `<small class="uq-error-text" title="${esc(item.error_message)}">${esc(item.error_message)}</small>` : ''}${warnings.length ? `<small class="uq-warning-text">${warnings.length} cảnh báo bước phụ</small>` : ''}${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">Xem trên YouTube</a>` : item.published_url ? '<small>URL video không hợp lệ</small>' : ''}</td>
      `;
  }
  function buildSummary(section, before) {
    const summary = document.createElement('div'); summary.className = 'uq-summary';
    const names = [['total','Tổng video'],['active','Đang xử lý'],['attention','Cần xử lý'],['completed','Đã đăng']];
    for (const [key,label] of names) {
      const box = document.createElement('div'); box.className = `uq-summary-card uq-summary-${key}`;
      box.innerHTML = `<span>${label}</span><strong id="uq-count-${key}">—</strong>`;
      if (key === 'active') {
        const stages = document.createElement('small');
        stages.id = 'uq-active-progress';
        box.append(stages);
      }
      if (key === 'attention') box.title = 'Video cần đối soát, có lỗi hoặc đã hủy. Đây là nhóm hiển thị, không thay đổi trạng thái video.';
      summary.append(box);
    }
    section.insertBefore(summary,before);
  }
  function configureSource(left, grid) {
    const project = $('sel-project')?.closest('.card');
    if (project) { project.classList.add('uq-project'); project.removeAttribute('style'); grid.before(project); }
    const details = document.createElement('details'); details.id = 'queue-source-panel'; details.className = 'uq-source-panel';
    const summary = document.createElement('summary'); summary.textContent = 'Thu gọn bảng thiết lập';
    details.append(summary); left.before(details); details.append(left);
    left.classList.add('uq-source-content');
    const scan = $('btn-subtab-scan'), bulk = $('btn-subtab-bulk');
    if (scan) scan.textContent = 'Thêm video';
    if (bulk) bulk.textContent = 'Gán cấu hình';
    const hint = $('subtab-pane-bulk')?.querySelector('.card-header + div');
    if (hint) {
      const actions = hint.querySelector('div');
      hint.replaceChildren();
      hint.append(document.createTextNode('Chọn video hoặc lọc hàng đợi, đặt các giá trị cần thay đổi rồi áp dụng. Thiết lập giả lập điện thoại luôn được áp dụng: bỏ tích = Desktop; hãy kiểm tra trước khi lưu. Xuất Excel dùng dòng đã chọn hoặc toàn bộ dự án; nhập Excel cập nhật các dòng khớp.'));
      if (actions) hint.append(actions);
    }
    const sourceCard = $('subtab-pane-scan')?.querySelector('.card');
    const drop = $('upload-dropzone'), urlField = $('scan-urls')?.closest('.form-group');
    if (sourceCard && drop && urlField) {
      const modes = document.createElement('div'); modes.className = 'uq-source-modes'; modes.setAttribute('role','group'); modes.setAttribute('aria-label','Loại nguồn video');
      const modeHint = document.createElement('p'); modeHint.className = 'uq-source-hint';
      const fileActions = [...drop.querySelectorAll('button')].filter(btn => /file-upload-input|browseNativeFiles/.test(btn.getAttribute('onclick') || ''));
      const folderActions = [...drop.querySelectorAll('button')].filter(btn => /browseNativeFolder|openBrowseFolderModal/.test(btn.getAttribute('onclick') || ''));
      const separator = drop.nextElementSibling?.id === 'upload-status-box' ? drop.nextElementSibling.nextElementSibling : null;
      const choose = mode => {
        modes.querySelectorAll('button').forEach(btn => btn.setAttribute('aria-pressed', String(btn.dataset.mode === mode)));
        drop.hidden = mode === 'link'; urlField.hidden = mode === 'file';
        if ($('btn-scan')) { $('btn-scan').hidden = mode === 'file'; $('btn-scan').textContent = 'Quét nguồn & thêm vào hàng đợi'; }
        fileActions.forEach(btn => {btn.hidden = mode !== 'file';}); folderActions.forEach(btn => {btn.hidden = mode !== 'folder';});
        if (separator && separator.contains(separator.querySelector('span'))) separator.hidden = true;
        modeHint.textContent = mode === 'file' ? 'Đặt cấu hình bên dưới trước khi chọn tệp. Tệp được nạp vào hàng đợi ngay sau khi chọn; không tự chạy pipeline.' : mode === 'folder' ? 'Đặt cấu hình trước khi duyệt. Duyệt thư mục Windows sẽ quét ngay các nguồn trong ô bên dưới, gồm nguồn đã nhập trước đó. Tệp nguồn được giữ nguyên.' : 'Dán liên kết kênh/video hoặc đường dẫn thư mục trên máy chạy tool, mỗi dòng một nguồn.';
        const title = drop.querySelector('.dropzone-content b');
        if (title) title.textContent = mode === 'folder' ? 'Chọn thư mục nguồn' : 'Kéo thả hoặc chọn tệp video';
      };
      [['file','Tệp'],['folder','Thư mục'],['link','Liên kết']].forEach(([mode,label]) => {
        const btn = actionButton(label, () => choose(mode)); btn.dataset.mode = mode; modes.append(btn);
      });
      drop.before(modes,modeHint); choose('file');
    }
    return details;
  }
  function addToolbar(card, old) {
    const buttons = [...old.querySelectorAll('button')];
    const find = expression => buttons.find(button => (button.getAttribute('onclick') || '').replace(/\s/g,'') === expression.replace(/\s/g,''));
    const toolbar = document.createElement('div'); toolbar.className = 'uq-toolbar';
    const main = document.createElement('div'); main.className = 'uq-toolbar-main';
    const menus = document.createElement('div'); menus.className = 'uq-more-actions'; menus.id = 'queue-more-actions'; menus.hidden = true;
    const more = actionButton('Thao tác khác', () => {
      menus.hidden = !menus.hidden;
      more.setAttribute('aria-expanded', String(!menus.hidden));
    });
    more.id = 'queue-more-toggle';
    more.setAttribute('aria-controls', menus.id);
    more.setAttribute('aria-expanded', 'false');
    menus.addEventListener('keydown', event => {
      if (event.key === 'Escape') { menus.hidden = true; more.setAttribute('aria-expanded','false'); more.focus(); }
    });
    const moved = new Set();
    function move(button,host,label) { if (!button) return; moved.add(button); button.removeAttribute('style'); if (label) button.textContent = label; button.className = 'btn btn-outline btn-sm'; host.append(button); }
    function group(label, entries) {
      const section = document.createElement('section');
      const heading = document.createElement('h3'); heading.textContent = label;
      const body = document.createElement('div'); section.append(heading, body); menus.append(section);
      entries.forEach(([handler,text]) => move(find(handler),body,text));
      return body;
    }
    const mode = document.createElement('select');
    mode.id = 'queue-publish-mode'; mode.setAttribute('aria-label','Cách xử lý trước khi đăng');
    mode.add(new Option('Không render','file'));
    mode.add(new Option('Render rồi đăng','full'));
    mode.title = 'Không render: dùng bản render đã lưu nếu có, nếu chưa có thì dùng tệp nguồn trên máy.';
    controls.mode = mode;
    controls.direct = actionButton('Đăng ngay', () => {
      const current = new Set(gQueueItems.map(item => Number(item.id)));
      const ids = [...gSelectedIds].filter(id => current.has(id));
      publishNow(ids);
    }, 'btn btn-primary');
    controls.direct.id = 'queue-publish-now';
    const add = actionButton('Thêm video', () => openSource('scan'));
    controls.bulk = actionButton('Chọn profile & kênh', () => {
      const ids = [...gSelectedIds];
      if (ids.length) chooseDestination(ids);
    });
    main.append(add, controls.bulk, mode, controls.direct);
    const publishing = group('Đăng video', []);
    publishing.id = 'queue-publish-actions';
    controls.upload = find('startPipeline(false,false,true)');
    move(controls.upload,publishing,'Xem trước & đăng');
    if (controls.upload) {
      controls.upload.removeAttribute('onclick');
      controls.upload.title = 'Xem tệp, kênh và chế độ hiển thị trước khi xác nhận.';
      controls.upload.addEventListener('click', () => {
        const full = mode.value === 'full';
        startPipeline(full, full, true);
      });
    }
    group('Xử lý', [['startPipeline(true,false,false)','Chỉ tải video'],['startPipeline(false,true,false)','Chỉ render'],['startPipeline(true,true,true)','Chạy toàn trình'],['previewSelectedDistribution()','Xem phân phối'],['reviewUploadSlot()','Kiểm tra lượt đăng']]);
    const edit = group('Biên tập', [['promptAutoSchedule()','Lên lịch giãn cách'],['applyShortsBooster()','Tối ưu cấu hình Shorts'],['openBulkDescriptionModal()','Mô tả hàng loạt']]);
    group('Nhập / xuất', [['exportExcel()','Xuất Excel'],['triggerImportExcel()','Excel: cập nhật video'],['exportCsv()','CSV: xuất toàn dự án'],['triggerImportCsv()','CSV: thêm video']]);
    controls.excel = find('exportExcel()'); controls.importExcel = find('triggerImportExcel()');
    controls.stop = find('stopPipeline()'); move(controls.stop,main,'Dừng lô hiện tại');
    if (controls.stop) {controls.stop.className = 'btn btn-danger'; controls.stop.hidden = true; controls.stop.title = 'Dừng lô thủ công đang chạy. Video đang upload có thể hoàn tất trước khi dừng hẳn. Nguồn tự động cần dừng riêng ở Theo dõi nguồn.';}
    main.append(more);
    const filters = document.createElement('div'); filters.className = 'uq-filters';
    const search = $('queue-search');
    if (search) { const wrap = search.closest('.search-input-wrap'); filters.append(wrap || search); wrap?.querySelector('.search-icon')?.setAttribute('aria-hidden','true'); search.setAttribute('aria-label','Tìm video theo tiêu đề, ID hoặc thông tin trong bảng'); search.placeholder = 'Tìm tiêu đề, mã video…'; }
    const status = document.createElement('select'); status.id = 'queue-status-filter'; status.setAttribute('aria-label','Lọc trạng thái video');
    statuses.forEach(([value,label]) => status.add(new Option(label,value)));
    const channel = document.createElement('select'); channel.id = 'queue-channel-filter'; channel.setAttribute('aria-label','Lọc kênh đích'); channel.add(new Option('Tất cả kênh',''));
    const reset = actionButton('Xóa bộ lọc',resetFilters); reset.id = 'queue-clear-filters';
    const density = actionButton('Bảng gọn',() => {
      const table = $('queue-tbody').closest('table'); const compact = table.classList.toggle('uq-compact');
      density.textContent = compact ? 'Bảng thoáng' : 'Bảng gọn'; density.setAttribute('aria-pressed',String(compact));
      try {localStorage.setItem('dhb.ui.queue-density.v1', compact ? 'compact' : 'comfortable');} catch (_) {}
    }); density.setAttribute('aria-pressed','false'); density.id = 'queue-density';
    filters.append(status,channel,reset,density); controls.status = status; controls.channel = channel;
    [status,channel].forEach(el => el.addEventListener('change',() => filter($('queue-search')?.value || '')));
    const scope = document.createElement('div'); scope.className = 'uq-scope';
    const summary = $('queue-selection-summary'); summary.removeAttribute('style'); scope.append(summary);
    const actions = document.createElement('div'); actions.className = 'uq-scope-actions';
    move($('queue-quick-edit'),edit,'Sửa nhanh');
    edit.prepend(actionButton('Gán cấu hình hàng loạt', () => openSource('bulk')));
    const selection = document.createElement('details'); selection.className = 'uq-menu';
    const selectionTitle = document.createElement('summary'); selectionTitle.textContent = 'Lựa chọn';
    const selectionBody = document.createElement('div'); selectionBody.className = 'uq-menu-body';
    move(find('toggleSelectAll(true)'),selectionBody,'Chọn các dòng hiển thị');
    move(find('toggleSelectAll(false)'),selectionBody,'Bỏ chọn dòng hiển thị');
    selectionBody.append(actionButton('Bỏ toàn bộ lựa chọn',() => {gSelectedIds.clear(); $('queue-tbody').querySelectorAll('.row-chk').forEach(box => {box.checked = false;}); updateQueueSelectionSummary();}));
    selection.append(selectionTitle,selectionBody); selectionBody.addEventListener('click',event => {if (event.target.closest('button')) selection.open = false;}); actions.append(selection);
    controls.delete = find('deleteSelectedQueue()'); move(controls.delete,edit,'Xóa dòng chọn');
    if (controls.delete) controls.delete.classList.add('uq-delete');
    scope.append(actions);
    // Preserve hidden file inputs and any future actions that have not been grouped yet.
    old.querySelectorAll('input[type=file]').forEach(input => toolbar.append(input));
    const toggle = $('btn-toggle-sidebar'); if (toggle) {moved.add(toggle); toggle.remove();}
    buttons.filter(button => !moved.has(button)).forEach(button => move(button,menus));
    toolbar.append(main,menus,filters,scope); old.replaceWith(toolbar);
    const meta = document.createElement('div'); meta.className = 'uq-table-meta';
    const results = document.createElement('p'); results.id = 'queue-filter-summary';
    const fresh = document.createElement('p'); fresh.id = 'queue-freshness'; fresh.setAttribute('role','status'); fresh.textContent = 'Đang tải hàng đợi…';
    meta.append(results,fresh); card.append(meta);
  }
  function init() {
    if (ready) return;
    const section = $('tab-workspace'), grid = section?.querySelector('.workspace-grid'), tbody = $('queue-tbody');
    if (!grid || !tbody) return;
    const left = grid.firstElementChild, card = tbody.closest('.card'), old = card.querySelector('.toolbar-wrap');
    if (!left || !old) return;
    ready = true; section.classList.add('uq-enabled'); grid.classList.remove('collapsed-sidebar');
    configureSource(left,grid); buildSummary(section,grid);
    addToolbar(card,old); card.classList.add('uq-queue-card');
    const table = tbody.closest('table'); table.classList.add('uq-table'); table.setAttribute('aria-label','Video trong dự án');
    table.querySelector('thead').innerHTML = '<tr><th scope="col" class="uq-check"><input type="checkbox" id="chk-all" aria-label="Chọn các video đang hiển thị"></th><th scope="col">Video</th><th scope="col">Kênh</th><th scope="col">Cấu hình & lịch</th><th scope="col">Tiến độ</th><th scope="col">Trạng thái</th></tr>';
    $('chk-all').addEventListener('change',event => toggleSelectAll(event.target.checked));
    const wrap = table.closest('.table-wrap'); wrap.tabIndex = 0; wrap.setAttribute('role','region'); wrap.setAttribute('aria-label','Bảng video, có thể cuộn để xem thêm');
    tbody.addEventListener('change',event => {if (event.target.matches('.row-chk')) onRowCheck(Number(event.target.dataset.id),event.target.checked);});
    tbody.addEventListener('click',event => {
      const button = event.target.closest('[data-queue-action]'); if (!button) return;
      const id = Number(button.closest('tr').dataset.queueId);
      switch (button.dataset.queueAction) {
        case 'details': showDetails(id); break;
        case 'edit': chooseDestination([id]); break;
        case 'publish': publishNow([id]); break;
      }
    });
    try {if (localStorage.getItem('dhb.ui.queue-density.v1') === 'compact') {table.classList.add('uq-compact'); $('queue-density').textContent = 'Bảng thoáng'; $('queue-density').setAttribute('aria-pressed','true');}} catch (_) {}
    window.StudioUI?.enhanceForm(section);
  }
  function updateChannelOptions(items) {
    const channels = new Map();
    items.forEach(item => {const dest = destination(item); if (dest.id) channels.set(String(dest.id),dest.name);});
    const choices = [...channels.entries()].sort((a,b) => a[1].localeCompare(b[1],'vi'));
    const signature = JSON.stringify(choices); if (signature === channelSignature) return;
    channelSignature = signature; const previous = controls.channel.value;
    controls.channel.replaceChildren(new Option('Tất cả kênh',''),new Option('Chưa chốt kênh cụ thể','unassigned'));
    choices.forEach(([id,name]) => controls.channel.add(new Option(name,id)));
    if (previous && ![...controls.channel.options].some(option => option.value === previous)) controls.channel.add(new Option('Kênh không còn trong kết quả',previous));
    controls.channel.value = previous;
  }
  function render(items) {
    if (!ready) init(); if (!ready) return false;
    const tbody = $('queue-tbody'), table = tbody.closest('.table-wrap');
    const scrollTop = table.scrollTop, scrollLeft = table.scrollLeft;
    const project = $('sel-project')?.value || '';
    if (project !== lastProject) {lastProject = project; rowsById.clear(); tbody.replaceChildren();}
    updateChannelOptions(items);
    const itemIds = new Set(items.map(item => Number(item.id)));
    for (const [id,row] of rowsById) if (!itemIds.has(id)) {row.remove(); rowsById.delete(id);}
    tbody.querySelectorAll('.uq-empty-row').forEach(row => row.remove());
    let cursor = tbody.firstElementChild;
    for (const item of items) {
      const id = Number(item.id); if (!Number.isSafeInteger(id)) continue;
      let row = rowsById.get(id);
      if (!row) {row = document.createElement('tr'); row.dataset.queueId = String(id); rowsById.set(id,row);}
      const html = rowMarkup(item);
      if (row._queueHtml !== html) {
        const active = row.contains(document.activeElement) ? document.activeElement : null;
        const focusAction = active?.dataset.queueAction;
        const focusedCheckbox = active?.classList.contains('row-chk');
        row.innerHTML = html; row._queueHtml = html;
        if (focusAction) row.querySelector(`[data-queue-action="${focusAction}"]`)?.focus({preventScroll:true});
        else if (focusedCheckbox) row.querySelector('.row-chk')?.focus({preventScroll:true});
      }
      row.querySelector('.row-chk').checked = gSelectedIds.has(id);
      row.classList.toggle('uq-selected',gSelectedIds.has(id));
      row.dataset.state = statusOf(item).key;
      row.dataset.channel = String(destination(item).id || 'unassigned');
      row._queueSearch = `${row.textContent} ${item.description || ''} ${item.source_url || ''}`.toLocaleLowerCase('vi');
      if (row !== cursor) tbody.insertBefore(row,cursor);
      cursor = row.nextElementSibling;
    }
    const counts = {total:items.length,active:0,attention:0,completed:0};
    const activeStages = [];
    for (const item of items) {
      const state = statusOf(item);
      if (state.key === 'active') {
        counts.active++;
        activeStages.push(`#${item.id} · ${statusText(item, state, progressOf(item))}`);
      }
      if (state.key === 'review' || state.key === 'error') counts.attention++;
      if (state.key === 'completed') counts.completed++;
    }
    $('uq-active-progress').textContent = activeStages.join('\n');
    Object.entries(counts).forEach(([key,value]) => { $(`uq-count-${key}`).textContent = String(value); });
    lastLoaded = new Date().toLocaleTimeString('vi-VN');
    $('queue-freshness').textContent = `Cập nhật lúc ${lastLoaded}`; $('queue-freshness').classList.remove('uq-stale');
    filter($('queue-search')?.value || ''); table.scrollTop = scrollTop; table.scrollLeft = scrollLeft;
    return true;
  }
  function filter(query = '') {
    if (!ready) return false;
    const q = String(query).toLocaleLowerCase('vi').trim(), status = controls.status.value, channel = controls.channel.value;
    let visible = 0;
    for (const row of rowsById.values()) {
      const matches = (!q || row._queueSearch.includes(q)) && (status === 'all' || status === row.dataset.state) && (!channel || channel === row.dataset.channel);
      row.style.display = matches ? '' : 'none'; if (matches) visible++;
    }
    const tbody = $('queue-tbody'); tbody.querySelectorAll('.uq-empty-row').forEach(row => row.remove());
    if (!visible) {
      const row = document.createElement('tr'); row.className = 'uq-empty-row';
      row.innerHTML = `<td colspan="6"><strong>${gQueueItems.length ? 'Không có video phù hợp bộ lọc' : 'Chưa có video trong dự án'}</strong><p>${gQueueItems.length ? 'Thử xóa bộ lọc để xem lại hàng đợi.' : 'Thêm tệp, thư mục hoặc liên kết để bắt đầu.'}</p></td>`;
      row.firstElementChild.append(actionButton(gQueueItems.length ? 'Xóa bộ lọc' : 'Thêm video',gQueueItems.length ? resetFilters : () => openSource('scan'))); tbody.append(row);
    }
    $('queue-filter-summary').textContent = `${visible} / ${gQueueItems.length} video hiển thị · Phạm vi: dự án hiện tại`;
    $('queue-clear-filters').disabled = !q && status === 'all' && !channel;
    updateQueueSelectionSummary();
    return true;
  }
  function resetFilters() {
    if ($('queue-search')) $('queue-search').value = '';
    controls.status.value = 'all'; controls.channel.value = ''; filter('');
  }
  async function publishNow(ids) {
    if (!ids.length || gPipelineStartPending) return;
    if (controls.batchActive) {
      showToast('Một lô đang chạy. Xem tiến độ trong hàng đợi.', 'warning');
      return;
    }
    const full = controls.mode.value === 'full';
    controls.checking = true;
    updateScope();
    try {
      await startPipeline(full, full, true, ids, {direct:true});
    } finally {
      controls.checking = false;
      updateScope();
    }
  }
  function updateScope() {
    if (!ready) return;
    const current = new Set(gQueueItems.map(item => Number(item.id)));
    const selected = [...gSelectedIds].filter(id => current.has(Number(id)));
    const visible = getVisibleQueueIds(); const hidden = selected.filter(id => !visible.includes(Number(id))).length;
    const count = selected.length || visible.length;
    controls.direct.disabled = !selected.length || controls.batchActive || Boolean(controls.checking);
    controls.direct.textContent = controls.checking ? 'Đang kiểm tra lô…' : selected.length ? `Đăng ngay (${selected.length})` : 'Đăng ngay';
    controls.direct.title = selected.length ? 'Bắt đầu đăng các video đã chọn theo kênh và lịch đã cấu hình.' : 'Tích chọn các video muốn đăng.';
    $('queue-selection-summary').textContent = selected.length ? `Đã chọn ${selected.length} video${hidden ? ` · ${hidden} đang ẩn` : ''}.` : 'Tích video để đăng cả lô hoặc bấm Đăng tại từng dòng.';
    for (const row of rowsById.values()) row.classList.toggle('uq-selected',gSelectedIds.has(Number(row.dataset.queueId)));
    if (controls.delete) {controls.delete.disabled = !selected.length; controls.delete.textContent = selected.length ? `Xóa ${selected.length} dòng chọn` : 'Xóa dòng chọn';}
    if (controls.upload) {controls.upload.disabled = count === 0; controls.upload.textContent = count ? `Xem trước & đăng (${count})` : 'Xem trước & đăng';}
    controls.bulk.disabled = !selected.length;
    controls.bulk.title = selected.length ? 'Đặt kênh đích cho các video đã chọn.' : 'Tích chọn video để đặt kênh đích.';
    const bulkApply = document.querySelector('[onclick="applyBulkConfig()"]');
    if (bulkApply) bulkApply.textContent = `Áp dụng cho ${selected.length || visible.length} video ${selected.length ? 'đã chọn' : 'theo bộ lọc'}`;
    if (controls.excel) controls.excel.textContent = selected.length ? `Excel: xuất ${selected.length} dòng đã chọn` : 'Excel: xuất toàn bộ dự án';
    if (controls.importExcel) controls.importExcel.textContent = selected.length ? `Excel: cập nhật trong ${selected.length} dòng đã chọn` : 'Excel: cập nhật video trong dự án';
    document.querySelectorAll('[data-queue-scope]').forEach(node => {
      node.textContent = `${$('sel-project')?.value || 'Dự án'} · ${selected.length ? `${selected.length} video đã chọn${hidden ? ` (${hidden} đang ẩn)` : ''}` : `${visible.length} video theo bộ lọc`}`;
    });
    document.dispatchEvent(new CustomEvent('studio:queue-scope',{detail:{count,selected:selected.length,hidden,visible:visible.length,project:$('sel-project')?.value || ''}}));
  }
  function updateRunning(data) {
    if (!ready) return;
    const stopping = data.batch_state === 'stopping';
    const active = Boolean(data.pipeline_running || stopping);
    if (controls.batchActive !== active) {
      controls.batchActive = active;
      updateScope();
    }
    if (controls.stop) {controls.stop.hidden = !data.pipeline_running && !stopping; controls.stop.disabled = stopping; controls.stop.textContent = stopping ? 'Đang dừng lô hiện tại…' : 'Dừng lô hiện tại';}
  }
  function setError(message) {
    if (!ready) return;
    const el = $('queue-freshness'); el.classList.add('uq-stale');
    el.textContent = `Không cập nhật được hàng đợi${lastLoaded ? ` · Dữ liệu lúc ${lastLoaded}` : ''}. ${message || 'Thử tải lại.'}`;
  }
  function onOverviewError() {
    if (!ready || !controls.stop) return;
    // Keep a previously visible stop action available when telemetry is stale.
    controls.stop.title = 'Không đọc được trạng thái mới. Hành động này yêu cầu dừng lô thủ công theo kiểm tra của máy chủ; nguồn tự động cần dừng riêng.';
  }
  function openSource(mode = 'scan') {
    if (!ready) init(); const panel = $('queue-source-panel'); if (!panel) return;
    panel.open = true; switchSubTab(mode);
    panel.scrollIntoView({block:'nearest',behavior:'auto'});
    $(mode === 'bulk' ? 'btn-subtab-bulk' : 'btn-subtab-scan')?.focus({preventScroll:true});
  }
  function toggleSource() { const panel = $('queue-source-panel'); if (!panel) return false; panel.open = !panel.open; return true; }
  function chooseDestination(ids) {
    const project = $('sel-project').value;
    const selected = new Set(ids);
    const editableSelection = () => gQueueItems.filter(item => selected.has(item.id) && canEdit(item,statusOf(item)));
    const items = editableSelection();
    if (!items.length || items.length !== selected.size) {
      showToast('Chọn các video còn có thể chỉnh sửa để đổi kênh.', 'warning');
      return;
    }
    const dialog = document.createElement('dialog');
    dialog.className = 'workspace-dialog uq-destination-dialog';
    dialog.setAttribute('aria-labelledby','queue-destination-title');
    dialog.innerHTML = `<form><h2 id="queue-destination-title">Chọn profile &amp; kênh</h2>
      <p></p><label for="queue-dest-profile">Profile Chrome</label><select id="queue-dest-profile"></select>
      <label for="queue-dest-channel">Kênh YouTube đích</label><select id="queue-dest-channel" required></select>
      <div class="uq-detail-actions"><button type="button" class="btn btn-outline">Hủy</button>
      <button type="submit" class="btn btn-primary">Lưu kênh</button></div></form>`;
    document.body.append(dialog);
    dialog.querySelector('p').textContent = items.length === 1 ? `#${items[0].id} · ${items[0].title || 'Video'}` : `${items.length} video đã chọn · ${project}`;
    const profile = $('queue-dest-profile'), channel = $('queue-dest-channel');
    const save = dialog.querySelector('[type=submit]');
    profile.add(new Option('Tất cả profile',''));
    const names = new Set([...gProfiles.map(p => p.name), ...gChannels.map(c => c.profile_name)].filter(Boolean));
    for (const name of names) profile.add(new Option(name,name));
    const first = destination(items[0]);
    if (names.has(first.profile) && items.every(item => destination(item).profile === first.profile)) profile.value = first.profile;
    function refresh() {
      fillTargetChannels(profile.id,channel.id,'Chọn kênh YouTube');
      save.disabled = !channel.value;
    }
    refresh();
    if (first.id && items.every(item => destination(item).id === first.id)) channel.value = String(first.id);
    save.disabled = !channel.value;
    profile.onchange = refresh;
    channel.onchange = () => {save.disabled = !channel.value;};
    dialog.querySelector('[type=button]').onclick = () => dialog.close();
    dialog.addEventListener('close', () => dialog.remove(), {once:true});
    let saving = false;
    dialog.addEventListener('cancel', event => {if (saving) event.preventDefault();});
    dialog.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      if (saving) return;
      const picked = gChannels.find(c => String(c.id) === channel.value && (!profile.value || c.profile_name === profile.value));
      if (!picked || $('sel-project').value !== project || editableSelection().length !== selected.size) {
        showToast('Video hoặc kênh đã thay đổi. Hãy chọn lại.', 'warning');
        return;
      }
      saving = true;
      const cancel = dialog.querySelector('[type=button]');
      save.disabled = cancel.disabled = true;
      try {
        const result = await apiFetch('/api/queue/bulk-update', {method:'POST', body:JSON.stringify({
          ids, updates:{target_type:'CHANNEL',target_channel_id:picked.id,target_profile_name:picked.profile_name || ''}
        })});
        if (!result.ok || result.existing !== ids.length) throw new Error('Danh sách video đã thay đổi. Hãy kiểm tra lại hàng đợi.');
        dialog.close();
        showToast(`Đã lưu kênh cho ${ids.length} video.`, 'success');
        await loadQueue(true);
      } catch (error) {showToast(error.message,'error');}
      finally {saving = false; save.disabled = !channel.value; cancel.disabled = false;}
    };
    dialog.showModal();
  }
  function showDetails(id) {
    const item = gQueueItems.find(row => Number(row.id) === Number(id)); if (!item || document.querySelector('.uq-details-dialog[open]')) return;
    const opener = document.activeElement, state = statusOf(item), dest = destination(item), warnings = warningsOf(item);
    const dialog = document.createElement('dialog'); dialog.className = 'workspace-dialog uq-details-dialog'; dialog.setAttribute('aria-labelledby','uq-details-title');
    const title = document.createElement('h2'); title.id = 'uq-details-title'; title.textContent = `${state.key === 'review' ? 'Đối soát' : 'Chi tiết'} video #${Number(item.id)}`;
    const intro = document.createElement('p'); intro.className = 'uq-detail-title'; intro.textContent = item.title || 'Chưa có tiêu đề';
    const content = document.createElement('div'); content.className = 'uq-detail-body';
    const field = (label,value) => `<div><dt>${label}</dt><dd>${esc(value || 'Chưa có')}</dd></div>`;
    content.innerHTML = `<span class="uq-badge uq-${state.key}">${esc(state.label)}</span><p class="uq-detail-note">Thông tin tại lúc mở. Mọi thay đổi được kiểm tra lại trước khi ghi.</p>
      <h3>Nội dung</h3><p class="uq-description">${esc(item.description || 'Chưa có mô tả')}</p><dl>${field('Thẻ từ khóa',item.tags)}${field('Nguồn',item.source_url)}</dl>
      <h3>Đích & lịch</h3><dl>${field('Kênh đích',dest.name + (dest.assigned ? ' · Đã chốt' : ''))}${field('Profile',dest.profile)}${field('Hiển thị',visibilityNames[item.visibility] || item.visibility)}${field('Lịch đã lưu',item.schedule_time)}${field('Đối tượng',item.made_for_kids ? 'Dành cho trẻ em' : 'Không dành cho trẻ em')}</dl>
      <h3>Render & tệp</h3><dl>${field('Cấu hình',renderNames[item.render_mode] || item.render_mode)}${field('Độ phân giải',item.resolution)}${field('Tệp đầu vào',item.input_video_path)}${field('Tệp kết quả',item.output_video_path)}</dl>
      <h3>Tiến độ đã ghi nhận</h3><div class="uq-detail-progress">${[['Tải',item.download_progress],['Render',item.render_progress],['Upload',item.upload_progress]].map(([label,value]) => `<div><span>${label}: ${Math.round(number(value))}%</span><progress value="${number(value)}" max="100" aria-label="${label} video #${Number(item.id)}"></progress></div>`).join('')}</div>
      ${item.error_message ? `<h3>Thông tin cần xử lý</h3><p class="uq-detail-error">${esc(item.error_message)}</p>` : ''}
      ${warnings.length ? `<h3>Cảnh báo bước phụ</h3><ul>${warnings.map(w => `<li>${esc(w.message || w.code || '')}</li>`).join('')}</ul><p>Kiểm tra video hiện có trước khi xử lý bước phụ; không cần đăng lại video.</p>` : ''}`;
    const href = safePublishedVideoUrl(item.published_url);
    if (href) {const a = document.createElement('a'); a.href = href; a.textContent = 'Mở video đã ghi nhận trên YouTube'; a.target = '_blank'; a.rel = 'noopener noreferrer'; content.append(a);}
    const footer = document.createElement('footer'); footer.className = 'uq-detail-actions';
    let followup = null;
    function next(label,action) {return actionButton(label,() => {followup = action; dialog.close();});}
    const close = actionButton('Đóng',() => dialog.close()); close.autofocus = true; footer.append(close);
    footer.append(next('Xem video',() => openPreviewModal(Number(item.id))));
    if (state.key === 'review') {
      const help = document.createElement('p'); help.className = 'uq-review-help'; help.textContent = 'Xem bằng chứng và kiểm tra YouTube trước khi quyết định. Video ID đã nhận chưa đồng nghĩa đã đăng thành công. Không tự chạy lại sau đối soát.'; content.prepend(help);
      footer.append(next('Xem bằng chứng',() => openUploadEvidence(Number(item.id))),next('Xác nhận đã đăng',() => manualReuploadQueueItem(Number(item.id),'confirmed_published')),next('Xác nhận chưa đăng',() => manualReuploadQueueItem(Number(item.id),'reupload')));
    } else {
      if (canEdit(item,state)) footer.append(next('Chỉnh sửa video',() => openEditModal(Number(item.id))));
      if (state.key === 'completed') footer.append(next('Cho phép đăng lại riêng video',() => manualReuploadQueueItem(Number(item.id),'reupload')));
    }
    dialog.append(title,intro,content,footer); document.body.append(dialog);
    dialog.addEventListener('close',() => {
      dialog.remove();
      if (opener?.isConnected) opener.focus({preventScroll:true});
      // Hand off after this dialog has restored focus, so it cannot steal focus
      // from the editor/evidence dialog opened by the next deliberate action.
      if (followup) requestAnimationFrame(() => followup());
    },{once:true});
    dialog.showModal();
  }
  window.StudioQueue = {init,render,filter,updateScope,updateRunning,setError,
    onLoadError:error => setError(error?.message || ''),onOverviewError,
    openSource,toggleSource,showDetails,statusOf,resetFilters};
  window.addEventListener('DOMContentLoaded',init);
})();
