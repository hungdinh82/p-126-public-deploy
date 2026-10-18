const status = document.querySelector('#role-status');
const form = document.querySelector('#login-form');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = form.querySelector('button');
  const values = new FormData(form);
  submit.disabled = true;
  status.textContent = 'Đang mở phiên riêng…';
  try {
    const response = await fetch('/api/v1/session/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: values.get('phone'), pin: values.get('pin') })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Không thể đăng nhập.');
    if (payload.conversation_id) sessionStorage.setItem('vivi-conversation-id', payload.conversation_id);
    window.location.assign(payload.redirect);
  } catch (error) {
    status.textContent = error.message;
    submit.disabled = false;
  }
});
