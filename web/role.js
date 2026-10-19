const status = document.querySelector('#role-status');
const form = document.querySelector('#login-form');
const demoShortcut = document.querySelector('#demo-shortcut');
const demoButton = document.querySelector('#demo-login');

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

async function initDemoShortcut() {
  if (!demoShortcut || !demoButton) return;
  try {
    const response = await fetch('/api/v1/session/demo-config');
    if (!response.ok) return;
    const payload = await response.json();
    if (!payload.enabled) return;
    demoShortcut.hidden = false;
  } catch (error) {
    // Demo shortcut stays hidden if we can't reach the config endpoint.
  }
}

demoButton?.addEventListener('click', async () => {
  demoButton.disabled = true;
  status.textContent = 'Đang mở phiên demo kỹ sư…';
  try {
    const response = await fetch('/api/v1/session/demo-login', { method: 'POST' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Không thể mở phiên demo.');
    window.location.assign(payload.redirect);
  } catch (error) {
    status.textContent = error.message;
    demoButton.disabled = false;
  }
});

initDemoShortcut();
