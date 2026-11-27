(() => {
  const form = document.getElementById('campaign-simulation-form');
  const next = document.getElementById('campaign-simulation-next');
  const status = document.getElementById('campaign-simulation-status');
  const runs = document.getElementById('campaign-simulation-runs');
  let simulation = null;

  form.addEventListener('submit', event => {
    event.preventDefault();
    const fields = new FormData(form);
    const slots = String(fields.get('slots')).trim().split(/[\s,]+/);
    const profile = document.getElementById('campaign-profile').value;
    if (!profile || slots.length > 15 || new Set(slots).size !== slots.length
        || slots.some(slot => !/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(slot))) {
      status.textContent = 'Chọn profile và nhập 1–15 mốc giờ HH:mm khác nhau.';
      return;
    }
    simulation = {
      profile,
      activity: form.elements.activity.selectedOptions[0].textContent,
      outcome: fields.get('outcome') === 'success' ? 'Mock thành công' : 'Mock lỗi dịch vụ',
      slots: slots.sort(),
    };
    runs.replaceChildren();
    next.disabled = false;
    status.textContent = 'Đang chờ mốc ' + simulation.slots[0];
  });

  next.addEventListener('click', () => {
    if (!simulation?.slots.length) return;
    const row = document.createElement('li');
    row.textContent = [simulation.slots.shift(), simulation.profile, simulation.activity, simulation.outcome].join(' · ');
    runs.append(row);
    next.disabled = !simulation.slots.length;
    status.textContent = next.disabled ? 'Mô phỏng đã hoàn tất.' : 'Đang chờ mốc ' + simulation.slots[0];
  });
})();
