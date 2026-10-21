/* 前端逻辑：提交 → 轮询 trace_id → 渲染回答与产物 */

const STAGE_LABEL = {
  queued: '排队中…',
  supervisor: '调度中…',
  researcher: '调研 Agent 正在检索资料',
  writer: '撰稿 Agent 正在写口播稿',
  voice: '配音 Agent 正在合成语音',
  finish: '正在汇总结果',
  done: '已完成',
};

const state = {
  sessionId: localStorage.getItem('vr_session') || '',
  traceId: '',
  busy: false,
};

const $ = (id) => document.getElementById(id);
const chat = $('chat');
const input = $('input');
const sendBtn = $('send');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------------------------------------------------------- 健康检查 */
async function loadHealth() {
  const pill = $('health-pill');
  try {
    const res = await fetch('/health');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);
    const tools = Object.entries(data.tools || {});
    const missing = tools.filter(([, t]) => t.enabled && !t.key_ready).map(([n]) => n);
    pill.className = 'pill ' + (missing.length ? 'err' : 'ok');
    pill.textContent = missing.length
      ? `缺少密钥：${missing.join(', ')}`
      : `${data.llm.model} · ${tools.length} 个工具`;
    pill.title = JSON.stringify(data, null, 2);
  } catch (err) {
    pill.className = 'pill err';
    pill.textContent = '后端未就绪';
  }
}

/* ---------------------------------------------------------------- 渲染工具 */
function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function addUser(text) {
  $('empty')?.remove();
  const row = document.createElement('div');
  row.className = 'msg user';
  row.innerHTML = `<div class="bubble"></div>`;
  row.querySelector('.bubble').textContent = text;
  chat.appendChild(row);
  scrollToBottom();
}

function addBotPlaceholder() {
  const row = document.createElement('div');
  row.className = 'msg bot';
  row.innerHTML = `
    <div class="avatar">🤖</div>
    <div class="bubble">
      <div class="stage"><span class="spinner"></span><span class="stage-text">已提交，等待调度…</span></div>
      <div class="content"></div>
    </div>`;
  chat.appendChild(row);
  scrollToBottom();
  return row;
}

function setStage(row, stage, message) {
  const box = row.querySelector('.stage');
  if (!box) return;
  box.innerHTML = `<span class="spinner"></span><span class="stage-text"></span>`;
  const text = message || STAGE_LABEL[stage] || stage || '处理中…';
  box.querySelector('.stage-text').textContent =
    (STAGE_LABEL[stage] ? STAGE_LABEL[stage] + ' · ' : '') + text;
  scrollToBottom();
}

function renderAnswer(row, data) {
  row.querySelector('.stage')?.remove();
  const content = row.querySelector('.content');
  const art = document.createElement('div');
  art.className = 'answer';
  art.innerHTML = data.answer_html || '<p>（没有返回内容）</p>';
  content.appendChild(art);

  const artifacts = data.artifacts || [];
  if (artifacts.length) {
    const box = document.createElement('div');
    box.className = 'artifacts';
    for (const item of artifacts) {
      const wrap = document.createElement('div');
      wrap.className = 'artifact';
      if (item.kind === 'audio') {
        const meta = item.meta || {};
        wrap.innerHTML = `
          <div class="at-title">🎧 语音播报 · ${meta.voice || ''} · ${Math.round((meta.bytes || 0) / 1024)} KB</div>
          <audio controls preload="metadata" src="${item.url}"></audio>`;
      } else if (item.kind === 'image') {
        wrap.innerHTML = `<div class="at-title">🖼 图片产物</div><img src="${item.url}" alt="artifact" />`;
      } else {
        continue;
      }
      box.appendChild(wrap);
    }
    content.appendChild(box);
  }

  // 调用链（录屏时可展开对照 logs/agent.log）
  const events = data.events || [];
  if (events.length) {
    const details = document.createElement('details');
    details.className = 'events';
    const rows = events
      .filter((e) => e.event !== 'llm_call')
      .slice(-18)
      .map(
        (e) =>
          `<tr><td>${e.event}</td><td>${e.span}</td><td>${e.duration_ms ?? ''}</td><td>${
            (e.detail && (e.detail.summary || e.detail.reason || e.detail.next || e.detail.error)) || ''
          }</td></tr>`,
      )
      .join('');
    details.innerHTML = `<summary>调用链（最近 ${Math.min(events.length, 18)} 条事件）</summary>
      <table>${rows}</table>`;
    content.appendChild(details);
  }
  scrollToBottom();
}

