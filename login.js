document.getElementById('loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('submit');
  const error = document.getElementById('error');
  button.disabled = true;
  error.textContent = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: document.getElementById('username').value.trim(), password: document.getElementById('password').value})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '登录失败，请稍后重试');
    document.getElementById('password').value = '';
    location.replace('/');
  } catch (failure) {
    error.textContent = failure.message || '无法连接服务器';
  } finally {
    button.disabled = false;
  }
});
