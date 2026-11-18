/* Presentation helpers. Business mutations stay in app.js and server guards. */
(() => {
  'use strict';
  const dialogs = new Map();
  const closeHandlers = {
    'modal-edit-video': () => closeEditModal(),
    'modal-edit-channel': () => closeChannelEdit(),
    'modal-preview-video': () => closePreviewModal(),
    'modal-browse-folder': () => closeBrowseFolderModal(),
    'modal-auto-schedule': () => closeAutoScheduleModal(),
    'modal-bulk-description': () => closeBulkDescriptionModal(),
  };
  const names = {
    'tab-workspace': ['Video & dự án', 'Chuẩn bị video, kiểm tra lô đăng và theo dõi tiến độ.'],
    'tab-ai': ['AI & nội dung', 'Biên tập nội dung theo đúng phạm vi video đã chọn.'],
    'tab-render': ['Render & preset', 'Chọn preset, chỉnh thông số và áp dụng cho video.'],
    'tab-channels': ['Kênh & profile', 'Quản lý đích đăng, phiên đăng nhập và bộ tùy chọn.'],
    'tab-monitor': ['Theo dõi nguồn', 'Quản lý nguồn video và trạng thái các luồng tự động.'],
    'tab-settings': ['Hệ thống', 'Cài đặt, hoạt động, lưu trữ và chẩn đoán.'],
  };

  function createButton(label, handler, className = 'btn btn-outline') {
    const el = document.createElement('button');
    el.type = 'button'; el.className = className; el.textContent = label;
    if (handler) el.addEventListener('click', handler);
    return el;
  }

  function enhanceForm(root = document) {
    root.querySelectorAll('label:not([for])').forEach(label => {
      if (label.querySelector('input,select,textarea')) return;
      const field = label.nextElementSibling?.matches('input,select,textarea')
        ? label.nextElementSibling : label.parentElement?.querySelector('input:not([type=hidden]),select,textarea');
      if (field?.id && !field.labels?.length) label.htmlFor = field.id;
    });
    root.querySelectorAll('input[type=datetime-local]').forEach(input => {
      if (input.dataset.timezoneHint) return;
      input.dataset.timezoneHint = 'true';
      const hint = document.createElement('small'); hint.className = 'ui-field-hint';
      hint.textContent = `Lịch phát hành dùng giờ trên máy chạy tool. Múi giờ trình duyệt: ${Intl.DateTimeFormat().resolvedOptions().timeZone}; kiểm tra hai máy cùng múi giờ nếu truy cập từ máy khác.`;
      input.after(hint);
    });
    root.querySelectorAll('button').forEach(button => {
      if (!button.hasAttribute('aria-label') && !/[\p{L}\p{N}]/u.test(button.textContent || '')) {
        const title = button.title || (/[×✖]/.test(button.textContent) ? 'Đóng hộp thoại' : 'Thao tác');
        button.setAttribute('aria-label', title);
      }
    });
  }

  function nativeDialog(id) {
    let el = document.getElementById(id);
    if (!el || el instanceof HTMLDialogElement) return el;
    const replacement = document.createElement('dialog');
    for (const attr of el.attributes) replacement.setAttribute(attr.name, attr.value);
    replacement.removeAttribute('style');
    while (el.firstChild) replacement.appendChild(el.firstChild);
    el.replaceWith(replacement);
    return replacement;
  }

  function registerDialog(el) {
    if (dialogs.has(el.id)) return;
    const state = {dirty:false, busy:false, opener:null, disabled:new Map()};
    dialogs.set(el.id, state);
    const title = el.querySelector('h2,h3,.card-title');
    if (title) { title.id ||= `${el.id}-title`; el.setAttribute('aria-labelledby', title.id); }
    el.setAttribute('aria-modal', 'true');
    el.addEventListener('input', () => { state.dirty = true; });
    el.addEventListener('change', () => { state.dirty = true; });
    el.addEventListener('cancel', event => {
      event.preventDefault();
      if (!state.busy) (closeHandlers[el.id] || (() => closeModal(el.id)))();
    });
    el.addEventListener('close', () => {
      if (state.opener?.isConnected) state.opener.focus({preventScroll:true});
      else document.querySelector('.nav-tab[aria-current=page]')?.focus({preventScroll:true});
    });
  }

  function openModal(id) {
    const el = nativeDialog(id); if (!el) return false;
    registerDialog(el);
    const state = dialogs.get(id);
    state.dirty = false; state.opener = document.activeElement;
    setError(id, '');
    el.style.removeProperty('display');
    if (!el.open) el.showModal();
    const body = el.querySelector('.modal-body'); if (body) body.scrollTop = 0;
    enhanceForm(el);
    requestAnimationFrame(() => {
      const field = el.querySelector('input:not([type=hidden]):not(:disabled),textarea:not(:disabled),select:not(:disabled)');
      (field || el.querySelector('button'))?.focus({preventScroll:true});
    });
    return true;
  }

  function closeModal(id, {force = false} = {}) {
    const el = document.getElementById(id), state = dialogs.get(id);
    if (!el) return true;
    if (!force && state?.busy) return false;
    if (!force && state?.dirty && !window.confirm('Bạn có thay đổi chưa lưu. Đóng và bỏ các thay đổi này?')) return false;
    if (state) state.dirty = false;
    el.style.removeProperty('display');
    if (el.open) el.close();
    return true;
  }

  function setBusy(id, busy) {
    const el = document.getElementById(id), state = dialogs.get(id);
    if (!el || !state) return;
    if (state.busy === busy) return;
    state.busy = busy; el.setAttribute('aria-busy', String(busy));
    el.querySelectorAll('button,input,textarea,select').forEach(control => {
      if (busy) { state.disabled.set(control, control.disabled); control.disabled = true; }
      else if (state.disabled.has(control)) control.disabled = state.disabled.get(control);
    });
    if (!busy) state.disabled.clear();
  }

  function markClean(id) {
    const state = dialogs.get(id);
    if (state) state.dirty = false;
  }

  function setError(id, message) {
    const el = document.getElementById(id); if (!el) return;
    let error = el.querySelector('.ui-form-error');
    if (!error) {
      error = document.createElement('p'); error.className = 'ui-form-error'; error.setAttribute('role','alert');
      (el.querySelector('.modal-footer') || el).prepend(error);
    }
    error.textContent = message; error.hidden = !message;
  }

  function messageDialog({title, message, confirmLabel = 'Xác nhận', rows = [], destructive = false}, ask) {
    return new Promise(resolve => {
      const opener = document.activeElement;
      const dialog = document.createElement('dialog'); dialog.className = 'workspace-dialog ui-message-dialog';
      const heading = document.createElement('h2'); heading.textContent = title; heading.id = `ui-message-${Date.now()}`;
      dialog.setAttribute('aria-labelledby', heading.id);
      const p = document.createElement('p'); p.className = 'ui-message-copy'; p.textContent = message;
      dialog.append(heading,p);
      if (rows.length) {
        const list = document.createElement('div'); list.className = 'ui-result-rows';
        rows.forEach(row => { const item = document.createElement('p'); item.textContent = typeof row === 'string' ? row : JSON.stringify(row); list.append(item); });
        dialog.append(list);
      }
      const footer = document.createElement('div'); footer.className = 'ui-dialog-actions';
      let accepted = false;
      footer.append(createButton(ask ? 'Hủy' : 'Đóng', () => dialog.close()));
      if (ask) footer.append(createButton(confirmLabel, () => { accepted = true; dialog.close(); }, `btn ${destructive ? 'btn-danger' : 'btn-primary'}`));
      dialog.append(footer); document.body.append(dialog);
      dialog.addEventListener('close', () => { dialog.remove(); if (opener?.isConnected) opener.focus({preventScroll:true}); resolve(accepted); }, {once:true});
      dialog.showModal(); footer.querySelector('button').focus();
    });
  }

  function navigation(tabId, focus = false) {
    document.querySelectorAll('.nav-tab').forEach(link => {
      const active = link.dataset.tab === tabId;
      if (active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
    });
    document.getElementById('navigation-toggle')?.setAttribute('aria-expanded','false');
    document.body.classList.remove('navigation-open');
    const heading = document.querySelector(`#${tabId} .ui-page-heading h2`);
    if (focus) heading?.focus({preventScroll:true});
    const project = document.getElementById('sel-project');
    const context = document.getElementById('studio-project-context');
    if (context) context.textContent = project?.selectedOptions[0]?.textContent || 'Chọn dự án trong Video & dự án';
    window.StudioContent?.updateScope();
  }

  function setupEditGroups() {
    const body = document.querySelector('#modal-edit-video .modal-body'); if (!body) return;
    const fields = [...body.children];
    const groups = [
      ['Nội dung', ['ed-vid-title','ed-vid-desc']],
      ['Đích đăng & lịch', ['ed-vid-profile','ed-vid-channel','ed-vid-groups','ed-vid-option-group','ed-vid-visibility','ed-vid-schedule-time']],
      ['Render', ['ed-vid-res','ed-vid-render','ed-vid-cut-start','ed-vid-cut-end']],
      ['Tùy chọn bổ sung', ['ed-vid-age-restriction','ed-vid-altered-content','ed-vid-mobile']],
    ];
    groups.forEach(([title,ids],i) => {
      const details = document.createElement('details'); details.className='ui-form-section'; details.open=i<2;
      const summary=document.createElement('summary'); summary.textContent=title; details.append(summary);
      fields.filter(node=>ids.some(id=>node.id===id || node.querySelector(`#${id}`))).forEach(node=>details.append(node));
      body.append(details);
    });
  }

  function init() {
    document.body.classList.add('studio-ui');
    for (const [id,[title,description]] of Object.entries(names)) {
      const section=document.getElementById(id); if(!section) continue;
      const heading=document.createElement('div'); heading.className='ui-page-heading'; heading.id=id==='tab-workspace'?'workspace-heading':`${id}-heading`;
      const text=document.createElement('div'); const h=document.createElement('h2'); h.textContent=title; h.tabIndex=-1;
      const p=document.createElement('p'); p.textContent=description; text.append(h,p); heading.append(text); section.prepend(heading);
    }
    Object.keys(closeHandlers).forEach(id=>{const el=nativeDialog(id);if(el) registerDialog(el);});
    document.querySelector('#modal-edit-video')?.classList.add('ui-drawer');
    setupEditGroups(); enhanceForm(); navigation('tab-workspace');
    document.getElementById('navigation-toggle')?.addEventListener('click',event=>{
      const open=document.body.classList.toggle('navigation-open'); event.currentTarget.setAttribute('aria-expanded',String(open));
    });
    document.addEventListener('keydown',event=>{if(event.key==='Escape' && document.body.classList.contains('navigation-open')) {document.body.classList.remove('navigation-open');document.getElementById('navigation-toggle').setAttribute('aria-expanded','false');document.getElementById('navigation-toggle').focus();}});
    document.getElementById('sel-project')?.addEventListener('change',()=>navigation(document.querySelector('.nav-tab.active')?.dataset.tab || 'tab-workspace'));
  }

  window.StudioUI={createButton,enhanceForm,openModal,closeModal,setBusy,markClean,setError,navigation,
    confirm:options=>messageDialog(options,true),result:options=>messageDialog(options,false)};
  window.addEventListener('DOMContentLoaded',init);
})();