function renderError(row, message) {
  row.querySelector('.stage')?.remove();
  const content = row.querySelector('.content');
  const box = document.createElement('div');
  box.className = 'error-box';
  box.textContent = message;
  content.appendChild(box);
  scrollToBottom();
}

function explainError(status, data) {
  const detail = data && data.detail;
  let msg = '';
  if (Array.isArray(detail)) {
    msg = detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('；');
  } else if (typeof detail === 'string') {
    msg = detail;
  }
  if (status === 422) return `请求参数不合法：${msg || '请检查输入内容'}`;
  if (status === 502) return `上游服务暂时不可用（502）：${msg || '请稍后重试'}`;
  if (status === 404) return `找不到记录：${msg || '服务可能已重启'}`;
  if (status >= 500) return `服务端错误（${status}）：${msg || '请查看后端日志'}`;
  return msg || `请求失败（HTTP ${status}）`;
}

function renderTrace(traceId, sessionId) {
  state.traceId = traceId;
  state.sessionId = sessionId;
  if (traceId) {
    $('tracebar').hidden = false;
    $('trace-id').textContent = traceId;
  }
  if (sessionId) {
    $('tracebar').hidden = false;
    $('session-id').textContent = sessionId;
  }
}

/* ---------------------------------------------------------------- 主流程 */
async function send(text) {
  if (state.busy) return;
  const message = (text ?? input.value).trim();
  if (!message) return;

  input.value = '';
  input.style.height = 'auto';
  state.busy = true;
  sendBtn.disabled = true;

  addUser(message);
  const row = addBotPlaceholder();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: state.sessionId || null }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      renderError(row, explainError(res.status, data));
      return;
    }

    renderTrace(data.trace_id, data.session_id);
    localStorage.setItem('vr_session', data.session_id);
    await poll(data.trace_id, row);
  } catch (err) {
    renderError(row, `网络异常：${err.message}。请确认后端已启动（python run.py）。`);
  } finally {
    state.busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

async function poll(traceId, row) {
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    let res, data;
    try {
      res = await fetch(`/chat/${traceId}`);
      data = await res.json().catch(() => ({}));
    } catch (err) {
      renderError(row, `轮询失败：${err.message}`);
      return;
    }
    if (!res.ok) {
      renderError(row, explainError(res.status, data));
      return;
    }

    if (data.status === 'done') {
      renderAnswer(row, data);
      return;
    }
    if (data.status === 'error') {
      renderError(row, `生成失败：${data.error || '未知错误'}`);
      return;
    }
    setStage(row, data.stage, data.stage_message);
    await sleep(900);
  }
  renderError(row, '等待超时（超过 5 分钟），请查看后端日志。');
}

/* ---------------------------------------------------------------- 事件绑定 */
sendBtn.addEventListener('click', () => send());

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
});

document.querySelectorAll('.chip').forEach((chip) => {
  chip.addEventListener('click', () => send(chip.dataset.q));
});

$('copy-trace').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(state.traceId);
    $('copy-trace').textContent = '已复制';
    setTimeout(() => ($('copy-trace').textContent = '复制'), 1200);
  } catch {
    /* 剪贴板不可用时忽略 */
  }
});

$('reset-btn').addEventListener('click', () => {
  localStorage.removeItem('vr_session');
  state.sessionId = '';
  state.traceId = '';
  $('tracebar').hidden = true;
  chat.innerHTML = '';
  location.reload();
});

/* 启动 */
loadHealth();
if (state.sessionId) {
  $('session-id').textContent = state.sessionId;
  $('tracebar').hidden = false;
}
input.focus();
