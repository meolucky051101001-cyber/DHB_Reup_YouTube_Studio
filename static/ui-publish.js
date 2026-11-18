/* Guided publishing entry point; existing preview, approval and worker guards own execution. */
(() => {
  'use strict';
  let session = null;
  const byId = id => document.getElementById(id);
  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  function queueItems() { return typeof gQueueItems !== 'undefined' ? gQueueItems : []; }
  function targetIds() {
    return typeof getTargetIdsOrAlert === 'function'
      ? [...new Set(getTargetIdsOrAlert().map(Number).filter(Number.isFinite))] : [];
  }
  function currentProject() {
    const select = byId('sel-project');
    return {value: select?.value || '', label: select?.selectedOptions[0]?.textContent || select?.value || 'Chưa tải dự án'};
  }
  function selectedIds() {
    const current = new Set(queueItems().map(item => Number(item.id)));
    return typeof gSelectedIds !== 'undefined' ? [...gSelectedIds].map(Number).filter(id => current.has(id)) : [];
  }
  function itemState(item) {
    const status = String(item.status || '').trim();
    if (item.requires_review || status === 'NeedsReview') return 'review';
    if (status === 'Completed' || String(item.published_url || '').trim()) return 'completed';
    if (item.is_claimed || item.claim_token || /^(Uploading|Downloading|Rendering|Verifying|Đang tải|Đang render|Đang đăng|Đang xác minh|Chờ giãn cách|Chờ lượt upload)/i.test(status)) return 'active';
    if (/error|copyright|bản quyền|cancelled|hủy/i.test(status)) return 'error';
    return 'ready';
  }
  function capture(state) {
    state.project = currentProject();
    state.ids = targetIds();
    state.selected = selectedIds().length > 0;
    const visible = new Set(typeof getVisibleQueueIds === 'function' ? getVisibleQueueIds().map(Number) : []);
    state.hidden = state.selected ? state.ids.filter(id => !visible.has(id)).length : 0;
    state.invalidated = '';
    state.signature = '';
  }
  function inspect(state) {
    if (currentProject().value !== state.project.value) state.invalidated = 'Dự án đã thay đổi. Bấm Cập nhật phạm vi để kiểm tra dự án và video hiện tại.';
    const map = new Map(queueItems().map(item => [Number(item.id), item]));
    const rows = state.ids.map(id => map.get(id)).filter(Boolean);
    const counts = {review: 0, completed: 0, active: 0, error: 0, ready: 0};
    rows.forEach(item => counts[itemState(item)]++);
    if (rows.length !== state.ids.length) state.invalidated ||= 'Có video không còn trong dữ liệu hàng đợi. Cập nhật phạm vi rồi kiểm tra lại.';
    if (rows.some(item => item.project_name !== undefined && String(item.project_name) !== state.project.value)) state.invalidated ||= 'Dữ liệu hàng đợi chưa khớp dự án hiện tại. Chờ tải xong, rồi bấm Cập nhật phạm vi.';
    if (counts.review || counts.completed || counts.active) state.invalidated ||= 'Phạm vi có video đã đăng, cần đối soát hoặc đang được worker giữ. Chọn lại các dòng phù hợp rồi bấm Cập nhật phạm vi.';
    const problems = [];
    if (!state.project.value) problems.push('Chưa có dự án hiện tại.');
    if (!state.ids.length) problems.push('Chưa có video trong phạm vi. Đóng hộp thoại để chọn video hoặc điều chỉnh bộ lọc.');
    if (state.invalidated) problems.push(state.invalidated);
    const current = targetIds();
    const sameTargets = current.length === state.ids.length && current.every(id => state.ids.includes(id));
    return {rows, counts, problems, sameTargets};
  }
  function afterClose(state, action) {
    state.followup = action;
    state.dialog.close();
  }
  function runFollowup(action) {
    try {
      Promise.resolve(action()).catch(error => {
        if (typeof showToast === 'function') showToast('Không mở được thao tác: ' + error.message, 'error');
      });
    } catch (error) {
      if (typeof showToast === 'function') showToast('Không mở được thao tác: ' + error.message, 'error');
    }
  }
  function preparation(state, action) {
    const result = inspect(state);
    if (result.problems.length || !result.sameTargets) { refreshValidity(); return; }
    afterClose(state, () => {
      const current = inspect(state);
      if (current.problems.length || !current.sameTargets) {
        if (typeof showToast === 'function') showToast('Phạm vi đã thay đổi. Mở lại Quy trình đăng tự động trước khi chuẩn bị lô.', 'warning');
        return;
      }
      return action();
    });
  }
  function refreshValidity() {
    const state = session;
    if (!state?.dialog.open) return false;
    const result = inspect(state);
    const summary = state.selected
      ? `Đã chọn ${state.ids.length} video${state.hidden ? ` · ${state.hidden} dòng ẩn bởi bộ lọc lúc chốt phạm vi` : ''}. Dùng đủ tập ID đã chốt.`
      : `${state.ids.length} video theo bộ lọc tại lúc chốt phạm vi. Chưa chọn thủ công.`;
    byId('publish-scope-summary').textContent = `${state.project.label} — ${summary}`;
    byId('publish-scope-counts').textContent = `Sơ bộ: ${result.counts.ready} chưa bị chặn tại máy · ${result.counts.error} có lỗi/đã hủy · ${result.counts.completed} đã đăng · ${result.counts.review} cần đối soát · ${result.counts.active} đang xử lý.`;
    const message = result.problems.length ? result.problems.join(' ') : 'Phạm vi được giữ cố định. Máy chủ kiểm tra file, đích, metadata và điều kiện đăng ở bước xem trước; phiên đăng nhập và kênh thực tế được xác minh trước khi gửi file.';
    byId('publish-scope-status').textContent = message;
    byId('publish-scope-status').dataset.state = result.problems.length ? 'error' : 'ready';
    byId('publish-continue').disabled = result.problems.length > 0 || state.continuing;
    const signature = JSON.stringify(result.rows.map(item => [item.id, item.title, item.status, item.error_message, item.visibility, item.schedule_time, item.target_channel_id, item.assigned_channel_id, item.target_profile_name, itemState(item)]));
    if (signature !== state.signature) {
      state.signature = signature;
      const list = byId('publish-items-list'); list.replaceChildren();
      const labels = {review:'Cần đối soát', completed:'Đã đăng', active:'Đang xử lý', error:'Có lỗi / đã hủy', ready:'Chờ kiểm tra máy chủ'};
      for (const item of result.rows) {
        const entry = node('li', 'publish-item');
        entry.append(node('strong', '', `#${Number(item.id)} ${item.title || 'Chưa có tiêu đề'}`));
        const visibility = {private:'Riêng tư', unlisted:'Không công khai', public:'Công khai', schedule:'Lên lịch phát hành'}[item.visibility] || item.visibility || 'Chưa đặt hiển thị';
        const channelId = item.assigned_channel_id || item.target_channel_id;
        entry.append(node('span', 'publish-item-meta', `${labels[itemState(item)]} · ${visibility}${item.schedule_time ? ` · ${item.schedule_time}` : ''}`));
        entry.append(node('span', 'publish-item-meta', channelId ? `Kênh #${channelId}${item.target_profile_name ? ` · Profile ${item.target_profile_name}` : ''}` : 'Đích theo nhóm hoặc cấu hình hiện tại; kiểm tra kênh/profile cụ thể trong xem trước.'));
        if (item.error_message) entry.append(node('span', 'publish-item-error', item.error_message));
        list.append(entry);
      }
    }
    const prepBlocked = result.problems.length > 0 || !result.sameTargets;
    byId('publish-prepare-config').disabled = prepBlocked;
    byId('publish-prepare-schedule').disabled = prepBlocked;
    byId('publish-prepare-quick').disabled = prepBlocked || !state.selected;
    byId('publish-prepare-hint').textContent = !result.sameTargets
      ? 'Lựa chọn/bộ lọc đã thay đổi. Lô vẫn giữ tập ID phía trên; cập nhật phạm vi trước khi mở thao tác chuẩn bị.'
      : 'Các thao tác chuẩn bị giữ lựa chọn hiện tại. Lưu xong, mở lại Quy trình đăng tự động để xem phạm vi mới. Sửa nhanh cần lựa chọn thủ công.';
    const review = result.rows.find(item => itemState(item) === 'review');
    const reviewButton = byId('publish-prepare-review');
    reviewButton.hidden = !review;
    reviewButton.disabled = currentProject().value !== state.project.value;
    reviewButton.textContent = review ? `Đối soát video #${Number(review.id)}` : 'Đối soát';
    state.reviewId = review ? Number(review.id) : null;
    return result.problems.length === 0;
  }
  function open() {
    if (session?.dialog.open) { session.dialog.focus(); return; }
    const dialog = node('dialog', 'workspace-dialog publish-dialog');
    dialog.id = 'publish-workflow-dialog'; dialog.setAttribute('aria-labelledby', 'publish-workflow-title');
    dialog.setAttribute('aria-modal', 'true');
    dialog.innerHTML = `<header class="publish-header"><div><p class="publish-eyebrow">Chuẩn bị → xem trước → xác nhận → theo dõi</p><h2 id="publish-workflow-title">Quy trình đăng tự động</h2><p>Chọn cách vận hành và kiểm tra phạm vi trước khi chạy.</p></div><button type="button" id="publish-close" class="btn btn-outline" autofocus>Đóng</button></header>
      <div class="publish-body"><section id="publish-schedule-status" class="publish-schedule-banner" aria-live="polite" hidden></section><fieldset class="publish-flow-choices"><legend>Cách vận hành</legend>
        <label><input type="radio" name="publish-flow" id="publish-flow-batch" value="batch" checked><span><strong>Đăng một lô</strong><small>Chuẩn bị tập video cụ thể, xem trước và duyệt một lần.</small></span></label>
        <label><input type="radio" name="publish-flow" id="publish-flow-monitor" value="monitor"><span><strong>Theo dõi nguồn liên tục</strong><small>Duyệt cấu hình luồng, tự xử lý video mới theo chu kỳ.</small></span></label></fieldset>
      <section id="publish-batch-panel" aria-labelledby="publish-batch-title"><h3 id="publish-batch-title">1. Kiểm tra video của lô</h3>
        <div class="publish-scope"><p id="publish-scope-summary"></p><button type="button" id="publish-refresh-scope" class="btn btn-outline btn-sm">Cập nhật phạm vi</button></div><p id="publish-scope-counts" class="publish-help"></p><p id="publish-scope-status" role="status" aria-live="polite"></p>
        <details class="publish-items"><summary>Danh sách ID, nội dung và trạng thái</summary><ul id="publish-items-list"></ul></details>
        <h3>2. Chuẩn bị nội dung, đích và lịch</h3><div class="publish-preparation"><button type="button" id="publish-prepare-quick" class="btn btn-outline btn-sm">Sửa nhanh</button><button type="button" id="publish-prepare-config" class="btn btn-outline btn-sm">Gán cấu hình</button><button type="button" id="publish-prepare-schedule" class="btn btn-outline btn-sm">Lên lịch phát hành</button><button type="button" id="publish-prepare-review" class="btn btn-outline btn-sm" hidden>Đối soát</button></div><p id="publish-prepare-hint" class="publish-help"></p>
        <fieldset class="publish-mode-choices"><legend>3. Chọn cách xử lý trước đăng</legend><label><input type="radio" name="publish-mode" id="publish-mode-file" value="file" checked><span><strong>Đăng file sẵn có</strong><small>Ưu tiên đường dẫn bản render đã lưu; chỉ dùng file nguồn khi chưa có đường dẫn bản render. File thiếu sẽ bị chặn trong xem trước. Không chạy lại tải/render.</small></span></label><label><input type="radio" name="publish-mode" id="publish-mode-full" value="full"><span><strong>Chạy toàn trình</strong><small>Tải nguồn → render theo cấu hình đã lưu → đăng YouTube.</small></span></label></fieldset>
        <div class="publish-notes"><h3>4. Xem trước rồi xác nhận</h3><p>Nút tiếp tục mở bản xem trước có file, kênh/profile, phương thức, metadata, chế độ hiển thị và lý do bị chặn. Kiểm tra đúng tất cả video rồi bấm “Xác nhận và chạy”.</p><p>Lịch phát hành là giờ YouTube công bố video. Upload bắt đầu sau khi duyệt và đến lượt; không đợi tới giờ phát hành mới gửi file. Cần đủ thời gian tải, render, upload và xác minh trước lịch.</p></div>
      </section>
      <section id="publish-monitor-panel" aria-labelledby="publish-monitor-title" hidden><h3 id="publish-monitor-title">Tự động xử lý video mới từ nguồn</h3><ol class="publish-steps"><li>Trong Theo dõi nguồn, chọn nguồn và kênh/nhóm đích; cấu hình nội dung, render, metadata và chu kỳ quét.</li><li>Lưu luồng. Tạo hoặc lưu luồng chưa khởi chạy đăng.</li><li>Bấm Bật nguồn, đọc và duyệt metadata hiệu lực cho các kênh. Cấu hình thay đổi cần được xem trước và xác nhận lại.</li><li>Sau khi bật, luồng quét nguồn mới → AI nếu đã chọn → tạo hàng đợi → tải → render → chờ lượt → đăng và xác minh.</li><li>Theo dõi dự án “Monitor - tên luồng” cùng trạng thái worker. Video lỗi được giữ trong hàng đợi để xử lý riêng; không mặc định tự thử lại video đã có. NeedsReview cần đối soát từng dòng.</li></ol><p class="publish-help">Giới hạn video mỗi lần quét, chu kỳ quét và khoảng nghỉ đăng là các thiết lập khác nhau. Dừng từng luồng tại Theo dõi nguồn và đợi worker kết thúc trước khi tắt máy.</p><button type="button" id="publish-go-monitor" class="btn btn-primary">Mở Theo dõi nguồn</button></section>
      <aside class="publish-notes publish-runtime"><h3>Khi tool đang chạy</h3><ul><li>Giữ server DHB hoạt động và máy thức để worker tiếp tục xử lý.</li><li>Mỗi lần chỉ một video được upload. Khoảng nghỉ chung tối thiểu 30 phút tính sau khi lượt trước kết thúc, áp dụng cả các profile và luồng khác nhau. Nhiều worker giúp xử lý tải/render song song, không tăng số upload cùng lúc.</li><li>Completed là lượt upload đã được xác minh; video Riêng tư hoặc có lịch phát hành chưa đồng nghĩa đã công khai.</li><li>NeedsReview giữ bằng chứng và chống trùng. Kiểm tra YouTube trước khi xác nhận đã đăng hoặc cho phép đăng lại riêng dòng đó.</li></ul></aside></div>
      <footer class="publish-footer"><p>Máy chủ luôn kiểm tra lại bản xem trước và điều kiện đăng.</p><button type="button" id="publish-continue" class="btn btn-success">Tiếp tục xem trước lô</button></footer>`;
    const state = {dialog, opener: document.activeElement, continuing: false, followup: null};
    session = state; capture(state); document.body.append(dialog);
    byId('publish-close').addEventListener('click', () => dialog.close());
    byId('publish-refresh-scope').addEventListener('click', () => { capture(state); refreshValidity(); });
    dialog.querySelectorAll('[name="publish-flow"]').forEach(radio => radio.addEventListener('change', () => {
      const batch = byId('publish-flow-batch').checked;
      byId('publish-batch-panel').hidden = !batch; byId('publish-monitor-panel').hidden = batch;
      byId('publish-continue').hidden = !batch;
    }));
    byId('publish-prepare-quick').addEventListener('click', () => preparation(state, () => openQuickQueueEdit()));
    byId('publish-prepare-config').addEventListener('click', () => preparation(state, () => window.StudioQueue?.openSource('bulk')));
    byId('publish-prepare-schedule').addEventListener('click', () => preparation(state, () => promptAutoSchedule()));
    byId('publish-prepare-review').addEventListener('click', () => {
      refreshValidity();
      if (state.reviewId && currentProject().value === state.project.value) afterClose(state, () => window.StudioQueue?.showDetails(state.reviewId));
    });
    byId('publish-go-monitor').addEventListener('click', () => afterClose(state, () => switchTab('tab-monitor', document.querySelector('.nav-tab[data-tab="tab-monitor"]'))));
    byId('publish-continue').addEventListener('click', () => {
      if (!refreshValidity() || state.continuing || !byId('publish-flow-batch').checked) return;
      state.continuing = true;
      const full = byId('publish-mode-full').checked;
      const ids = state.ids.slice();
      afterClose(state, () => {
        if (inspect(state).problems.length) {
          if (typeof showToast === 'function') showToast('Dự án hoặc trạng thái video đã thay đổi. Mở lại Quy trình đăng tự động để kiểm tra.', 'warning');
          return;
        }
        return startPipeline(full, full, true, ids);
      });
    });
    const onProjectChange = () => { state.invalidated = 'Dự án đã thay đổi. Bấm Cập nhật phạm vi để kiểm tra dự án và video hiện tại.'; refreshValidity(); };
    byId('sel-project')?.addEventListener('change', onProjectChange);
    dialog.addEventListener('close', () => {
      clearInterval(state.timer); byId('sel-project')?.removeEventListener('change', onProjectChange);
      dialog.remove(); if (session === state) session = null;
      if (state.opener?.isConnected) state.opener.focus({preventScroll: true});
      if (state.followup) requestAnimationFrame(() => runFollowup(state.followup));
    }, {once: true});
    dialog.showModal(); refreshValidity(); state.timer = setInterval(refreshValidity, 500);
    if (typeof apiFetch === 'function') {
      apiFetch('/api/scheduled-uploads/status', { cache: 'no-store' }).then(sched => {
        if (!dialog.isConnected || !sched?.configured) return;
        const box = byId('publish-schedule-status');
        if (!box) return;
        box.replaceChildren();
        box.dataset.state = sched.action_required ? 'error' : (sched.status === 'finished' ? 'done' : 'ready');
        const heading = node('strong', '', `Lịch đăng Private định kỳ: ${sched.message || sched.status}`);
        const details = [];
        if (Number.isInteger(sched.completed_count) && Number.isInteger(sched.total_count)) {
          details.push(`Tiến độ: ${sched.completed_count}/${sched.total_count} video`);
        }
        if (sched.inflight_queue_id) details.push(`Đang xử lý: #${sched.inflight_queue_id}`);
        else if (sched.next_queue_id) details.push(`Kế tiếp: #${sched.next_queue_id}`);
        if (sched.next_due_at) {
          const dt = new Date(sched.next_due_at);
          details.push(`Hẹn chạy tiếp: ${Number.isNaN(dt.getTime()) ? sched.next_due_at : dt.toLocaleString('vi-VN')}`);
        }
        if (sched.last_result?.queue_id) {
          details.push(`Lượt gần nhất: #${sched.last_result.queue_id} (${sched.last_result.status})`);
        }
        const meta = node('p', 'publish-schedule-meta', details.join(' · '));
        box.append(heading);
        if (details.length) box.append(meta);
        if (sched.task_disable_recommended) {
          box.append(node('p', 'publish-schedule-note', 'Đã hoàn tất toàn bộ lô video; bạn có thể tắt Windows Task để kết thúc chu kỳ kiểm tra.'));
        } else if (sched.task_session_required) {
          box.append(node('p', 'publish-schedule-note', 'Lưu ý: Windows Scheduled Task yêu cầu máy bật, có mạng và tài khoản hiện tại đang đăng nhập.'));
        }
        box.hidden = false;
      }).catch(() => {});
    }
  }
  function mount() {
    if (byId('publish-workflow-open')) return;
    const heading = byId('workspace-heading') || document.querySelector('#tab-workspace .ui-page-heading');
    if (!heading) return;
    const button = node('button', 'btn btn-primary', 'Quy trình đăng tự động');
    button.type = 'button'; button.id = 'publish-workflow-open'; button.addEventListener('click', open); heading.append(button);
  }
  window.StudioPublish = {open, refreshValidity, mount};
  window.addEventListener('DOMContentLoaded', mount);
})();
