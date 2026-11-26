(() => {
  const el = id => document.getElementById('campaign-' + id);
  const state = { campaigns: [], lists: [], runs: [], channels: [], revision: '', gap: 30 };
  let sheetRevision = '';
  let logStream;
  let commentCursor = '';
  const labels = {
    draft: 'Nháp', enabled: 'Đang bật', paused: 'Tạm dừng', queued: 'Chờ pipeline',
    submitting: 'Đang xử lý', completed: 'Đã đăng', missed: 'Lỡ mốc', canceled: 'Đã hủy',
    blocked: 'Cần kiểm tra', needs_review: 'Cần đối soát', error: 'Lỗi', empty: 'Hết video',
  };
  const dateLabel = date => new Date(date).toLocaleString('vi-VN');
  const minutes = value => {
    const [hour, minute] = value.split(':').map(Number);
    return hour * 60 + minute;
  };

  function selectOptions(select, options, placeholder, value = select.value) {
    select.replaceChildren(new Option(placeholder, ''), ...options.map(item => new Option(item.label, item.id)));
    if ([...select.options].some(option => option.value === String(value))) select.value = value;
  }

  function current() {
    return state.campaigns.find(item => String(item.id) === el('edit').value);
  }

  function invalidatePreview() {
    state.revision = '';
    el('enable').disabled = true;
    el('preview-items').replaceChildren();
  }

  function renderSummary() {
    const item = current();
    const next = item?.next_at ? ' · Lượt kế tiếp ' + dateLabel(item.next_at) : '';
    el('summary').textContent = item
      ? `${labels[item.state] || item.state}${next} · Giãn cách gửi tối thiểu ${state.gap} phút`
      : 'Giãn cách gửi tối thiểu ' + state.gap + ' phút';
    el('pause').disabled = item?.state !== 'enabled';
    el('preview').disabled = !item;
  }

  function renderRuns() {
    const selected = current();
    const names = new Map(state.campaigns.map(item => [item.id, item.name]));
    const rows = state.runs.filter(run => !selected || run.campaign_id === selected.id).map(run => {
      const row = document.createElement('tr');
      let detail = run.message;
      if (run.state === 'queued' && state.cooldown_until) {
        detail = 'Giãn cách gửi đến ' + dateLabel(state.cooldown_until * 1000);
      } else if (run.state === 'submitting') {
        detail = run.queue_status || detail;
      }
      if (run.playlist_state === 'completed') detail += ' · Đã thêm playlist';
      if (run.playlist_state === 'queued' || run.playlist_state === 'submitting') detail += ' · Đang xử lý playlist';
      if (run.playlist_state === 'error') detail += ' · Playlist: ' + run.playlist_message;
      const values = [run.local_day + ' ' + run.local_time, names.get(run.campaign_id) || '',
        run.queue_id ? '#' + run.queue_id : '—', run.started_at ? dateLabel(run.started_at * 1000) : '—',
        labels[run.state] || run.state, detail || ''];
      for (const value of values) row.insertCell().textContent = value;
      const actions = row.insertCell();
      if (run.queue_id) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn';
        button.textContent = 'Xem log';
        button.addEventListener('click', () => openRunLog(run));
        actions.append(button);
        if (run.playlist_state === 'error') {
          const retry = document.createElement('button');
          retry.type = 'button';
          retry.className = 'btn';
          retry.textContent = 'Thử lại playlist';
          retry.addEventListener('click', handle(async () => {
            await save('/runs/' + run.id + '/playlist-retry', {});
            await refresh();
          }));
          actions.append(retry);
        }
      }
      if (run.state === 'missed' && state.campaigns.some(item => item.id === run.campaign_id && item.state === 'enabled')) {
        const catchUp = document.createElement('button');
        catchUp.type = 'button';
        catchUp.className = 'btn';
        catchUp.textContent = 'Chạy bù';
        catchUp.addEventListener('click', handle(async () => {
          await save('/runs/' + run.id + '/catch-up', {});
          await refresh();
        }));
        actions.append(catchUp);
      }
      return row;
    });
    el('run-rows').replaceChildren(...rows);
  }

  function closeRunLog() {
    logStream?.abort();
    logStream = undefined;
    el('log').hidden = true;
  }

  async function openRunLog(run) {
    closeRunLog();
    const controller = new AbortController();
    logStream = controller;
    el('log-title').textContent = `Lượt #${run.id} · video #${run.queue_id}`;
    el('log-lines').textContent = '';
    el('log').hidden = false;
    try {
      const token = await ensureApiToken();
      const response = await fetch('/api/campaigns/runs/' + run.id + '/logs', {
        headers: { 'X-DHB-Token': token }, signal: controller.signal,
      });
      if (!response.ok || !response.headers.get('content-type')?.startsWith('text/event-stream')) {
        throw new Error('Không mở được nhật ký lượt chạy.');
      }
      const reader = response.body.getReader();
      const decode = new TextDecoder();
      let buffer = '';
      while (!controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decode.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop();
        for (const frame of frames) {
          const payload = frame.split('\n').find(line => line.startsWith('data: '));
          if (!payload) continue;
          const item = JSON.parse(payload.slice(6));
          const lines = el('log-lines');
          lines.textContent = (lines.textContent + `${item.created_at} · ${item.status} · ${item.message}\n`).slice(-30_000);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) el('log-lines').textContent = error.message;
    }
  }

  function fillChannels(selectedId) {
    const profile = el('profile').value;
    const channels = state.channels.filter(channel => (channel.profile_name || '(OAuth)') === profile);
    selectOptions(el('channel'), channels.map(channel => ({
      id: channel.id, label: channel.name + (channel.channel_id ? ' · ' + channel.channel_id : ''),
    })), 'Chọn kênh đã liên kết', selectedId);
  }

  function fillCampaign() {
    invalidatePreview();
    const item = current();
    const channel = state.channels.find(entry => entry.id === item?.channel_id);
    el('name').value = item?.name || '';
    el('list').value = item?.list_id || '';
    el('order').value = item?.content_order || 'sequential';
    el('visibility').value = item?.visibility || 'private';
    el('start').value = item?.start_date || '';
    el('times').value = item?.times.join(', ') || '';
    el('offset').value = item?.utc_offset_minutes ?? -new Date().getTimezoneOffset();
    el('render').checked = item?.render || false;
    el('target-playlist').value = item?.playlist_id || '';
    el('profile').value = channel ? (channel.profile_name || '(OAuth)') : '';
    fillChannels(item?.channel_id);
    renderSummary();
    renderRuns();
  }

  async function refresh(loadChannels = false) {
    try {
      const [data, channels] = await Promise.all([
        apiFetch('/api/campaigns'), loadChannels ? apiFetch('/api/channels') : Promise.resolve(state.channels),
      ]);
      Object.assign(state, data, { channels, gap: data.upload_gap_minutes });
      if (loadChannels) {
        const selectedChannel = el('channel').value;
        const profiles = [...new Set(channels.map(channel => channel.profile_name || '(OAuth)'))];
        selectOptions(el('profile'), profiles.map(name => ({ id: name, label: name })), 'Chọn profile');
        fillChannels(selectedChannel);
      }
      const lists = state.lists.map(list => ({ id: list.id, label: list.name }));
      selectOptions(el('edit-list'), lists, 'Tạo danh sách mới');
      selectOptions(el('list'), lists, 'Chọn danh sách');
      selectOptions(el('edit'), state.campaigns.map(item => ({ id: item.id, label: item.name })), 'Tạo chiến dịch mới');
      renderSummary();
      renderRuns();
    } catch (error) {
      el('notice').textContent = error.message;
    }
  }

  function save(url, data, method = 'POST') {
    return apiFetch('/api/campaigns' + url, { method, body: JSON.stringify(data) });
  }

  function handle(action) {
    return async event => {
      event.preventDefault();
      const control = event.submitter || event.currentTarget;
      control.disabled = true;
      el('notice').textContent = '';
      try { await action(); }
      catch (error) { el('notice').textContent = error.message; }
      finally {
        control.disabled = false;
        el('enable').disabled = !state.revision;
        el('sheet-import').disabled = !sheetRevision;
        renderSummary();
      }
    };
  }

  function suggestTimes() {
    const count = el('slot-count').valueAsNumber;
    const start = minutes(el('slot-start').value);
    const end = minutes(el('slot-end').value);
    const span = (end - start + 1440) % 1440;
    if (!Number.isInteger(count) || count < 1 || count > 15 || !Number.isFinite(span) || span < 1) {
      throw new Error('Chọn từ 1 đến 15 bài và khung giờ có độ dài hợp lệ.');
    }
    const zone = el('offset').valueAsNumber;
    if (!Number.isInteger(zone) || zone < -720 || zone > 840) throw new Error('Nhập múi giờ hợp lệ.');
    const occupied = state.campaigns.filter(item => item.id !== current()?.id && item.state === 'enabled')
      .flatMap(item => item.times.map(slot => ((minutes(slot) - item.utc_offset_minutes + zone) % 1440 + 1440) % 1440));
    const available = Array.from({ length: span + 1 }, (_, i) => i).filter(offset =>
      occupied.every(other => {
        const distance = Math.abs((start + offset) % 1440 - other);
        return Math.min(distance, 1440 - distance) >= state.gap;
      }));
    const selected = [];
    for (const offset of available) {
      if (selected.length === count) break;
      if (!selected.length || (offset - selected.at(-1) >= state.gap && 1440 - offset + selected[0] >= state.gap)) {
        selected.push(offset);
      }
    }
    if (selected.length < count) throw new Error('Không đủ mốc. Hãy mở rộng khung giờ hoặc giảm số bài.');
    // Move from the back towards the centre of each segment while preserving the minimum gap.
    for (let i = count - 1; i >= 0; i--) {
      const ideal = Math.floor((i + 0.5) * span / count);
      const latest = i + 1 < count ? selected[i + 1] - state.gap : Math.min(span, selected[0] + 1440 - state.gap);
      selected[i] = available.findLast(offset => offset >= selected[i] && offset <= Math.min(ideal, latest))
        ?? selected[i];
    }
    el('times').value = selected.map(offset => {
      const minute = (start + offset) % 1440;
      return [Math.floor(minute / 60), minute % 60].map(n => String(n).padStart(2, '0')).join(':');
    }).join(', ');
    invalidatePreview();
  }

  el('use-selection').addEventListener('click', () => {
    el('list-ids').value = [...gSelectedIds].join(', ');
  });
  el('edit-list').addEventListener('change', () => {
    const list = state.lists.find(item => String(item.id) === el('edit-list').value);
    el('list-name').value = list?.name || '';
    el('list-ids').value = list?.queue_ids.join(', ') || '';
  });
  el('list-form').addEventListener('submit', handle(async () => {
    const rawIds = el('list-ids').value.trim().split(/[\s,]+/);
    if (rawIds.some(id => !/^[1-9][0-9]*$/.test(id) || !Number.isSafeInteger(Number(id)))) {
      throw new Error('Nhập các ID video hợp lệ, phân cách bằng dấu phẩy hoặc xuống dòng.');
    }
    const listId = el('edit-list').value;
    const result = await save(listId ? '/lists/' + listId : '/lists', {
      name: el('list-name').value, queue_ids: rawIds.map(Number),
    }, listId ? 'PUT' : 'POST');
    invalidatePreview();
    await refresh();
    el('edit-list').value = result.id;
    el('list').value = result.id;
    el('notice').textContent = 'Đã lưu danh sách. Dùng gán hàng loạt trong dự án để đặt kênh và quyền hiển thị cho các video.';
  }));
  function resetSheet() {
    sheetRevision = '';
    el('sheet-import').disabled = true;
    el('sheet-results').replaceChildren();
  }

  function sheetPayload() {
    const payload = {
      project: document.getElementById('sel-project').value,
      name: el('list-name').value.trim(), channel_id: Number(el('channel').value),
      visibility: el('visibility').value,
    };
    if (!payload.project || !payload.channel_id || !payload.name) {
      throw new Error('Chọn dự án, kênh và nhập tên danh sách.');
    }
    if (el('import-source').value === 'links') {
      const links = el('drive-links').value.trim();
      if (!links) throw new Error('Dán các liên kết Drive, mỗi dòng một liên kết.');
      return { ...payload, links };
    }
    const column = field => el('sheet-' + field).value === '' ? null : Number(el('sheet-' + field).value);
    const url = el('sheet-url').value.trim();
    const source_column = column('source');
    const title_column = column('title');
    if (!url || source_column === null || title_column === null) {
      throw new Error('Nhập liên kết Sheets rồi chọn cột liên kết Drive và tiêu đề.');
    }
    return { ...payload, url, source_column, title_column, description_column: column('description'),
      tags_column: column('tags') };
  }

  el('sheet').addEventListener('input', resetSheet);
  el('import-source').addEventListener('change', () => {
    const fromSheet = el('import-source').value === 'sheet';
    el('sheet-fields').hidden = !fromSheet;
    el('links-fields').hidden = fromSheet;
  });
  el('list-name').addEventListener('input', resetSheet);
  el('sheet-columns').addEventListener('click', handle(async () => {
    resetSheet();
    const result = await save('/sources/sheet-columns', { url: el('sheet-url').value.trim() });
    const columns = result.columns.map((label, id) => ({ id, label: `${id + 1}. ${label || 'Cột chưa có tên'}` }));
    for (const field of ['source', 'title', 'description', 'tags']) {
      selectOptions(el('sheet-' + field), columns, 'Chọn cột', '');
    }
    el('notice').textContent = `Đã đọc ${result.row_count} dòng. Chọn cách ánh xạ các cột.`;
  }));
  el('sheet-preview').addEventListener('click', handle(async () => {
    resetSheet();
    const result = await save('/sources/' + el('import-source').value + '-preview', sheetPayload());
    sheetRevision = result.revision;
    const lines = [
      `${result.ready} hợp lệ · ${result.duplicates} trùng · ${result.errors.length} lỗi`,
      `${result.channel} · ${result.youtube_channel_id} · ${result.visibility}`,
      ...result.samples.map(row => `Dòng ${row.line} · ${row.title}`),
      ...result.errors.map(row => `Dòng ${row.line} · ${row.message}`),
    ];
    el('sheet-results').replaceChildren(...lines.map(line => {
      const li = document.createElement('li');
      li.textContent = line;
      return li;
    }));
    el('sheet-import').disabled = !sheetRevision;
  }));
  el('sheet-import').addEventListener('click', handle(async () => {
    const revision = sheetRevision;
    resetSheet();
    const result = await save('/sources/' + el('import-source').value + '-import', { ...sheetPayload(), revision });
    await refresh();
    el('edit-list').value = result.id;
    el('list').value = result.id;
    el('list-ids').value = result.queue_ids.join(', ');
    invalidatePreview();
    el('notice').textContent = `Đã nhập ${result.queue_ids.length} video vào danh sách mới.`;
  }));
  el('edit').addEventListener('change', () => {
    closeRunLog();
    fillCampaign();
  });
  el('log-close').addEventListener('click', closeRunLog);
  el('profile').addEventListener('change', () => fillChannels(''));
  el('form').addEventListener('input', () => {
    invalidatePreview();
    resetSheet();
  });
  el('suggest').addEventListener('click', handle(suggestTimes));
  el('form').addEventListener('submit', handle(async () => {
    const id = el('edit').value;
    const result = await save(id ? '/' + id : '', {
      name: el('name').value, channel_id: Number(el('channel').value), list_id: Number(el('list').value),
      visibility: el('visibility').value, content_order: el('order').value,
      start_date: el('start').value || null, utc_offset_minutes: el('offset').valueAsNumber,
      times: el('times').value.trim().split(/[\s,]+/), render: el('render').checked,
      playlist_id: el('target-playlist').value.trim(),
    }, id ? 'PUT' : 'POST');
    await refresh();
    el('edit').value = result.id;
    fillCampaign();
    el('notice').textContent = 'Đã lưu chiến dịch.';
  }));
  el('preview').addEventListener('click', handle(async () => {
    invalidatePreview();
    const item = current();
    if (!item) return;
    const form = document.getElementById('campaign-form');
    if (!form.reportValidity()) return;
    if (el('name').value.trim() !== item.name || Number(el('channel').value) !== item.channel_id
        || Number(el('list').value) !== item.list_id || el('visibility').value !== item.visibility
        || el('order').value !== item.content_order || (el('start').value || null) !== item.start_date
        || el('offset').valueAsNumber !== item.utc_offset_minutes || el('render').checked !== item.render
        || el('target-playlist').value.trim() !== (item.playlist_id || '')
        || el('times').value.trim().split(/[\s,]+/).sort().join(',') !== item.times.join(',')) {
      throw new Error('Lưu cấu hình đã sửa trước khi xem trước.');
    }
    const preview = await apiFetch('/api/campaigns/' + item.id + '/preview');
    state.revision = preview.revision;
    const lines = preview.problems.length ? preview.problems : preview.items.map(row =>
      `#${row.id} · ${row.title} · ${row.youtube_channel_id} · ${row.visibility} · ${row.file_kind}`);
    if (!preview.problems.length && item.playlist_id) lines.unshift('Playlist sau khi đăng · ' + item.playlist_id);
    el('preview-items').replaceChildren(...lines.map(line => {
      const li = document.createElement('li');
      li.textContent = line;
      return li;
    }));
  }));
  el('enable').addEventListener('click', handle(async () => {
    const item = current();
    if (!item || !state.revision) return;
    await save('/' + item.id + '/enable', { revision: state.revision });
    invalidatePreview();
    await refresh();
    el('notice').textContent = 'Đã bật chiến dịch.';
  }));
  el('pause').addEventListener('click', handle(async () => {
    const item = current();
    if (!item) return;
    await save('/' + item.id + '/pause', {});
    invalidatePreview();
    await refresh();
    el('notice').textContent = 'Đã tạm dừng nhận lượt mới.';
  }));

  function channelPath() {
    const channel = el('channel').value;
    if (!channel) throw new Error('Chọn kênh trong mục Cấu hình.');
    return '/api/channel-workspace/' + channel;
  }

  function videoId() {
    const id = el('channel-video').value.trim();
    if (!/^[A-Za-z0-9_-]{11}$/.test(id)) throw new Error('Nhập Video ID gồm 11 ký tự.');
    return id;
  }

  function resetComments() {
    commentCursor = '';
    el('comment-items').replaceChildren();
    el('comment-more').hidden = true;
  }

  function resetChannel() {
    resetComments();
    el('metrics').replaceChildren();
    el('realtime-result').textContent = '';
    el('channel-notice').textContent = '';
  }

  el('channel-video').addEventListener('input', resetComments);
  el('channel').addEventListener('change', resetChannel);
  el('profile').addEventListener('change', resetChannel);
  el('edit').addEventListener('change', resetChannel);
  el('channel-connect').addEventListener('click', handle(async () => {
    channelPath();
    await openChannelOauthWizard(Number(el('channel').value));
  }));
  el('analytics').addEventListener('click', handle(async () => {
    const path = channelPath();
    const stats = await apiFetch(path + '/analytics');
    if (path !== channelPath()) return;
    const values = [
      ['Kênh', stats.name], ['Người đăng ký', stats.subscribers], ['Lượt xem của kênh', stats.channel_views],
      ['Lượt xem 28 ngày', stats.views_28d], ['Giờ xem 28 ngày', stats.watch_hours_28d],
      ['Cập nhật', dateLabel(stats.updated_at)],
    ];
    el('metrics').replaceChildren(...values.flatMap(([name, value]) => {
      const heading = document.createElement('dt');
      const text = document.createElement('dd');
      heading.textContent = name;
      text.textContent = typeof value === 'number' ? value.toLocaleString('vi-VN') : value ?? 'Chưa có dữ liệu';
      return [heading, text];
    }));
    el('channel-notice').textContent = stats.warning;
  }));
  el('realtime').addEventListener('click', handle(async () => {
    const path = channelPath();
    el('realtime-result').textContent = 'Đang đọc Studio…';
    try {
      const result = await apiFetch(path + '/analytics/realtime', { method: 'POST' });
      if (path === channelPath()) {
        el('realtime-result').textContent =
          `${result.views_48h.toLocaleString('vi-VN')} lượt xem trong 48 giờ · ${dateLabel(result.updated_at)}`;
      }
    } catch (error) {
      if (path === channelPath()) el('realtime-result').textContent = error.message;
    }
  }));

  async function loadComments(append) {
    const path = channelPath();
    const id = videoId();
    if (!append) resetComments();
    const params = new URLSearchParams({ video_id: id, cursor: commentCursor });
    const result = await apiFetch(path + '/comments?' + params);
    if (path !== channelPath() || id !== videoId()) return;
    commentCursor = result.next;
    el('comment-more').hidden = !commentCursor;
    for (const comment of result.items) {
      const li = document.createElement('li');
      li.textContent = `${comment.author} · ${comment.text}`;
      if (!comment.reply_state) {
        const input = document.createElement('textarea');
        input.placeholder = 'Viết trả lời';
        input.setAttribute('aria-label', 'Trả lời ' + comment.author);
        input.maxLength = 10000;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn';
        button.textContent = 'Gửi trả lời';
        button.addEventListener('click', handle(async () => {
          if (path !== channelPath() || id !== videoId()) throw new Error('Đã đổi kênh hoặc video.');
          const text = input.value.trim();
          if (!text) throw new Error('Nhập nội dung trả lời.');
          await apiFetch(path + '/comments/reply', {
            method: 'POST', body: JSON.stringify({ comment_id: comment.id, video_id: id, text }),
          });
          input.remove();
          button.remove();
          li.append(' · Đã trả lời');
        }));
        li.append(input, button);
      } else {
        li.append(comment.reply_state === 'completed' ? ' · Đã trả lời' : ' · Cần kiểm tra trong Studio');
      }
      el('comment-items').append(li);
    }
    el('channel-notice').textContent = result.items.length ? '' : 'Trang này chưa có bình luận.';
  }

  el('comments').addEventListener('click', handle(() => loadComments(false)));
  el('comment-more').addEventListener('click', handle(() => loadComments(true)));
  el('playlist-add').addEventListener('click', handle(async () => {
    const playlistId = el('playlist-id').value.trim();
    if (!playlistId) throw new Error('Nhập Playlist ID.');
    const result = await apiFetch(channelPath() + '/playlist', {
      method: 'POST', body: JSON.stringify({ video_id: videoId(), playlist_id: playlistId }),
    });
    el('channel-notice').textContent = result.state === 'already_added'
      ? 'Video đã có trong playlist.' : 'Đã thêm video vào playlist.';
  }));

  window.DhbCampaigns = { refresh, closeRunLog };
  setInterval(() => {
    if (document.getElementById('tab-campaigns').classList.contains('active')) refresh();
  }, 5000);
})();
