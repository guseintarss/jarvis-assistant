/* Веб-панель Jarvis — логика вкладок, WebSocket-чат, управление */
'use strict';

const $ = (sel) => document.querySelector(sel);

/* ---------- вкладки ---------- */
document.querySelectorAll('nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach((b) =>
      b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.remove('active'));
    btn.classList.add('active');
    $('#tab-' + btn.dataset.tab).classList.add('active');
  });
});

/* ---------- чат (WebSocket) ---------- */
const messages = $('#messages');
const chatForm = $('#chat-form');
const chatInput = $('#chat-input');

function addMessage(role, text, meta) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  if (meta) {
    const m = document.createElement('span');
    m.className = 'meta';
    m.textContent = meta;
    div.appendChild(m);
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

let ws = null;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = localStorage.getItem('jarvis_token') || '';
  ws = new WebSocket(`${proto}://${location.host}/ws/chat?token=${token}`);
  ws.onopen = () => addMessage('bot', 'Соединение установлено. Здравствуйте!');
  ws.onmessage = (e) => {
    const r = JSON.parse(e.data);
    addMessage('bot', r.response || '(пустой ответ)',
      `[${r.intent} · ${Math.round((r.confidence || 0) * 100)}% · ${r.route}]`);
  };
  ws.onclose = () => {
    addMessage('bot', 'Соединение разорвано, переподключаюсь…');
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}
connect();

chatForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  addMessage('user', text);
  ws.send(text);
  chatInput.value = '';
});

/* ---------- напоминания ---------- */
function loadReminders() {
  fetch('/api/reminders')
    .then((r) => r.json())
    .then((data) => {
      const list = $('#reminder-list');
      list.innerHTML = '';
      data.reminders.forEach((rem) => {
        const li = document.createElement('li');
        const span = document.createElement('span');
        span.innerHTML = `<b>${esc(rem.text)}</b> <span class="when">` +
          `${esc(rem.when.replace('T', ' '))}</span>`;
        const del = document.createElement('button');
        del.textContent = 'Отменить';
        del.onclick = () =>
          fetch('/api/reminders/' + rem.id, { method: 'DELETE' })
            .then(loadReminders);
        li.append(span, del);
        list.appendChild(li);
      });
    });
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
       "'": '&#39;' }[c]));
}

$('#reminder-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const when = $('#reminder-when').value;
  const text = $('#reminder-text').value.trim();
  if (!when || !text) return;
  fetch('/api/reminders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ when: when + ':00', text }),
  }).then(() => {
    $('#reminder-text').value = '';
    loadReminders();
  });
});

/* ---------- настройки ---------- */
function loadSettings() {
  fetch('/api/settings')
    .then((r) => r.json())
    .then((data) => {
      const body = $('#tools-table tbody');
      body.innerHTML = '';
      Object.entries(data.tools).forEach(([name, t]) => {
        const tr = document.createElement('tr');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.className = 'tool-check';
        check.checked = t.enabled;
        check.onchange = () =>
          saveTool(name, { enabled: check.checked });
        const risk = document.createElement('select');
        ['low', 'medium', 'high'].forEach((r) => {
          const opt = document.createElement('option');
          opt.value = r;
          opt.textContent = r;
          opt.selected = t.risk === r;
          risk.appendChild(opt);
        });
        risk.onchange = () => saveTool(name, { risk: risk.value });
        tr.innerHTML = `<td><b>${esc(name)}</b></td>` +
          `<td class="muted">${esc(t.description)}</td>`;
        const td1 = document.createElement('td');
        const td2 = document.createElement('td');
        td1.appendChild(check);
        td2.appendChild(risk);
        tr.append(td1, td2);
        body.appendChild(tr);
      });
    });
}

function saveTool(name, patch) {
  fetch('/api/settings/tool', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, ...patch }),
  }).then((r) => {
    if (!r.ok) return r.json().then((e) => alert(e.detail));
  });
}

/* ---------- логи ---------- */
function loadLogs() {
  const lines = $('#log-lines').value || 100;
  fetch(`/api/logs?lines=${lines}`)
    .then((r) => r.json())
    .then((data) => {
      $('#log-output').textContent = data.events
        .map((e) => JSON.stringify(e, null, 1))
        .join('\n');
    })
    .catch(() => { $('#log-output').textContent = 'Лог за сегодня не найден.'; });
}
$('#log-refresh').addEventListener('click', loadLogs);

/* ---------- статус ---------- */
function loadStatus() {
  fetch('/api/status')
    .then((r) => r.json())
    .then((data) => {
      $('#status-output').textContent = JSON.stringify(data, null, 2);
    });
}

/* ---------- инициализация вкладок ---------- */
document.querySelector('[data-tab="reminders"]')
  .addEventListener('click', loadReminders);
document.querySelector('[data-tab="settings"]')
  .addEventListener('click', loadSettings);
document.querySelector('[data-tab="logs"]')
  .addEventListener('click', loadLogs);
document.querySelector('[data-tab="status"]')
  .addEventListener('click', loadStatus);