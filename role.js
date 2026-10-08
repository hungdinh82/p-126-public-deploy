const status = document.querySelector('#role-status');
document.querySelectorAll('[data-role]').forEach((button) => button.addEventListener('click', async () => {
  const role = button.dataset.role;
  document.querySelectorAll('[data-role]').forEach((item) => { item.disabled = true; });
  status.textContent = 'Đang mở không gian…';
  try {
    const response = await fetch('/api/v1/session/role', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Không thể tạo phiên demo.');
    window.location.assign(payload.redirect);
  } catch (error) {
    status.textContent = error.message;
    document.querySelectorAll('[data-role]').forEach((item) => { item.disabled = false; });
  }
}));
