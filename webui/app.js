/* LocalHarness UI — event stream renderer (layout borrowed from DeepSeek Harness, Build-studio restraint). */
const $ = (sel) => document.querySelector(sel);
const thread = $('#thread');
const log = $('#log');
const app = $('#app');
let sessionId = null;
let since = 0;
let running = false;
let streamAbort = null;
let stateCache = null;
let turn = null;
let diveEl = null;
let answerEl = null;
let lastSpeed = {};
let lastSteps = null;
let lastRounds = null;
let lastContext = null;
let replaying = false;
let currentView = 'chat';
let plugCache = null;
let plugFilter = '';
let lmJobTimer = null;
let showReasoning = localStorage.getItem('lh.showReasoning') !== '0';
let expandReasoning = localStorage.getItem('lh.expandReasoning') === '1';
let showGoalChecklist = localStorage.getItem('lh.goalChecklist') !== '0';
let lastGoalPoints = null;
let attached = [];
const ATTACH_MAX = 10 * 1024 * 1024;
const ATTACH_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

function renderAttached() {
  const strip = $('#attachStrip');
  if (!strip) return;
  strip.innerHTML = '';
  attached.forEach((item, idx) => {
    const box = node('div', 'attach-thumb');
    const img = document.createElement('img');
    img.src = item.url;
    img.title = item.name;
    const del = document.createElement('button');
    del.type = 'button';
    del.textContent = '×';
    del.title = 'убрать';
    del.onclick = () => { attached.splice(idx, 1); renderAttached(); };
    box.appendChild(img);
    box.appendChild(del);
    strip.appendChild(box);
  });
  strip.hidden = !attached.length;
  const name = $('#visionName');
  if (name) name.textContent = attached.length ? ('картинок: ' + attached.length) : 'не выбрано';
}

function addFiles(files) {
  const list = Array.from(files || []);
  for (const file of list) {
    if (!ATTACH_TYPES.includes(file.type)) { pushNotice('не картинка: ' + file.name, 'warn'); continue; }
    if (file.size > ATTACH_MAX) { pushNotice('картинка больше 10 МБ: ' + file.name, 'warn'); continue; }
    const reader = new FileReader();
    reader.onload = () => { attached.push({ name: file.name, url: reader.result }); renderAttached(); };
    reader.readAsDataURL(file);
  }
}

function clearAttached() {
  attached = [];
  renderAttached();
  const vf = $('#visionFile');
  if (vf) vf.value = '';
}

function esc(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let userTurnCount = 0;

function md(text) {
  let html = esc(text);
  const fences = [];
  html = html.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const cleanLang = (lang || 'code').trim();
    const cleanCode = code.replace(/\n$/, '');
    fences.push(
      '<div class="code-wrap">' +
        '<div class="code-head">' +
          '<span class="code-lang">' + esc(cleanLang) + '</span>' +
          '<button class="code-copy-btn" type="button">📋 Копировать</button>' +
        '</div>' +
        '<pre><code>' + cleanCode + '</code></pre>' +
      '</div>'
    );
    return '\u0000' + (fences.length - 1) + '\u0000';
  });
  html = html.replace(/`([^`\n]+)`/g, (m, code) => '<code>' + code + '</code>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  html = html.replace(/(^|[\s(])(https?:\/\/[^\s)<>"']+)/g,
    (m, pre, url) => pre + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>');
  html = html.replace(/\n/g, '<br>');
  html = html.replace(/\u0000(\d+)\u0000/g, (m, i) => fences[+i]);
  return html;
}

if (thread) {
  thread.addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('.code-copy-btn');
    if (!copyBtn) return;
    const wrap = copyBtn.closest('.code-wrap');
    const pre = wrap && wrap.querySelector('pre');
    if (!pre) return;
    try {
      await navigator.clipboard.writeText(pre.textContent || '');
      const old = copyBtn.textContent;
      copyBtn.textContent = '✓ Скопировано';
      copyBtn.classList.add('copied');
      setTimeout(() => {
        copyBtn.textContent = old;
        copyBtn.classList.remove('copied');
      }, 1500);
    } catch (err) {
      pushNotice('Не удалось скопировать код: ' + err.message, 'warn');
    }
  });
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (!res.ok && res.status >= 400) {
    let detail = '';
    try { detail = (await res.json()).error || ''; } catch (e) { detail = res.statusText; }
    throw new Error(path + ': ' + detail);
  }
  return res.json();
}

function node(tag, cls, html) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}

function at(ts) {
  return new Date(ts || Date.now()).toLocaleTimeString('ru-RU', { hour12: false });
}

function add(el) {
  thread.appendChild(el);
  scroll();
  return el;
}

function scroll() { log.scrollTop = log.scrollHeight; }

function ensureTurn() {
  if (!turn) turn = add(node('div', 'turn'));
  return turn;
}

function dropWelcome() {
  const welcome = thread.querySelector('.welcome');
  if (welcome) welcome.remove();
}

const WELCOME_PROMPTS = [
  ['🖥', 'что у меня сейчас на экране?'],
  ['📂', 'посмотри папку проекта и предложи, что улучшить'],
  ['🌐', 'найди в интернете последнюю версию Python'],
  ['⚡', 'проверь, почему тормозит ноутбук'],
];

function renderWelcome() {
  if (thread.querySelector('.welcome')) return;
  const wrap = node('div', 'welcome');
  wrap.appendChild(node('div', 'welcome-mark', 'LH'));
  wrap.appendChild(node('div', 'welcome-title', 'LocalHarness'));
  wrap.appendChild(node('div', 'welcome-sub',
    'Локальный агент на этом компьютере. Напиши задачу — он сам найдёт файлы, откроет инструменты и проверит результат.'));
  const chips = node('div', 'welcome-chips');
  WELCOME_PROMPTS.forEach(([ico, text]) => {
    const chip = node('button', 'welcome-chip', '<span class="ico">' + ico + '</span>' + esc(text));
    chip.type = 'button';
    chip.onclick = () => {
      const input = $('#input');
      input.value = text;
      input.focus();
      input.dispatchEvent(new Event('input'));
    };
    chips.appendChild(chip);
  });
  wrap.appendChild(chips);
  thread.appendChild(wrap);
}

function pushUser(text, images) {
  dropWelcome();
  ensureTurn();
  const turnIdx = userTurnCount++;
  const box = node('div', 'msg user');
  box.dataset.turnIndex = String(turnIdx);
  const bodyEl = node('div', 'body', md(text));
  box.appendChild(bodyEl);
  if (images) box.appendChild(node('div', 'attach', 'прикреплено изображений: ' + images));

  const acts = node('div', 'msg-actions');
  const editBtn = node('button', 'msg-act-btn', '✏️ Изменить');
  editBtn.type = 'button';
  editBtn.onclick = () => {
    if (running) { pushNotice('Дождись завершения ответа или нажми Стоп', 'warn'); return; }
    if (box.querySelector('.msg-edit-box')) return;
    bodyEl.hidden = true;
    acts.hidden = true;
    const editor = node('div', 'msg-edit-box');
    const ta = document.createElement('textarea');
    ta.className = 'msg-edit-area';
    ta.value = text || '';
    const bar = node('div', 'msg-edit-bar');
    const cancelBtn = node('button', 'ghost small-btn', 'Отмена');
    cancelBtn.type = 'button';
    cancelBtn.onclick = () => {
      editor.remove();
      bodyEl.hidden = false;
      acts.hidden = false;
    };
    const saveBtn = node('button', 'primary small-btn', 'Сохранить и отправить');
    saveBtn.type = 'button';
    saveBtn.onclick = async () => {
      const updated = (ta.value || '').trim();
      if (!updated) return;
      await editAndResend(turnIdx, updated);
    };
    bar.append(cancelBtn, saveBtn);
    editor.append(ta, bar);
    box.appendChild(editor);
    ta.focus();
  };
  acts.appendChild(editBtn);
  box.appendChild(acts);
  add(box);
}

async function editAndResend(turnIdx, newText) {
  if (!sessionId || running) return;
  try {
    running = true;
    $('#send').disabled = true;
    $('#stopBtn').disabled = false;
    $('#stat').textContent = 'перезапуск…';
    const mode = $('#goalMode') && $('#goalMode').checked ? 'goal' : 'chat';
    await api('/api/chat/edit', { session: sessionId, turn_index: turnIdx, text: newText, mode });
    await reloadAndStreamSession(sessionId);
  } catch (err) {
    running = false;
    $('#send').disabled = false;
    $('#stopBtn').disabled = true;
    pushNotice('Ошибка редактирования: ' + err.message, 'bad');
  }
}

async function regenerateTurn(turnIdx) {
  if (!sessionId || running) return;
  try {
    running = true;
    $('#send').disabled = true;
    $('#stopBtn').disabled = false;
    $('#stat').textContent = 'перегенерация…';
    const mode = $('#goalMode') && $('#goalMode').checked ? 'goal' : 'chat';
    await api('/api/chat/regenerate', { session: sessionId, turn_index: turnIdx, mode });
    await reloadAndStreamSession(sessionId);
  } catch (err) {
    running = false;
    $('#send').disabled = false;
    $('#stopBtn').disabled = true;
    pushNotice('Ошибка перегенерации: ' + err.message, 'bad');
  }
}

async function reloadAndStreamSession(id) {
  if (streamAbort) streamAbort.abort();
  since = 0;
  userTurnCount = 0;
  turn = null; resetStream(); dropDive(); closeBand();
  thread.innerHTML = '';
  $('#trace').innerHTML = '';
  const data = await api('/api/history?session=' + encodeURIComponent(id));
  replaying = true;
  (data.events || []).forEach((ev) => render(ev, true));
  replaying = false;
  running = true;
  $('#send').disabled = true;
  $('#stopBtn').disabled = false;
  stream(id);
}

function attachAssistantActions(box, rawText, turnIdx) {
  if (!box || box.querySelector('.msg-actions')) return;
  const acts = node('div', 'msg-actions');
  const regenBtn = node('button', 'msg-act-btn', '🔄 Перегенерировать');
  regenBtn.type = 'button';
  regenBtn.onclick = () => regenerateTurn(turnIdx);

  const copyBtn = node('button', 'msg-act-btn', '📋 Копировать');
  copyBtn.type = 'button';
  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(rawText || box.querySelector('.body').textContent || '');
      copyBtn.textContent = '✓ Скопировано';
      setTimeout(() => { copyBtn.textContent = '📋 Копировать'; }, 1500);
    } catch (e) {}
  };

  const speakBtn = node('button', 'msg-act-btn', '🔊 Озвучить');
  speakBtn.type = 'button';
  speakBtn.onclick = () => {
    if (typeof speakWithServer === 'function') speakWithServer(rawText || '');
    else if (typeof speakAnswer === 'function') speakAnswer(rawText || '');
  };

  acts.append(regenBtn, copyBtn, speakBtn);
  box.appendChild(acts);
}

function pushAssistant(html, label) {
  dropWelcome();
  ensureTurn();
  const box = node('div', 'msg assistant');
  if (label) box.appendChild(node('div', 'who', label));
  const body = node('div', 'body', html);
  box.appendChild(body);
  add(box);
  return body;
}

function pushNotice(text, cls) {
  ensureTurn();
  return add(node('div', 'notice ' + (cls || ''), md(text)));
}

/* ---------- reasoning & Screenshot-2 inline action timeline ("глубокие раздумья") ---------- */
let lastPlanNote = '';

function _fmtShortBytes(bytes) {
  const b = Number(bytes) || 0;
  if (b <= 0) return '';
  if (b < 1024) return b + 'B';
  if (b < 1048576) return Math.max(1, Math.round(b / 1024)) + 'KB';
  return (b / 1048576).toFixed(1) + 'MB';
}

function _parseArgsObj(raw) {
  if (raw && typeof raw === 'object') return raw;
  if (typeof raw === 'string') {
    try { return JSON.parse(raw); } catch (e) { return { raw }; }
  }
  return {};
}

function _parseResultText(raw) {
  if (raw == null) return '';
  if (typeof raw === 'string') {
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === 'object') {
        if (typeof obj.stdout === 'string' || typeof obj.stderr === 'string') {
          return [obj.stdout || '', obj.stderr ? ('[stderr]\n' + obj.stderr) : '', obj.error || ''].filter(Boolean).join('\n').trim();
        }
        if (typeof obj.out === 'string') return (obj.out + (obj.error ? '\n' + obj.error : '')).trim();
        if (typeof obj.content === 'string') return obj.content;
        if (typeof obj.error === 'string' && Object.keys(obj).length <= 2) return 'Ошибка: ' + obj.error;
      }
    } catch (e) {}
    return raw;
  }
  return JSON.stringify(raw, null, 2);
}

function _renderDiffHtml(diffText) {
  if (!diffText) return '<div class="diff-line">Изменения без текстового диффа</div>';
  return String(diffText).split('\n').map((ln) => {
    let cls = 'diff-line';
    if (ln.startsWith('+++') || ln.startsWith('---') || ln.startsWith('@@')) cls += ' hunk';
    else if (ln.startsWith('+')) cls += ' add';
    else if (ln.startsWith('-')) cls += ' del';
    return '<div class="' + cls + '">' + esc(ln || ' ') + '</div>';
  }).join('');
}

function _updateDiveHeader(d) {
  if (!d) return;
  const durEl = d.head.querySelector('.dive-dur');
  const secs = d.live ? ((Date.now() - d.t0) / 1000).toFixed(1) + ' с' : (d.finalSecs || '');
  const parts = [];
  if (secs) parts.push(secs);
  if (d.toolCount > 0) {
    const w = d.toolCount === 1 ? 'действие' : (d.toolCount < 5 ? 'действия' : 'действий');
    parts.push(d.toolCount + ' ' + w);
  } else if (d.totalChars > 0) {
    parts.push(d.totalChars.toLocaleString('ru-RU') + ' зн.');
  }
  if (durEl) durEl.textContent = parts.length ? ('· ' + parts.join(' · ')) : '';
}

function diveStart(forTool) {
  if (diveEl) {
    if (forTool) {
      diveEl.wrap.classList.remove('only-thoughts');
      diveEl.wrap.hidden = false;
      if (!diveEl.userToggled) {
        diveEl.body.hidden = false;
        diveEl.meta.textContent = diveEl.live ? 'ход работы и раздумья · нажми, чтобы свернуть' : 'ход работы · нажми, чтобы свернуть';
      }
    }
    return diveEl;
  }
  dropWelcome();
  ensureTurn();
  const wrap = node('div', 'dive' + (forTool ? '' : ' only-thoughts'));
  const head = node('div', 'dive-head' + (replaying ? '' : ' live'));
  head.innerHTML = '<span class="mark">⌁</span><span class="label">Глубокие раздумья</span>' +
    '<span class="dive-dur"></span>';
  const body = node('div', 'dive-body dive-timeline');
  const initOpen = forTool ? true : expandReasoning;
  const meta = node('div', 'dive-meta', initOpen ? 'идёт поток · нажми, чтобы свернуть' : 'свернуто · нажми, чтобы открыть');
  const state = {
    wrap, head, body, meta,
    curThoughtEl: null,
    curThoughtText: '',
    totalChars: 0,
    toolCount: 0,
    pendingStepEl: null,
    userToggled: false,
    t0: Date.now(),
    finalSecs: '',
    live: !replaying,
  };
  head.onclick = () => {
    state.userToggled = true;
    body.hidden = !body.hidden;
    meta.textContent = body.hidden ? 'свернуто · нажми, чтобы открыть' : 'открыто · нажми, чтобы свернуть';
    if (!body.hidden) body.scrollTop = body.scrollHeight;
  };
  wrap.append(head, meta, body);
  body.hidden = !initOpen;
  if (!showReasoning && !forTool) wrap.hidden = true;
  add(wrap);
  diveEl = state;
  return diveEl;
}

function _ensureThoughtSeg(d) {
  if (!d.curThoughtEl) {
    d.curThoughtEl = node('div', 'dive-thought');
    d.body.appendChild(d.curThoughtEl);
  }
  return d.curThoughtEl;
}

function showDive(piece) {
  if (!piece) return;
  const d = diveStart(false);
  if (d.toolCount === 0) d.wrap.hidden = !showReasoning;
  const seg = _ensureThoughtSeg(d);
  d.curThoughtText += piece;
  d.totalChars += piece.length;
  seg.textContent = d.curThoughtText.length > 8000 ? d.curThoughtText.slice(-8000) : d.curThoughtText;
  _updateDiveHeader(d);
  if (!d.body.hidden) d.body.scrollTop = d.body.scrollHeight;
}

function diveFinal(text) {
  const clean = String(text || '').trim();
  if (!clean && !diveEl) return;
  const d = diveEl || diveStart(false);
  const seg = _ensureThoughtSeg(d);
  if (clean) {
    const prevLen = d.curThoughtText.length;
    d.curThoughtText = clean;
    d.totalChars = Math.max(d.totalChars, d.totalChars - prevLen + clean.length);
    seg.textContent = clean.length > 12000 ? clean.slice(-12000) : clean;
  }
  // Закрываем текущий текстовый сегмент, чтобы следующее действие встало отдельной строкой ниже
  d.curThoughtEl = null;
  d.curThoughtText = '';
  _updateDiveHeader(d);
}

function divePlan(text) {
  const clean = String(text || '').trim();
  if (!clean) return;
  lastPlanNote = clean;
  const d = diveStart(true);
  d.curThoughtEl = null;
  d.curThoughtText = '';
  const row = node('div', 'dive-plan-note', esc(clean));
  d.body.appendChild(row);
  if (!d.body.hidden) d.body.scrollTop = d.body.scrollHeight;
}

function closeDive() {
  if (!diveEl) return;
  const d = diveEl;
  diveEl = null;
  d.head.classList.remove('live');
  if (d.live) d.finalSecs = ((Date.now() - d.t0) / 1000).toFixed(1) + ' с';
  d.live = false;
  if (d.pendingStepEl) { d.pendingStepEl.remove(); d.pendingStepEl = null; }
  _updateDiveHeader(d);
  d.meta.textContent = d.body.hidden ? 'свернуто · нажми, чтобы открыть' : 'завершено · нажми, чтобы свернуть';
}

function dropDive() {
  closeDive();
  lastPlanNote = '';
}

const _EDIT_SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2.5h-5A1.5 1.5 0 0 0 3 4v8a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 12V7.5"/><path d="m12.1 2.4 1.5 1.5L8.5 9H7V7.5z"/></svg>';

function _describeToolForDive(tool, argsObj, fileEdit, noteHint) {
  if (fileEdit || tool === 'write_file' || tool === 'edit_file') {
    const fname = (fileEdit && (fileEdit.name || fileEdit.rel_path)) || String(argsObj.path || 'file').split(/[\\/]/).pop();
    const fsize = fileEdit ? _fmtShortBytes(fileEdit.size_bytes) : '';
    const added = fileEdit && fileEdit.added != null ? fileEdit.added : null;
    const removed = fileEdit && fileEdit.removed != null ? fileEdit.removed : null;
    return { mode: 'edit', kind: 'Edit', fname, fsize, added, removed, fileEdit };
  }
  if (tool === 'shell' || tool === 'powershell' || tool === 'ssh' || tool === 'python' || tool === 'git') {
    let kind = 'Pwsh';
    let cmdText = '';
    if (tool === 'python') {
      kind = 'Python';
      cmdText = String(argsObj.code || '');
    } else if (tool === 'ssh') {
      kind = 'SSH';
      cmdText = String(argsObj.command || '');
    } else if (tool === 'git') {
      kind = 'Git';
      cmdText = 'git ' + [argsObj.action, argsObj.path, argsObj.message ? ('-m "' + argsObj.message + '"') : ''].filter(Boolean).join(' ');
    } else {
      kind = argsObj.mode === 'bash' ? 'Bash' : 'Pwsh';
      cmdText = String(argsObj.cmd || argsObj.command || '');
    }
    const firstLine = cmdText.trim().split('\n')[0] || tool;
    const desc = (noteHint && noteHint.length <= 90) ? noteHint : (firstLine.length > 72 ? firstLine.slice(0, 72) + '…' : firstLine);
    return { mode: 'cmd', kind, desc, cmdText };
  }
  if (tool === 'read_file') {
    const p = String(argsObj.path || '').split(/[\\/]/).slice(-2).join('/');
    const range = argsObj.offset ? `${argsObj.offset}–${Number(argsObj.offset) + Number(argsObj.limit || 200)}` : '';
    return { mode: 'info', kind: 'Read', fname: p || 'file', fsize: range };
  }
  if (tool === 'list_dir' || tool === 'glob') {
    const p = String(argsObj.path || argsObj.pattern || '.');
    return { mode: 'info', kind: tool === 'glob' ? 'Glob' : 'List', desc: p };
  }
  if (tool === 'grep') {
    return { mode: 'info', kind: 'Grep', desc: `${argsObj.pattern || ''}` + (argsObj.path ? ` in ${argsObj.path}` : '') };
  }
  if (tool === 'web_search' || tool === 'rag_search') {
    return { mode: 'info', kind: tool === 'rag_search' ? 'RAG' : 'Search', desc: String(argsObj.query || '') };
  }
  if (tool === 'web_fetch') {
    return { mode: 'info', kind: 'Fetch', desc: String(argsObj.url || '') };
  }
  if (tool === 'check_goal_point' || tool === 'finish_goal') {
    return {
      mode: 'info',
      kind: 'Goal ✓',
      desc: tool === 'finish_goal' ? String(argsObj.summary || 'закрытие цели') : ('Пункт #' + (argsObj.index || '?') + ' проверен'),
    };
  }
  const preview = Object.values(argsObj).filter((v) => typeof v === 'string' || typeof v === 'number').slice(0, 2).join(' · ');
  return { mode: 'info', kind: tool, desc: preview || 'вызов инструмента' };
}

function diveToolStart(ev) {
  const argsObj = _parseArgsObj(ev.args);
  traceRow('', ev.tool, typeof ev.args === 'string' ? ev.args : JSON.stringify(argsObj));
  const d = diveStart(true);
  d.curThoughtEl = null;
  d.curThoughtText = '';
  if (d.pendingStepEl) d.pendingStepEl.remove();
  const info = _describeToolForDive(ev.tool, argsObj, null, lastPlanNote);
  const step = node('div', 'dive-step pending');
  const head = node('div', 'dive-step-head');
  head.innerHTML = `<span class="dive-step-ico">⏳</span><span class="dive-kind">${esc(info.kind)}</span>`
    + `<span class="dive-dot">·</span><span class="dive-desc">${esc(info.fname || info.desc || ev.tool)}</span>`
    + `<span class="dive-running-pill">выполняется…</span>`;
  step.appendChild(head);
  d.body.appendChild(step);
  d.pendingStepEl = step;
  if (!d.body.hidden) d.body.scrollTop = d.body.scrollHeight;
}

function syncQuickControls() {
  $('#showReasoning').checked = showReasoning;
  $('#expandReasoning').checked = expandReasoning;
  $('#autoCompress').checked = localStorage.getItem('lh.autoCompress') !== '0';
  $('#goalChecklistToggle').checked = showGoalChecklist;
  app.classList.toggle('thinkoff', !showReasoning);
}

/* ---------- streaming answer ---------- */
function streamPiece(piece) {
  if (!answerEl) answerEl = pushAssistant('', '');
  answerEl.textContent += piece;
  scroll();
}

function finishAnswer(html, label, rawText) {
  const turnIdx = Math.max(0, userTurnCount - 1);
  if (answerEl) {
    const box = answerEl.parentNode;
    const who = box.querySelector('.who');
    if (label && who) who.textContent = label;
    else if (label) box.insertBefore(node('div', 'who', esc(label)), box.firstChild);
    answerEl.innerHTML = html;
    attachAssistantActions(box, rawText, turnIdx);
    answerEl = null;
    return;
  }
  const body = pushAssistant(html, label);
  if (body && body.parentNode) attachAssistantActions(body.parentNode, rawText, turnIdx);
}

function resetStream() { answerEl = null; }

/* ---------- tool calls rendered inside .dive (Screenshot-2 style) ---------- */
function toolLine(ev, open) {
  dropWelcome();
  ensureTurn();
  const d = diveStart(true);
  d.curThoughtEl = null;
  d.curThoughtText = '';
  if (d.pendingStepEl) { d.pendingStepEl.remove(); d.pendingStepEl = null; }
  d.toolCount += 1;
  _updateDiveHeader(d);

  const argsObj = _parseArgsObj(ev.args);
  const noteHint = lastPlanNote;
  lastPlanNote = '';
  const info = _describeToolForDive(ev.tool, argsObj, ev.file_edit, noteHint);
  const outClean = _parseResultText(ev.result);

  const step = node('div', 'dive-step ' + (ev.ok ? 'good' : 'bad'));
  const head = node('div', 'dive-step-head');

  if (info.mode === 'edit') {
    const plusHtml = info.added != null ? `<span class="dive-plus">+${info.added}</span>` : '';
    const minusHtml = info.removed != null ? `<span class="dive-minus">-${info.removed}</span>` : '';
    const sizeHtml = info.fsize ? `<span class="dive-fsize">${esc(info.fsize)}</span>` : '';
    head.innerHTML = `<span class="dive-step-ico">${_EDIT_SVG}</span>`
      + `<span class="dive-kind">Edit</span><span class="dive-dot">·</span>`
      + `<span class="dive-fname" title="${esc((info.fileEdit && info.fileEdit.path) || argsObj.path || '')}">${esc(info.fname)}</span>`
      + sizeHtml + plusHtml + minusHtml;

    const diffCard = node('div', 'dive-step-card');
    diffCard.hidden = !open;
    if (info.fileEdit && info.fileEdit.diff) {
      diffCard.innerHTML = _renderDiffHtml(info.fileEdit.diff);
    } else {
      diffCard.appendChild(node('pre', 'dive-out-pre', esc(shorten(outClean, 6000))));
    }

    head.onclick = (e) => {
      if (e.target.classList.contains('dive-fname') && info.fileEdit) {
        e.stopPropagation();
        openReviewModal({ files_changed: 1, insertions: info.added || 0, deletions: info.removed || 0, files: [info.fileEdit] });
        return;
      }
      diffCard.hidden = !diffCard.hidden;
    };
    step.append(head, diffCard);
  } else if (info.mode === 'cmd') {
    const cmdLines = String(info.cmdText || '').trim().split('\n').filter((l) => l !== '');
    const startExpanded = open || (cmdLines.length > 0 && cmdLines.length <= 10);
    head.innerHTML = `<span class="dive-step-ico dive-chev">${startExpanded ? '⌄' : '›'}</span>`
      + `<span class="dive-kind">${esc(info.kind)}</span><span class="dive-dot">·</span>`
      + `<span class="dive-desc" title="${esc(info.cmdText)}">${esc(info.desc)}</span>`;

    const cmdCard = node('div', 'dive-cmd-card');
    cmdCard.hidden = !startExpanded;
    cmdLines.forEach((ln) => {
      const row = node('div', 'dive-cmd-line');
      row.innerHTML = `<span class="dive-dollar">$</span><span>${esc(ln)}</span>`;
      cmdCard.appendChild(row);
    });
    if (outClean) {
      const outPre = node('pre', 'dive-out-pre', esc(shorten(outClean, 12000)));
      if (!ev.ok) outPre.style.color = 'var(--fail)';
      cmdCard.appendChild(outPre);
    }
    head.onclick = () => {
      cmdCard.hidden = !cmdCard.hidden;
      const ch = head.querySelector('.dive-chev');
      if (ch) ch.textContent = cmdCard.hidden ? '›' : '⌄';
    };
    step.append(head, cmdCard);
  } else {
    const showOpen = open || !!ev.image || !ev.ok;
    const targetHtml = info.fname
      ? `<span class="dive-fname">${esc(info.fname)}</span>`
      : `<span class="dive-desc">${esc(info.desc || '')}</span>`;
    const sizeHtml = info.fsize ? `<span class="dive-fsize">${esc(info.fsize)}</span>` : '';
    head.innerHTML = `<span class="dive-step-ico dive-chev">${showOpen ? '⌄' : '›'}</span>`
      + `<span class="dive-kind">${esc(info.kind)}</span><span class="dive-dot">·</span>`
      + targetHtml + sizeHtml;

    const card = node('div', 'dive-step-card');
    card.hidden = !showOpen;
    if (outClean) {
      const pre = node('pre', 'dive-out-pre', esc(shorten(outClean, 16000)));
      pre.style.marginTop = '0';
      pre.style.paddingTop = '0';
      pre.style.borderTop = 'none';
      card.appendChild(pre);
    }
    if (ev.image) {
      const img = node('img');
      img.src = '/shot?path=' + encodeURIComponent(ev.image);
      img.alt = 'скриншот';
      img.addEventListener('click', () => window.open(img.src));
      card.appendChild(img);
    }
    head.onclick = () => {
      card.hidden = !card.hidden;
      const ch = head.querySelector('.dive-chev');
      if (ch) ch.textContent = card.hidden ? '›' : '⌄';
    };
    step.append(head, card);
  }

  d.body.appendChild(step);
  if (!d.body.hidden) d.body.scrollTop = d.body.scrollHeight;
  const rawArgsStr = typeof ev.args === 'string' ? ev.args : JSON.stringify(argsObj);
  traceRow(ev.ok ? 'good' : 'bad', ev.tool, rawArgsStr);
  return step;
}

function shorten(value, limit) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  if (!text) return '';
  const cap = limit || 24000;
  return text.length > cap ? text.slice(0, cap) + '\n…[обрезано в интерфейсе]' : text;
}

/* ---------- right panel ---------- */
function traceRow(cls, kind, txt) {
  const row = node('div', 'trace-row ' + cls);
  row.innerHTML = '<span class="kind">›</span><span class="txt">' + esc(kind + ' ' + (txt || '').slice(0, 40)) +
    '</span><span class="t">' + at() + '</span>';
  $('#trace').appendChild(row);
  $('#trace').scrollTop = $('#trace').scrollHeight;
}

function paintUsage(sp, steps, rounds) {
  if (sp && Object.keys(sp).length) lastSpeed = sp;
  if (steps != null) lastSteps = steps;
  if (rounds != null) lastRounds = rounds;
  const bits = [];
  bits.push('TPS        <b>' + (lastSpeed.tps || '—') + '</b> ток/с');
  bits.push('Output     <b>' + (lastSpeed.tokens || 0) + '</b> ток' + (lastSpeed.exact === false ? ' (оценка)' : ''));
  bits.push('TTFT       <b>' + (lastSpeed.ttft == null ? '—' : lastSpeed.ttft + ' с') + '</b>');
  bits.push('Duration   <b>' + (lastSpeed.gen_seconds == null ? '—' : lastSpeed.gen_seconds + ' с') + '</b>');
  bits.push('Steps      <b>' + (lastSteps == null ? '—' : lastSteps) + '</b>');
  if (lastRounds) bits.push('Goal tries <b>' + lastRounds + '</b>');
  if (lastContext) {
    bits.push('Context <b>' + fmtTok(lastContext.tokens) + '</b> / ' + fmtTok(lastContext.window)
      + ' ток · сжатие при ' + fmtTok(lastContext.threshold));
  }
  $('#usage').innerHTML = bits.join('\n');
}

function showSpeed(ev) {
  lastSpeed = ev || lastSpeed;
  const bits = [];
  if (ev.tps) bits.push(ev.tps + ' ток/с');
  bits.push((ev.tokens || 0) + ' ток');
  if (ev.ttft != null) bits.push('старт ' + ev.ttft + ' с');
  if (ev.exact === false) bits.push('~оценка');
  if (ev.gpu_throttled) bits.push('GPU ' + (ev.gpu_temp != null ? ev.gpu_temp : '') + '°C 🧊 троттлинг');
  else if (ev.gpu_temp && (ev.gpu_status === 'hot' || ev.gpu_status === 'warm')) bits.push('GPU ' + ev.gpu_temp + '°C');
  $('#speed').textContent = bits.join(' · ');
  paintUsage(lastSpeed, null, null);
}

/* ---------- context meter: сколько контекста съедено и где начнётся сжатие ---------- */
const CTX_MIN = 512;
const CTX_MAX = 262144;

function clampContext(value, fallback) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(CTX_MAX, Math.max(CTX_MIN, n));
}

function fmtTok(n) {
  n = Number(n) || 0;
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace('.0', '') + 'k';
  return String(n);
}

function paintContext(ev) {
  const bar = $('#ctxBar');
  lastContext = ev;
  const win = Number(ev.window) || 0;
  const used = Number(ev.tokens) || 0;
  const tools = Number(ev.tools) || 0;
  const answer = Number(ev.answer) || 0;
  const history = ev.history != null ? Number(ev.history) : Math.max(0, used - tools);
  const threshold = Number(ev.threshold) || 0;
  const pct = Math.max(0, Math.min(100, Number(ev.pct) || 0));
  const share = n => win ? Math.max(0, Math.min(100, n * 100 / win)) : 0;
  const toolsPct = share(tools);
  const histPct = Math.min(100 - toolsPct, share(history));
  const markPct = share(tools + threshold);
  bar.hidden = false;
  const toolsEl = $('#ctxTools');
  toolsEl.style.width = toolsPct + '%';
  toolsEl.title = 'схема инструментов: ' + fmtTok(tools) + ' ток';
  const fill = $('#ctxFill');
  fill.style.left = toolsPct + '%';
  fill.style.width = histPct + '%';
  fill.className = 'ctxbar-fill' + (pct >= markPct ? ' over' : (pct >= markPct * 0.85 ? ' warn' : ''));
  const ansEl = $('#ctxAnswer');
  ansEl.style.width = share(answer) + '%';
  ansEl.title = 'резерв под ответ: ' + fmtTok(answer) + ' ток';
  const mark = $('#ctxMark');
  mark.style.left = markPct + '%';
  mark.title = 'сжатие, когда диалог дойдёт до ~' + fmtTok(threshold) + ' ток';
  $('#ctxLabel').textContent = 'контекст ' + pct + '% · диалог ' + fmtTok(history) + '/' + fmtTok(threshold) + ' ток';
  $('#ctxLimit').textContent = 'схема ' + fmtTok(tools) + ' · ответ ' + fmtTok(answer) + ' · окно ' + fmtTok(win);
  paintUsage(lastSpeed, null, null);
}

/* ---------- intercepting bands above the composer ---------- */
function band(html) {
  const el = $('#approveBand');
  el.innerHTML = html;
  el.hidden = false;
  el.scrollIntoView({ block: 'nearest' });
  return el;
}

function closeBand() {
  const el = $('#approveBand');
  el.hidden = true;
  el.innerHTML = '';
}

function askBand(ev) {
  const isApprove = ev.type === 'approve';
  const head = isApprove
    ? '<div class="band-title">Опасное действие · ' + esc(ev.tool) + '</div>' +
      '<div>' + esc(ev.reason || '') + '</div>' +
      '<div class="band-cmd">' + esc(shorten(ev.args, 1200)) + '</div>'
    : '<div class="band-title">' + esc(ev.question || 'Вопрос') + '</div>';
  const el = band(head + '<div class="band-row"></div>');
  const row = el.querySelector('.band-row');
  if (isApprove) {
    const yes = node('button', 'allow', 'Разрешить один раз');
    const always = node('button', 'allow', 'Разрешить всегда');
    const no = node('button', 'deny', 'Запретить');
    yes.onclick = () => respond(ev.key, { allow: true }).then(closeBand);
    always.onclick = () => respond(ev.key, { allow: true, remember: true }).then(closeBand);
    no.onclick = () => respond(ev.key, { allow: false }).then(closeBand);
    row.append(yes, always, no);
  } else {
    const input = node('input');
    input.type = 'text';
    input.placeholder = 'ответ…';
    const send = node('button', 'allow', 'Ответить');
    const done = () => respond(ev.key, { text: input.value }).then(closeBand);
    send.onclick = done;
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(); });
    row.append(input, send);
    (ev.options || []).forEach((opt) => {
      const btn = node('button', 'deny', esc(String(opt)));
      btn.onclick = () => respond(ev.key, { text: String(opt) }).then(closeBand);
      row.appendChild(btn);
    });
    input.focus();
  }
}

async function respond(key, value) {
  try { await api('/api/respond', { session: sessionId, key, value }); }
  catch (err) { pushNotice(err.message, 'bad'); }
}

function renderChecklist(points) {
  const box = $('#goalChecklist');
  if (points) lastGoalPoints = points;
  if (!showGoalChecklist) { box.hidden = true; box.innerHTML = ''; return; }
  if (!points || !points.length) { box.hidden = true; box.innerHTML = ''; return; }
  const done = points.filter((p) => p.done).length;
  box.hidden = false;
  box.innerHTML = '<div class="goal-progress">пункты ' + done + '/' + points.length + '</div>' +
    points.map((p) => (
      '<label class="goal-point' + (p.done ? ' done' : '') + '">' +
      '<input type="checkbox" disabled' + (p.done ? ' checked' : '') + '>' +
      '<span>' + esc(p.text || '') + '</span>' +
      (p.evidence ? '<em class="goal-evidence" title="' + esc(p.evidence) + '">проверено</em>' : '') +
      '</label>'
    )).join('');
}

function goalBand(ev) {
  const el = $('#goalBand');
  el.hidden = false;
  const state = $('#goalState');
  const text = $('#goalText');
  state.className = 'goal-state';
  if (ev.state === 'checklist' || ev.state === 'point') {
    renderChecklist(ev.points);
    const pts = ev.points || [];
    const done = pts.filter((p) => p.done).length;
    state.textContent = ev.state === 'point'
      ? (ev.done ? 'пункт ' + ev.index + ' отмечен' : 'пункт ' + ev.index + ' снят')
      : 'цель: чеклист';
    text.textContent = pts.length ? ('выполнено ' + done + '/' + pts.length) : '';
    if (ev.state === 'point' && pts.length && done === pts.length) {
      state.textContent = 'все пункты отмечены';
      state.classList.add('done');
    }
    return;
  }
  if (ev.state === 'refined' || ev.state === 'started') renderChecklist(null);
  const map = {
    refined: ['цель уточнена', (ev.objective || '') + (ev.criteria ? ' · критерий: ' + ev.criteria : '')],
    started: ['цель запущена', 'подходов: ' + (ev.rounds > 0 ? ev.rounds : '∞') + ' · ' + (ev.objective || '') + (ev.criteria ? ' · критерий: ' + ev.criteria : '')],
    continue: ['в работе', 'подход ' + (ev.round + 1) + ' · шагов ' + (ev.steps || 0) + ' · ' + (ev.summary || '')],
    exhausted: ['остановлено', (ev.summary || ev.objective || '')],
    closed: [ev.achieved ? 'цель достигнута' : 'не достигнуто', (ev.summary || '') + (ev.evidence ? ' · проверка: ' + ev.evidence : '')],
  };
  const pair = map[ev.state] || ['цель: ' + ev.state, ev.summary || ev.objective || ''];
  state.textContent = pair[0];
  text.textContent = pair[1];
  if (ev.state === 'closed') state.classList.add(ev.achieved ? 'done' : 'stop');
  if (ev.state === 'exhausted') state.classList.add('stop');
}

/* ---------- event router ---------- */
function render(ev, replayed) {
  if (replayed && (ev.type === 'question' || ev.type === 'approve')) {
    pushNotice((ev.question || (ev.tool + ': ' + ev.reason)) + ' — уже обработано', 'warn');
    return;
  }
  if (!replayed && typeof onVoiceCallEvent === 'function') {
    try { onVoiceCallEvent(ev); } catch (e) {}
  }
  switch (ev.type) {
    case 'user':
      turn = null; dropDive(); resetStream(); closeBand();
      pushUser(ev.text, ev.images);
      break;
    case 'plan': divePlan(ev.text); break;
    case 'reason': showDive(ev.text); break;
    case 'thinking': diveFinal(ev.text); break;
    case 'delta': streamPiece(ev.text); break;
    case 'speed': showSpeed(ev); break;
    case 'goal': goalBand(ev); break;
    case 'tool_start': diveToolStart(ev); break;
    case 'tool': resetStream(); toolLine(ev, false); break;
    case 'question': case 'approve': resetStream(); closeDive(); askBand(ev); break;
    case 'notice': pushNotice(ev.text, 'warn'); break;
    case 'context': paintContext(ev); break;
    case 'compact': pushNotice('контекст сжат: свёрнуто ходов — ' + ev.folded_turns
      + (ev.before ? ' · ' + fmtTok(ev.before) + '→' + fmtTok(ev.after) + ' ток' : ''), 'warn compact'); break;
    case 'error': resetStream(); closeDive(); pushNotice('сбой: ' + ev.text, 'bad'); break;
    case 'stopped':
      resetStream(); closeDive(); pushNotice('остановлено', 'warn');
      finishRun(ev || {});
      break;
    case 'done':
      closeDive();
      finishAnswer(md(ev.text), 'модель · ' + (ev.protocol === 'json' ? 'без tool_use' : 'native'), ev.text);
      if ((ev.changes && ev.changes.files_changed > 0) || ev.goal) {
        renderChangesSummaryBar(ev.changes, ev.goal, ev.steps, ev.rounds);
      }
      if (!replayed && typeof onVoiceCallAnswer === 'function' && ev.text) {
        const handledByCall = onVoiceCallAnswer(ev.text);
        if (!handledByCall && typeof speakAnswer === 'function') speakAnswer(ev.text);
      } else if (!replayed && typeof speakAnswer === 'function' && ev.text) {
        speakAnswer(ev.text);
      }
      finishRun(ev);
      break;
    default: break;
  }
}

function finishRun(ev) {
  running = false;
  $('#stopBtn').disabled = true;
  $('#send').disabled = false;
  const sp = (ev && ev.speed) || null;
  if (sp) lastSpeed = sp;
  lastSteps = (ev && ev.steps) != null ? ev.steps : lastSteps;
  lastRounds = (ev && ev.rounds) || null;
  const parts = ['шагов: ' + (lastSteps != null ? lastSteps : 0)];
  if (lastRounds) parts.push('подходов: ' + lastRounds);
  if (sp && sp.tps) parts.push(sp.tps + ' ток/с');
  $('#stat').textContent = parts.join(' · ');
  paintUsage(sp, lastSteps, lastRounds);
  if (streamAbort) { streamAbort.abort(); streamAbort = null; }
  refreshState();
}

/* ---------- transport ---------- */
async function stream(id) {
  if (streamAbort) streamAbort.abort();
  streamAbort = new AbortController();
  try {
    const res = await fetch('/api/events?session=' + encodeURIComponent(id) + '&since=' + since,
      { signal: streamAbort.signal });
    if (!res.ok) return;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let split;
      while ((split = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, split);
        buf = buf.slice(split + 2);
        if (!block.startsWith('data: ')) continue;
        let ev;
        try { ev = JSON.parse(block.slice(6)); } catch (e) { continue; }
        if (typeof ev._i === 'number') since = Math.max(since, ev._i + 1);
        render(ev);
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') console.warn(err);
  } finally {
    if (running && (!streamAbort || !streamAbort.signal.aborted)) {
      try {
        const data = await api('/api/history?session=' + encodeURIComponent(id));
        if (!data.running) {
          const events = data.events || [];
          const lastEv = events[events.length - 1];
          if (lastEv && lastEv.type === 'done') {
            render(lastEv);
          } else {
            finishRun({});
          }
        }
      } catch (e) {
        finishRun({});
      }
    }
  }
}

async function send() {
  const text = $('#input').value.trim();
  if ((!text && !attached.length) || running) return;
  $('#input').value = '';
  running = true;
  turn = null; resetStream(); dropDive(); closeBand();
  $('#trace').innerHTML = '';
  $('#goalBand').hidden = !$('#goalMode').checked;
  $('#goalChecklist').hidden = true;
  $('#goalChecklist').innerHTML = '';
  $('#stopBtn').disabled = false;
  $('#send').disabled = true;
  $('#stat').textContent = 'думаю…';
  try {
    const body = { session: sessionId, text: text || 'Посмотри на картинку.' };
    if (attached.length) {
      body.images = attached.map((item) => item.url);
      clearAttached();
    }
    if ($('#goalMode').checked) {
      body.mode = 'goal';
      body.criteria = $('#goalCriteria').value.trim();
    }
    const res = await api('/api/chat', body);
    if (res.session !== sessionId) { sessionId = res.session; }
    since = 0;
    stream(sessionId);
  } catch (err) {
    pushNotice(err.message, 'bad');
    running = false;
    $('#send').disabled = false;
    $('#stopBtn').disabled = true;
  }
}

async function openSession(id) {
  setSideDrawer(false);
  if (streamAbort) streamAbort.abort();
  running = false;
  sessionId = id;
  since = 0;
  userTurnCount = 0;
  turn = null; resetStream(); dropDive(); closeBand();
  thread.innerHTML = '';
  $('#trace').innerHTML = '';
  $('#goalBand').hidden = true;
  $('#goalChecklist').hidden = true;
  $('#goalChecklist').innerHTML = '';
  $('#ctxBar').hidden = true;
  $('#speed').textContent = '';
  $('#stat').textContent = '';
  $('#usage').textContent = '—';
  lastSteps = null; lastRounds = null; lastSpeed = {}; lastContext = null;
  $('#stopBtn').disabled = true;
  $('#send').disabled = false;
  document.querySelectorAll('.session').forEach((el) => el.classList.toggle('active', el.dataset.id === id));
  const data = await api('/api/history?session=' + encodeURIComponent(id));
  const title = (data.meta && data.meta.title) || 'новая сессия';
  $('#sessionTitle').textContent = title;
  if (typeof syncHeaderWorkspaceChip === 'function') {
    syncHeaderWorkspaceChip((data.meta && data.meta.workspace) || '');
  }
  if (data.running) {
    running = true;
    $('#send').disabled = true;
    $('#stopBtn').disabled = false;
    $('#stat').textContent = 'думаю…';
    stream(id);
    return;
  }
  replaying = true;
  (data.events || []).forEach((ev) => render(ev, true));
  replaying = false;
  if (!(data.events || []).length) {
    const first = (data.messages || []).filter((m) => m.role === 'user')[0];
    if (first) {
      const text = typeof first.content === 'string' ? first.content
        : (first.content || []).filter((p) => p && p.type === 'text').map((p) => p.text).join('\n');
      pushUser(text);
    }
  }
  if (!thread.children.length) renderWelcome();
}

/* ---------- view tabs ---------- */
function setView(name) {
  setSideDrawer(false);
  currentView = name;
  document.querySelectorAll('.view-tab').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const chat = name === 'chat';
  log.hidden = !chat;
  $('#viewPlugins').hidden = name !== 'plugins';
  $('#viewLM').hidden = name !== 'lm';
  if ($('#viewDashboard')) $('#viewDashboard').hidden = name !== 'dashboard';
  if ($('#viewCluster')) $('#viewCluster').hidden = name !== 'cluster';
  if ($('#viewSchedule')) $('#viewSchedule').hidden = name !== 'schedule';
  if ($('#viewTrain')) $('#viewTrain').hidden = name !== 'train';
  if ($('#viewFiles')) $('#viewFiles').hidden = name !== 'files';
  const composer = document.querySelector('.composer');
  if (composer) composer.style.display = chat ? '' : 'none';
  const quick = document.querySelector('.quick-controls');
  if (quick) quick.style.display = chat ? '' : 'none';
  $('#right').style.display = chat ? '' : 'none';
  app.classList.toggle('wide', !chat);
  localStorage.setItem('lh.view', name);
  if (name === 'plugins') {
    refreshPlugins();
    if (currentPlugSubtab === 'workshop') refreshWorkshop();
  }
  if (name === 'lm') refreshLM();
  if (name === 'dashboard') refreshDashboard();
  else if (typeof dashTimer !== 'undefined' && dashTimer) { clearTimeout(dashTimer); dashTimer = null; }
  if (name === 'cluster') refreshCluster();
  else if (typeof clusterTimer !== 'undefined' && clusterTimer) { clearTimeout(clusterTimer); clusterTimer = null; }
  if (name === 'schedule') refreshSchedule();
  else if (typeof schedTimer !== 'undefined' && schedTimer) { clearTimeout(schedTimer); schedTimer = null; }
  if (name === 'train') {
    if (typeof refreshTrain === 'function') refreshTrain(true);
  } else if (typeof trainTimer !== 'undefined' && trainTimer) {
    clearTimeout(trainTimer); trainTimer = null;
  }
  if (name === 'files' && typeof refreshIdeFiles === 'function') {
    refreshIdeFiles();
  }
}

/* ---------- state ---------- */
function shortModel(id) {
  const clean = String(id).split('/').pop().replace('.gguf', '');
  return clean.length > 30 ? clean.slice(0, 30) + '…' : clean;
}

function fillSelect(sel, pairs, current) {
  const wanted = current || (pairs[0] && pairs[0][0]) || '';
  const open = document.activeElement === sel;
  sel.innerHTML = '';
  pairs.forEach((pair) => {
    const opt = node('option', null, esc(pair[1]));
    opt.value = pair[0];
    if (pair[0] === wanted) opt.selected = true;
    sel.appendChild(opt);
  });
  if (open) sel.focus();
}

function renderModelMenu(st) {
  const ps = st.provider_status || {};
  const models = ps.models || [];
  const currentModel = st.model || models[0] || '';
  const provTitle = $('#mmProviderTitle');
  if (provTitle) provTitle.textContent = ps.label || st.provider || 'провайдер';
  const chipProv = $('#modelChipProv');
  if (chipProv) chipProv.textContent = ps.label || st.provider || '';
  const chipName = $('#modelChipName');
  if (chipName) chipName.textContent = currentModel ? shortModel(currentModel) : 'модель не выбрана';
  const dot = $('#modelChipDot');
  if (dot) dot.classList.toggle('bad', !ps.alive);

  const provBox = $('#mmProviders');
  if (provBox) {
    provBox.innerHTML = '';
    Object.entries(st.providers || {}).forEach(([key, label]) => {
      const b = node('button', 'mm-prov' + (key === st.provider ? ' on' : ''), esc(label));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', key === st.provider ? 'true' : 'false');
      b.onclick = async () => {
        const sel = $('#provider');
        sel.value = key;
        await patchConfig('provider', key);
      };
      provBox.appendChild(b);
    });
  }

  const lmsBar = $('#mmLmsBar');
  if (lmsBar) {
    const isLms = st.provider === 'lmstudio';
    lmsBar.hidden = !isLms;
    if (isLms) {
      const rtInfo = st.lms_runtimes || {};
      const engChip = $('#mmLmsEngineChip');
      if (engChip) {
        if (rtInfo.vulkan_active) {
          engChip.textContent = '🌋 Vulkan' + (rtInfo.active_version ? ' v' + rtInfo.active_version : '');
          engChip.title = 'Активен движок Vulkan llama.cpp (нажми для настроек LM Studio)';
        } else if (rtInfo.active_name) {
          const low = rtInfo.active_name.toLowerCase();
          const ico = low.includes('cuda') ? '🟢 CUDA' : (low.includes('rocm') ? '🔴 ROCm' : '💻 CPU');
          engChip.textContent = ico + (rtInfo.active_version ? ' v' + rtInfo.active_version : '');
          engChip.title = 'Активен движок: ' + rtInfo.active_name + ' (нажми, чтобы переключить на Vulkan)';
        } else {
          engChip.textContent = '⚡ LM Studio';
        }
        engChip.onclick = () => {
          setModelMenu(false);
          setView('lm');
        };
      }
      const qCtx = $('#mmQuickCtx');
      if (qCtx && document.activeElement !== qCtx) {
        const curCtx = clampContext(st.lms_context_length || st.context_tokens, 16384);
        const stdVals = ['2048', '4096', '8192', '16384', '32768', '65536', '131072'];
        let customOpt = qCtx.querySelector('option[data-custom="1"]');
        if (!stdVals.includes(String(curCtx))) {
          if (!customOpt) {
            customOpt = document.createElement('option');
            customOpt.dataset.custom = '1';
            qCtx.appendChild(customOpt);
          }
          customOpt.value = String(curCtx);
          customOpt.textContent = curCtx.toLocaleString('ru-RU') + ' ток.';
        } else if (customOpt) {
          customOpt.remove();
        }
        qCtx.value = String(curCtx);
      }
      const openBtn = $('#mmOpenLoaderBtn');
      if (openBtn) {
        openBtn.onclick = () => {
          setModelMenu(false);
          openLmsLoadModal(currentModel);
        };
      }
    }
  }

  const list = $('#mmModels');
  if (list) {
    const keepScroll = list.scrollTop;
    list.innerHTML = '';
    if (!models.length) {
      list.appendChild(node('div', 'mm-empty', 'Моделей нет — проверь LM Studio или провайдера.'));
    }
    const vision = new Set(ps.vision_models || []);
    const metaMap = ps.model_meta || {};
    const loadedSet = new Set((st.lms_loaded || []).map((x) => String(x || '').toLowerCase()));
    models.forEach((m) => {
      const on = m === currentModel;
      const meta = metaMap[m] || {};
      const isLoaded = !!(meta.loaded || loadedSet.has(String(m).toLowerCase()));
      const loadedCtxTxt = meta.loadedContextLength
        ? (meta.loadedContextLength >= 1024 ? Math.round(meta.loadedContextLength / 1024) + 'K' : meta.loadedContextLength)
        : '';
      const stateBadge = st.provider === 'lmstudio'
        ? (isLoaded
            ? '<span class="mm-state-pill loaded" title="Модель загружена в видеопамять">● ' + esc(loadedCtxTxt || 'VRAM') + '</span>'
            : '<span class="mm-state-pill disk" title="Модель на диске (не загружена)">диск</span>')
        : '';
      const b = node('button', 'mm-model' + (on ? ' on' : ''),
        '<span class="mm-model-name">' + esc(shortModel(m)) + '</span>'
        + (vision.has(m) ? '<span class="mm-vision">vision</span>' : '')
        + stateBadge
        + '<span class="mm-check">✓</span>');
      b.type = 'button';
      b.title = m + (meta.maxContextLength ? ' · макс. контекст ' + meta.maxContextLength : '');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.onclick = async () => {
        const sel = $('#model');
        sel.value = m;
        setModelMenu(false);
        await patchConfig('model', m);
      };
      if (st.provider === 'lmstudio') {
        const row = node('div', 'mm-model-row');
        const cfgBtn = node('button', 'mm-cfg-btn', isLoaded ? '⚙️' : '⚡ Загрузить');
        cfgBtn.type = 'button';
        cfgBtn.title = isLoaded
          ? 'Изменить окно контекста / GPU Offload / перезагрузить модель'
          : 'Выбрать окно контекста и загрузить модель в память как в LM Studio';
        cfgBtn.onclick = (ev) => {
          ev.stopPropagation();
          setModelMenu(false);
          openLmsLoadModal(m);
        };
        row.append(b, cfgBtn);
        list.appendChild(row);
      } else {
        list.appendChild(b);
      }
    });
    list.scrollTop = keepScroll;
  }
}

async function refreshState() {
  const st = await api('/api/state');
  stateCache = st;
  const ps = st.provider_status || {};
  $('#provDot').className = 'dot ' + (ps.alive ? 'ok' : 'bad');
  $('#dnsChip').textContent = 'dns-обход: ' + (st.dns_probe && st.dns_probe.ok ? st.dns_probe.ip : 'не работает');
  $('#dnsChip').title = st.dns_probe && st.dns_probe.ok ? '' : (st.dns_probe || {}).error || '';
  $('#modelCount').textContent = (ps.label || st.provider) + ' · моделей: ' + ((ps.models || []).length);
  const curLmsCtx = clampContext(st.lms_context_length || st.context_tokens, 16384);
  if ($('#lmContext') && document.activeElement !== $('#lmContext')) $('#lmContext').value = curLmsCtx;
  if ($('#lmGpuQuick') && document.activeElement !== $('#lmGpuQuick')) $('#lmGpuQuick').value = st.lms_gpu_offload || 'max';
  if ($('#lmKvQuick') && document.activeElement !== $('#lmKvQuick')) $('#lmKvQuick').value = st.lms_kv_quant || 'f16';
  if ($('#lmTtl') && document.activeElement !== $('#lmTtl')) $('#lmTtl').value = st.lms_ttl || 0;
  document.querySelectorAll('#lmPageCtxChips .ctx-preset-pill').forEach((btn) => {
    btn.classList.toggle('active', Number(btn.dataset.ctx) === curLmsCtx);
  });
  if (document.activeElement !== $('#systemPrompt')) $('#systemPrompt').value = st.system_prompt || '';
  $('#searxngEnabled').checked = !!st.search_searxng;
  if (document.activeElement !== $('#searxngUrl')) $('#searxngUrl').value = st.search_searxng_url || 'http://127.0.0.1:8080';
  if ($('#obscuraEnabled')) $('#obscuraEnabled').checked = !!st.search_obscura;
  if ($('#obscuraPath') && document.activeElement !== $('#obscuraPath')) $('#obscuraPath').value = st.obscura_path || '';
  if ($('#obscuraCdp') && document.activeElement !== $('#obscuraCdp')) $('#obscuraCdp').value = st.obscura_cdp || 'http://127.0.0.1:9223';
  const obsChip = $('#obscuraStatusChip');
  if (obsChip && st.obscura_status) {
    const ost = st.obscura_status;
    obsChip.className = 'sec-chip ' + (ost.running ? 'ok' : (ost.installed ? 'warn' : ''));
    obsChip.textContent = ost.running ? 'запущен (CDP)' : (ost.installed ? 'установлен' : 'не найден');
  }
  $('#memoryEnabled').checked = !!st.memory_enabled;
  $('#memorySection').hidden = !st.memory_enabled;
  if (st.memory_enabled) refreshMemory();
  $('#imageGenEnabled').checked = !!st.image_gen_enabled;
  $('#imageGenSection').hidden = !st.image_gen_enabled;
  if (document.activeElement !== $('#comfyuiUrl')) $('#comfyuiUrl').value = st.comfyui_url || 'http://127.0.0.1:8188';
  $('#lanAccessEnabled').checked = !!st.lan_access;
  $('#lanSection').hidden = !st.lan_access;
  if (st.lan_access) {
    $('#lanUrlText').textContent = st.lan_url || '';
    const qr = $('#qrCodeContainer');
    if (qr.dataset.url !== (st.lan_url || '')) {
      qr.dataset.url = st.lan_url || '';
      qr.innerHTML = '<img src="/api/qrcode?url=' + encodeURIComponent(st.lan_url || '') + '" width="150" height="150" alt="QR Code">';
    }
  }
  $('#gameFailsafeEnabled').checked = st.game_failsafe !== false;
  const vtOn = !!st.virustotal_enabled;
  const keyChip = $('#vtKeyChip');
  if (keyChip) {
    keyChip.className = 'sec-chip ' + (vtOn ? 'ok' : '');
    keyChip.textContent = vtOn ? 'ключ задан' : 'ключ не задан';
  }
  const modeChip = $('#vtModeChip');
  if (modeChip) {
    modeChip.className = 'sec-chip ' + (vtOn ? 'ok' : '');
    modeChip.textContent = vtOn ? 'VirusTotal API' : 'локальный аудит';
  }
  const secNote = $('#plugSecNote');
  if (secNote) secNote.textContent = vtOn ? 'аудит кода + VirusTotal' : 'аудит кода включён';
  renderThermalState(st.gpu_thermal);
  if (typeof syncExtendedState === 'function') syncExtendedState(st);
  $('#toolsChip').textContent = 'инструментов: ' + (st.tools || []).length + ' · профиль ' + (st.tool_profile || 'core');
  $('#toolsChip').title = (st.tools || []).join(', ');
  fillSelect($('#provider'), Object.keys(st.providers || {}).map((k) => [k, st.providers[k]]), st.provider);
  const models = ps.models || [];
  const visionSet = new Set(ps.vision_models || []);
  fillSelect($('#model'), models.map((m) => [m, shortModel(m) + (visionSet.has(m) ? ' · vision' : '')]), st.model || models[0] || '');
  fillSelect($('#profile'), [['core', 'core'], ['full', 'full']], st.tool_profile);
  fillSelect($('#protocol'), [['auto', 'авто'], ['native', 'native'], ['json', 'без tool_use']], st.tool_protocol);
  fillSelect($('#perms'), [['auto', 'права: авто'], ['unrestricted', 'права: всё'], ['ask', 'права: спрос']],
    (st.permissions && st.permissions.mode) || 'auto');
  renderModelMenu(st);
  if (typeof syncWorkspaceSidebarUi === 'function') syncWorkspaceSidebarUi(st);
  $('#modeHint').textContent = {
    auto: 'авто: чёрный список всё равно спрашивает',
    unrestricted: 'ВСЕ ПРАВА: подтверждений нет',
    ask: 'спрос перед каждым действием',
  }[(st.permissions && st.permissions.mode) || 'auto'] || '';
  const pl = st.plugins || {};
  const broken = (pl.plugins || []).filter((p) => p.error);
  $('#plugChip').textContent = 'плагины: ' + (pl.loaded || 0) + ' · ' + (pl.tools || []).length + ' инст.'
    + (broken.length ? ' · ошибок ' + broken.length : '');
  $('#plugChip').title = (pl.tools || []).join(', ') + '\n' + (pl.dir || '')
    + (broken.length ? '\n' + broken.map((p) => p.file + ': ' + p.error).join('\n') : '');
  renderSessions();
  const current = (st.sessions || []).find((row) => row.id === sessionId);
  if (current) $('#sessionTitle').textContent = current.title;

  const hasLoaded = !!(st.model_loaded || (st.lms_loaded && st.lms_loaded.length));
  const headUnload = $('#headUnloadBtn');
  if (headUnload) {
    headUnload.hidden = !hasLoaded;
    if (hasLoaded) {
      headUnload.onclick = async () => {
        headUnload.disabled = true;
        headUnload.textContent = 'выгружается…';
        try {
          await api('/api/lms/unload', { model: '' });
          pushNotice('Модель выгружена из памяти', 'ok');
          await refreshState();
          if (currentView === 'lm') refreshLM();
        } catch (err) { alert(err.message); }
        finally { headUnload.disabled = false; headUnload.textContent = '⏏ Выгрузить'; }
      };
    }
  }
  const drawerUnload = $('#drawerUnloadBtn');
  const drawerModel = $('#currentLoadedModelName');
  if (drawerUnload && drawerModel) {
    if (hasLoaded) {
      const activeName = (st.lms_loaded && st.lms_loaded[0]) || st.model || 'модель';
      drawerModel.textContent = shortModel(activeName);
      drawerUnload.disabled = false;
      drawerUnload.onclick = async () => {
        drawerUnload.disabled = true;
        try {
          await api('/api/lms/unload', { model: '' });
          pushNotice('Модель выгружена из памяти', 'ok');
          await refreshState();
          if (currentView === 'lm') refreshLM();
        } catch (err) { alert(err.message); }
        finally { drawerUnload.disabled = false; }
      };
    } else {
      drawerModel.textContent = 'не загружена';
      drawerUnload.disabled = true;
    }
  }

  refreshReadiness();
}

function confirmDelete(title) {
  const dialog = $('#deleteDialog');
  $('#deleteQuestion').textContent = 'Удалить сессию «' + title + '» безвозвратно?';
  return new Promise((resolve) => {
    const finish = (confirmed) => {
      dialog.close();
      dialog.oncancel = null;
      dialog.querySelector('form').onsubmit = null;
      resolve(confirmed);
    };
    dialog.oncancel = (e) => { e.preventDefault(); finish(false); };
    dialog.querySelector('form').onsubmit = (e) => {
      e.preventDefault();
      finish(e.submitter.value === 'delete');
    };
    dialog.showModal();
  });
}

let wsFilterOnlyActive = localStorage.getItem('lh.wsFilterOnlyActive') === '1';
let wsCollapsedMap = {};
try { wsCollapsedMap = JSON.parse(localStorage.getItem('lh.wsCollapsed') || '{}') || {}; } catch (e) { wsCollapsedMap = {}; }

function _wsKey(rawPath) {
  const s = String(rawPath || '').trim();
  if (!s) return '__none__';
  return s.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
}

function _wsShortName(rawPath, customName) {
  if (customName && String(customName).trim()) return String(customName).trim();
  const s = String(rawPath || '').trim().replace(/[\\/]+$/, '');
  if (!s) return 'Без пространства';
  const parts = s.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || s;
}

function _buildSessionRowEl(row) {
  const el = node('div', 'session' + (row.id === sessionId ? ' active' : ''));
  el.dataset.id = row.id;
  const label = node('button', 'session-title', esc(row.title || row.id));
  label.title = (row.title || row.id) + (row.workspace ? `\n📁 ${row.workspace}` : '\n🌐 Без рабочего пространства');
  label.onclick = async () => {
    try {
      const targetWs = row.workspace || '';
      const curWs = (stateCache && stateCache.workspace) || '';
      if (_wsKey(targetWs) !== _wsKey(curWs)) {
        try {
          await api('/api/workspace/select', { path: targetWs });
        } catch (e) { /* если папка была перемещена — всё равно откроем историю чата */ }
      }
      await openSession(row.id);
      if (currentView !== 'chat') setView('chat');
      await refreshState();
    } catch (err) {
      pushNotice(err.message, 'bad');
    }
  };

  const moveWs = node('button', 'session-action', '📁');
  moveWs.title = row.workspace
    ? `Пространство: ${row.workspace} (нажми, чтобы сменить или отвязать)`
    : 'Привязать чат к рабочему пространству (по желанию)';
  moveWs.setAttribute('aria-label', moveWs.title);
  moveWs.style.fontSize = '12px';
  moveWs.onclick = (e) => {
    e.stopPropagation();
    openWorkspaceModal(row.id);
  };

  const rename = node('button', 'session-action', '✎');
  rename.title = 'Переименовать сессию';
  rename.setAttribute('aria-label', rename.title);
  rename.onclick = () => editSessionTitle(el, row);

  const download = node('button', 'session-action', '↓');
  download.title = 'Экспорт в Markdown';
  download.setAttribute('aria-label', download.title);
  download.onclick = async () => {
    download.disabled = true;
    try {
      const res = await fetch('/api/session/export?session=' + encodeURIComponent(row.id));
      if (!res.ok) throw new Error('Не удалось экспортировать сессию (' + res.status + ')');
      const url = URL.createObjectURL(await res.blob());
      const link = node('a');
      link.href = url;
      link.download = row.id + '.md';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { pushNotice(err.message, 'bad'); }
    finally { download.disabled = false; }
  };

  const del = node('button', 'session-action session-del', '&times;');
  del.title = 'Удалить сессию';
  del.setAttribute('aria-label', del.title);
  del.onclick = async () => {
    if (!await confirmDelete(row.title || row.id)) return;
    try {
      await api('/api/session/delete', { session: row.id });
      if (sessionId === row.id) {
        const res = await api('/api/new', { title: '' });
        await openSession(res.session);
      }
      await refreshState();
    } catch (err) { pushNotice(err.message, 'bad'); }
  };

  el.append(label, moveWs, rename, download, del);
  return el;
}

function renderSessions() {
  if ($('#sessions .session-edit')) return;
  const st = stateCache || {};
  const activeWs = st.workspace || '';
  const activeKey = _wsKey(activeWs);
  const filter = ($('#sessionSearch').value || '').trim().toLocaleLowerCase();

  const filterBtn = $('#wsFilterToggleBtn');
  if (filterBtn) {
    filterBtn.classList.toggle('only-active', wsFilterOnlyActive);
    filterBtn.textContent = wsFilterOnlyActive ? 'Текущее' : 'Все';
    filterBtn.title = wsFilterOnlyActive
      ? 'Показаны только чаты текущего рабочего пространства (нажми, чтобы показать все пространства)'
      : 'Показаны все рабочие пространства с группировкой чатов (нажми, чтобы оставить только текущее)';
  }

  const allSessions = (st.sessions || []).filter((row) => {
    if (wsFilterOnlyActive && _wsKey(row.workspace) !== activeKey) return false;
    if (!filter) return true;
    const hay = `${row.title || ''} ${row.id || ''} ${row.workspace || ''} ${row.workspace_name || ''}`.toLocaleLowerCase();
    return hay.includes(filter);
  });

  const box = $('#sessions');
  box.replaceChildren();

  // Собираем группы рабочих пространств
  const groupsMap = new Map();
  const ensureGroup = (rawPath, customName) => {
    const key = _wsKey(rawPath);
    if (!groupsMap.has(key)) {
      groupsMap.set(key, {
        key,
        path: key === '__none__' ? '' : String(rawPath || ''),
        name: key === '__none__' ? 'Без пространства' : _wsShortName(rawPath, customName),
        isNone: key === '__none__',
        rows: [],
        latestMtime: 0,
      });
    } else if (customName && key !== '__none__') {
      groupsMap.get(key).name = customName;
    }
    return groupsMap.get(key);
  };

  if (!wsFilterOnlyActive) {
    if (activeWs) ensureGroup(activeWs);
    (st.workspaces || []).forEach((w) => {
      if (w && w.path && (!filter || `${w.name || ''} ${w.path || ''}`.toLocaleLowerCase().includes(filter))) {
        ensureGroup(w.path, w.name);
      }
    });
    if (!filter) ensureGroup('', 'Без пространства');
  } else {
    ensureGroup(activeWs);
  }

  allSessions.forEach((row) => {
    const g = ensureGroup(row.workspace || '', row.workspace_name || '');
    g.rows.push(row);
    if ((row.mtime || 0) > g.latestMtime) g.latestMtime = row.mtime || 0;
  });

  const groups = Array.from(groupsMap.values()).sort((a, b) => {
    if ((a.key === activeKey) !== (b.key === activeKey)) return a.key === activeKey ? -1 : 1;
    if (a.isNone !== b.isNone) return a.isNone ? 1 : -1;
    return (b.latestMtime - a.latestMtime) || a.name.localeCompare(b.name, 'ru');
  });

  if (!groups.length || (filter && !allSessions.length && !groups.some((g) => g.rows.length))) {
    box.append(node('div', 'page-sub', filter ? 'Ничего не найдено' : 'Пока нет чатов'));
    return;
  }

  groups.forEach((g) => {
    const isCurrent = g.key === activeKey;
    const isCollapsed = !filter && !!wsCollapsedMap[g.key] && !g.rows.some((r) => r.id === sessionId);
    const wrap = node('div', 'ws-group');
    const head = node('div', 'ws-group-head' + (isCurrent ? ' current' : ''));
    const toggle = node('button', 'ws-group-toggle');
    toggle.type = 'button';
    toggle.title = g.isNone
      ? 'Общие чаты без привязки к папке проекта'
      : `Рабочее пространство: ${g.path}`;
    const ico = g.isNone ? '🌐' : '📁';
    toggle.innerHTML = `<span>${isCollapsed ? '▸' : '▾'}</span><span>${ico}</span>`
      + `<span class="ws-group-title">${esc(g.name)}</span>`
      + `<span class="ws-group-count">${g.rows.length}</span>`;

    const addBtn = node('button', 'ws-group-add', '+');
    addBtn.type = 'button';
    addBtn.title = g.isNone
      ? 'Новый чат без рабочего пространства'
      : `Новый чат в пространстве «${g.name}»`;
    addBtn.onclick = (e) => {
      e.stopPropagation();
      createNewChatInWorkspace(g.path);
    };

    const itemsBox = node('div', 'ws-group-items');
    itemsBox.hidden = isCollapsed;
    if (!g.rows.length) {
      itemsBox.appendChild(node('div', 'ws-group-empty', 'чатов пока нет — нажми +'));
    } else {
      g.rows.forEach((row) => itemsBox.appendChild(_buildSessionRowEl(row)));
    }

    toggle.onclick = () => {
      const nextHidden = !itemsBox.hidden;
      itemsBox.hidden = nextHidden;
      wsCollapsedMap[g.key] = nextHidden;
      localStorage.setItem('lh.wsCollapsed', JSON.stringify(wsCollapsedMap));
      const chev = toggle.querySelector('span');
      if (chev) chev.textContent = nextHidden ? '▸' : '▾';
    };

    head.append(toggle, addBtn);
    wrap.append(head, itemsBox);
    box.appendChild(wrap);
  });
}

function editSessionTitle(el, row) {
  if ($('#sessions .session-edit')) return;
  const input = node('input', 'session-edit');
  input.value = row.title || row.id;
  input.maxLength = 120;
  input.setAttribute('aria-label', 'Название сессии');
  const save = node('button', 'session-action', '✓');
  save.title = 'Сохранить название';
  save.setAttribute('aria-label', save.title);
  const cancel = node('button', 'session-action', '×');
  cancel.title = 'Отменить переименование';
  cancel.setAttribute('aria-label', cancel.title);
  const error = node('span', 'session-error');
  error.setAttribute('role', 'alert');
  cancel.onclick = () => { el.replaceChildren(); renderSessions(); };
  save.onclick = async () => {
    const title = input.value.trim();
    if (!title) { error.textContent = 'Введите название'; input.focus(); return; }
    save.disabled = cancel.disabled = true;
    try {
      await api('/api/session/rename', { session: row.id, title });
      row.title = title;
      if (sessionId === row.id) $('#sessionTitle').textContent = title;
      el.replaceChildren();
      renderSessions();
      await refreshState();
    } catch (err) { error.textContent = err.message; }
    finally { save.disabled = cancel.disabled = false; }
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); save.click(); }
    if (e.key === 'Escape') cancel.click();
  };
  el.replaceChildren(input, save, cancel, error);
  input.focus();
  input.select();
}

let readinessRequest = 0;
async function refreshReadiness() {
  const request = ++readinessRequest;
  try {
    const res = await api('/api/model/readiness');
    if (request !== readinessRequest) return;
    const warnings = res.warnings || [];
    $('#modelWarning').textContent = warnings.join(' ');
    $('#modelWarning').hidden = !warnings.length;
  } catch (err) {
    if (request !== readinessRequest) return;
    $('#modelWarning').textContent = 'Проверка модели недоступна: ' + err.message;
    $('#modelWarning').hidden = false;
  }
}

async function refreshDesktop() {
  try {
    const res = await api('/api/desktop');
    $('#autostart').checked = !!res.enabled;
    $('#autostart').disabled = !res.supported;
    $('#desktopStatus').textContent = res.tray ? 'Работает в трее' : 'Без трея';
  } catch (err) { $('#desktopStatus').textContent = err.message; }
}

async function waitServerRestart() {
  pushNotice('Сервер перезапускается…', 'warn');
  for (let i = 0; i < 50; i++) {
    await new Promise((r) => setTimeout(r, 300));
    try {
      const st = await api('/api/state');
      pushNotice(st.lan_access
        ? 'Сервер перезапущен. Доступ по Wi-Fi: ' + (st.lan_url || '—')
        : 'Сервер перезапущен. Доступ по Wi-Fi выключен.', 'ok');
      return st;
    } catch (err) { /* сервер ещё поднимается */ }
  }
  pushNotice('Сервер не ответил после перезапуска — проверь трей или консоль.', 'bad');
  return null;
}

async function patchConfig(key, value) {
  const res = await api('/api/config', { config: { [key]: value } });
  (res && res.warnings || []).forEach((w) => pushNotice(w, 'warn'));
  if (res && res.restart) await waitServerRestart();
  await refreshState();
}

/* ---------- plugins view ---------- */
function plugCard(info, actions) {
  const card = node('div', 'pcard' + (info.error ? ' err' : '') + (info.enabled === false ? ' disabled' : ''));
  const head = node('div', 'pcard-head');
  head.append(node('div', 'pcard-name', esc(info.name || info.file || '?')));
  if (info.version) head.append(node('span', 'pcard-file', esc(info.version)));
  if (info.badge) {
    const b = node('span', 'pbadge fit-' + (info.badgeColor || 'green'), esc(info.badge));
    if (info.badgeTitle) b.title = info.badgeTitle;
    head.append(b);
  }
  head.append(node('span', 'grow'));
  if (actions) head.append(...actions);
  card.append(head);
  if (info.desc) card.append(node('div', 'pcard-desc', esc(info.desc)));
  if (info.file) card.append(node('div', 'pcard-file', esc(info.file + (info.dir && !info.path ? '' : ''))));
  if (info.recommendation) card.append(node('div', 'pcard-rec', esc(info.recommendation)));
  if (info.tools && info.tools.length) {
    const row = node('div', 'pcard-tools');
    info.tools.forEach((t) => row.append(node('span', 'ptool', esc(typeof t === 'string' ? t : t.name))));
    card.append(row);
  }
  if (info.error) card.append(node('div', 'err-text', esc(info.error)));
  return card;
}

function switchEl(checked, onchange) {
  const lab = node('label', 'switch');
  const inp = node('input');
  inp.type = 'checkbox';
  inp.checked = !!checked;
  inp.onchange = onchange;
  lab.append(inp, node('span', 'knob'));
  return lab;
}

function delBtn(title, onclick) {
  const b = node('button', 'ghost', 'удалить');
  b.title = title;
  b.onclick = onclick;
  return b;
}

async function refreshPlugins() {
  try {
    const [st, cat, ws] = await Promise.all([
      api('/api/plugins'),
      api('/api/plugins/catalog'),
      api('/api/plugins/workshop').catch(() => null),
    ]);
    plugCache = { st, cat };
    if (ws) {
      workshopCache = ws;
      const cnt = $('#plugCountWorkshop');
      if (cnt) cnt.textContent = (ws.entries || []).length;
    }
    renderPlugins();
    renderWorkshop();
    renderVtQuick();
  } catch (err) {
    $('#plugInstalled').innerHTML = '';
    $('#plugInstalled').append(node('div', 'empty-note', 'не удалось получить список: ' + err.message));
  }
}

function renderPlugins() {
  if (!plugCache) return;
  const st = plugCache.st;
  const cat = plugCache.cat;
  $('#plugDir').textContent = 'папка: ' + (st.dir || '…') + ' · инструментов: ' + (st.tools || []).length;
  const total = (st.plugins || []).length;
  const cntInst = $('#plugCountInstalled');
  if (cntInst) cntInst.textContent = total;
  const cntBox = $('#plugInstalledCount');
  if (cntBox) cntBox.textContent = total ? total + ' · активно ' + (st.plugins || []).filter((p) => p.enabled !== false).length : '';
  const q = plugFilter.trim().toLowerCase();
  const inst = $('#plugInstalled');
  inst.innerHTML = '';
  const files = (st.plugins || []).filter((p) => !q || (p.name + ' ' + p.file + ' ' + (p.tools || []).join(' ')).toLowerCase().includes(q));
  if (!files.length) inst.append(node('div', 'empty-note', 'пока пусто — поставь что-нибудь из каталога'));
  files.forEach((p) => {
    const on = p.enabled !== false;
    const actions = [];
    if (p.error) actions.push(node('span', 'pcard-file', 'ошибка'));
    const pubBtn = node('button', 'ghost small-btn', 'В Мастерскую 🚀');
    pubBtn.title = 'Опубликовать плагин в Мастерскую сообщества LocalHarness';
    pubBtn.onclick = async () => {
      pubBtn.disabled = true;
      pubBtn.textContent = 'публикую…';
      try {
        const res = await api('/api/plugins/publish', { file: p.file });
        if (!res.ok) throw new Error(res.error || 'Ошибка публикации');
        pushNotice(`Плагин ${p.name} опубликован в Мастерскую сообщества!`, 'ok');
        await refreshWorkshop();
        await refreshPlugins();
      } catch (err) {
        alert('Ошибка публикации: ' + err.message);
      } finally {
        pubBtn.disabled = false;
        pubBtn.textContent = 'В Мастерскую 🚀';
      }
    };
    actions.push(pubBtn);

    const runBtn = node('button', 'primary small-btn', '⚡ Запуск / Настройки');
    runBtn.title = 'Интерактивный запуск инструментов и настройки плагина';
    runBtn.onclick = () => openPluginInteractiveModal(p);
    actions.push(runBtn);

    const codeQuickBtn = node('button', 'ghost small-btn', '💻 Код');
    codeQuickBtn.title = 'Быстрое редактирование кода плагина с горячей перезагрузкой';
    codeQuickBtn.onclick = () => editPluginInModal(p);
    actions.push(codeQuickBtn);

    const editBtn = node('button', 'ghost small-btn', '✏️ Конструктор');
    editBtn.title = 'Открыть и редактировать плагин в Конструкторе';
    editBtn.onclick = async () => {
      try {
        const res = await api('/api/plugins/code?file=' + encodeURIComponent(p.file));
        if (!res.ok) throw new Error(res.error || 'Ошибка чтения файла');
        if ($('#creatorName')) $('#creatorName').value = res.name || p.name;
        if ($('#creatorDesc')) $('#creatorDesc').value = res.desc || p.desc || '';
        if ($('#creatorCode')) $('#creatorCode').value = res.code || '';
        setPlugSubtab('creator');
        if (typeof updateTesterToolOptions === 'function') {
          updateTesterToolOptions(res.tools || p.tools || []);
        }
        pushNotice(`Плагин ${p.name} открыт в Конструкторе`, 'ok');
      } catch (err) {
        alert('Ошибка: ' + err.message);
      }
    };
    actions.push(editBtn);

    const dlBtn = node('a', 'ghost small-btn', '📥 Скачать');
    dlBtn.href = '/api/plugins/download?file=' + encodeURIComponent(p.file);
    dlBtn.download = p.file;
    dlBtn.title = 'Скачать .py файл плагина';
    actions.push(dlBtn);
    const tail = node('div', 'pcard-tail');
    tail.append(switchEl(on, async () => {
      try {
        await api('/api/plugins/toggle', { file: p.file, enabled: !on });
        refreshPlugins();
        refreshState();
      } catch (err) { alert(err.message); }
    }));
    tail.append(delBtn('удалить файл плагина', async () => {
      if (!confirm('Удалить ' + p.file + ' безвозвратно?')) return;
      try {
        await api('/api/plugins/remove', { file: p.file });
        refreshPlugins();
        refreshState();
      } catch (err) { alert(err.message); }
    }));
    actions.push(tail);
    inst.append(plugCard(p, actions));
  });
  const list = $('#plugCatalog');
  list.innerHTML = '';
  const entries = (cat.entries || []).filter((e) => !q || (e.name + ' ' + (e.desc || '')).toLowerCase().includes(q));
  if (!entries.length) list.append(node('div', 'empty-note', 'каталог пуст — добавь записи в plugins\\catalog.json'));
  entries.forEach((e) => {
    const installed = (st.plugins || []).some((p) => p.name === e.name || p.file === e.file);
    const actions = [];
    if (installed) actions.push(node('span', 'pcard-file', 'установлен'));
    else {
      const btn = node('button', 'primary', 'Установить');
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = 'качаю…';
        try {
          await api('/api/plugins/install', { id: e.id, url: e.url || '' });
          refreshPlugins();
          refreshState();
        } catch (err) { alert(err.message); btn.disabled = false; btn.textContent = 'Установить'; }
      };
      actions.push(btn);
    }
    list.append(plugCard({ name: e.name, desc: e.desc, tools: e.tools, version: e.version, file: e.url || e.file }, actions));
  });
}

/* ---------- plugin sub-views: installed / workshop / converter / creator ---------- */
let currentPlugSubtab = 'installed';
let workshopCache = null;
let workshopFilter = 'all';

function setPlugSubtab(tab) {
  currentPlugSubtab = tab;
  document.querySelectorAll('#plugNav .seg-btn').forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if ($('#plugTabInstalled')) $('#plugTabInstalled').hidden = tab !== 'installed';
  if ($('#plugTabWorkshop')) $('#plugTabWorkshop').hidden = tab !== 'workshop';
  if ($('#plugTabConverter')) $('#plugTabConverter').hidden = tab !== 'converter';
  if ($('#plugTabCreator')) $('#plugTabCreator').hidden = tab !== 'creator';
  if (tab === 'workshop' && !workshopCache) refreshWorkshop();
  if (tab === 'creator') populateTesterFromInstalled();
  if (tab === 'installed') renderPlugins();
}

function populateTesterFromInstalled() {
  const sel = $('#creatorTestTool');
  if (!sel || sel.options.length > 1) return;
  const names = [];
  (plugCache?.st?.plugins || []).forEach((p) => {
    if (!p.error && p.enabled !== false) names.push(...(p.tools || []));
  });
  if (names.length) updateTesterToolOptions(names);
}

$('#plugNav').addEventListener('click', (e) => {
  const btn = e.target.closest('.seg-btn');
  if (btn) setPlugSubtab(btn.dataset.tab);
});

if ($('#plugCreateTopBtn')) {
  $('#plugCreateTopBtn').onclick = () => setPlugSubtab('creator');
}

document.querySelectorAll('.wfilter-chip').forEach((chip) => {
  chip.onclick = () => {
    document.querySelectorAll('.wfilter-chip').forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');
    workshopFilter = chip.dataset.filter || 'all';
    renderWorkshop();
  };
});

async function refreshWorkshop() {
  const list = $('#plugWorkshopList');
  try {
    const data = await api('/api/plugins/workshop');
    workshopCache = data;
    const cnt = $('#plugCountWorkshop');
    if (cnt) cnt.textContent = (data.entries || []).length;
    renderWorkshop();
    renderVtQuick();
  } catch (err) {
    if (list) list.innerHTML = `<div class="empty-note">Не удалось загрузить Мастерскую: ${esc(err.message)}</div>`;
  }
}

function secChip(audit) {
  const clean = !audit || audit.clean !== false;
  const tone = clean ? 'ok' : (audit && audit.findings > 1 ? 'danger' : 'warn');
  const text = clean ? 'аудит: чисто' : 'замечаний: ' + (audit ? audit.findings : '?');
  return node('span', 'sec-chip ' + tone, text);
}

function reportRow(label, value, mono) {
  const rowLabel = node('div', 'report-label', label);
  const rowValue = node('div', mono === false ? 'report-value plain' : 'report-value', value || '—');
  return [rowLabel, rowValue];
}

let currentEditingPluginFile = '';

function openCodeDialog(title, meta, editableFile) {
  const dlg = $('#codeDialog');
  if (!dlg) return;
  currentEditingPluginFile = editableFile || '';
  $('#codeDialogTitle').textContent = title;
  $('#codeDialogMeta').textContent = meta || '';
  const pre = $('#codeDialogText');
  const ed = $('#codeDialogEditor');
  const saveBtn = $('#codeDialogSave');
  if (editableFile && ed) {
    pre.hidden = true;
    ed.hidden = false;
    ed.value = 'Загрузка кода…';
    if (saveBtn) saveBtn.hidden = false;
  } else {
    pre.hidden = false;
    pre.textContent = 'Загрузка кода…';
    if (ed) ed.hidden = true;
    if (saveBtn) saveBtn.hidden = true;
  }
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
}

async function editPluginInModal(p) {
  openCodeDialog(p.file, 'Горячее редактирование · AST-проверка', p.file);
  try {
    const res = await api('/api/plugins/code?file=' + encodeURIComponent(p.file));
    if ($('#codeDialogEditor')) $('#codeDialogEditor').value = res.ok ? (res.code || '') : ('# Ошибка: ' + res.error);
  } catch (err) {
    if ($('#codeDialogEditor')) $('#codeDialogEditor').value = '# Ошибка загрузки: ' + err.message;
  }
}

async function openPluginInteractiveModal(p) {
  const dlg = $('#plugToolDialog');
  const body = $('#plugToolDialogBody');
  if (!dlg || !body) return;
  $('#plugToolDialogTitle').textContent = '🧩 ' + (p.name || p.file);
  $('#plugToolDialogMeta').textContent = p.file + (p.version ? ' · v' + p.version : '');
  body.innerHTML = '<div class="page-sub">Загрузка параметров плагина…</div>';
  if (typeof dlg.showModal === 'function') dlg.showModal();

  let setRes = null;
  try {
    setRes = await api('/api/plugins/settings?file=' + encodeURIComponent(p.file));
  } catch (e) {}
  body.innerHTML = '';

  if (setRes && setRes.ok && Array.isArray(setRes.schema) && setRes.schema.length) {
    const sBox = node('div', 'cluster-box');
    sBox.append(node('div', 'plate-title', '⚙️ Настройки плагина'));
    const inputs = {};
    setRes.schema.forEach((field) => {
      const row = node('label', 'tcfg-item');
      row.style.marginTop = '6px';
      row.append(node('span', null, (field.label || field.key)));
      const inp = document.createElement('input');
      inp.className = 'field';
      inp.type = field.type === 'number' ? 'number' : 'text';
      inp.value = setRes.values && setRes.values[field.key] != null ? setRes.values[field.key] : (field.default != null ? field.default : '');
      inputs[field.key] = { el: inp, type: field.type };
      row.append(inp);
      sBox.append(row);
    });
    const saveSBtn = node('button', 'primary small-btn', '💾 Сохранить настройки');
    saveSBtn.style.marginTop = '8px';
    saveSBtn.onclick = async () => {
      const vals = {};
      Object.keys(inputs).forEach((k) => {
        const it = inputs[k];
        vals[k] = it.type === 'number' ? Number(it.el.value) : it.el.value;
      });
      const r = await api('/api/plugins/settings', { file: p.file, values: vals });
      pushNotice(r && r.ok ? 'Настройки плагина сохранены' : ((r && r.error) || 'Ошибка'), r && r.ok ? 'ok' : 'bad');
    };
    sBox.append(saveSBtn);
    body.append(sBox);
  }

  const specs = Array.isArray(p.tool_specs) && p.tool_specs.length
    ? p.tool_specs
    : (p.tools || []).map((t) => ({ name: t, desc: '', params: {}, required: [] }));
  if (!specs.length) {
    body.append(node('div', 'empty-note', 'У плагина нет активных инструментов (возможно он выключен).'));
    return;
  }
  specs.forEach((sp) => {
    const tBox = node('div', 'cluster-box');
    const head = node('div', 'plate-title-row');
    head.append(node('div', 'plate-title', '🛠️ ' + sp.name));
    tBox.append(head);
    if (sp.desc) tBox.append(node('div', 'page-sub', sp.desc));

    const paramEls = {};
    const pKeys = Object.keys(sp.params || {});
    pKeys.forEach((pk) => {
      const meta = sp.params[pk] || {};
      const req = (sp.required || []).includes(pk);
      const wrap = node('label', 'tcfg-item');
      wrap.style.marginTop = '6px';
      wrap.append(node('span', null, pk + (req ? ' *' : '') + ' — ' + (meta.desc || meta.type || '')));
      const inp = document.createElement('input');
      inp.className = 'field';
      inp.type = String(meta.type || '').startsWith('number') ? 'number' : 'text';
      inp.placeholder = meta.type || 'string';
      paramEls[pk] = { el: inp, type: String(meta.type || 'string') };
      wrap.append(inp);
      tBox.append(wrap);
    });

    const outPre = node('pre', 'code-modal-pre');
    outPre.style.maxHeight = '160px';
    outPre.style.marginTop = '8px';
    outPre.hidden = true;

    const runTBtn = node('button', 'primary small-btn', '▶ Выполнить ' + sp.name);
    runTBtn.style.marginTop = '8px';
    runTBtn.onclick = async () => {
      const args = {};
      Object.keys(paramEls).forEach((pk) => {
        const raw = paramEls[pk].el.value;
        if (raw === '') return;
        if (paramEls[pk].type.startsWith('number')) args[pk] = Number(raw);
        else if (paramEls[pk].type.startsWith('boolean')) args[pk] = raw === 'true' || raw === '1';
        else args[pk] = raw;
      });
      runTBtn.disabled = true;
      outPre.hidden = false;
      outPre.textContent = '⏳ Выполнение в изолированной песочнице…';
      try {
        const r = await api('/api/plugins/test_tool', { tool: sp.name, args });
        outPre.textContent = JSON.stringify(r, null, 2);
      } catch (err) {
        outPre.textContent = 'Ошибка: ' + err.message;
      } finally {
        runTBtn.disabled = false;
      }
    };
    tBox.append(runTBtn, outPre);
    body.append(tBox);
  });
}

function renderWorkshop() {
  const list = $('#plugWorkshopList');
  if (!list || !workshopCache) return;
  const entries = workshopCache.entries || [];
  const issues = entries.filter((e) => e.audit && !e.audit.clean).length;
  const stats = $('#workshopStats');
  if (stats) {
    stats.innerHTML = '';
    stats.append(
      node('span', 'stat-chip', entries.length + ' плагинов'),
      node('span', 'stat-chip', issues ? 'замечания: ' + issues : 'аудит: чисто'),
      node('span', 'stat-chip', workshopCache.vt_api_enabled ? 'VirusTotal: вкл' : 'VirusTotal: ключ не задан'),
    );
  }

  list.innerHTML = '';
  const q = plugFilter.trim().toLowerCase();
  const filtered = entries.filter((e) => {
    if (q && !(e.name + ' ' + (e.desc || '') + ' ' + (e.tags || []).join(' ')).toLowerCase().includes(q)) {
      return false;
    }
    if (workshopFilter === 'lh') {
      return !!e.made_in_localharness || (e.tags || []).includes('localharness') || (e.author || '').toLowerCase().includes('localharness');
    }
    if (workshopFilter === 'utils') {
      return (e.tags || []).some((t) => ['система', 'мониторинг', 'железо', 'утилиты'].includes(t));
    }
    if (workshopFilter === 'crypto') {
      return (e.tags || []).some((t) => ['финансы', 'крипта', 'рынок'].includes(t));
    }
    if (workshopFilter === 'msg') {
      return (e.tags || []).some((t) => ['мессенджер', 'уведомления', 'алерты'].includes(t));
    }
    return true;
  });
  if (!filtered.length) {
    list.append(node('div', 'empty-note', q ? 'В Мастерской ничего не найдено' : 'В этой категории пока нет плагинов'));
    renderVtQuick();
    return;
  }
  const installedNames = new Set((plugCache?.st?.plugins || []).map((p) => p.name));

  filtered.forEach((e) => {
    const card = node('div', 'wcard');
    const isInstalled = installedNames.has(e.name) || e.installed;

    const head = node('div', 'wcard-head');
    const name = node('span', 'wcard-name', e.name);
    head.append(name);
    if (e.version) head.append(node('span', 'wcard-ver', 'v' + e.version));
    if (e.made_in_localharness || (e.tags || []).includes('localharness')) {
      head.append(node('span', 'lh-badge', '✨ LocalHarness'));
    }
    head.append(secChip(e.audit));
    card.append(head);

    const meta = [];
    if (e.author) meta.push(e.author);
    if (e.tools && e.tools.length) meta.push(e.tools.length + ' инстр.');
    if (meta.length) card.append(node('div', 'wcard-meta', meta.join(' · ')));
    if (e.desc) card.append(node('div', 'wcard-desc', e.desc));

    if (e.tools && e.tools.length) {
      const toolsBox = node('div', 'wcard-tools');
      e.tools.forEach((t) => toolsBox.append(node('span', 'ptool', t)));
      card.append(toolsBox);
    }
    if (e.tags && e.tags.length) {
      const tagsBox = node('div', 'wcard-tags');
      e.tags.forEach((t) => tagsBox.append(node('span', 'wcard-tag', '#' + t)));
      card.append(tagsBox);
    }

    const foot = node('div', 'wcard-foot');
    const hash = node('span', 'wcard-hash', e.sha256 ? 'sha256 ' + e.sha256.slice(0, 10) : '');
    if (e.sha256) hash.title = e.sha256;
    foot.append(hash);

    const actions = node('div', 'wcard-actions');
    const codeBtn = node('button', 'ghost small-btn', 'Код');
    codeBtn.title = 'Посмотреть исходный код';
    codeBtn.onclick = () => previewWorkshopCode(e);
    actions.append(codeBtn);

    const dlBtn = node('a', 'ghost small-btn', '📥 .py');
    dlBtn.href = '/api/plugins/download?file=' + encodeURIComponent(e.file || (e.name + '.py'));
    dlBtn.download = e.file || (e.name + '.py');
    dlBtn.title = 'Скачать файл плагина';
    actions.append(dlBtn);

    if (e.made_in_localharness) {
      const unpubBtn = node('button', 'ghost small-btn', 'Снять');
      unpubBtn.title = 'Отозвать плагин из Мастерской';
      unpubBtn.onclick = async () => {
        if (!confirm(`Отозвать ${e.name} из Мастерской сообщества?`)) return;
        try {
          await api('/api/plugins/unpublish', { id: e.id || e.name });
          pushNotice(`Плагин ${e.name} снят с публикации`, 'ok');
          await refreshWorkshop();
        } catch (err) { alert(err.message); }
      };
      actions.append(unpubBtn);
    }

    if (isInstalled) {
      actions.append(node('span', 'sec-chip ok', 'установлен'));
    } else {
      const btn = node('button', 'primary small-btn', 'Установить');
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = 'ставлю…';
        try {
          const res = await api('/api/plugins/workshop/install', { id: e.id });
          if (!res.ok) throw new Error(res.error || 'Ошибка установки');
          pushNotice('Плагин ' + e.name + ' установлен и прошёл аудит', 'ok');
          await refreshPlugins();
          await refreshWorkshop();
          await refreshState();
        } catch (err) {
          alert('Ошибка установки: ' + err.message);
          btn.disabled = false;
          btn.textContent = 'Установить';
        }
      };
      actions.append(btn);
    }
    foot.append(actions);
    card.append(foot);
    list.append(card);
  });
  renderVtQuick();
}

async function previewWorkshopCode(e) {
  openCodeDialog(e.name + '.py', 'Мастерская · ' + (e.author || 'сообщество'));
  try {
    const res = await api(`/api/plugins/workshop/code?id=${encodeURIComponent(e.id)}`);
    $('#codeDialogText').textContent = res.ok ? res.code : 'Ошибка: ' + res.error;
  } catch (err) {
    $('#codeDialogText').textContent = 'Ошибка загрузки кода: ' + err.message;
  }
}

function renderVtQuick() {
  const wrap = $('#vtQuickPick');
  if (!wrap) return;
  wrap.innerHTML = '';
  const names = new Set();
  (plugCache?.st?.plugins || []).forEach((p) => { if (p.file) names.add(p.file); });
  (workshopCache?.entries || []).forEach((e) => { if (e.file) names.add(e.file); });
  Array.from(names).slice(0, 10).forEach((fname) => {
    const chip = node('button', 'preset-chip', fname);
    chip.type = 'button';
    chip.onclick = () => {
      $('#vtCheckInput').value = fname;
      runVtCheck();
    };
    wrap.append(chip);
  });
}

async function runVtCheck() {
  const input = $('#vtCheckInput').value.trim();
  if (!input) return;
  const box = $('#vtCheckResult');
  box.hidden = false;
  box.innerHTML = '';
  box.append(node('div', 'report-wait', 'Считаю SHA-256 и сверяю с базой…'));
  try {
    const res = await api('/api/plugins/virustotal', { file: input, code: input });
    if (res.ok === false) throw new Error(res.error || 'Ошибка проверки');
    const vt = res.vt || {};
    const queried = !!vt.queried_api;
    const clean = !!res.clean;
    const tone = clean ? 'ok' : (res.threat_level === 'malicious' ? 'danger' : 'warn');

    box.innerHTML = '';
    const head = node('div', 'report-head');
    head.append(node('div', 'report-title', 'Результат проверки'));
    head.append(node('span', 'sec-chip ' + tone,
      clean ? (queried ? 'VirusTotal: чисто' : 'аудит кода: чисто') : 'есть замечания'));
    const copy = node('button', 'ghost small-btn', 'Копировать SHA');
    copy.onclick = () => {
      if (!res.sha256) return;
      navigator.clipboard?.writeText(res.sha256).then(
        () => pushNotice('SHA-256 скопирован', 'ok'),
        () => {},
      );
    };
    head.append(copy);
    box.append(head);

    const grid = node('div', 'report-grid');
    grid.append(...reportRow('Файл', res.file || input));
    grid.append(...reportRow('SHA-256', res.sha256));
    grid.append(...reportRow('Детекты', queried ? (res.score || '—') : '—'));
    grid.append(...reportRow('Режим', queried ? 'VirusTotal API' : 'локальный AST-аудит', false));
    box.append(grid);

    if (res.findings && res.findings.length) {
      const list = node('ul', 'report-find');
      res.findings.forEach((f) => list.append(node('li', '', (f.line ? 'стр. ' + f.line + ': ' : '') + (f.msg || f.type))));
      box.append(list);
    } else {
      box.append(node('div', 'report-note', 'Деструктивных команд, eval/exec и обфускации не найдено.'));
    }
    if (!queried) {
      box.append(node('div', 'report-note', 'Ключ VirusTotal не задан — онлайн-проверка по антивирусам не выполнялась. Добавить ключ: Настройки → Проверка плагинов.'));
    } else if (vt.permalink) {
      const link = node('a', 'report-note', 'Открыть отчёт на virustotal.com →');
      link.href = vt.permalink;
      link.target = '_blank';
      link.rel = 'noopener';
      box.append(link);
    }
  } catch (err) {
    box.innerHTML = '';
    box.append(node('div', 'report-note', 'Ошибка проверки: ' + err.message));
  }
}

if ($('#vtCheckBtn')) {
  $('#vtCheckBtn').onclick = runVtCheck;
}

if ($('#repoConvertBtn')) {
  $('#repoConvertBtn').onclick = async () => {
    const url = $('#repoUrlInput').value.trim();
    const name = $('#repoNameInput').value.trim();
    if (!url) {
      alert('Укажи ссылку на GitHub-репозиторий, например https://github.com/owner/repo');
      return;
    }
    const btn = $('#repoConvertBtn');
    const progress = $('#repoConvertProgress');
    const box = $('#repoConvertResult');
    btn.disabled = true;
    btn.textContent = 'Собираю…';
    box.hidden = true;
    progress.hidden = false;
    try {
      const res = await api('/api/plugins/convert_repo', { url, name });
      if (!res.ok) throw new Error(res.error || 'Ошибка конвертации');
      const sec = res.security || {};
      const queried = !!(sec.vt && sec.vt.queried_api);
      const clean = sec.clean !== false;

      box.hidden = false;
      box.innerHTML = '';
      const head = node('div', 'conv-result-head');
      head.append(node('span', 'conv-repo', res.repo));
      if (res.stars !== undefined) head.append(node('span', 'wcard-ver', '★ ' + res.stars));
      head.append(node('span', 'sec-chip ' + (clean ? 'ok' : 'warn'),
        clean ? (queried ? 'VirusTotal: чисто' : 'аудит кода: чисто') : 'есть замечания'));
      box.append(head);

      const grid = node('div', 'report-grid');
      grid.append(...reportRow('Файл', res.file));
      grid.append(...reportRow('Режим', res.mode === 'cli' ? 'репозиторий + CLI' : 'только чтение репозитория', false));
      if (res.language) grid.append(...reportRow('Язык', res.language, false));
      if (res.package) grid.append(...reportRow('Пакет', res.package));
      if ((res.cli || []).length) grid.append(...reportRow('CLI', res.cli.join(', '), false));
      if (res.files_count !== undefined) grid.append(...reportRow('Файлов в репо', String(res.files_count), false));
      grid.append(...reportRow('Инструменты', String((res.tools || []).length) + ' · ' + ((res.tools || []).join(', ') || '—'), false));
      grid.append(...reportRow('Установка', res.installed ? 'включён в plugins' : 'не установлен', false));
      box.append(grid);
      if (res.description) box.append(node('div', 'report-note', res.description));
      if (res.install_hint) box.append(node('div', 'report-note', 'Чтобы включить CLI: ' + res.install_hint));
      if (!res.installed) {
        box.append(node('div', 'report-note', res.install_error
          ? 'Плагин собран, но не установлен: ' + res.install_error
          : 'Плагин собран, но не активирован: установку остановила проверка безопасности. Замечания ниже.'));
      }
      if ((sec.findings || []).length) {
        const list = node('ul', 'report-find');
        sec.findings.forEach((f) => list.append(node('li', '', f.msg || f.type)));
        box.append(list);
      }

      const fold = document.createElement('details');
      fold.className = 'code-fold';
      const summary = document.createElement('summary');
      summary.textContent = 'Исходный код плагина';
      const pre = document.createElement('pre');
      pre.textContent = res.code || '';
      fold.append(summary, pre);
      box.append(fold);

      pushNotice('Репозиторий ' + res.name + ' собран в плагин', 'ok');
      await refreshPlugins();
      await refreshWorkshop();
      await refreshState();
    } catch (err) {
      box.hidden = false;
      box.innerHTML = '';
      box.append(node('div', 'report-note', 'Ошибка конвертации: ' + err.message));
    } finally {
      progress.hidden = true;
      btn.disabled = false;
      btn.textContent = 'Собрать плагин';
    }
  };
}

if ($('#codeDialogClose')) {
  $('#codeDialogClose').onclick = () => {
    const dlg = $('#codeDialog');
    if (dlg) dlg.close();
  };
}
if ($('#codeDialog')) {
  $('#codeDialog').addEventListener('click', (e) => {
    if (e.target === $('#codeDialog')) $('#codeDialog').close();
  });
}
if ($('#codeDialogCopy')) {
  $('#codeDialogCopy').onclick = async () => {
    const ed = $('#codeDialogEditor');
    const text = (ed && !ed.hidden ? ed.value : $('#codeDialogText')?.textContent) || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      pushNotice('Код скопирован в буфер обмена', 'ok');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      pushNotice('Код скопирован в буфер обмена', 'ok');
    }
  };
}
if ($('#codeDialogSave')) {
  $('#codeDialogSave').onclick = async () => {
    const ed = $('#codeDialogEditor');
    if (!currentEditingPluginFile || !ed) return;
    const btn = $('#codeDialogSave');
    btn.disabled = true;
    try {
      const res = await api('/api/plugins/save_code', { file: currentEditingPluginFile, code: ed.value });
      if (!res.ok) throw new Error(res.error || 'Ошибка сохранения');
      pushNotice('✅ Плагин ' + res.file + ' сохранён и перезагружен (' + (res.tools || []).length + ' инстр.)', 'ok');
      $('#codeDialog').close();
      await refreshPlugins();
      await refreshState();
    } catch (err) {
      alert('Ошибка сохранения: ' + err.message);
    } finally {
      btn.disabled = false;
    }
  };
}
if ($('#plugToolDialogClose')) {
  $('#plugToolDialogClose').onclick = () => $('#plugToolDialog')?.close();
}
if ($('#plugToolDialog')) {
  $('#plugToolDialog').addEventListener('click', (e) => {
    if (e.target === $('#plugToolDialog')) $('#plugToolDialog').close();
  });
}

document.querySelectorAll('.preset-chip').forEach((chip) => {
  chip.onclick = () => {
    const repo = chip.dataset.repo || '';
    const name = chip.dataset.name || '';
    const inputUrl = $('#repoUrlInput');
    const inputName = $('#repoNameInput');
    if (inputUrl) inputUrl.value = repo.startsWith('http') ? repo : `https://github.com/${repo}`;
    if (inputName) inputName.value = name;
  };
});

if ($('#vtApiKeySave')) {
  $('#vtApiKeySave').onclick = async () => {
    const btn = $('#vtApiKeySave');
    const field = $('#vtApiKey');
    const key = (field?.value || '').trim();
    btn.disabled = true;
    try {
      await patchConfig('virustotal_api_key', key);
      const on = !!key;
      const keyChip = $('#vtKeyChip');
      if (keyChip) {
        keyChip.className = 'sec-chip ' + (on ? 'ok' : '');
        keyChip.textContent = on ? 'ключ задан' : 'ключ не задан';
      }
      const modeChip = $('#vtModeChip');
      if (modeChip) {
        modeChip.className = 'sec-chip ' + (on ? 'ok' : '');
        modeChip.textContent = on ? 'VirusTotal API' : 'локальный аудит';
      }
      if (field) {
        field.value = '';
        field.placeholder = on ? 'ключ сохранён — введи новый, чтобы заменить' : 'VirusTotal API key';
      }
      pushNotice(on ? 'Ключ VirusTotal сохранён' : 'Ключ VirusTotal удалён', 'ok');
      btn.textContent = 'Сохранено';
      setTimeout(() => { btn.textContent = 'Сохранить'; }, 1500);
    } catch (err) {
      alert('Ошибка сохранения ключа: ' + err.message);
    } finally {
      btn.disabled = false;
    }
  };
}

$('#vtApiKey')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#vtApiKeySave')?.click(); }
});
$('#vtCheckInput')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#vtCheckBtn')?.click(); }
});
$('#repoUrlInput')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#repoConvertBtn')?.click(); }
});
$('#repoNameInput')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#repoConvertBtn')?.click(); }
});

/* ---------- Plugin Studio / Creator ---------- */
const CREATOR_TEMPLATES = {
  tool: {
    name: 'my_custom_tool',
    desc: 'Пользовательский инструмент для агента.',
    code: `"""Плагин с пользовательскими инструментами для агента."""
NAME = "my_custom_tool"

def calc_sum(cfg, args):
    """Складывает два числа a и b."""
    a = float(args.get("a", 0))
    b = float(args.get("b", 0))
    return {"ok": True, "result": a + b}

TOOLS = [
    {
        "name": "calc_sum",
        "desc": "Складывает два числа a и b.",
        "params": {
            "a": ("number", "Первое число"),
            "b": ("number", "Второе число")
        },
        "required": ["a", "b"],
        "fn": calc_sum
    }
]
`
  },
  http: {
    name: 'api_fetcher',
    desc: 'Выполняет HTTP GET запрос к внешнему JSON API.',
    code: `"""Плагин для обращения к внешним API."""
import urllib.request
import json

NAME = "api_fetcher"

def fetch_json(cfg, args):
    """Делает GET-запрос к API и возвращает JSON ответ."""
    url = args.get("url")
    req = urllib.request.Request(url, headers={"User-Agent": "LocalHarness/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {"ok": True, "data": data}

TOOLS = [
    {
        "name": "fetch_json",
        "desc": "Выполняет HTTP GET запрос к публичному JSON API.",
        "params": {"url": ("string", "URL адрес эндпоинта")},
        "required": ["url"],
        "fn": fetch_json
    }
]
`
  },
  sys: {
    name: 'system_diagnostic',
    desc: 'Диагностика системного окружения и параметров среды.',
    code: `"""Плагин системной утилиты."""
import platform
import os

NAME = "system_diagnostic"

def check_env(cfg, args):
    """Возвращает информацию об окружении и переменных среды."""
    target_var = args.get("var_name")
    return {
        "ok": True,
        "os": platform.platform(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "env_value": os.environ.get(target_var, "") if target_var else None
    }

TOOLS = [
    {
        "name": "check_env",
        "desc": "Диагностика системного окружения и переменных.",
        "params": {"var_name": ("string?", "Имя переменной окружения")},
        "required": [],
        "fn": check_env
    }
]
`
  },
  ai: {
    name: 'text_cleaner',
    desc: 'Очищает текст от HTML разметки и нормализует пробелы.',
    code: `"""Плагин для обработки текста и форматирования данных."""
import re

NAME = "text_cleaner"

def clean_text(cfg, args):
    """Очищает текст от HTML-тегов и переносов."""
    text = args.get("text", "")
    clean = re.sub(r"<[^>]+>", "", text)
    clean = re.sub(r"\\s+", " ", clean).strip()
    return {"ok": True, "cleaned": clean, "length": len(clean)}

TOOLS = [
    {
        "name": "clean_text",
        "desc": "Очищает текст от HTML разметки и нормализует пробелы.",
        "params": {"text": ("string", "Исходный текст")},
        "required": ["text"],
        "fn": clean_text
    }
]
`
  }
};

function applyCreatorTemplate(key) {
  const tpl = CREATOR_TEMPLATES[key] || CREATOR_TEMPLATES.tool;
  if ($('#creatorName')) $('#creatorName').value = tpl.name;
  if ($('#creatorDesc')) $('#creatorDesc').value = tpl.desc;
  if ($('#creatorCode')) $('#creatorCode').value = tpl.code;
}

if ($('#creatorCode') && !$('#creatorCode').value.trim()) {
  applyCreatorTemplate('tool');
}

document.querySelectorAll('.template-chip').forEach((chip) => {
  chip.onclick = () => applyCreatorTemplate(chip.dataset.tpl);
});

const creatorCodeArea = $('#creatorCode');
if (creatorCodeArea) {
  creatorCodeArea.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const start = creatorCodeArea.selectionStart;
      const end = creatorCodeArea.selectionEnd;
      creatorCodeArea.value = creatorCodeArea.value.substring(0, start) + '    ' + creatorCodeArea.value.substring(end);
      creatorCodeArea.selectionStart = creatorCodeArea.selectionEnd = start + 4;
    }
  });
}

if ($('#creatorScanBtn')) {
  $('#creatorScanBtn').onclick = async () => {
    const code = ($('#creatorCode')?.value || '').trim();
    if (!code) {
      alert('Сначала введите или выберите код плагина');
      return;
    }
    const box = $('#creatorResultBox');
    box.hidden = false;
    box.className = 'report report-wait';
    box.textContent = 'Анализ безопасности кода и аудит AST…';
    try {
      const res = await api('/api/plugins/virustotal', { code });
      box.className = 'report';
      const queried = !!(res.vt && res.vt.queried_api);
      const clean = !!res.clean;
      const chipClass = clean ? 'ok' : (res.threat_level === 'malicious' ? 'danger' : 'warn');
      const chipText = clean ? (queried ? 'VirusTotal: чисто' : 'аудит кода: чисто') : 'есть замечания';
      box.innerHTML = `
        <div class="report-head">
          <span class="report-title">Результат аудита безопасности</span>
          <span class="sec-chip ${chipClass}">${chipText}</span>
        </div>
        <div class="report-grid">
          <div class="report-label">Хеш SHA-256:</div>
          <div class="report-value">${esc(res.sha256 || '—')}</div>
          <div class="report-label">Детекты:</div>
          <div class="report-value">${esc(queried ? (res.score || '—') : '—')}</div>
          <div class="report-label">Режим:</div>
          <div class="report-value plain">${queried ? 'VirusTotal API' : 'локальный AST-аудит'}</div>
        </div>
        ${res.findings && res.findings.length
          ? `<div style="color:var(--fail); margin-top:8px;">Замечания: ${esc(res.findings.map(f => f.msg || f.type).join('; '))}</div>`
          : '<div style="color:var(--ok); margin-top:8px;">✓ Деструктивные команды, eval/exec и обфускация не обнаружены.</div>'}
        ${queried ? '' : '<div class="report-note">Ключ VirusTotal не задан — проверка выполнена локально по AST, файлы никуда не отправлялись.</div>'}
      `;
    } catch (err) {
      box.className = 'report';
      box.innerHTML = `<span style="color:var(--fail);">Ошибка проверки: ${esc(err.message)}</span>`;
    }
  };
}

if ($('#creatorSaveBtn')) {
  $('#creatorSaveBtn').onclick = async () => {
    const name = ($('#creatorName')?.value || '').trim();
    const desc = ($('#creatorDesc')?.value || '').trim();
    const author = ($('#creatorAuthor')?.value || '').trim();
    const tags = ($('#creatorTags')?.value || '').split(',').map((t) => t.trim()).filter(Boolean);
    const code = ($('#creatorCode')?.value || '').trim();
    const publish = !!$('#creatorPublish')?.checked;

    if (!name) {
      alert('Укажите имя плагина (латиница, цифры и _)');
      $('#creatorName')?.focus();
      return;
    }
    if (!code) {
      alert('Код плагина не может быть пустым');
      $('#creatorCode')?.focus();
      return;
    }

    const btn = $('#creatorSaveBtn');
    const box = $('#creatorResultBox');
    btn.disabled = true;
    btn.textContent = 'Сохранение…';
    box.hidden = false;
    box.className = 'report report-wait';
    box.textContent = 'Проверка безопасности и установка плагина в систему…';

    try {
      const res = await api('/api/plugins/create', { name, desc, author, code, publish, tags });
      if (!res.ok) throw new Error(res.error || 'Ошибка создания плагина');
      box.className = 'report';
      const pubNote = res.published
        ? '✨ Плагин также опубликован в Мастерскую сообщества!'
        : (res.publish_error ? '⚠️ Плагин установлен, но опубликовать не удалось: ' + res.publish_error : '');
      box.innerHTML = `
        <div class="report-head">
          <span class="report-title">✓ Плагин <code>${esc(res.name)}</code> успешно создан и активирован!</span>
          <span class="sec-chip ok">активен</span>
        </div>
        <div class="report-grid">
          <div class="report-label">Файл:</div>
          <div class="report-value">${esc(res.file)}</div>
          <div class="report-label">Инструменты:</div>
          <div class="report-value">${(res.tools || []).length ? esc(res.tools.join(', ')) : 'нет'}</div>
          <div class="report-label">SHA-256:</div>
          <div class="report-value">${esc(res.sha256 || '—')}</div>
        </div>
        ${pubNote ? `<div style="color:${res.published ? 'var(--accent)' : 'var(--warn, #d9a441)'}; font-weight:600; margin-top:8px;">${esc(pubNote)}</div>` : ''}
        ${res.name_warning ? `<div class="report-note">${esc(res.name_warning)}</div>` : ''}
      `;
      if (res.tools && res.tools.length) updateTesterToolOptions(res.tools);
      pushNotice(`Плагин ${res.name} создан${res.published ? ' и опубликован в Мастерскую' : ''}!`, 'ok');
      await refreshPlugins();
      await refreshWorkshop();
      await refreshState();
    } catch (err) {
      box.className = 'report';
      box.innerHTML = `<span style="color:var(--fail);">Ошибка создания: ${esc(err.message)}</span>`;
    } finally {
      btn.disabled = false;
      btn.textContent = '💾 Сохранить и активировать';
    }
  };
}

function updateTesterToolOptions(tools) {
  const sel = $('#creatorTestTool');
  if (!sel) return;
  sel.innerHTML = '';
  if (!tools || !tools.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'инструменты не найдены';
    sel.appendChild(opt);
    return;
  }
  tools.forEach((t) => {
    const opt = document.createElement('option');
    opt.value = t;
    opt.textContent = t;
    sel.appendChild(opt);
  });
}

// AI Plugin Code Generator
if ($('#creatorAiBtn')) {
  $('#creatorAiBtn').onclick = async () => {
    const prompt = ($('#creatorAiPrompt')?.value || '').trim();
    if (!prompt) {
      alert('Опишите задачу для создания плагина');
      $('#creatorAiPrompt')?.focus();
      return;
    }
    const btn = $('#creatorAiBtn');
    btn.disabled = true;
    btn.textContent = 'Генерирую с ИИ…';
    try {
      const res = await api('/api/plugins/generate_ai', { prompt });
      if (!res.ok) throw new Error(res.error || 'Ошибка генерации');
      if ($('#creatorName')) $('#creatorName').value = res.name;
      if ($('#creatorDesc')) $('#creatorDesc').value = res.desc || prompt;
      if ($('#creatorCode')) $('#creatorCode').value = res.code;
      updateTesterToolOptions(res.tools || []);
      const box = $('#creatorResultBox');
      if (!res.generated_by_ai && box) {
        box.hidden = false;
        box.className = 'report';
        box.innerHTML = `<div class="report-head"><span class="report-title">Собран шаблон</span><span class="sec-chip warn">ИИ недоступен</span></div>
          <div class="report-note">${esc(res.warning || 'Локальная модель не ответила — отредактируй код под задачу или повтори позже.')}</div>`;
        pushNotice('Модель недоступна — собран шаблон плагина', 'warn');
      } else {
        pushNotice(`Плагин ${res.name} сгенерирован ИИ — проверь код перед сохранением`, 'ok');
      }
    } catch (err) {
      alert('Ошибка генерации: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Сгенерировать с ИИ';
    }
  };
  $('#creatorAiPrompt')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('#creatorAiBtn')?.click(); }
  });
}

// Interactive Tool Sandbox Runner
if ($('#creatorTestBtn')) {
  $('#creatorTestBtn').onclick = async () => {
    const tool = $('#creatorTestTool')?.value;
    const rawArgs = ($('#creatorTestArgs')?.value || '').trim() || '{}';
    const resPre = $('#creatorTestResult');
    if (!tool) {
      alert('Сначала выберите инструмент для тестирования (сохраните плагин, чтобы он зарегистрировался)');
      return;
    }
    resPre.hidden = false;
    resPre.textContent = `⏳ Вызываю инструмент ${tool} в изолированном процессе…`;
    try {
      const res = await api('/api/plugins/test_tool', { tool, args: rawArgs });
      if (!res.ok) throw new Error(res.error || 'Ошибка вызова');
      const parts = [`⏱️ Изолированный процесс · ${res.duration_ms} мс`];
      if (res.output) parts.push('Вывод плагина:\n' + res.output);
      parts.push('Результат:\n' + JSON.stringify(res.result, null, 2));
      resPre.textContent = parts.join('\n\n');
    } catch (err) {
      resPre.textContent = `❌ Ошибка тестирования:\n${err.message}`;
    }
  };
}

/* ---------- LM Studio view: two plates ---------- */
function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  const gb = bytes / 1073741824;
  if (gb >= 0.5) return gb.toFixed(1) + ' ГБ';
  return Math.round(bytes / 1048576) + ' МБ';
}

let lmsStateCache = null;

function renderLmsRuntimes(rtInfo) {
  const box = $('#lmRuntimesList');
  const chip = $('#lmRuntimeActiveChip');
  if (!box) return;
  const info = rtInfo || {};
  const runtimes = info.runtimes || [];
  if (chip) {
    if (info.vulkan_active) {
      chip.className = 'sec-chip ok';
      chip.textContent = '🌋 Активен: Vulkan' + (info.active_version ? ' v' + info.active_version : '');
    } else if (info.active_name) {
      chip.className = 'sec-chip ok';
      chip.textContent = 'Активен: ' + info.active_name.replace('llama.cpp-win-x86_64-', '') + (info.active_version ? ' v' + info.active_version : '');
    } else {
      chip.className = 'sec-chip';
      chip.textContent = 'автовыбор LM Studio';
    }
  }
  box.innerHTML = '';
  if (!runtimes.length) {
    box.appendChild(node('div', 'empty-note', 'Движки LM Studio не обнаружены — нажмите «Скачать / Обновить Vulkan».'));
    return;
  }
  const kindIcon = { vulkan: '🌋', cuda: '🟢', rocm: '🔴', metal: '🍎', cpu: '💻' };
  runtimes.forEach((rt) => {
    const card = node('div', 'lms-rt-card' + (rt.selected ? ' active' : '') + (rt.kind === 'vulkan' ? ' vulkan-card' : ''));
    const head = node('div', 'lms-rt-head');
    head.appendChild(node('span', 'lms-rt-title', (kindIcon[rt.kind] || '⚙️') + ' ' + esc(rt.label || rt.name)));
    if (rt.version) head.appendChild(node('span', 'lms-rt-ver', 'v' + esc(rt.version)));
    else if (!rt.installed) head.appendChild(node('span', 'lms-rt-ver', 'не скачан'));
    card.appendChild(head);

    let metaTxt = '';
    let metaBad = false;
    if (!rt.installed) {
      metaTxt = 'Универсальный движок Vulkan для NVIDIA / AMD / Intel GPU. Устанавливается в 1 клик.';
    } else if (rt.compatible) {
      const gpuPart = (rt.gpus && rt.gpus.length) ? rt.gpus[0].replace('NVIDIA GeForce ', '') : (rt.kind === 'cpu' ? 'Процессор AVX2' : 'GPU совместим');
      const vramPart = rt.vram_gb > 0 ? ' · ' + rt.vram_gb + ' ГБ VRAM' : '';
      metaTxt = '✅ Совместим · ' + gpuPart + vramPart;
    } else {
      metaBad = true;
      metaTxt = '⚠️ Ошибка опроса GPU (' + (rt.status || 'несовместим') + ') — рекомендуется переключиться на Vulkan!';
    }
    card.appendChild(node('div', 'lms-rt-meta' + (metaBad ? ' bad' : ''), esc(metaTxt)));

    const acts = node('div', 'lms-rt-actions');
    if (!rt.installed) {
      const dlBtn = node('button', 'primary small-btn', '⬇️ Скачать Vulkan');
      dlBtn.type = 'button';
      dlBtn.onclick = () => installLmsRuntime(rt.install_query || rt.name, dlBtn);
      acts.appendChild(dlBtn);
    } else if (rt.selected) {
      const curBtn = node('button', 'ghost small-btn', '✓ Активен');
      curBtn.type = 'button';
      curBtn.disabled = true;
      acts.appendChild(curBtn);
      if (rt.kind === 'vulkan') {
        const updBtn = node('button', 'ghost small-btn', '🔄 Обновить');
        updBtn.type = 'button';
        updBtn.onclick = () => installLmsRuntime(rt.install_query || rt.name, updBtn);
        acts.appendChild(updBtn);
      }
    } else {
      const selBtn = node('button', rt.kind === 'vulkan' ? 'primary small-btn' : 'ghost small-btn',
        rt.kind === 'vulkan' ? '🌋 Включить Vulkan' : '⚡ Выбрать');
      selBtn.type = 'button';
      selBtn.onclick = async () => {
        selBtn.disabled = true;
        selBtn.textContent = 'переключаю…';
        try {
          const res = await api('/api/lms/runtime/select', { name: rt.name, version: rt.version });
          if (!res.ok) throw new Error(res.error || 'Не удалось переключить движок');
          pushNotice('Активный движок LM Studio переключён на ' + (rt.label || rt.name) + '!', 'ok');
          renderLmsRuntimes(res);
          await refreshState();
        } catch (err) {
          pushNotice(err.message, 'bad');
          selBtn.disabled = false;
          selBtn.textContent = rt.kind === 'vulkan' ? '🌋 Включить Vulkan' : '⚡ Выбрать';
        }
      };
      acts.appendChild(selBtn);
    }
    card.appendChild(acts);
    box.appendChild(card);
  });
}

async function installLmsRuntime(query, btnEl) {
  if (btnEl) btnEl.disabled = true;
  try {
    const res = await api('/api/lms/runtime/install', { query: query || 'vulkan' });
    if (!res.ok) throw new Error(res.error || 'Не удалось запустить установку движка');
    pushNotice('Запущено скачивание / обновление движка: ' + (res.query || 'Vulkan'), 'ok');
    const box = $('#lmJobLog');
    if (box) {
      box.hidden = false;
      box.textContent = 'Скачивание движка ' + (res.query || 'Vulkan') + '…';
    }
    refreshLM();
  } catch (err) {
    pushNotice(err.message, 'bad');
  } finally {
    if (btnEl) btnEl.disabled = false;
  }
}

function renderLM(data) {
  lmsStateCache = data;
  const hw = data.hardware || {};
  let hwSummary = '';
  if (hw.ram_total_gb) {
    hwSummary += ' · RAM: ' + hw.ram_free_gb + '/' + hw.ram_total_gb + ' ГБ';
  }
  if (hw.has_gpu && hw.gpu_name) {
    hwSummary += ' · ' + hw.gpu_name.replace('NVIDIA GeForce ', '') + ': ' + hw.vram_free_gb + '/' + hw.vram_total_gb + ' ГБ VRAM';
  }
  $('#lmBase').textContent = 'сервер: ' + (data.base || '…') + ' · ' +
    (data.alive ? 'живой' : 'молчит') + ' · моделей на диске: ' + (data.disk || []).length + hwSummary;
  renderLmsRuntimes(data.runtimes);

  const loaded = $('#lmLoaded');
  loaded.innerHTML = '';
  const hasAnyLoaded = !!(data.loaded && data.loaded.length);
  if ($('#lmsUnloadAll')) $('#lmsUnloadAll').disabled = !hasAnyLoaded;
  if ($('#lmsUnloadPlateBtn')) $('#lmsUnloadPlateBtn').hidden = !hasAnyLoaded;
  if (!hasAnyLoaded) loaded.append(node('div', 'empty-note', 'в памяти пусто — нажми «⚙️ Загрузить…» на модели справа'));
  (data.loaded || []).forEach((m) => {
    const cfgBtn = node('button', 'ghost', '⚙️ Контекст');
    cfgBtn.title = 'Изменить окно контекста, GPU Offload или перезагрузить модель';
    cfgBtn.onclick = () => openLmsLoadModal(m.modelKey || m.path || m.identifier);
    const unloadBtn = node('button', 'ghost', 'выгрузить');
    unloadBtn.onclick = async () => {
      unloadBtn.disabled = true;
      unloadBtn.textContent = 'выгружается…';
      try {
        await api('/api/lms/unload', { model: m.identifier || m.modelKey || m.path });
        pushNotice('Модель выгружена из памяти', 'ok');
        await refreshLM();
        await refreshState();
      } catch (err) { alert(err.message); }
      finally { unloadBtn.disabled = false; unloadBtn.textContent = 'выгрузить'; }
    };
    const actions = [cfgBtn, unloadBtn];
    const est = m.estimate || {};
    const card = plugCard({
      name: m.displayName || m.modelKey || m.path,
      desc: [m.status, m.contextLength ? 'контекст ' + Number(m.contextLength).toLocaleString('ru-RU') + ' ток.' : '', m.paramsString || '',
        m.quantization && m.quantization.name ? m.quantization.name : '', fmtSize(m.sizeBytes)]
        .filter(Boolean).join(' · '),
      badge: est.fit_label ? (est.fit_label + ' · ' + (est.tok_per_sec || '')) : '',
      badgeColor: est.fit_color || 'green',
      badgeTitle: est.recommendation || '',
      recommendation: est.recommendation ? ('⚡ ' + est.ui_comfort + ': ' + est.recommendation) : '',
      tools: [],
      file: m.modelKey || m.path || m.identifier,
    }, actions);
    loaded.append(card);
  });
  const disk = $('#lmDisk');
  disk.innerHTML = '';
  if (!(data.disk || []).length) disk.append(node('div', 'empty-note', 'на диске моделей нет — скачай из каталога ниже'));
  const normKey = (v) => String(v || '').replace(/\\/g, '/').trim();
  const loadedKeys = new Set(
    (data.loaded || []).flatMap((m) => [m.modelKey, m.path, m.identifier, m.displayName, m.indexedModelIdentifier].map(normKey).filter(Boolean))
  );
  (data.disk || []).forEach((m) => {
    const isLoaded = [m.modelKey, m.path, m.identifier, m.displayName, m.indexedModelIdentifier]
      .map(normKey)
      .some((k) => k && loadedKeys.has(k));
    const actions = [];
    if (isLoaded) {
      const unloadBtn = node('button', 'ghost', 'Выгрузить');
      unloadBtn.onclick = async () => {
        unloadBtn.disabled = true;
        unloadBtn.textContent = 'выгружается…';
        try {
          await api('/api/lms/unload', { model: m.identifier || m.modelKey || m.path });
          pushNotice('Модель выгружена из памяти', 'ok');
          await refreshLM();
          await refreshState();
        } catch (err) {
          alert(err.message);
          unloadBtn.disabled = false;
          unloadBtn.textContent = 'Выгрузить';
        }
      };
      actions.push(unloadBtn);
    }
    const quickBtn = node('button', 'ghost', '⚡ Быстро');
    quickBtn.title = 'Быстро загрузить с текущими параметрами контекста и GPU со страницы';
    quickBtn.onclick = async () => {
      quickBtn.disabled = true;
      quickBtn.textContent = 'грузится…';
      try {
        await api('/api/lms/load', {
          model: m.modelKey || m.path,
          context_length: clampContext($('#lmContext').value, 16384),
          gpu: ($('#lmGpuQuick') && $('#lmGpuQuick').value) || 'max',
          kv_quant: ($('#lmKvQuick') && $('#lmKvQuick').value) || 'f16',
          ttl: Number($('#lmTtl').value) || 0,
        });
        pushNotice('Запущена загрузка модели «' + (m.displayName || m.modelKey) + '»', 'ok');
        await refreshLM();
        await refreshState();
      } catch (err) {
        alert(err.message);
        quickBtn.disabled = false;
        quickBtn.textContent = '⚡ Быстро';
      }
    };
    actions.push(quickBtn);

    const cfgLoadBtn = node('button', isLoaded ? 'ghost' : 'primary', isLoaded ? '⚙️ Перезагрузить…' : '⚙️ Загрузить…');
    cfgLoadBtn.title = 'Открыть окно настройки контекста, GPU Offload, Vulkan и KV-кэша как в LM Studio';
    cfgLoadBtn.onclick = () => openLmsLoadModal(m.modelKey || m.path);
    actions.push(cfgLoadBtn);

    const est = m.estimate || {};
    const maxCtxStr = m.maxContextLength ? Number(m.maxContextLength).toLocaleString('ru-RU') : '?';
    const card = plugCard({
      name: m.displayName || m.modelKey || m.path,
      desc: [m.type, m.paramsString || '', m.architecture || '', m.trainedForToolUse ? 'tools' : '',
        m.vision ? 'vision' : '', 'контекст до ' + maxCtxStr,
        m.quantization && m.quantization.name ? m.quantization.name : '', fmtSize(m.sizeBytes)]
        .filter(Boolean).join(' · '),
      badge: isLoaded ? '● в памяти' : (est.fit_label ? (est.fit_label + ' · ' + (est.tok_per_sec || '')) : ''),
      badgeColor: isLoaded ? 'green' : (est.fit_color || 'green'),
      badgeTitle: est.recommendation || '',
      recommendation: est.recommendation ? ('⚡ ' + est.ui_comfort + ': ' + est.recommendation) : '',
      tools: [],
      file: m.modelKey || m.path,
    }, actions);
    disk.append(card);
  });
}

function renderJob(job) {
  const box = $('#lmJobLog');
  if (!job || (!job.lines && !job.running)) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = (job.kind || 'job') + ' ' + (job.status || '') + '\n' + (job.lines || []).join('\n');
}

let lmJobWasRunning = false;
async function refreshLM() {
  try {
    const data = await api('/api/lms/state');
    renderLM(data);
    const job = await api('/api/lms/job');
    renderJob(job);
    const isRunning = !!(job && job.running);
    if (lmJobWasRunning && !isRunning) {
      lmJobWasRunning = false;
      const freshData = await api('/api/lms/state');
      renderLM(freshData);
      await refreshState();
    }
    if (isRunning) {
      lmJobWasRunning = true;
      if (!lmJobTimer) {
        lmJobTimer = setTimeout(async () => { lmJobTimer = null; refreshLM(); }, 1500);
      }
    }
  } catch (err) {
    $('#lmBase').textContent = 'сервер: ошибка ' + err.message;
  }
}

async function startDownload() {
  const qEl = $('#lmQuery');
  const q = qEl ? qEl.value.trim() : '';
  if (!q) {
    if (qEl) qEl.focus();
    return;
  }
  $('#lmDownload').disabled = true;
  try {
    await api('/api/lms/download', { query: q });
    $('#lmJobLog').hidden = false;
    $('#lmJobLog').textContent = 'запустил скачивание: ' + q;
    refreshLM();
  } catch (err) { alert(err.message); }
  $('#lmDownload').disabled = false;
}

if ($('#lmDownload')) $('#lmDownload').onclick = startDownload;
$('#lmQuery')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); startDownload(); }
});

/* ---------- wiring ---------- */
$('#visionFile').onchange = (e) => {
  const files = e.target.files;
  addFiles(files);
  e.target.value = '';
};
$('#visionClear').onclick = () => { clearAttached(); };
$('#attachBtn').onclick = () => $('#attachFile').click();
$('#attachFile').onchange = (e) => { addFiles(e.target.files); e.target.value = ''; };
document.addEventListener('paste', (e) => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  const files = [];
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  if (!files.length) return;
  e.preventDefault();
  addFiles(files);
});
const shell = document.querySelector('.input-shell');
if (shell) {
  ['dragenter', 'dragover'].forEach((ev) => shell.addEventListener(ev, (e) => {
    e.preventDefault();
    shell.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach((ev) => shell.addEventListener(ev, (e) => {
    e.preventDefault();
    shell.classList.remove('dragover');
  }));
  shell.addEventListener('drop', (e) => {
    const files = (e.dataTransfer && e.dataTransfer.files) || [];
    addFiles(files);
  });
}
$('#lmImport').onclick = async () => {
  const file = $('#lmImportPath').value.trim();
  if (!file) return;
  try { await api('/api/lms/import', { path: file, mode: $('#lmImportMode').value }); $('#lmImportPath').value = ''; refreshLM(); }
  catch (err) { alert(err.message); }
};
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#send').onclick = send;
$('#stopBtn').onclick = async () => {
  if (!sessionId || !running) return;
  $('#stopBtn').disabled = true;
  $('#stat').textContent = 'останавливаю…';
  try {
    await api('/api/stop', { session: sessionId });
  } catch (err) {
    pushNotice(err.message, 'bad');
    // The server may already have closed the run; keep the UI usable.
    running = false;
    $('#send').disabled = false;
  }
};
$('#newChat').onclick = async () => {
  const res = await api('/api/new', { title: '' });
  await openSession(res.session);
  $('#sessionTitle').textContent = 'новая сессия';
  $('#input').focus();
  refreshState();
};
$('#sessionSearch').oninput = () => {
  const edit = $('#sessions .session-edit');
  if (edit) edit.parentNode.replaceChildren();
  renderSessions();
};
$('#autostart').onchange = async (e) => {
  const checkbox = e.target;
  checkbox.disabled = true;
  try {
    await api('/api/desktop/autostart', { enabled: checkbox.checked });
  } catch (err) { pushNotice(err.message, 'bad'); }
  finally { await refreshDesktop(); }
};
$('#provider').onchange = (e) => patchConfig('provider', e.target.value);
$('#model').onchange = (e) => patchConfig('model', e.target.value);
$('#profile').onchange = (e) => patchConfig('tool_profile', e.target.value);
$('#protocol').onchange = (e) => patchConfig('tool_protocol', e.target.value);
$('#perms').onchange = (e) => patchConfig('permissions', { mode: e.target.value });
$('#goalMode').onchange = (e) => {
  $('#goalCriteria').hidden = !e.target.checked;
  $('#goalBand').hidden = !e.target.checked;
  if (e.target.checked) $('#goalCriteria').focus();
};
$('#railBtn').onclick = () => {
  app.classList.toggle('rail');
  localStorage.setItem('lh.rail', app.classList.contains('rail') ? '1' : '0');
};
const mobileLayout = () => window.matchMedia('(max-width: 768px)').matches;
const modelMenuEl = () => $('#modelMenu');
const modelMenuOpen = () => !!modelMenuEl() && modelMenuEl().classList.contains('open');
function syncBackdrop() {
  const backdrop = $('#sideBackdrop');
  if (!backdrop) return;
  const open = mobileLayout() && (
    ($('#side') && $('#side').classList.contains('open')) ||
    modelMenuOpen() ||
    (settingsDrawer() && settingsDrawer().classList.contains('open'))
  );
  backdrop.hidden = !open;
  backdrop.classList.toggle('active', open);
}
function setSideDrawer(open) {
  const side = $('#side');
  if (!side) return;
  side.classList.toggle('open', open);
  syncBackdrop();
}
if ($('#mobileMenuBtn')) $('#mobileMenuBtn').onclick = () => setSideDrawer(true);
if ($('#sideCloseBtn')) $('#sideCloseBtn').onclick = () => setSideDrawer(false);

function setModelMenu(open) {
  const menu = modelMenuEl();
  if (!menu) return;
  menu.classList.toggle('open', open);
  menu.setAttribute('aria-hidden', open ? 'false' : 'true');
  const chip = $('#modelChip');
  if (chip) chip.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) {
    if (mobileLayout()) {
      menu.style.left = '';
      menu.style.right = '';
      menu.style.bottom = '';
      menu.style.width = '';
    } else if (chip) {
      const cr = chip.getBoundingClientRect();
      const width = Math.min(430, window.innerWidth - 32);
      const left = Math.min(Math.max(12, cr.left), window.innerWidth - width - 12);
      menu.style.width = width + 'px';
      menu.style.left = left + 'px';
      menu.style.right = 'auto';
      menu.style.bottom = Math.max(12, window.innerHeight - cr.top + 10) + 'px';
    }
  }
  syncBackdrop();
  if (open) {
    const on = menu.querySelector('.mm-model.on') || menu.querySelector('.mm-model');
    if (on) setTimeout(() => on.scrollIntoView({ block: 'nearest' }), 60);
  }
}
if ($('#modelChip')) $('#modelChip').onclick = () => setModelMenu(!modelMenuOpen());
if ($('#modelMenuClose')) $('#modelMenuClose').onclick = () => setModelMenu(false);
document.addEventListener('pointerdown', (e) => {
  if (!modelMenuOpen()) return;
  if (e.target.closest('#modelMenu, #modelChip')) return;
  setModelMenu(false);
});

const settingsDrawer = () => $('#settingsDrawer');
function setSettingsDrawer(open) {
  const d = settingsDrawer();
  if (!d) return;
  if (!open && d.contains(document.activeElement)) {
    const gear = $('#gearBtn');
    if (gear && gear.offsetParent) gear.focus();
  }
  d.classList.toggle('open', open);
  d.setAttribute('aria-hidden', open ? 'false' : 'true');
  if ($('#gearBtn')) $('#gearBtn').classList.toggle('on', open);
  if (open) refreshRemote();
  syncBackdrop();
}
if ($('#gearBtn')) $('#gearBtn').onclick = () => setSettingsDrawer(!settingsDrawer().classList.contains('open'));
if ($('#drawerClose')) $('#drawerClose').onclick = () => setSettingsDrawer(false);
if ($('#sideBackdrop')) {
  $('#sideBackdrop').onclick = () => {
    setSideDrawer(false);
    setModelMenu(false);
    setSettingsDrawer(false);
  };
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    setSettingsDrawer(false);
    setSideDrawer(false);
    setModelMenu(false);
  }
});
window.addEventListener('resize', syncBackdrop);
window.addEventListener('resize', () => { if (modelMenuOpen()) setModelMenu(false); });
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => {
    if (currentView === 'chat' && log) log.scrollTop = log.scrollHeight;
  });
}
$('#input').addEventListener('focus', () => {
  if (window.innerWidth <= 768 && log) {
    setTimeout(() => { log.scrollTop = log.scrollHeight; }, 300);
  }
});
$('#showReasoning').onchange = (e) => {
  showReasoning = e.target.checked;
  localStorage.setItem('lh.showReasoning', showReasoning ? '1' : '0');
  app.classList.toggle('thinkoff', !showReasoning);
  if (diveEl && diveEl.toolCount === 0) diveEl.wrap.hidden = !showReasoning;
};
$('#expandReasoning').onchange = (e) => {
  expandReasoning = e.target.checked;
  localStorage.setItem('lh.expandReasoning', expandReasoning ? '1' : '0');
  if (diveEl) diveEl.body.hidden = !expandReasoning;
};
$('#autoCompress').onchange = (e) => {
  localStorage.setItem('lh.autoCompress', e.target.checked ? '1' : '0');
  patchConfig('context_mode', e.target.checked ? 'auto' : 'off');
};
$('#goalChecklistToggle').onchange = (e) => {
  showGoalChecklist = e.target.checked;
  localStorage.setItem('lh.goalChecklist', showGoalChecklist ? '1' : '0');
  if (showGoalChecklist) renderChecklist(lastGoalPoints);
  else { const box = $('#goalChecklist'); box.hidden = true; box.innerHTML = ''; }
};
$('#thinkBtn').onclick = () => {
  const off = app.classList.toggle('thinkoff');
  localStorage.setItem('lh.thinkoff', off ? '1' : '0');
  $('#thinkBtn').classList.toggle('off', off);
  $('#thinkBtn').setAttribute('aria-pressed', off ? 'false' : 'true');
};
document.querySelector('.right-head').onclick = () => {
  app.classList.toggle('no-right');
  localStorage.setItem('lh.noright', app.classList.contains('no-right') ? '1' : '0');
};

document.querySelectorAll('.view-tab').forEach((b) => {
  b.onclick = () => setView(b.dataset.view);
});
$('#plugSearch').oninput = (e) => { plugFilter = e.target.value; renderPlugins(); renderWorkshop(); };
$('#plugReload').onclick = async () => {
  try {
    await api('/api/plugins/reload', {});
    await refreshPlugins();
    refreshWorkshop();
    refreshState();
  } catch (err) { alert(err.message); }
};
$('#plugInstallUrl').onclick = async () => {
  const url = $('#plugUrl').value.trim();
  if (!url) return;
  $('#plugInstallUrl').disabled = true;
  try {
    await api('/api/plugins/install', { url });
    $('#plugUrl').value = '';
    refreshPlugins();
    refreshState();
  } catch (err) { alert(err.message); }
  $('#plugInstallUrl').disabled = false;
};
$('#lmContext').onchange = async (e) => {
  const prev = clampContext(stateCache && (stateCache.lms_context_length || stateCache.context_tokens), 16384);
  const next = clampContext(e.target.value, prev);
  if (String(e.target.value).trim() !== String(next)) {
    pushNotice('Контекст ограничен диапазоном 512–262144 токенов: ' + next, 'warn');
  }
  e.target.value = next;
  if (next !== prev) {
    await api('/api/config', { config: { lms_context_length: next, context_tokens: next } });
    await refreshState();
  }
};
if ($('#lmGpuQuick')) $('#lmGpuQuick').onchange = (e) => patchConfig('lms_gpu_offload', e.target.value);
if ($('#lmKvQuick')) $('#lmKvQuick').onchange = (e) => patchConfig('lms_kv_quant', e.target.value);
document.querySelectorAll('#lmPageCtxChips .ctx-preset-pill').forEach((btn) => {
  btn.onclick = async () => {
    const val = clampContext(btn.dataset.ctx, 16384);
    if ($('#lmContext')) $('#lmContext').value = val;
    await api('/api/config', { config: { lms_context_length: val, context_tokens: val } });
    await refreshState();
    if (currentView === 'lm') refreshLM();
  };
});
if ($('#mmQuickCtx')) {
  $('#mmQuickCtx').onchange = async (e) => {
    const val = clampContext(e.target.value, 16384);
    await api('/api/config', { config: { lms_context_length: val, context_tokens: val } });
    await refreshState();
    pushNotice('Окно контекста установлено: ' + val.toLocaleString('ru-RU') + ' токенов (перезагрузи модель кнопкой ⚡, если она уже в памяти)', 'ok');
  };
}
if ($('#lmInstallVulkanBtn')) {
  $('#lmInstallVulkanBtn').onclick = () => installLmsRuntime('vulkan', $('#lmInstallVulkanBtn'));
}
if ($('#lmsOpenLoaderTopBtn')) {
  $('#lmsOpenLoaderTopBtn').onclick = () => openLmsLoadModal(stateCache && stateCache.model);
}
$('#lmTtl').onchange = (e) => patchConfig('lms_ttl', Number(e.target.value) || 0);
$('#searxngEnabled').onchange = (e) => patchConfig('search_searxng', e.target.checked);
$('#searxngUrl').onchange = (e) => patchConfig('search_searxng_url', e.target.value.trim() || 'http://127.0.0.1:8080');
if ($('#obscuraEnabled')) $('#obscuraEnabled').onchange = (e) => patchConfig('search_obscura', e.target.checked);
if ($('#obscuraPath')) $('#obscuraPath').onchange = (e) => patchConfig('obscura_path', e.target.value.trim());
if ($('#obscuraCdp')) $('#obscuraCdp').onchange = (e) => patchConfig('obscura_cdp', e.target.value.trim() || 'http://127.0.0.1:9223');
if ($('#obscuraTestBtn')) {
  $('#obscuraTestBtn').onclick = async () => {
    const btn = $('#obscuraTestBtn');
    const hint = $('#obscuraHint');
    btn.disabled = true;
    btn.textContent = 'Проверка…';
    try {
      const res = await api('/api/obscura/test', {
        obscura_path: ($('#obscuraPath') && $('#obscuraPath').value.trim()) || '',
        obscura_cdp: ($('#obscuraCdp') && $('#obscuraCdp').value.trim()) || 'http://127.0.0.1:9223'
      });
      if (res && res.ok) {
        if (hint) hint.textContent = '✅ ' + (res.message || 'Obscura готов к работе');
        pushNotice(res.message || 'Obscura готов', 'ok');
      } else {
        if (hint) hint.textContent = '⚠️ ' + ((res && res.error) || 'Obscura не найден');
        pushNotice((res && res.error) || 'Obscura не найден', 'warn');
      }
      refreshState();
    } catch (err) {
      if (hint) hint.textContent = 'Ошибка: ' + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Проверить';
    }
  };
}

async function refreshMemory() {
  try {
    const res = await api('/api/memory');
    const facts = res.facts || [];
    $('#memoryCount').textContent = 'Фактов: ' + facts.length;
    const list = $('#memoryList');
    list.innerHTML = '';
    if (!facts.length) {
      list.innerHTML = '<div class="page-sub" style="padding: 4px;">Пока нет сохранённых фактов</div>';
      return;
    }
    facts.forEach((f) => {
      const item = node('div', 'memory-item');
      item.appendChild(node('span', null, esc(f.text)));
      const del = node('button', 'memory-del', '×');
      del.title = 'Удалить факт';
      del.onclick = async () => {
        await api('/api/memory/delete', { id: f.id });
        refreshMemory();
      };
      item.appendChild(del);
      list.appendChild(item);
    });
  } catch (err) { console.warn(err); }
}

$('#memoryEnabled').onchange = (e) => {
  patchConfig('memory_enabled', e.target.checked);
  $('#memorySection').hidden = !e.target.checked;
  if (e.target.checked) refreshMemory();
};
$('#memoryAddBtn').onclick = async () => {
  const val = $('#memoryNewInput').value.trim();
  if (!val) return;
  $('#memoryNewInput').value = '';
  await api('/api/memory/add', { text: val });
  refreshMemory();
};
$('#memoryNewInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#memoryAddBtn').click();
});
$('#memoryClearBtn').onclick = async () => {
  if (confirm('Очистить все сохранённые факты памяти?')) {
    await api('/api/memory/clear', {});
    refreshMemory();
  }
};
$('#imageGenEnabled').onchange = (e) => {
  patchConfig('image_gen_enabled', e.target.checked);
  $('#imageGenSection').hidden = !e.target.checked;
};
$('#comfyuiUrl').onchange = (e) => patchConfig('comfyui_url', e.target.value.trim() || 'http://127.0.0.1:8188');
$('#comfyuiTestBtn').onclick = async () => {
  $('#comfyuiStatus').textContent = 'проверяю…';
  try {
    const res = await api('/api/comfyui/status');
    if (res.ok) {
      $('#comfyuiStatus').textContent = '✓ подключено к ' + (res.type === 'comfyui' ? 'ComfyUI' : 'SD WebUI');
      $('#comfyuiStatus').style.color = 'var(--ok-soft)';
    } else {
      $('#comfyuiStatus').textContent = '✗ ' + (res.error || 'не отвечает');
      $('#comfyuiStatus').style.color = 'var(--fail)';
    }
  } catch (err) {
    $('#comfyuiStatus').textContent = '✗ ' + err.message;
    $('#comfyuiStatus').style.color = 'var(--fail)';
  }
};
$('#lanAccessEnabled').onchange = (e) => {
  $('#lanSection').hidden = !e.target.checked;
  patchConfig('lan_access', e.target.checked);
};
$('#lanCopyBtn').onclick = () => {
  const url = $('#lanUrlText').textContent;
  if (!url) return;
  const done = () => pushNotice('Ссылка скопирована: ' + url, 'ok');
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (err) { pushNotice('Не удалось скопировать', 'bad'); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(done).catch(fallback);
  } else {
    fallback();
  }
};
$('#gameFailsafeEnabled').onchange = (e) => patchConfig('game_failsafe', e.target.checked);

let remoteTimer = null;
let remoteQrUrl = '';
function remoteClock(exp) {
  if (!exp) return '';
  const left = Math.max(0, exp - Math.floor(Date.now() / 1000));
  const h = Math.floor(left / 3600);
  const m = Math.floor((left % 3600) / 60);
  return h ? h + ' ч ' + m + ' мин' : m + ' мин';
}
function copyText(text, okText) {
  const done = () => pushNotice(okText + ': ' + text, 'ok');
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (err) { pushNotice('Не удалось скопировать', 'bad'); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done).catch(fallback);
  else fallback();
}
function renderRemote(st) {
  if (!st) return;
  const phase = st.phase || 'idle';
  const busy = phase === 'starting';
  $('#remoteEnabled').checked = busy || phase === 'running';
  $('#remoteSection').hidden = phase === 'idle';
  if (phase === 'idle') {
    $('#remoteMeta').textContent = '';
    $('#remoteHint').textContent = '';
  }
  $('#remoteCode').textContent = st.display || '·····-·····';
  $('#remoteCopyBtn').disabled = !st.code;
  $('#remoteRotateBtn').disabled = phase !== 'running';
  const meta = [];
  if (phase === 'starting') meta.push('Подключаюсь к реле…');
  else if (phase === 'running') {
    meta.push(st.connected ? 'Реле на связи' : 'Реле переподключается…');
    if (st.expires) meta.push('код ещё ' + remoteClock(st.expires));
    if (!st.worker_url) meta.push('реле не задано');
    else if (!st.room) meta.push('реле: ' + (st.worker_error || 'код не отправлен'));
  } else if (phase === 'error') meta.push('Ошибка: ' + (st.error || 'реле не ответило'));
  $('#remoteMeta').textContent = meta.join(' · ');
  const qr = $('#remoteQr');
  if (st.qr_url && st.qr_url !== remoteQrUrl) {
    remoteQrUrl = st.qr_url;
    qr.innerHTML = '<img src="/api/qrcode?url=' + encodeURIComponent(st.qr_url) + '" width="150" height="150" alt="QR Code">';
  } else if (!st.qr_url) {
    remoteQrUrl = '';
    qr.innerHTML = '';
  }
  $('#remoteSiteText').textContent = st.site_url || st.worker_url || 'сайт не настроен';
  $('#remoteOpenSite').disabled = !(st.site_url || st.worker_url);
  if (document.activeElement !== $('#remoteSiteUrl')) $('#remoteSiteUrl').value = st.site_url || '';
  if (document.activeElement !== $('#remoteWorkerUrl')) $('#remoteWorkerUrl').value = st.worker_url || '';
  let hint = '';
  if (phase === 'running') {
    if (!st.connected) hint = 'Связи с реле нет — переподключаюсь сам, ничего делать не нужно.';
    else if (st.worker_error) hint = 'Реле ответило ошибкой: ' + st.worker_error;
    else if (!st.worker_url) hint = 'Задай адрес реле ниже — тогда код можно вводить руками на сайте, а QR откроет его сразу.';
    else hint = 'Открой сайт, введи код — и ты внутри. Код привязан к этой машине и перестаёт работать после «Выключить» или «Новый код».';
  } else if (phase === 'error') {
    hint = st.error || '';
  } else if (phase === 'starting') {
    hint = 'Поднимаю исходящее соединение с реле — без установки cloudflared.';
  }
  $('#remoteHint').textContent = hint;
}
async function refreshRemote() {
  let st = null;
  try { st = await api('/api/remote/status'); } catch (err) { return null; }
  renderRemote(st);
  if (remoteTimer) { clearTimeout(remoteTimer); remoteTimer = null; }
  if (st.phase === 'starting') remoteTimer = setTimeout(refreshRemote, 1200);
  else if (st.phase === 'running') remoteTimer = setTimeout(refreshRemote, 15000);
  return st;
}
$('#remoteEnabled').onchange = async (e) => {
  const on = e.target.checked;
  e.target.disabled = true;
  $('#remoteSection').hidden = false;
  $('#remoteMeta').textContent = on ? 'Запускаю…' : 'Останавливаю…';
  try {
    const st = await api(on ? '/api/remote/start' : '/api/remote/stop', {});
    renderRemote(st);
    refreshRemote();
  } catch (err) {
    e.target.checked = !on;
    pushNotice('Удалённый доступ: ' + err.message, 'bad');
  } finally { e.target.disabled = false; }
};
$('#remoteCopyBtn').onclick = () => { const code = $('#remoteCode').textContent.trim(); if (code && !code.includes('·')) copyText(code, 'Код скопирован'); };
$('#remoteRotateBtn').onclick = async () => {
  try {
    const st = await api('/api/remote/rotate', {});
    renderRemote(st);
    pushNotice('Новый код выдан, старый погашен', 'ok');
  } catch (err) { pushNotice('Не получилось: ' + err.message, 'bad'); }
};
$('#remoteOpenSite').onclick = () => {
  const url = $('#remoteSiteText').textContent.trim();
  if (url && url !== 'сайт не настроен') window.open(url, '_blank', 'noopener');
};
$('#remoteCfgSave').onclick = async () => {
  const site = $('#remoteSiteUrl').value.trim().replace(/\/+$/, '');
  const worker = $('#remoteWorkerUrl').value.trim().replace(/\/+$/, '');
  try {
    await api('/api/config', { config: { remote: { site_url: site, worker_url: worker } } });
    await refreshRemote();
    pushNotice('Адреса сохранены', 'ok');
  } catch (err) { pushNotice('Не сохранилось: ' + err.message, 'bad'); }
};
$('#lmsRefresh').onclick = refreshLM;
const unloadAllFn = async () => {
  try {
    await api('/api/lms/unload', { model: '' });
    pushNotice('Все модели выгружены из памяти', 'ok');
    await refreshLM();
    await refreshState();
  } catch (err) { alert(err.message); }
};
if ($('#lmsUnloadAll')) $('#lmsUnloadAll').onclick = unloadAllFn;
if ($('#lmsUnloadPlateBtn')) $('#lmsUnloadPlateBtn').onclick = unloadAllFn;
$('#lmsWake').onclick = async () => {
  $('#lmBase').textContent = 'сервер: поднимаю…';
  try {
    const res = await api('/api/provider/start', { wait: 60 });
    $('#lmBase').textContent = res.ok ? 'сервер живой, моделей: ' + (res.models || []).length
      : 'не поднялся: ' + (res.error || 'порт молчит');
    refreshLM();
    refreshState();
  } catch (err) { $('#lmBase').textContent = 'ошибка: ' + err.message; }
};
$('#saveSystemPrompt').onclick = async () => {
  const btn = $('#saveSystemPrompt');
  btn.disabled = true;
  try { await patchConfig('system_prompt', $('#systemPrompt').value); btn.textContent = 'Сохранено'; setTimeout(() => { btn.textContent = 'Сохранить промпт'; }, 1200); }
  catch (err) { alert(err.message); }
  finally { btn.disabled = false; }
};

/* ---------- GPU Thermal Protection & Cooldown ---------- */
let thermalSnoozeUntil = 0;
let cooldownTimerId = null;
let cooldownTotalSec = 180;
let cooldownTargetT = 70;

function fmtTimer(sec) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

let thermalModalTimerId = null;
let thermalBgTimerId = null;

function fillThermalModal(th) {
  if (!th) return;
  const pwr = th.power_w != null ? (' · ' + th.power_w + 'W') : '';
  if ($('#gpuDialogMeta')) $('#gpuDialogMeta').textContent = (th.gpu_name || 'GPU') + pwr;
  if ($('#tmetricTemp')) $('#tmetricTemp').textContent = th.temp_c != null ? th.temp_c + '°C' : '—';
  if ($('#tmetricTempSub')) $('#tmetricTempSub').textContent = th.status_label || 'Норма';
  if ($('#tmetricUtil')) $('#tmetricUtil').textContent = th.utilization_pct != null ? th.utilization_pct + '%' : '—';
  if ($('#tmetricUtilSub')) $('#tmetricUtilSub').textContent = th.power_w != null ? ('Мощность: ' + th.power_w + ' Вт') : 'GPU Load';
  const usedGb = th.vram_used_mb ? (th.vram_used_mb / 1024).toFixed(1) : '0.0';
  const totGb = th.vram_total_mb ? (th.vram_total_mb / 1024).toFixed(1) : '0.0';
  if ($('#tmetricVram')) $('#tmetricVram').textContent = usedGb + ' / ' + totGb + ' ГБ';
  if ($('#tmetricGovernor')) $('#tmetricGovernor').textContent = th.throttle_active ? 'Троттлинг 🧊' : 'В норме';
  if ($('#tmetricGovernorSub')) {
    $('#tmetricGovernorSub').textContent = th.throttle_active
      ? ('Задержка: ' + (th.throttle_delay * 1000).toFixed(0) + ' мс/токен')
      : 'Снижение скорости не требуется';
  }
  if ($('#thermalModalDesc')) $('#thermalModalDesc').textContent = th.message || 'Датчик работает штатно.';
}

function renderThermalState(th) {
  if (!th) return;
  const pill = $('#gpuThermalPill');
  if (pill) {
    if (th.has_gpu && th.temp_c != null) {
      pill.hidden = false;
      const ico = th.status === 'critical' ? '🔥' : (th.status === 'hot' ? '⚠️' : (th.status === 'warm' ? '🌡️' : '❄️'));
      $('#gpuPillIco').textContent = ico;
      const utilStr = th.utilization_pct != null ? (' · ' + th.utilization_pct + '%') : '';
      $('#gpuPillTxt').textContent = th.temp_c + '°C' + utilStr;
      pill.className = 'gpu-pill small-btn ' + (th.status === 'critical' ? 'critical' : (th.status === 'hot' ? 'hot' : (th.status === 'warm' ? 'warm' : 'ok')));
      const vramStr = th.vram_total_mb ? (' · VRAM ' + (th.vram_used_mb / 1024).toFixed(1) + '/' + (th.vram_total_mb / 1024).toFixed(1) + ' ГБ') : '';
      pill.title = (th.gpu_name ? th.gpu_name + ' · ' : '') + th.status_label + utilStr + vramStr + (th.throttle_active ? ' · троттлинг активен' : '');
    } else {
      pill.hidden = true;
    }
  }
  if ($('#gpuThermalDialog') && $('#gpuThermalDialog').open) {
    fillThermalModal(th);
  }

  // Settings block
  if ($('#gpuThermalEnabled') && th.thresholds) {
    if (document.activeElement !== $('#gpuTempWarn') &&
        document.activeElement !== $('#gpuTempThrottle') &&
        document.activeElement !== $('#gpuTempCritical') &&
        document.activeElement !== $('#gpuCooldownDuration')) {
      $('#gpuThermalEnabled').checked = th.thresholds.enabled !== false;
      $('#gpuTempWarn').value = th.thresholds.warn || 80;
      $('#gpuTempThrottle').value = th.thresholds.throttle || 85;
      $('#gpuTempCritical').value = th.thresholds.critical || 95;
      $('#gpuCooldownDuration').value = Math.round((th.thresholds.duration || 180) / 60);
    }
  }
  const sChip = $('#gpuStatusChip');
  if (sChip) {
    sChip.textContent = th.status_label || 'опрос...';
    sChip.className = 'sec-chip ' + (th.status === 'normal' ? 'ok' : (th.status === 'warm' ? 'warn' : (th.status === 'critical' ? 'err' : '')));
  }
  const sText = $('#gpuStatusText');
  if (sText) {
    sText.textContent = (th.has_gpu ? th.gpu_name + ' · ' : '') + (th.message || '');
  }

  // Check critical condition
  if (th.status === 'critical') {
    const isCooldownOpen = $('#gpuCooldownDialog') && $('#gpuCooldownDialog').open;
    if (!isCooldownOpen && Date.now() > thermalSnoozeUntil) {
      const critDiag = $('#gpuCriticalDialog');
      if (critDiag && !critDiag.open) {
        $('#gpuCritTempDisplay').textContent = th.temp_c != null ? th.temp_c : '100+';
        critDiag.showModal();
      }
    }
  }
}

async function startGpuCooldown(durationSec, targetTemp) {
  durationSec = Number(durationSec) || 180;
  targetTemp = Number(targetTemp) || 70;
  cooldownTotalSec = durationSec;
  cooldownTargetT = targetTemp;

  const res = await api('/api/thermal/cooldown/start', {
    duration: durationSec,
    target_temp: targetTemp,
    unload: true
  });

  const crit = $('#gpuCriticalDialog');
  if (crit && crit.open) crit.close();
  const thDiag = $('#gpuThermalDialog');
  if (thDiag && thDiag.open) thDiag.close();

  const cdiag = $('#gpuCooldownDialog');
  if (cdiag) {
    $('#gpuCooldownTimerDisplay').textContent = fmtTimer(durationSec);
    $('#gpuCooldownTargetTemp').textContent = '< ' + targetTemp + '°C';
    $('#gpuCooldownCurTemp').textContent = (res && res.current_temp != null ? res.current_temp : '--') + '°C';
    $('#gpuCooldownPbarFill').style.width = '0%';
    $('#gpuCooldownSubNote').textContent = 'Модель выгружена из VRAM. Вентиляторы охлаждают видеокарту...';
    $('#gpuCooldownCancelBtn').hidden = false;
    $('#gpuCooldownDoneBtn').hidden = true;
    if (!cdiag.open) cdiag.showModal();
  }

  if (cooldownTimerId) clearInterval(cooldownTimerId);
  let remaining = durationSec;
  cooldownTimerId = setInterval(async () => {
    remaining--;
    if (remaining < 0) remaining = 0;
    if ($('#gpuCooldownTimerDisplay')) {
      $('#gpuCooldownTimerDisplay').textContent = fmtTimer(remaining);
      const pct = Math.min(100, Math.round(((durationSec - remaining) / durationSec) * 100));
      $('#gpuCooldownPbarFill').style.width = pct + '%';
    }

    if (remaining % 3 === 0 || remaining === 0) {
      try {
        const curStatus = await api('/api/thermal/status');
        if (curStatus) {
          if ($('#gpuCooldownCurTemp')) {
            $('#gpuCooldownCurTemp').textContent = (curStatus.temp_c != null ? curStatus.temp_c : '--') + '°C';
          }
          if (curStatus.temp_c != null && curStatus.temp_c <= targetTemp && (durationSec - remaining) >= 15) {
            remaining = 0;
          }
        }
      } catch (e) {}
    }

    if (remaining <= 0) {
      clearInterval(cooldownTimerId);
      cooldownTimerId = null;
      if ($('#gpuCooldownTimerDisplay')) $('#gpuCooldownTimerDisplay').textContent = '00:00';
      if ($('#gpuCooldownPbarFill')) $('#gpuCooldownPbarFill').style.width = '100%';
      if ($('#gpuCooldownSubNote')) {
        $('#gpuCooldownSubNote').textContent = '✅ Охлаждение завершено! Температура видеокарты снизилась. Можно продолжать работу.';
      }
      if ($('#gpuCooldownCancelBtn')) $('#gpuCooldownCancelBtn').hidden = true;
      if ($('#gpuCooldownDoneBtn')) $('#gpuCooldownDoneBtn').hidden = false;
      refreshState();
    }
  }, 1000);
}

// Thermal Pill and Dialog click handlers (with live 1.5s refresh while open + 3s background poll)
function stopThermalModalTimer() {
  if (thermalModalTimerId) {
    clearInterval(thermalModalTimerId);
    thermalModalTimerId = null;
  }
}

async function refreshThermalLive() {
  if (document.hidden) return;
  try {
    const th = await api('/api/thermal/status?force=1');
    if (th) renderThermalState(th);
  } catch (e) {}
}

if (!thermalBgTimerId) {
  thermalBgTimerId = setInterval(refreshThermalLive, 3000);
}

if ($('#gpuThermalPill')) {
  $('#gpuThermalPill').onclick = async () => {
    try {
      const th = await api('/api/thermal/status?force=1');
      if (!th) return;
      renderThermalState(th);
      fillThermalModal(th);
      const dlg = $('#gpuThermalDialog');
      if (dlg && !dlg.open) dlg.showModal();
      stopThermalModalTimer();
      thermalModalTimerId = setInterval(async () => {
        if (!dlg || !dlg.open) { stopThermalModalTimer(); return; }
        await refreshThermalLive();
      }, 1500);
    } catch (e) {
      pushNotice('Не удалось прочитать датчики GPU: ' + e.message, 'warn');
    }
  };
}
if ($('#gpuDialogClose')) {
  $('#gpuDialogClose').onclick = () => {
    stopThermalModalTimer();
    $('#gpuThermalDialog').close();
  };
}
if ($('#gpuThermalDialog')) {
  $('#gpuThermalDialog').addEventListener('close', stopThermalModalTimer);
}
if ($('#gpuDialogUnloadBtn')) {
  $('#gpuDialogUnloadBtn').onclick = () => {
    const dur = (Number($('#gpuCooldownDuration').value) || 3) * 60;
    startGpuCooldown(dur, 70);
  };
}

// Critical Warning handlers
if ($('#gpuCriticalUnloadBtn')) {
  $('#gpuCriticalUnloadBtn').onclick = () => {
    const dur = (Number($('#gpuCooldownDuration').value) || 3) * 60;
    startGpuCooldown(dur, 70);
  };
}
if ($('#gpuCriticalDismissBtn')) {
  $('#gpuCriticalDismissBtn').onclick = () => {
    thermalSnoozeUntil = Date.now() + 120000;
    $('#gpuCriticalDialog').close();
    pushNotice('Предупреждение о перегреве отложено на 2 минуты', 'warn');
  };
}

// Cooldown Modal handlers
if ($('#gpuCooldownCancelBtn')) {
  $('#gpuCooldownCancelBtn').onclick = async () => {
    if (cooldownTimerId) {
      clearInterval(cooldownTimerId);
      cooldownTimerId = null;
    }
    await api('/api/thermal/cooldown/cancel', {});
    $('#gpuCooldownDialog').close();
    pushNotice('Охлаждение прервано', 'warn');
    refreshState();
  };
}
if ($('#gpuCooldownDoneBtn')) $('#gpuCooldownDoneBtn').onclick = () => $('#gpuCooldownDialog').close();
if ($('#gpuCooldownClose')) {
  $('#gpuCooldownClose').onclick = () => {
    if (cooldownTimerId) {
      clearInterval(cooldownTimerId);
      cooldownTimerId = null;
    }
    $('#gpuCooldownDialog').close();
  };
}

// Settings handlers
if ($('#gpuThermalSaveBtn')) {
  $('#gpuThermalSaveBtn').onclick = async () => {
    const btn = $('#gpuThermalSaveBtn');
    btn.disabled = true;
    try {
      await patchConfig('gpu_thermal_enabled', $('#gpuThermalEnabled').checked);
      await patchConfig('gpu_temp_warn', Number($('#gpuTempWarn').value) || 80);
      await patchConfig('gpu_temp_throttle', Number($('#gpuTempThrottle').value) || 85);
      await patchConfig('gpu_temp_critical', Number($('#gpuTempCritical').value) || 95);
      await patchConfig('gpu_cooldown_duration', (Number($('#gpuCooldownDuration').value) || 3) * 60);
      btn.textContent = 'Сохранено';
      setTimeout(() => { btn.textContent = 'Сохранить пороги'; }, 1200);
      pushNotice('Пороги термозащиты GPU сохранены', 'ok');
    } catch (e) {
      alert(e.message);
    } finally {
      btn.disabled = false;
    }
  };
}
if ($('#gpuThermalTestWarnBtn')) {
  $('#gpuThermalTestWarnBtn').onclick = async () => {
    await api('/api/thermal/simulate', { temp: 88 });
    await refreshState();
    pushNotice('Симуляция: GPU 88°C (активен термо-троттлинг)', 'warn');
  };
}
if ($('#gpuThermalTestCritBtn')) {
  $('#gpuThermalTestCritBtn').onclick = async () => {
    await api('/api/thermal/simulate', { temp: 102 });
    await refreshState();
    pushNotice('Симуляция: GPU 102°C (критический перегрев)', 'bad');
  };
}
if ($('#gpuThermalResetSimBtn')) {
  $('#gpuThermalResetSimBtn').onclick = async () => {
    await api('/api/thermal/simulate', { temp: null });
    await refreshState();
    pushNotice('Симуляция выключена, опрашивается реальный датчик', 'ok');
  };
}

/* ---------- System Dashboard (Canvas Charts) ---------- */
let dashTimer = null;
let lastDashData = null;

function _prepCanvas(canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  const parent = canvas.parentElement;
  const rect = parent ? parent.getBoundingClientRect() : { width: 520, height: 185 };
  const rawW = rect.width || (parent ? parent.clientWidth : 0) || 520;
  const rawH = rect.height || (parent ? parent.clientHeight : 0) || 185;
  const w = Math.max(200, Math.floor(rawW));
  const h = Math.max(120, Math.floor(rawH));
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

const _dashChartMeta = {};

function _isLightTheme() {
  return document.documentElement.dataset.theme === 'light';
}

function _fmtChartTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ru-RU', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function _drawGridAndAxes(ctx, w, h, pad, yMin, yMax, unit, tMin, tMax) {
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;
  const steps = 4;
  const light = _isLightTheme();
  ctx.save();
  ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.fillStyle = light ? 'rgba(15, 23, 42, .62)' : 'rgba(255, 255, 255, .48)';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.strokeStyle = light ? 'rgba(15, 23, 42, .10)' : 'rgba(255, 255, 255, .07)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  for (let i = 0; i <= steps; i++) {
    const frac = i / steps;
    const y = pad.t + plotH * frac;
    const val = Math.round(yMax - (yMax - yMin) * frac);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.fillText(val + (unit || ''), pad.l - 6, y);
  }
  if (tMin && tMax && tMax > tMin) {
    ctx.setLineDash([]);
    ctx.textBaseline = 'bottom';
    ctx.textAlign = 'left';
    ctx.fillText(_fmtChartTime(tMin), pad.l, h - 1);
    ctx.textAlign = 'right';
    ctx.fillText(_fmtChartTime(tMax), w - pad.r, h - 1);
  }
  ctx.restore();
  return { plotW, plotH };
}

function _drawEmptyChart(ctx, w, h) {
  const light = _isLightTheme();
  ctx.save();
  ctx.font = '12px system-ui, sans-serif';
  ctx.fillStyle = light ? 'rgba(15, 23, 42, .45)' : 'rgba(255, 255, 255, .38)';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('Сбор данных…', w / 2, h / 2);
  ctx.restore();
}

function _bindChartHover(canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || canvas.dataset.hoverBound) return;
  canvas.dataset.hoverBound = '1';
  canvas.addEventListener('mousemove', (ev) => {
    const meta = _dashChartMeta[canvasId];
    const tip = $('#dashTooltip');
    if (!meta || !meta.data || !meta.data.length || !tip) return;
    const rect = canvas.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const frac = Math.max(0, Math.min(1, (mx - meta.pad.l) / Math.max(1, meta.plotW)));
    let best = meta.data[0];
    if (meta.tMax > meta.tMin) {
      const targetT = meta.tMin + frac * (meta.tMax - meta.tMin);
      let bestDiff = Infinity;
      for (let i = 0; i < meta.data.length; i++) {
        const d = Math.abs((meta.data[i].ts || 0) - targetT);
        if (d < bestDiff) { bestDiff = d; best = meta.data[i]; }
      }
    } else {
      best = meta.data[Math.min(meta.data.length - 1, Math.round(frac * (meta.data.length - 1)))];
    }
    const parts = [_fmtChartTime(best.ts)];
    meta.keys.forEach((k) => {
      if (best[k.key] != null) parts.push(k.label + ': ' + best[k.key] + (k.unit || ''));
    });
    tip.textContent = parts.join(' · ');
    tip.hidden = false;
    tip.style.left = Math.min(window.innerWidth - 220, ev.clientX + 12) + 'px';
    tip.style.top = Math.max(8, ev.clientY - 32) + 'px';
  });
  canvas.addEventListener('mouseleave', () => {
    const tip = $('#dashTooltip');
    if (tip) tip.hidden = true;
  });
}

function drawLineChart(canvasId, data, key, opts) {
  const prep = _prepCanvas(canvasId);
  if (!prep) return;
  _bindChartHover(canvasId);
  const { ctx, w, h } = prep;
  const pad = { l: 46, r: 10, t: 10, b: 18 };
  const yMin = opts.yMin != null ? opts.yMin : 0;
  const yMax = opts.yMax != null ? opts.yMax : 100;
  const tMin = data && data.length ? (data[0].ts || 0) : 0;
  const tMax = data && data.length ? (data[data.length - 1].ts || 0) : 0;
  const { plotW, plotH } = _drawGridAndAxes(ctx, w, h, pad, yMin, yMax, opts.unit, tMin, tMax);
  _dashChartMeta[canvasId] = { data: data || [], pad, plotW, tMin, tMax, keys: [{ key, label: opts.label || key, unit: opts.unit || '' }] };

  const vals = (data || []).map((d) => d[key]).filter((v) => v != null && Number.isFinite(Number(v)));
  if (!vals.length) {
    _drawEmptyChart(ctx, w, h);
    return;
  }

  const yOf = (v) => {
    const clamped = Math.max(yMin, Math.min(yMax, Number(v)));
    return pad.t + plotH * (1 - (clamped - yMin) / Math.max(1, yMax - yMin));
  };

  const drawThresh = (val, color) => {
    if (val == null || val < yMin || val > yMax) return;
    const y = yOf(val);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.restore();
  };
  drawThresh(opts.warnLine, 'rgba(249, 115, 22, .55)');
  drawThresh(opts.critLine, 'rgba(239, 68, 68, .65)');

  const pts = [];
  const n = data.length;
  const tSpan = tMax - tMin;
  for (let i = 0; i < n; i++) {
    const v = data[i][key];
    if (v == null || !Number.isFinite(Number(v))) continue;
    const frac = n === 1 ? 0.5 : (tSpan > 1 ? ((data[i].ts - tMin) / tSpan) : (i / (n - 1)));
    const x = pad.l + frac * plotW;
    pts.push({ x, y: yOf(v) });
  }
  if (!pts.length) return;

  if (opts.fillColor && pts.length >= 2) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pad.t + plotH);
    for (let i = 0; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    ctx.lineTo(pts[pts.length - 1].x, pad.t + plotH);
    ctx.closePath();
    ctx.fillStyle = opts.fillColor;
    ctx.fill();
    ctx.restore();
  }

  ctx.save();
  ctx.beginPath();
  ctx.strokeStyle = opts.color || '#38bdf8';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  for (let i = 0; i < pts.length; i++) {
    if (i === 0) ctx.moveTo(pts[i].x, pts[i].y);
    else ctx.lineTo(pts[i].x, pts[i].y);
  }
  ctx.stroke();
  // Highlight latest point
  const last = pts[pts.length - 1];
  ctx.beginPath();
  ctx.arc(last.x, last.y, 3.2, 0, Math.PI * 2);
  ctx.fillStyle = opts.color || '#38bdf8';
  ctx.fill();
  ctx.restore();
}

function drawAreaChart(canvasId, data, key, opts) {
  drawLineChart(canvasId, data, key, Object.assign({
    fillColor: opts.fillColor || 'rgba(34, 211, 238, .15)'
  }, opts));
}

function drawDualLineChart(canvasId, data, key1, key2, opts) {
  const prep = _prepCanvas(canvasId);
  if (!prep) return;
  _bindChartHover(canvasId);
  const { ctx, w, h } = prep;
  const pad = { l: 42, r: 10, t: 10, b: 18 };
  const yMin = opts.yMin != null ? opts.yMin : 0;
  const yMax = opts.yMax != null ? opts.yMax : 100;
  const tMin = data && data.length ? (data[0].ts || 0) : 0;
  const tMax = data && data.length ? (data[data.length - 1].ts || 0) : 0;
  const { plotW, plotH } = _drawGridAndAxes(ctx, w, h, pad, yMin, yMax, opts.unit, tMin, tMax);
  _dashChartMeta[canvasId] = {
    data: data || [], pad, plotW, tMin, tMax,
    keys: [{ key: key1, label: 'RAM', unit: '%' }, { key: key2, label: 'CPU', unit: '%' }]
  };

  if (!data || !data.length) {
    _drawEmptyChart(ctx, w, h);
    return;
  }

  const yOf = (v) => {
    const clamped = Math.max(yMin, Math.min(yMax, Number(v)));
    return pad.t + plotH * (1 - (clamped - yMin) / Math.max(1, yMax - yMin));
  };

  const tSpan = tMax - tMin;
  const drawSeries = (key, color) => {
    const pts = [];
    const n = data.length;
    for (let i = 0; i < n; i++) {
      const v = data[i][key];
      if (v == null || !Number.isFinite(Number(v))) continue;
      const frac = n === 1 ? 0.5 : (tSpan > 1 ? ((data[i].ts - tMin) / tSpan) : (i / (n - 1)));
      const x = pad.l + frac * plotW;
      pts.push({ x, y: yOf(v) });
    }
    if (!pts.length) return;
    ctx.save();
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    for (let i = 0; i < pts.length; i++) {
      if (i === 0) ctx.moveTo(pts[i].x, pts[i].y);
      else ctx.lineTo(pts[i].x, pts[i].y);
    }
    ctx.stroke();
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.restore();
  };

  drawSeries(key1, opts.color1 || '#a78bfa');
  drawSeries(key2, opts.color2 || '#34d399');
}

async function refreshDashboard() {
  try {
    const mins = ($('#dashWindow') && Number($('#dashWindow').value)) || 10;
    const data = await api('/api/dashboard/history?minutes=' + encodeURIComponent(mins));
    if (!data || !data.ok) return;
    const history = data.history || [];
    const summary = data.summary || {};
    const latest = history.length ? history[history.length - 1] : {};

    // Uptime
    if (summary.uptime != null) {
      const totalSec = Math.floor(summary.uptime);
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;
      $('#dashUptime').textContent = 'аптайм: ' + (h ? h + 'ч ' : '') + m + 'м ' + s + 'с';
    }

    // Cards
    $('#dashGpuTemp').textContent = latest.gpu_temp != null ? latest.gpu_temp + '°C' : '—';
    const gpuBaseName = latest.gpu_name ? latest.gpu_name.replace('NVIDIA GeForce ', '') : 'нет датчика';
    $('#dashGpuName').textContent = gpuBaseName + (latest.gpu_power_w != null ? (' · ' + Math.round(latest.gpu_power_w) + ' Вт') : '');

    const vUsed = Number(latest.vram_used_mb || 0);
    const vTot = Number(latest.vram_total_mb || 0);
    if (vTot > 0) {
      $('#dashVram').textContent = (vUsed / 1024).toFixed(1) + ' / ' + (vTot / 1024).toFixed(1) + ' ГБ';
      $('#dashVramPct').textContent = Math.round((vUsed / vTot) * 100) + ' % занято';
    } else {
      $('#dashVram').textContent = '—';
      $('#dashVramPct').textContent = 'нет GPU';
    }

    $('#dashRam').textContent = latest.ram_used_pct != null ? latest.ram_used_pct + ' %' : '—';
    $('#dashRamFree').textContent = latest.ram_free_gb != null ? latest.ram_free_gb + ' ГБ свободно' : '—';

    $('#dashCpu').textContent = latest.cpu_pct != null ? Math.round(latest.cpu_pct) + ' %' : '—';
    $('#dashCpuCores').textContent = latest.cpu_cores ? latest.cpu_cores + ' потоков' : 'CPU';

    if ($('#dashTps')) $('#dashTps').textContent = summary.last_tps ? (summary.last_tps + ' tok/s') : '— tok/s';
    if ($('#dashTpsSub')) $('#dashTpsSub').textContent = 'пик: ' + (summary.peak_tps || '—') + ' tok/s';
    if ($('#dashDiskNet')) $('#dashDiskNet').textContent = latest.disk_free_gb != null ? (latest.disk_free_gb + ' ГБ своб.') : '—';
    if ($('#dashDiskNetSub')) $('#dashDiskNetSub').textContent = '↓' + Math.round(latest.net_rx_kbps || 0) + ' ↑' + Math.round(latest.net_tx_kbps || 0) + ' КБ/с';

    $('#dashTokens').textContent = (summary.total_tokens || 0).toLocaleString('ru-RU');
    $('#dashSessions').textContent = summary.total_sessions || 0;
    if ($('#dashSessionsSub')) $('#dashSessionsSub').textContent = 'активно: ' + (summary.active_sessions || 0) + ' · инстр.: ' + (summary.tool_calls || 0);
    $('#dashPeakGpu').textContent = summary.peak_gpu_temp != null ? summary.peak_gpu_temp + '°C' : '--°C';
    $('#dashAvgGpu').textContent = summary.avg_gpu_temp != null ? Math.round(summary.avg_gpu_temp) + '°C' : '--°C';
    if ($('#dashAvgTps')) $('#dashAvgTps').textContent = summary.avg_tps ? (summary.avg_tps + ' tok/s') : '—';
    if ($('#dashToolCalls')) $('#dashToolCalls').textContent = (summary.tool_calls || 0) + (summary.tool_errors ? (' (ошиб: ' + summary.tool_errors + ')') : '');
    $('#dashModelLoads').textContent = summary.model_loads || 0;

    // Fetch top processes non-blocking
    api('/api/dashboard/processes').then((pr) => {
      const tb = $('#dashProcBody');
      if (!tb || !pr || !pr.processes) return;
      tb.innerHTML = pr.processes.map((p) =>
        '<tr><td>' + p.pid + '</td><td>' + esc(p.name) + '</td><td>' + p.rss_mb + '</td><td>' + p.mem_pct + '%</td><td>' + p.cpu_pct + '%</td></tr>'
      ).join('') || '<tr><td colspan="5" class="page-sub">Нет данных</td></tr>';
    }).catch(() => {});

    if ($('#chartGpuTempVal')) $('#chartGpuTempVal').textContent = latest.gpu_temp != null ? latest.gpu_temp + '°C' : '—';
    if ($('#chartGpuUtilVal')) $('#chartGpuUtilVal').textContent = latest.gpu_util != null ? latest.gpu_util + '%' : '—';
    if ($('#chartVramVal')) $('#chartVramVal').textContent = vTot > 0 ? Math.round(vUsed) + ' / ' + Math.round(vTot) + ' МБ' : '—';

    // Charts
    lastDashData = { history, vTot };
    const drawAllDashCharts = () => {
      if (!lastDashData) return;
      const hData = lastDashData.history;
      const vt = lastDashData.vTot;
      drawLineChart('chartGpuTemp', hData, 'gpu_temp', {
        color: '#f97316', fillColor: 'rgba(249, 115, 22, .12)',
        yMin: 20, yMax: 110, unit: '°',
        warnLine: 85, critLine: 95
      });
      drawLineChart('chartGpuUtil', hData, 'gpu_util', {
        color: '#818cf8', fillColor: 'rgba(129, 140, 248, .12)',
        yMin: 0, yMax: 100, unit: '%'
      });
      drawAreaChart('chartVram', hData, 'vram_used_mb', {
        color: '#22d3ee', fillColor: 'rgba(34, 211, 238, .14)',
        yMin: 0, yMax: vt || 8192, unit: ''
      });
      drawDualLineChart('chartSystem', hData, 'ram_used_pct', 'cpu_pct', {
        color1: '#a78bfa', color2: '#34d399',
        yMin: 0, yMax: 100, unit: '%'
      });
    };
    drawAllDashCharts();
    window._drawAllDashCharts = drawAllDashCharts;

    if (dashTimer) clearTimeout(dashTimer);
    if (currentView === 'dashboard') {
      dashTimer = setTimeout(refreshDashboard, 3000);
    }
  } catch (e) {
    console.warn('dashboard refresh failed:', e);
  }
}

window.addEventListener('resize', () => {
  if (currentView === 'dashboard' && typeof window._drawAllDashCharts === 'function') {
    window._drawAllDashCharts();
  }
});

if ($('#dashRefreshBtn')) $('#dashRefreshBtn').onclick = refreshDashboard;
if ($('#dashWindow')) $('#dashWindow').onchange = refreshDashboard;
if ($('#dashExportCsvBtn')) {
  $('#dashExportCsvBtn').onclick = () => {
    const mins = ($('#dashWindow') && Number($('#dashWindow').value)) || 60;
    window.open('/api/dashboard/export.csv?minutes=' + encodeURIComponent(mins), '_blank');
  };
}
if ($('#dashResetBtn')) {
  $('#dashResetBtn').onclick = async () => {
    await api('/api/dashboard/reset', {});
    pushNotice('Счётчики дашборда сброшены', 'ok');
    refreshDashboard();
  };
}

/* ---------- Wi-Fi Cluster ---------- */
let clusterTimer = null;

async function refreshCluster() {
  try {
    const st = await api('/api/cluster/status');
    if (!st || !st.ok) return;

    if ($('#clusterLanNotice')) $('#clusterLanNotice').hidden = !!st.lan_access;
    if ($('#clusterOffloadHot')) $('#clusterOffloadHot').checked = st.offload_on_hot !== false;

    const wUrl = st.worker_url || (location.origin + '/worker');
    if ($('#clusterWorkerUrl')) $('#clusterWorkerUrl').value = wUrl;
    const qrBox = $('#clusterQrBox');
    if (qrBox && qrBox.dataset.url !== wUrl) {
      qrBox.dataset.url = wUrl;
      qrBox.innerHTML = '<img src="/api/qrcode?url=' + encodeURIComponent(wUrl) + '" width="136" height="136" alt="QR Worker">';
    }

    if ($('#clusterWorkersChip')) {
      $('#clusterWorkersChip').textContent = 'воркеров: ' + (st.workers_online || 0);
      $('#clusterWorkersChip').className = 'sec-chip ' + (st.workers_online ? 'ok' : '');
    }
    if ($('#clusterThreadsChip')) $('#clusterThreadsChip').textContent = 'потоков: ' + (st.total_worker_threads || 0);
    if ($('#clusterDoneChip')) $('#clusterDoneChip').textContent = 'задач: ' + (st.total_tasks_done || 0);

    // Workers grid
    const wList = $('#clusterWorkersList');
    if (wList) {
      wList.innerHTML = '';
      const workers = st.workers || [];
      if (!workers.length) {
        wList.append(node('div', 'page-sub', 'Пока ни одно устройство не подключено. Отсканируй QR-код телефоном или нажми «Открыть воркер здесь ↗» для проверки.'));
      } else {
        workers.forEach((w) => {
          const card = node('div', 'cworker-card');
          const ico = w.device_type === 'mobile' ? '📱' : '💻';
          const head = node('div', 'cworker-head');
          head.append(node('span', null, ico + ' ' + esc(w.name || w.id)));
          const badge = node('span', 'sec-chip ' + (w.paused ? 'warn' : 'ok'), w.paused ? 'пауза' : 'в сети');
          badge.style.marginLeft = 'auto';
          head.append(badge);
          card.append(head);

          const meta = node('div', 'cworker-meta');
          meta.append(node('span', null, 'потоков: ' + (w.max_threads || 1) + '/' + (w.cores || 1)));
          if (w.battery_pct != null) meta.append(node('span', null, 'батарея: ' + w.battery_pct + '%' + (w.charging ? '⚡' : '')));
          meta.append(node('span', null, 'задач: ' + (w.tasks_done || 0)));
          if (w.avg_ms) meta.append(node('span', null, '~' + w.avg_ms + ' мс'));
          if (w.ip) meta.append(node('span', null, esc(w.ip)));
          card.append(meta);
          wList.append(card);
        });
      }
    }

    // LLM nodes list
    const llmList = $('#clusterLlmList');
    if (llmList) {
      llmList.innerHTML = '';
      const nodes = st.llm_nodes || [];
      if (!nodes.length) {
        llmList.append(node('div', 'page-sub', 'Удалённых LLM-узлов пока нет. Нажми «Сканировать Wi-Fi» или введи IP другого ПК.'));
      } else {
        nodes.forEach((n) => {
          const row = node('div', 'cllm-row');
          const info = node('div', 'cllm-info');
          const on = n.enabled !== false;
          info.append(node('div', 'cllm-title', (!on ? '⏸️ ' : (n.alive ? '🟢 ' : '🔴 ')) + esc(n.label || n.url)));
          const modelsTxt = (n.models || []).length ? ('моделей: ' + n.models.length + ' (' + n.models.slice(0, 2).join(', ') + ')') : (n.error || 'ожидание опроса…');
          info.append(node('div', 'cllm-sub', esc(n.url) + (n.latency_ms ? ' · ' + n.latency_ms + ' мс' : '') + ' · ' + esc(modelsTxt)));
          row.append(info);
          const acts = node('div', 'sched-actions');
          acts.append(switchEl(on, async () => {
            await api('/api/cluster/node/toggle', { url: n.url, enabled: !on });
            refreshCluster();
          }));
          const del = node('button', 'ghost small-btn', 'Удалить');
          del.type = 'button';
          del.onclick = async () => {
            await api('/api/cluster/node/remove', { url: n.url });
            refreshCluster();
          };
          acts.append(del);
          row.append(acts);
          llmList.append(row);
        });
      }
    }

    // Native llama.cpp RPC status
    const rpc = st.rpc || {};
    if ($('#clusterRpcChip')) {
      $('#clusterRpcChip').className = 'sec-chip ' + (rpc.running ? 'ok' : (rpc.available ? '' : 'warn'));
      $('#clusterRpcChip').textContent = rpc.running ? ('онлайн · PID ' + rpc.pid) : (rpc.available ? 'готов к запуску' : 'бинарник не найден');
    }
    if ($('#clusterRpcCliHint')) $('#clusterRpcCliHint').value = rpc.cli_hint || '';
    if ($('#clusterRpcStartBtn')) $('#clusterRpcStartBtn').disabled = !!rpc.running || !rpc.available;
    if ($('#clusterRpcStopBtn')) $('#clusterRpcStopBtn').disabled = !rpc.running;

    if (clusterTimer) clearTimeout(clusterTimer);
    if (currentView === 'cluster') {
      clusterTimer = setTimeout(refreshCluster, 3000);
    }
  } catch (e) {
    console.warn('cluster refresh failed:', e);
  }
}

if ($('#clusterRefreshBtn')) $('#clusterRefreshBtn').onclick = refreshCluster;
if ($('#clusterEnableLanBtn')) {
  $('#clusterEnableLanBtn').onclick = async () => {
    await patchConfig('lan_access', true);
    setTimeout(refreshCluster, 650);
  };
}
if ($('#clusterTestNodeBtn')) {
  $('#clusterTestNodeBtn').onclick = async () => {
    const prompt = ($('#clusterTestPrompt') && $('#clusterTestPrompt').value.trim()) || 'Привет! Ответь одним коротким предложением.';
    const out = $('#clusterTestOut');
    if (out) out.textContent = '⏳ Запрос к активному LAN LLM-узлу…';
    $('#clusterTestNodeBtn').disabled = true;
    try {
      const r = await api('/api/cluster/llm_query', { prompt });
      if (out) {
        out.textContent = r && r.ok
          ? ('✅ [' + r.node + ' · ' + r.model + ' · ' + r.elapsed_ms + ' мс]: ' + r.text)
          : ('❌ ' + ((r && r.error) || 'Нет активных узлов'));
      }
    } catch (e) {
      if (out) out.textContent = '❌ ' + e.message;
    } finally {
      $('#clusterTestNodeBtn').disabled = false;
    }
  };
}
if ($('#clusterRpcStartBtn')) {
  $('#clusterRpcStartBtn').onclick = async () => {
    const port = Number($('#clusterRpcPort') && $('#clusterRpcPort').value) || 50052;
    const r = await api('/api/cluster/rpc/start', { port });
    pushNotice(r && r.ok ? ('🔗 ggml-rpc-server запущен на порту ' + port) : ((r && r.error) || 'Ошибка RPC'), r && r.ok ? 'ok' : 'bad');
    refreshCluster();
  };
}
if ($('#clusterRpcStopBtn')) {
  $('#clusterRpcStopBtn').onclick = async () => {
    await api('/api/cluster/rpc/stop', {});
    pushNotice('🛑 ggml-rpc-server остановлен', 'warn');
    refreshCluster();
  };
}
if ($('#clusterCopyUrlBtn')) {
  $('#clusterCopyUrlBtn').onclick = () => {
    const val = ($('#clusterWorkerUrl') && $('#clusterWorkerUrl').value) || '';
    if (val && navigator.clipboard) navigator.clipboard.writeText(val);
    pushNotice('Ссылка воркера скопирована: ' + val, 'ok');
  };
}
if ($('#clusterOpenWorkerBtn')) {
  $('#clusterOpenWorkerBtn').onclick = () => {
    window.open('/worker', '_blank');
  };
}
if ($('#clusterOffloadHot')) {
  $('#clusterOffloadHot').onchange = (e) => patchConfig('cluster_offload_on_hot', !!e.target.checked);
}
if ($('#clusterAddNodeBtn')) {
  $('#clusterAddNodeBtn').onclick = async () => {
    const inp = $('#clusterNodeInput');
    const url = (inp && inp.value.trim()) || '';
    if (!url) return;
    const btn = $('#clusterAddNodeBtn');
    btn.disabled = true;
    try {
      const res = await api('/api/cluster/node/add', { url });
      if (res && res.ok) {
        inp.value = '';
        pushNotice(res.node && res.node.alive ? 'Узел добавлен и отвечает!' : 'Узел добавлен (сейчас не отвечает)', res.node && res.node.alive ? 'ok' : 'warn');
        refreshCluster();
      } else {
        pushNotice((res && res.error) || 'Не удалось добавить узел', 'bad');
      }
    } finally {
      btn.disabled = false;
    }
  };
}
if ($('#clusterScanBtn')) {
  $('#clusterScanBtn').onclick = async () => {
    const btn = $('#clusterScanBtn');
    const stEl = $('#clusterScanStatus');
    btn.disabled = true;
    if (stEl) stEl.textContent = 'Сканирую локальную Wi-Fi подсеть (Ollama / LM Studio / llama.cpp)…';
    try {
      const res = await api('/api/cluster/scan', {});
      if (res && res.ok) {
        const cnt = (res.found || []).length;
        if (stEl) stEl.textContent = 'Просканировано ' + (res.subnet || 'LAN') + ' за ' + res.elapsed_ms + ' мс · найдено узлов: ' + cnt;
        pushNotice(cnt ? ('Найдено LLM-узлов в Wi-Fi: ' + cnt) : 'Других LLM-серверов в Wi-Fi сети не найдено', cnt ? 'ok' : 'warn');
        refreshCluster();
      } else {
        if (stEl) stEl.textContent = (res && res.error) || 'Ошибка сканирования';
      }
    } finally {
      btn.disabled = false;
    }
  };
}
if ($('#clusterBenchBtn')) {
  $('#clusterBenchBtn').onclick = async () => {
    const btn = $('#clusterBenchBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Считаем…';
    try {
      const res = await api('/api/cluster/benchmark', { chunks: 12, iterations: 45000 });
      const box = $('#clusterBenchResult');
      const bars = $('#clusterBenchBars');
      if (box && bars && res) {
        box.hidden = false;
        $('#clusterBenchTime').textContent = (res.elapsed_ms || 0) + ' мс · устройств: ' + (res.workers_used || 0);
        bars.innerHTML = '';
        const per = res.per_worker || {};
        const total = Math.max(1, res.chunks || 12);
        Object.keys(per).forEach((name) => {
          const cnt = per[name];
          const pct = Math.round((cnt / total) * 100);
          const row = node('div', 'cbench-row');
          row.append(node('span', 'cbench-name', esc(name)));
          const track = node('div', 'cbench-track');
          const fill = node('div', 'cbench-fill');
          fill.style.width = pct + '%';
          track.append(fill);
          row.append(track);
          row.append(node('span', 'cbench-cnt', cnt + ' / ' + total + ' (' + pct + '%)'));
          bars.append(row);
        });
        pushNotice('Кластерный тест завершён за ' + res.elapsed_ms + ' мс', 'ok');
        refreshCluster();
      }
    } catch (e) {
      pushNotice('Ошибка теста: ' + e.message, 'bad');
    } finally {
      btn.disabled = false;
      btn.textContent = '⚡ Тест нагрузки';
    }
  };
}

/* ---------- Voice Mode (STT Microphone + TTS Speech) ---------- */
let voiceTtsEnabled = localStorage.getItem('lh.voice.tts') === '1';
let voiceRecognition = null;
let voiceRecording = false;
let voiceLastWasMic = false;

function syncVoiceButtons() {
  const ttsBtn = $('#voiceTtsBtn');
  if (ttsBtn) {
    ttsBtn.classList.toggle('active', voiceTtsEnabled);
    ttsBtn.setAttribute('aria-pressed', voiceTtsEnabled ? 'true' : 'false');
    ttsBtn.title = voiceTtsEnabled ? 'Озвучка ответов включена (нажми чтобы выключить)' : 'Включить озвучку ответов (TTS)';
  }
}
syncVoiceButtons();

function speakAnswer(rawText) {
  if (!voiceTtsEnabled) return;
  const cleaned = String(rawText || '')
    .replace(/```[\s\S]*?```/g, ' код пропущен. ')
    .replace(/`[^`]+`/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[#*_~>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 1500);
  if (!cleaned) return;
  if (typeof speakWithServer === 'function') {
    speakWithServer(cleaned);
    return;
  }
}

if ($('#voiceTtsBtn')) {
  $('#voiceTtsBtn').onclick = () => {
    voiceTtsEnabled = !voiceTtsEnabled;
    localStorage.setItem('lh.voice.tts', voiceTtsEnabled ? '1' : '0');
    if (!voiceTtsEnabled && 'speechSynthesis' in window) window.speechSynthesis.cancel();
    syncVoiceButtons();
    pushNotice(voiceTtsEnabled ? '🔊 Озвучка ответов модели включена' : '🔇 Озвучка ответов выключена', 'ok');
  };
}

if ($('#voiceMicBtn')) {
  $('#voiceMicBtn').onclick = () => {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {
      pushNotice('Голосовой ввод не поддерживается в этом браузере (открой в Chrome/Edge)', 'warn');
      return;
    }
    if (voiceRecording && voiceRecognition) {
      voiceRecognition.stop();
      return;
    }
    const rec = new SpeechRec();
    rec.lang = 'ru-RU';
    rec.interimResults = true;
    rec.continuous = false;
    const baseText = ($('#input').value || '').trim();
    voiceRecognition = rec;
    voiceRecording = true;
    $('#voiceMicBtn').classList.add('recording');
    $('#voiceMicBtn').setAttribute('aria-pressed', 'true');
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();

    rec.onresult = (ev) => {
      let transcript = '';
      for (let i = 0; i < ev.results.length; i++) {
        transcript += ev.results[i][0].transcript;
      }
      $('#input').value = (baseText ? baseText + ' ' : '') + transcript.trim();
    };
    rec.onerror = (ev) => {
      pushNotice('Микрофон: ' + (ev.error || 'ошибка распознавания'), 'warn');
    };
    rec.onend = () => {
      voiceRecording = false;
      $('#voiceMicBtn').classList.remove('recording');
      $('#voiceMicBtn').setAttribute('aria-pressed', 'false');
      const finalVal = ($('#input').value || '').trim();
      if (finalVal && finalVal !== baseText) {
        voiceLastWasMic = true;
        $('#input').focus();
      }
    };
    rec.start();
  };
}

/* ---------- Scheduler & Automations ---------- */
let schedTimer = null;
let schedHadRunning = false;

async function refreshSchedule() {
  try {
    const data = await api('/api/scheduler/list');
    if (!data || !data.ok) return;
    const tasks = data.tasks || [];
    const anyRunning = tasks.some((t) => !!t.running);
    if (schedHadRunning && !anyRunning) {
      schedHadRunning = false;
      refreshState();
    }
    if (anyRunning) schedHadRunning = true;

    const tList = $('#schedTasksList');
    if (tList) {
      tList.innerHTML = '';
      if (!tasks.length) {
        tList.append(node('div', 'page-sub', 'Запланированных задач пока нет. Создай первую выше!'));
      } else {
        tasks.forEach((t) => {
          const row = node('div', 'cllm-row');
          const info = node('div', 'cllm-info');
          const ico = t.kind === 'shell' ? '💻' : '🤖';
          const when = t.schedule_type === 'daily' ? ('ежедневно в ' + t.daily_time) : ('каждые ' + t.interval_min + ' мин');
          const nxt = t.next_run_at ? new Date(t.next_run_at * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '—';
          info.append(node('div', 'cllm-title', ico + ' ' + esc(t.name) + (t.enabled ? '' : ' (выкл.)')));
          info.append(node('div', 'cllm-sub', when + ' · след. ' + nxt + ' · запусков: ' + (t.runs_count || 0) + ' · ' + esc((t.payload || '').slice(0, 70))));
          row.append(info);

          const acts = node('div', 'sched-actions');
          const runBtn = node('button', 'ghost small-btn', t.running ? '⏳' : '▶ Сейчас');
          runBtn.type = 'button';
          runBtn.disabled = !!t.running;
          runBtn.onclick = async () => {
            runBtn.disabled = true;
            runBtn.textContent = '⏳';
            try {
              await api('/api/scheduler/run', { id: t.id });
              pushNotice('Задача «' + t.name + '» запущена', 'ok');
              schedHadRunning = true;
              setTimeout(refreshSchedule, 800);
            } catch (err) {
              pushNotice(err.message, 'bad');
              runBtn.disabled = false;
              runBtn.textContent = '▶ Сейчас';
            }
          };
          const togBtn = node('button', 'ghost small-btn', t.enabled ? 'Пауза' : 'Включить');
          togBtn.type = 'button';
          togBtn.onclick = async () => {
            await api('/api/scheduler/toggle', { id: t.id, enabled: !t.enabled });
            refreshSchedule();
          };
          const delBtn = node('button', 'ghost small-btn danger-text', '✕');
          delBtn.type = 'button';
          delBtn.onclick = async () => {
            await api('/api/scheduler/delete', { id: t.id });
            refreshSchedule();
          };
          acts.append(runBtn, togBtn, delBtn);
          row.append(acts);
          tList.append(row);
        });
      }
    }

    const hList = $('#schedHistoryList');
    if (hList) {
      hList.innerHTML = '';
      const hist = data.history || [];
      if (!hist.length) {
        hList.append(node('div', 'page-sub', 'История запусков пуста.'));
      } else {
        hist.slice(0, 15).forEach((h) => {
          const row = node('div', 'cllm-row');
          const info = node('div', 'cllm-info');
          const ts = h.ran_at ? new Date(h.ran_at * 1000).toLocaleString('ru-RU') : '';
          info.append(node('div', 'cllm-title', (h.ok ? '✅ ' : '❌ ') + esc(h.task_name || 'Задача') + ' · ' + ts + ' (' + (h.elapsed_s || 0) + 'с)'));
          info.append(node('div', 'cllm-sub', esc((h.output || '').slice(0, 180))));
          row.append(info);
          if (h.session_id) {
            const openBtn = node('button', 'ghost small-btn', 'Открыть чат');
            openBtn.type = 'button';
            openBtn.onclick = async () => {
              await openSession(h.session_id);
              setView('chat');
            };
            row.append(openBtn);
          }
          hList.append(row);
        });
      }
    }
    if (schedTimer) clearTimeout(schedTimer);
    if (currentView === 'schedule') {
      schedTimer = setTimeout(refreshSchedule, anyRunning ? 1500 : 5000);
    }
  } catch (e) {
    console.warn('schedule refresh failed:', e);
  }
}

if ($('#schedType')) {
  $('#schedType').onchange = (e) => {
    const daily = e.target.value === 'daily';
    if ($('#schedInterval')) $('#schedInterval').hidden = daily;
    if ($('#schedDailyTime')) $('#schedDailyTime').hidden = !daily;
  };
}
if ($('#schedRefreshBtn')) $('#schedRefreshBtn').onclick = refreshSchedule;
if ($('#schedAddBtn')) {
  $('#schedAddBtn').onclick = async () => {
    const name = ($('#schedName') && $('#schedName').value.trim()) || 'Автозадача';
    const kind = ($('#schedKind') && $('#schedKind').value) || 'prompt';
    const stype = ($('#schedType') && $('#schedType').value) || 'interval';
    const intervalMin = Number($('#schedInterval') && $('#schedInterval').value) || 60;
    const dailyTime = ($('#schedDailyTime') && $('#schedDailyTime').value) || '09:00';
    const payload = ($('#schedPayload') && $('#schedPayload').value.trim()) || '';
    if (!payload) { pushNotice('Введи текст промпта или команду', 'warn'); return; }
    const res = await api('/api/scheduler/add', {
      name, kind, schedule_type: stype, interval_min: intervalMin, daily_time: dailyTime, payload
    });
    if (res && res.ok) {
      $('#schedName').value = '';
      $('#schedPayload').value = '';
      pushNotice('Задача добавлена в расписание', 'ok');
      refreshSchedule();
    } else {
      pushNotice((res && res.error) || 'Не удалось добавить задачу', 'bad');
    }
  };
}

/* ---------- Profile Backup Export / Import (.zip) ---------- */
if ($('#bkExportBtn')) {
  $('#bkExportBtn').onclick = () => {
    const s = ($('#bkSessions') && $('#bkSessions').checked) ? '1' : '0';
    const p = ($('#bkPlugins') && $('#bkPlugins').checked) ? '1' : '0';
    const m = ($('#bkMemory') && $('#bkMemory').checked) ? '1' : '0';
    window.location.href = '/api/backup/export?sessions=' + s + '&plugins=' + p + '&memory=' + m;
    pushNotice('Скачивание резервной копии профиля (.zip)…', 'ok');
  };
}
if ($('#bkImportFile')) {
  $('#bkImportFile').onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const stEl = $('#bkStatus');
    if (stEl) stEl.textContent = 'Восстановление из ' + file.name + '…';
    try {
      const buf = await file.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = '';
      for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      const data_b64 = btoa(bin);
      const res = await api('/api/backup/import', {
        data_b64,
        restore_config: true,
        restore_memory: $('#bkMemory') ? $('#bkMemory').checked : true,
        restore_sessions: $('#bkSessions') ? $('#bkSessions').checked : true,
        restore_plugins: $('#bkPlugins') ? $('#bkPlugins').checked : true
      });
      if (res && res.ok) {
        const r = res.restored || {};
        const msg = '✅ Восстановлено: конфиг=' + (r.config ? 'да' : 'нет')
          + ', сессий=' + (r.sessions || 0)
          + ', плагинов=' + (r.plugins || 0)
          + ', память=' + (r.memory ? 'да' : 'нет');
        if (stEl) stEl.textContent = msg;
        pushNotice(msg, 'ok');
        await refreshState();
      } else {
        const err = (res && res.error) || 'Ошибка импорта';
        if (stEl) stEl.textContent = '❌ ' + err;
        pushNotice(err, 'bad');
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    } finally {
      e.target.value = '';
    }
  };
}

/* ---------- Theme (Dark / Light), Personas, Toast, Updater, RAG, MCP & Voice Call (XTTS v2) ---------- */
function applyTheme(theme, persist) {
  const t = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  localStorage.setItem('lh.theme', t);
  const btn = $('#themeToggleBtn');
  if (btn) btn.textContent = t === 'light' ? '🌙' : '☀️';
  const chk = $('#lightThemeToggle');
  if (chk) chk.checked = t === 'light';
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', t === 'light' ? '#f4f6fa' : '#121316');
  if (typeof window._drawAllDashCharts === 'function') {
    try { window._drawAllDashCharts(); } catch (e) {}
  }
  if (persist) {
    api('/api/config', { config: { theme: t } }).catch(() => {});
  }
}

applyTheme(localStorage.getItem('lh.theme') || 'dark', false);

if ($('#themeToggleBtn')) {
  $('#themeToggleBtn').onclick = () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    applyTheme(next, true);
  };
}
if ($('#lightThemeToggle')) {
  $('#lightThemeToggle').onchange = (e) => applyTheme(e.target.checked ? 'light' : 'dark', true);
}

/* ---------- Personas ---------- */
if ($('#personaSelect')) {
  $('#personaSelect').onchange = async (e) => {
    const persona = e.target.value || 'default';
    try {
      await api('/api/config', { config: { persona } });
      pushNotice('Персона переключена: ' + e.target.options[e.target.selectedIndex].text, 'ok');
    } catch (err) {
      pushNotice('Не удалось сменить персону: ' + err.message, 'bad');
    }
  };
}

/* ---------- Windows Toast Notifications ---------- */
if ($('#toastNotifToggle')) {
  $('#toastNotifToggle').onchange = (e) => {
    api('/api/config', { config: { toast_notifications: !!e.target.checked } }).catch(() => {});
  };
}
if ($('#toastTestBtn')) {
  $('#toastTestBtn').onclick = async () => {
    try {
      const res = await api('/api/notify/test', {
        title: 'LocalHarness',
        message: 'Windows Toast уведомления активны и готовы к работе!',
      });
      pushNotice(res.ok ? '🔔 Тестовое уведомление отправлено' : 'Уведомления отключены в настройках', res.ok ? 'ok' : 'warn');
    } catch (err) {
      pushNotice(err.message, 'bad');
    }
  };
}

/* ---------- Self-Updater ---------- */
let latestUpdateInfo = null;
if ($('#updateCheckBtn')) {
  $('#updateCheckBtn').onclick = async () => {
    const stEl = $('#updateStatusText');
    const dlBtn = $('#updateDownloadBtn');
    if (stEl) stEl.textContent = 'Проверка GitHub Releases…';
    try {
      const info = await api('/api/update/check?force=1');
      latestUpdateInfo = info;
      if (!info.ok) {
        if (stEl) stEl.textContent = '❌ ' + (info.error || 'Ошибка проверки');
        return;
      }
      if (info.available) {
        if (stEl) stEl.textContent = '🎉 Доступна новая версия v' + info.latest + ' (у вас v' + info.current + ')';
        if (dlBtn) dlBtn.hidden = !info.installer_url;
      } else {
        if (stEl) stEl.textContent = '✅ Установлена актуальная версия v' + info.current;
        if (dlBtn) dlBtn.hidden = true;
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    }
  };
}
if ($('#updateDownloadBtn')) {
  $('#updateDownloadBtn').onclick = async () => {
    if (!latestUpdateInfo || !latestUpdateInfo.installer_url) return;
    const stEl = $('#updateStatusText');
    const dlBtn = $('#updateDownloadBtn');
    dlBtn.disabled = true;
    try {
      const res = await api('/api/update/download', {
        installer_url: latestUpdateInfo.installer_url,
        installer_name: latestUpdateInfo.installer_name,
        launch: true,
      });
      if (!res.ok) {
        if (stEl) stEl.textContent = '❌ ' + (res.error || 'Ошибка загрузки');
        dlBtn.disabled = false;
        return;
      }
      if (stEl) stEl.textContent = '⬇️ Скачивание установщика… 0%';
      const poll = setInterval(async () => {
        try {
          const ds = await api('/api/update/status');
          if (stEl) stEl.textContent = ds.status === 'done'
            ? '✅ Установщик скачан и запущен!'
            : (ds.status === 'error' ? '❌ ' + ds.error : '⬇️ Скачивание… ' + (ds.progress || 0) + '%');
          if (!ds.running) {
            clearInterval(poll);
            dlBtn.disabled = false;
          }
        } catch (e) {
          clearInterval(poll);
          dlBtn.disabled = false;
        }
      }, 900);
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
      dlBtn.disabled = false;
    }
  };
}

/* ---------- Local RAG Knowledge Base ---------- */
function renderRagState(rag) {
  if (!rag) return;
  const chip = $('#ragCountChip');
  if (chip) chip.textContent = (rag.files_count || 0) + ' файлов · ' + (rag.chunks_count || 0) + ' чанков';
  const list = $('#ragFoldersList');
  if (!list) return;
  list.innerHTML = '';
  (rag.folders || []).forEach((folder) => {
    const row = node('div', 'memory-item');
    row.append(node('span', 'memory-text', '📁 ' + esc(folder)));
    const del = node('button', 'ghost small-btn danger-text', '✕');
    del.type = 'button';
    del.onclick = async () => {
      const res = await api('/api/rag/remove', { path: folder });
      renderRagState(res);
    };
    row.append(del);
    list.append(row);
  });
}

if ($('#ragIndexBtn')) {
  $('#ragIndexBtn').onclick = async () => {
    const p = ($('#ragPathInput') && $('#ragPathInput').value.trim()) || '';
    if (!p) { pushNotice('Укажи путь к папке или файлу для RAG', 'warn'); return; }
    const out = $('#ragSearchOut');
    if (out) out.textContent = 'Индексация…';
    try {
      const res = await api('/api/rag/index', { path: p });
      if (res.ok) {
        renderRagState(res);
        if (out) out.textContent = '✅ Проиндексировано файлов: ' + res.indexed_files + ', чанков: ' + res.added_chunks;
        $('#ragPathInput').value = '';
      } else if (out) {
        out.textContent = '❌ ' + (res.error || 'Ошибка индексации');
      }
    } catch (err) {
      if (out) out.textContent = '❌ ' + err.message;
    }
  };
}
if ($('#ragSearchBtn')) {
  $('#ragSearchBtn').onclick = async () => {
    const q = ($('#ragQueryInput') && $('#ragQueryInput').value.trim()) || '';
    const out = $('#ragSearchOut');
    if (!q) return;
    try {
      const res = await api('/api/rag/search', { query: q, top_k: 4 });
      if (!res.results || !res.results.length) {
        if (out) out.textContent = res.note || 'Совпадений не найдено.';
      } else if (out) {
        out.innerHTML = res.results.map((r) =>
          '<div><b>' + esc((r.file || '').split(/[\\/]/).pop()) + '</b> (' + r.score + '): ' + esc((r.text || '').slice(0, 140)) + '…</div>'
        ).join('');
      }
    } catch (err) {
      if (out) out.textContent = '❌ ' + err.message;
    }
  };
}
if ($('#ragClearBtn')) {
  $('#ragClearBtn').onclick = async () => {
    const res = await api('/api/rag/clear', {});
    renderRagState(res);
    if ($('#ragSearchOut')) $('#ragSearchOut').textContent = 'Индекс базы знаний очищен.';
  };
}

/* ---------- MCP Servers ---------- */
function renderMcpState(servers) {
  const rows = Array.isArray(servers) ? servers : [];
  const chip = $('#mcpCountChip');
  if (chip) chip.textContent = rows.length + ' серверов';
  const list = $('#mcpServerList');
  if (!list) return;
  list.innerHTML = '';
  rows.forEach((s) => {
    const row = node('div', 'memory-item');
    const stIcon = s.connected ? '🟢' : (s.enabled ? '🟡' : '⚪');
    const toolsCnt = (s.tools || []).length;
    row.append(node('span', 'memory-text',
      stIcon + ' <b>' + esc(s.name) + '</b> (' + esc(s.command) + ')' +
      (toolsCnt ? ' · ' + toolsCnt + ' инстр.' : '') +
      (s.error ? ' · ' + esc(s.error.slice(0, 60)) : '')
    ));
    const reconn = node('button', 'ghost small-btn', '🔌');
    reconn.type = 'button';
    reconn.title = 'Переподключить';
    reconn.onclick = async () => {
      const res = await api('/api/mcp/connect', { name: s.name });
      pushNotice(res.connected ? ('MCP ' + s.name + ' подключён (' + (res.tools || []).length + ' инстр.)') : ('MCP ' + s.name + ': ' + (res.error || 'ошибка')), res.connected ? 'ok' : 'warn');
      refreshState();
    };
    const del = node('button', 'ghost small-btn danger-text', '✕');
    del.type = 'button';
    del.onclick = async () => {
      await api('/api/mcp/remove', { name: s.name });
      refreshState();
    };
    row.append(reconn, del);
    list.append(row);
  });
}

if ($('#mcpAddBtn')) {
  $('#mcpAddBtn').onclick = async () => {
    const name = ($('#mcpNameInput') && $('#mcpNameInput').value.trim()) || '';
    const command = ($('#mcpCmdInput') && $('#mcpCmdInput').value.trim()) || '';
    const args = ($('#mcpArgsInput') && $('#mcpArgsInput').value.trim()) || '';
    const msg = $('#mcpStatusMsg');
    if (!name || !command) {
      if (msg) msg.textContent = 'Укажите имя и команду MCP-сервера';
      return;
    }
    if (msg) msg.textContent = 'Подключение MCP ' + name + '…';
    try {
      const res = await api('/api/mcp/add', { name, command, args, connect: true });
      if (res.ok) {
        $('#mcpNameInput').value = '';
        $('#mcpCmdInput').value = '';
        $('#mcpArgsInput').value = '';
        if (msg) msg.textContent = res.connected
          ? ('✅ Подключён: ' + (res.tools || []).length + ' инструментов')
          : ('⚠️ Добавлен, но не подключён: ' + (res.error || ''));
        refreshState();
      } else if (msg) {
        msg.textContent = '❌ ' + (res.error || 'Ошибка');
      }
    } catch (err) {
      if (msg) msg.textContent = '❌ ' + err.message;
    }
  };
}

/* ---------- Voice Engine 2.0 (Edge Neural / XTTS v2 / SAPI5) & Voice Call Modal ---------- */
let voiceAudioEl = null;
let voiceCallActive = false;
let voiceCallMuted = false;
let voiceCallTimer = null;
let voiceCallStartedAt = 0;
let voiceCallRec = null;
let voiceCallRecording = false;
let _callSpeechQueue = [];
let _callSpeechPlaying = false;
let _callStreamBuf = '';
let _callSpokenAnyChunk = false;
let _orbAudioCtx = null;
let _orbRaf = null;

function fillVoicePresets(presets, currentId) {
  const list = Array.isArray(presets) ? presets : [];
  ['#voicePresetSelect', '#callVoiceSelect'].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = '';
    list.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      el.appendChild(opt);
    });
    if (currentId && list.some((p) => p.id === currentId)) el.value = currentId;
  });
}

function _stopOrbVisualizer() {
  if (_orbRaf) { cancelAnimationFrame(_orbRaf); _orbRaf = null; }
  const ring = document.querySelector('#callOrb .call-orb-ring');
  if (ring) ring.style.transform = '';
}

function _attachOrbVisualizer(audioEl) {
  _stopOrbVisualizer();
  if (!voiceCallActive || !window.AudioContext) return;
  try {
    if (!_orbAudioCtx) _orbAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (_orbAudioCtx.state === 'suspended') _orbAudioCtx.resume();
    const src = _orbAudioCtx.createMediaElementSource(audioEl);
    const analyser = _orbAudioCtx.createAnalyser();
    analyser.fftSize = 64;
    src.connect(analyser);
    analyser.connect(_orbAudioCtx.destination);
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const ring = document.querySelector('#callOrb .call-orb-ring');
    const tick = () => {
      if (!voiceAudioEl || voiceAudioEl !== audioEl) { _stopOrbVisualizer(); return; }
      analyser.getByteFrequencyData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i];
      const avg = sum / Math.max(1, buf.length);
      const scale = 1 + Math.min(0.42, avg / 260);
      if (ring) ring.style.transform = 'scale(' + scale.toFixed(3) + ')';
      _orbRaf = requestAnimationFrame(tick);
    };
    _orbRaf = requestAnimationFrame(tick);
  } catch (e) {}
}

function stopVoiceAudio() {
  _callSpeechQueue = [];
  _callSpeechPlaying = false;
  _callStreamBuf = '';
  _stopOrbVisualizer();
  if (voiceAudioEl) {
    try { voiceAudioEl.pause(); voiceAudioEl.src = ''; } catch (e) {}
    voiceAudioEl = null;
  }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
}

function _currentVoiceParams() {
  const voiceId = ($('#callVoiceSelect') && voiceCallActive && $('#callVoiceSelect').value) ||
    ($('#voicePresetSelect') && $('#voicePresetSelect').value) || 'ru_female_warm';
  const speed = voiceCallActive && $('#callSpeedSelect')
    ? parseFloat($('#callSpeedSelect').value || '1.0')
    : parseFloat(($('#voiceSpeedRange') && $('#voiceSpeedRange').value) || '1.0');
  const pitch = parseInt(($('#voicePitchRange') && $('#voicePitchRange').value) || '0', 10) || 0;
  return { voice: voiceId, speed, pitch };
}

async function speakWithServer(text, onEnd) {
  const clean = String(text || '').trim();
  if (!clean) { if (onEnd) onEnd(); return null; }
  stopVoiceAudio();
  const vp = _currentVoiceParams();
  try {
    const res = await api('/api/voice/speak', { text: clean, voice: vp.voice, speed: vp.speed, pitch: vp.pitch });
    if (res && res.ok && res.audio_b64) {
      const audio = new Audio(res.audio_b64);
      voiceAudioEl = audio;
      _attachOrbVisualizer(audio);
      audio.onended = () => { _stopOrbVisualizer(); voiceAudioEl = null; if (onEnd) onEnd(); };
      audio.onerror = () => { _stopOrbVisualizer(); voiceAudioEl = null; if (onEnd) onEnd(); };
      await audio.play();
      return res;
    }
  } catch (e) {
    console.warn('Server TTS fallback to WebSpeech:', e);
  }
  if ('speechSynthesis' in window) {
    const utter = new SpeechSynthesisUtterance(clean.slice(0, 1200));
    utter.lang = 'ru-RU';
    utter.onend = () => { if (onEnd) onEnd(); };
    utter.onerror = () => { if (onEnd) onEnd(); };
    window.speechSynthesis.speak(utter);
  } else if (onEnd) {
    onEnd();
  }
  return null;
}

async function _drainCallSpeechQueue(onAllDone) {
  if (_callSpeechPlaying) return;
  _callSpeechPlaying = true;
  while (_callSpeechQueue.length && voiceCallActive && !voiceCallMuted) {
    const chunkText = _callSpeechQueue.shift();
    const vp = _currentVoiceParams();
    try {
      const res = await api('/api/voice/speak', { text: chunkText, voice: vp.voice, speed: vp.speed, pitch: vp.pitch });
      if (!voiceCallActive || voiceCallMuted) break;
      if (res && res.ok && res.audio_b64) {
        setCallState('speaking', 'Агент говорит…');
        await new Promise((resolve) => {
          const audio = new Audio(res.audio_b64);
          voiceAudioEl = audio;
          _attachOrbVisualizer(audio);
          audio.onended = () => { _stopOrbVisualizer(); voiceAudioEl = null; resolve(); };
          audio.onerror = () => { _stopOrbVisualizer(); voiceAudioEl = null; resolve(); };
          audio.play().catch(() => resolve());
        });
      }
    } catch (e) {}
  }
  _callSpeechPlaying = false;
  if (!_callSpeechQueue.length && !running && voiceCallActive) {
    setCallState('idle', 'Готов к разговору');
    if (onAllDone) onAllDone();
    else if ($('#callHandsFree') && $('#callHandsFree').checked && !pendingCallAsk) {
      setTimeout(() => { if (voiceCallActive && !running && !pendingCallAsk && !_callSpeechPlaying) callStartListening(); }, 420);
    }
  }
}

function enqueueCallSpeech(sentence, onAllDone) {
  const s = String(sentence || '').replace(/\s+/g, ' ').trim();
  if (!s || voiceCallMuted || !voiceCallActive) return;
  _callSpokenAnyChunk = true;
  _callSpeechQueue.push(s);
  _drainCallSpeechQueue(onAllDone);
}

function _encodePcmToWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }
  return new Uint8Array(buf);
}

if ($('#voiceSpeedRange')) {
  $('#voiceSpeedRange').oninput = (e) => {
    if ($('#voiceSpeedVal')) $('#voiceSpeedVal').textContent = Number(e.target.value).toFixed(2).replace(/\.00$/, '.0') + '×';
  };
}
if ($('#voicePitchRange')) {
  $('#voicePitchRange').oninput = (e) => {
    const v = Number(e.target.value) || 0;
    if ($('#voicePitchVal')) $('#voicePitchVal').textContent = (v > 0 ? '+' : '') + v + 'Hz';
  };
}
if ($('#voiceRecordSampleBtn')) {
  let recStream = null;
  let recCtx = null;
  $('#voiceRecordSampleBtn').onclick = async () => {
    const btn = $('#voiceRecordSampleBtn');
    const stEl = $('#voiceSettingsStatus');
    if (recStream) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (stEl) stEl.textContent = '❌ Браузер не дал доступ к микрофону';
      return;
    }
    try {
      recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
      const src = recCtx.createMediaStreamSource(recStream);
      const proc = recCtx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      proc.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      src.connect(proc);
      proc.connect(recCtx.destination);
      let left = 7;
      btn.textContent = '⏹️ ' + left + 'с…';
      if (stEl) stEl.textContent = '🔴 Говорите чётко в микрофон (7 сек) для создания WAV-образца клона…';
      const iv = setInterval(() => {
        left--;
        if (left > 0) btn.textContent = '⏹️ ' + left + 'с…';
      }, 1000);
      await new Promise((r) => setTimeout(r, 7000));
      clearInterval(iv);
      proc.disconnect();
      src.disconnect();
      recStream.getTracks().forEach((t) => t.stop());
      const sr = recCtx.sampleRate || 24000;
      await recCtx.close();
      recStream = null;
      recCtx = null;
      btn.textContent = '🔴 Запись WAV';

      const totalLen = chunks.reduce((a, c) => a + c.length, 0);
      const merged = new Float32Array(totalLen);
      let pos = 0;
      chunks.forEach((c) => { merged.set(c, pos); pos += c.length; });
      const wavBytes = _encodePcmToWav(merged, sr);
      let bin = '';
      for (let i = 0; i < wavBytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, wavBytes.subarray(i, i + 0x8000));
      const name = ($('#voiceCloneName') && $('#voiceCloneName').value.trim()) || ('mic_clone_' + new Date().toTimeString().slice(0, 5).replace(':', ''));
      const res = await api('/api/voice/sample/upload', { name, data_b64: btoa(bin) });
      if (res && res.ok) {
        fillVoicePresets(res.presets, res.id);
        if (stEl) stEl.textContent = '✅ Записан и сохранён WAV-клон: ' + res.file + ' (' + res.size_kb + ' КБ)';
      } else if (stEl) {
        stEl.textContent = '❌ ' + ((res && res.error) || 'Ошибка сохранения');
      }
    } catch (err) {
      if (recStream) { recStream.getTracks().forEach((t) => t.stop()); recStream = null; }
      btn.textContent = '🔴 Запись WAV';
      if (stEl) stEl.textContent = '❌ Ошибка микрофона: ' + err.message;
    }
  };
}

if ($('#voiceSaveBtn')) {
  $('#voiceSaveBtn').onclick = async () => {
    const cfgPatch = {
      voice_engine: ($('#voiceEngineSelect') && $('#voiceEngineSelect').value) || 'auto',
      voice_language: ($('#voiceLangSelect') && $('#voiceLangSelect').value) || 'ru',
      voice_xtts_url: ($('#voiceXttsUrl') && $('#voiceXttsUrl').value.trim()) || 'http://127.0.0.1:8020',
      voice_preset: ($('#voicePresetSelect') && $('#voicePresetSelect').value) || 'ru_female_warm',
      voice_speed: parseFloat(($('#voiceSpeedRange') && $('#voiceSpeedRange').value) || '1.0') || 1.0,
      voice_pitch: parseInt(($('#voicePitchRange') && $('#voicePitchRange').value) || '0', 10) || 0,
    };
    await api('/api/config', { config: cfgPatch });
    const st = await api('/api/voice/status');
    const stEl = $('#voiceSettingsStatus');
    if (stEl) {
      stEl.textContent = st.xtts_alive
        ? ('✅ XTTS v2 активен (' + st.xtts_url + ')')
        : (st.edge_available ? '✅ Активен студийный Microsoft Edge Neural TTS (24 кГц)' : 'ℹ️ Активен встроенный офлайн Windows SAPI5');
    }
    refreshState();
  };
}
if ($('#voiceTestSpeakBtn')) {
  $('#voiceTestSpeakBtn').onclick = async () => {
    const stEl = $('#voiceSettingsStatus');
    if (stEl) stEl.textContent = '🔊 Синтез тестовой фразы…';
    const res = await speakWithServer('Привет! Голосовой движок LocalHarness готов к работе.');
    if (stEl) stEl.textContent = res ? ('✅ Озвучено через ' + res.engine_used) : '✅ Озвучено через браузер';
  };
}
if ($('#voiceCloneFile')) {
  $('#voiceCloneFile').onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const stEl = $('#voiceSettingsStatus');
    if (stEl) stEl.textContent = 'Загрузка WAV-образца…';
    try {
      const buf = await file.arrayBuffer();
      const bytes = new Uint8Array(buf);
      let bin = '';
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      const name = ($('#voiceCloneName') && $('#voiceCloneName').value.trim()) || file.name.replace(/\.wav$/i, '');
      const res = await api('/api/voice/sample/upload', { name, data_b64: btoa(bin) });
      if (res.ok) {
        fillVoicePresets(res.presets, res.id);
        if (stEl) stEl.textContent = '✅ Клон голоса сохранён: ' + res.file + ' (' + res.size_kb + ' КБ)';
        if ($('#voiceCloneName')) $('#voiceCloneName').value = '';
      } else if (stEl) {
        stEl.textContent = '❌ ' + (res.error || 'Ошибка загрузки');
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    } finally {
      e.target.value = '';
    }
  };
}

function setCallState(mode, label) {
  const orb = $('#callOrb');
  const core = $('#callOrbCore');
  const lbl = $('#callStateLabel');
  if (orb) {
    orb.classList.remove('listening', 'thinking', 'speaking');
    if (mode && mode !== 'idle') orb.classList.add(mode);
  }
  if (core) {
    core.textContent = mode === 'listening' ? '🎤' : (mode === 'thinking' ? '🧠' : (mode === 'speaking' ? '🔊' : '🎙️'));
  }
  if (lbl) lbl.textContent = label || 'Готов к разговору';
}

function pushCallSubtitle(role, text) {
  const box = $('#callSubtitles');
  if (!box) return;
  const cls = role === 'user' ? 'call-sub-user' : (role === 'ai' ? 'call-sub-ai' : 'call-sub-sys');
  const prefix = role === 'user' ? 'Вы: ' : (role === 'ai' ? 'Агент: ' : '');
  const line = node('div', 'call-sub-line ' + cls, esc(prefix + text));
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function updateCallTimer() {
  const el = $('#callTimer');
  if (!el || !voiceCallActive) return;
  const sec = Math.floor((Date.now() - voiceCallStartedAt) / 1000);
  const mm = String(Math.floor(sec / 60)).padStart(2, '0');
  const ss = String(sec % 60).padStart(2, '0');
  el.textContent = mm + ':' + ss;
}

let pendingCallAsk = null;

function setCallToolPill(text) {
  const pill = $('#callToolPill');
  if (!pill) return;
  if (text) {
    pill.textContent = text;
    pill.hidden = false;
  } else {
    pill.hidden = true;
    pill.textContent = '';
  }
}

function clearCallAsk() {
  pendingCallAsk = null;
  const band = $('#callAskBand');
  if (band) {
    band.innerHTML = '';
    band.hidden = true;
  }
}

async function respondCallAsk(value) {
  if (!pendingCallAsk || !sessionId) return;
  const key = pendingCallAsk.key;
  clearCallAsk();
  closeBand();
  try {
    await api('/api/respond', { session: sessionId, key, value });
    setCallState('thinking', 'Ответ передан агенту…');
  } catch (e) {
    pushCallSubtitle('sys', 'Ошибка ответа агенту: ' + e.message);
  }
}

function showCallAsk(ev) {
  const band = $('#callAskBand');
  if (!band) return;
  pendingCallAsk = ev;
  band.innerHTML = '';
  const isApprove = ev.type === 'approve';
  const msg = isApprove
    ? ('⚠️ Подтвердите действие: ' + (ev.tool || 'инструмент') + (ev.reason ? ' — ' + ev.reason : ''))
    : ('❓ Вопрос агента: ' + (ev.question || ''));
  band.append(node('div', null, esc(msg)));
  const acts = node('div', 'call-ask-actions');
  if (isApprove) {
    const yesBtn = node('button', 'primary small-btn', '✅ Разрешить');
    yesBtn.type = 'button';
    yesBtn.onclick = () => respondCallAsk({ allow: true });
    const noBtn = node('button', 'danger small-btn', '❌ Отклонить');
    noBtn.type = 'button';
    noBtn.onclick = () => respondCallAsk({ allow: false });
    acts.append(yesBtn, noBtn);
  } else {
    const opts = Array.isArray(ev.options) ? ev.options : [];
    opts.forEach((opt) => {
      const b = node('button', 'ghost small-btn', esc(opt));
      b.type = 'button';
      b.onclick = () => respondCallAsk({ text: opt });
      acts.append(b);
    });
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'field grow';
    inp.placeholder = 'Ответь текстом или голосом…';
    const sendB = node('button', 'primary small-btn', 'Ответить');
    sendB.type = 'button';
    sendB.onclick = () => { if (inp.value.trim()) respondCallAsk({ text: inp.value.trim() }); };
    inp.onkeydown = (e) => { if (e.key === 'Enter' && inp.value.trim()) respondCallAsk({ text: inp.value.trim() }); };
    acts.append(inp, sendB);
  }
  band.append(acts);
  band.hidden = false;
  if (!voiceCallMuted) {
    setCallState('speaking', isApprove ? 'Жду подтверждения…' : 'Агент спрашивает…');
    speakWithServer(msg, () => {
      if (voiceCallActive && pendingCallAsk && $('#callHandsFree') && $('#callHandsFree').checked) {
        setTimeout(() => { if (voiceCallActive && pendingCallAsk) callStartListening(); }, 350);
      }
    });
  }
}

function pushCallToolEvent(text, imagePath) {
  const box = $('#callSubtitles');
  if (!box) return;
  const line = node('div', 'call-sub-line call-sub-tool', esc(text));
  box.appendChild(line);
  if (imagePath) {
    const img = document.createElement('img');
    img.className = 'call-sub-img';
    img.src = '/shot?path=' + encodeURIComponent(imagePath);
    img.alt = 'скриншот';
    img.onclick = () => window.open(img.src, '_blank');
    box.appendChild(img);
  }
  box.scrollTop = box.scrollHeight;
}

function callSendPrompt(text) {
  const clean = String(text || '').trim();
  if (!clean) return;
  // Если агент ждёт подтверждения или ответа на вопрос — отвечаем прямо в /api/respond!
  if (pendingCallAsk) {
    pushCallSubtitle('user', clean);
    if (pendingCallAsk.type === 'approve') {
      const low = clean.toLowerCase();
      const allow = /\b(да|разрешаю|можно|ок|окей|подтверждаю|выполняй|yes|allow|ok)\b/.test(low) &&
        !/\b(нет|нельзя|отмена|запрещаю|no|deny|cancel)\b/.test(low);
      respondCallAsk({ allow });
    } else {
      respondCallAsk({ text: clean });
    }
    return;
  }
  _callStreamBuf = '';
  _callSpokenAnyChunk = false;
  pushCallSubtitle('user', clean);
  setCallState('thinking', 'Агент работает…');
  if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = false;
  if (!running) {
    const wantGoal = $('#callGoalToggle') && $('#callGoalToggle').checked;
    if (wantGoal && $('#goalMode')) {
      $('#goalMode').checked = true;
    }
    $('#input').value = clean;
    $('#send').click();
    if (wantGoal && $('#callGoalToggle')) {
      $('#callGoalToggle').checked = false;
      if ($('#goalMode')) $('#goalMode').checked = false;
    }
  }
}

function onVoiceCallEvent(ev) {
  if (!voiceCallActive || !ev) return;
  switch (ev.type) {
    case 'delta':
      if (!voiceCallMuted && $('#callStreamToggle') && $('#callStreamToggle').checked && ev.text) {
        _callStreamBuf += ev.text;
        const m = _callStreamBuf.match(/^([\s\S]{24,}?[.!?…])(\s+[\s\S]*)$/);
        if (m) {
          const readySentence = m[1];
          _callStreamBuf = m[2].trimStart();
          enqueueCallSpeech(readySentence);
        }
      }
      break;
    case 'tool_start':
      setCallState('thinking', 'Выполняю инструмент…');
      setCallToolPill('⚙️ ' + (ev.tool || 'tool') + '…');
      if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = false;
      break;
    case 'tool':
      setCallToolPill('');
      pushCallToolEvent((ev.ok ? '✅ ' : '❌ ') + (ev.tool || 'tool'), ev.image);
      break;
    case 'plan':
      pushCallToolEvent('📋 ' + (ev.text || ''));
      break;
    case 'goal':
      if (ev.state === 'started') pushCallToolEvent('🎯 Цель запущена: ' + (ev.objective || '').slice(0, 100));
      else if (ev.state === 'closed') pushCallToolEvent((ev.achieved ? '🏆 Цель достигнута: ' : '🎯 Цель закрыта: ') + (ev.summary || '').slice(0, 120));
      break;
    case 'question':
    case 'approve':
      setCallToolPill('');
      showCallAsk(ev);
      break;
    case 'error':
      setCallToolPill('');
      clearCallAsk();
      if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = true;
      pushCallSubtitle('sys', '❌ ' + (ev.text || 'Ошибка'));
      setCallState('idle', 'Ошибка — попробуй снова');
      break;
    case 'stopped':
      setCallToolPill('');
      clearCallAsk();
      if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = true;
      pushCallSubtitle('sys', '🛑 Агент остановлен');
      setCallState('idle', 'Остановлено');
      break;
    case 'done':
      setCallToolPill('');
      clearCallAsk();
      if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = true;
      break;
    default:
      break;
  }
}

async function openVoiceCall() {
  const dlg = $('#voiceCallDialog');
  if (!dlg) return;
  voiceCallActive = true;
  voiceCallMuted = false;
  voiceCallStartedAt = Date.now();
  clearCallAsk();
  setCallToolPill('');
  if ($('#callStopAgentBtn')) $('#callStopAgentBtn').disabled = !running;
  if ($('#callMuteBtn')) {
    $('#callMuteBtn').classList.remove('muted');
    $('#callMuteBtn').textContent = '🔊';
  }
  if ($('#callPersonaSelect') && $('#personaSelect')) {
    $('#callPersonaSelect').innerHTML = $('#personaSelect').innerHTML;
    $('#callPersonaSelect').value = $('#personaSelect').value;
  }
  setCallState('idle', 'Подключение голосового движка…');
  if (typeof dlg.showModal === 'function' && !dlg.open) dlg.showModal();
  if (voiceCallTimer) clearInterval(voiceCallTimer);
  voiceCallTimer = setInterval(updateCallTimer, 1000);
  updateCallTimer();
  try {
    const st = await api('/api/voice/status');
    fillVoicePresets(st.presets, st.voice_preset);
    if ($('#callEngineMeta')) {
      const toolsCnt = (stateCache && stateCache.tools ? stateCache.tools.length : 0);
      const engLabel = st.xtts_alive
        ? 'XTTS v2 онлайн'
        : (st.edge_available ? 'Edge Neural 24kHz' : 'Windows SAPI5 (офлайн)');
      $('#callEngineMeta').textContent = engLabel + ' · ' + st.voice_language.toUpperCase() +
        (toolsCnt ? (' · ' + toolsCnt + ' инстр.') : '');
    }
    setCallState('idle', 'Нажми 🎤 или напиши — доступны все функции');
  } catch (e) {
    setCallState('idle', 'Готов к разговору');
  }
}

function closeVoiceCall() {
  voiceCallActive = false;
  clearCallAsk();
  setCallToolPill('');
  stopVoiceAudio();
  if (voiceCallRec && voiceCallRecording) {
    try { voiceCallRec.stop(); } catch (e) {}
  }
  voiceCallRecording = false;
  if (voiceCallTimer) { clearInterval(voiceCallTimer); voiceCallTimer = null; }
  const dlg = $('#voiceCallDialog');
  if (dlg && dlg.open) dlg.close();
}

function callStartListening() {
  if (!voiceCallActive) return;
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    pushCallSubtitle('sys', 'Распознавание речи не поддерживается браузером (используй Chrome/Edge или текстовое поле ниже).');
    return;
  }
  if (voiceCallRecording && voiceCallRec) {
    voiceCallRec.stop();
    return;
  }
  stopVoiceAudio();
  const rec = new SpeechRec();
  const lang = ($('#voiceLangSelect') && $('#voiceLangSelect').value) || 'ru';
  rec.lang = lang === 'en' ? 'en-US' : 'ru-RU';
  rec.interimResults = true;
  rec.continuous = false;
  voiceCallRec = rec;
  voiceCallRecording = true;
  if ($('#callMicBtn')) $('#callMicBtn').classList.add('recording');
  if ($('#callLiveInterim')) { $('#callLiveInterim').hidden = true; $('#callLiveInterim').textContent = ''; }
  setCallState('listening', pendingCallAsk ? 'Слушаю ваш ответ…' : 'Слушаю вас (говорите)…');

  let captured = '';
  rec.onresult = (ev) => {
    let finalChunk = '';
    let interimChunk = '';
    for (let i = 0; i < ev.results.length; i++) {
      if (ev.results[i].isFinal) finalChunk += ev.results[i][0].transcript;
      else interimChunk += ev.results[i][0].transcript;
    }
    captured = finalChunk || interimChunk;
    if ($('#callLiveInterim')) {
      const liveTxt = (finalChunk + ' ' + interimChunk).trim();
      $('#callLiveInterim').textContent = liveTxt ? ('🎙️ «' + liveTxt + '»') : '';
      $('#callLiveInterim').hidden = !liveTxt;
    }
  };
  rec.onerror = (ev) => {
    if ($('#callLiveInterim')) $('#callLiveInterim').hidden = true;
    setCallState('idle', 'Микрофон: ' + (ev.error || 'пауза'));
  };
  rec.onend = async () => {
    voiceCallRecording = false;
    if ($('#callMicBtn')) $('#callMicBtn').classList.remove('recording');
    if ($('#callLiveInterim')) $('#callLiveInterim').hidden = true;
    const text = captured.trim();
    if (!text) {
      if (voiceCallActive) setCallState('idle', 'Ничего не услышал — нажми 🎤 или Пробел');
      return;
    }
    callSendPrompt(text);
  };
  rec.start();
}

function onVoiceCallAnswer(rawText) {
  if (!voiceCallActive) return false;
  const clean = String(rawText || '').trim();
  if (!clean) return false;
  pushCallSubtitle('ai', clean.slice(0, 420));
  if (voiceCallMuted) {
    setCallState('idle', 'Звук выключен (Mute)');
    return true;
  }
  if (_callSpokenAnyChunk) {
    const rem = _callStreamBuf.trim();
    _callStreamBuf = '';
    if (rem) enqueueCallSpeech(rem);
    else if (!_callSpeechPlaying) {
      setCallState('idle', 'Готов к разговору');
      if ($('#callHandsFree') && $('#callHandsFree').checked && !pendingCallAsk) {
        setTimeout(() => { if (voiceCallActive && !running && !pendingCallAsk) callStartListening(); }, 420);
      }
    }
    return true;
  }
  setCallState('speaking', 'Агент говорит…');
  speakWithServer(clean, () => {
    if (!voiceCallActive) return;
    setCallState('idle', 'Готов к разговору');
    if ($('#callHandsFree') && $('#callHandsFree').checked) {
      setTimeout(() => { if (voiceCallActive && !running && !pendingCallAsk) callStartListening(); }, 450);
    }
  });
  return true;
}

if ($('#voiceCallBtn')) $('#voiceCallBtn').onclick = openVoiceCall;
if ($('#voiceOpenCallFromDrawer')) $('#voiceOpenCallFromDrawer').onclick = () => {
  if ($('#drawerClose')) $('#drawerClose').click();
  openVoiceCall();
};
if ($('#callCloseBtn')) $('#callCloseBtn').onclick = closeVoiceCall;
if ($('#callEndBtn')) $('#callEndBtn').onclick = closeVoiceCall;
if ($('#callMicBtn')) $('#callMicBtn').onclick = callStartListening;
if ($('#callInterruptBtn')) $('#callInterruptBtn').onclick = () => {
  stopVoiceAudio();
  setCallState('idle', 'Речь прервана');
};
if ($('#callMuteBtn')) $('#callMuteBtn').onclick = () => {
  voiceCallMuted = !voiceCallMuted;
  $('#callMuteBtn').classList.toggle('muted', voiceCallMuted);
  $('#callMuteBtn').textContent = voiceCallMuted ? '🔇' : '🔊';
  if (voiceCallMuted) stopVoiceAudio();
};
if ($('#callVoiceSelect')) {
  $('#callVoiceSelect').onchange = (e) => {
    const val = e.target.value;
    if ($('#voicePresetSelect')) $('#voicePresetSelect').value = val;
    api('/api/config', { config: { voice_preset: val } }).catch(() => {});
  };
}
if ($('#callPersonaSelect')) {
  $('#callPersonaSelect').onchange = async (e) => {
    const persona = e.target.value || 'default';
    if ($('#personaSelect')) $('#personaSelect').value = persona;
    await api('/api/config', { config: { persona } }).catch(() => {});
    pushCallToolEvent('🎭 Персона: ' + e.target.options[e.target.selectedIndex].text);
  };
}
if ($('#callScreenBtn')) {
  $('#callScreenBtn').onclick = () => {
    if (running) return;
    callSendPrompt('Сделай скриншот моего экрана через screenshot и кратко опиши, что на нём сейчас происходит.');
  };
}
if ($('#callAttachBtn')) {
  $('#callAttachBtn').onclick = () => {
    if ($('#attachFile')) $('#attachFile').click();
    pushCallToolEvent('🖼️ Выбери изображение — оно прикрепится к следующему запросу в звонке');
  };
}
if ($('#callStopAgentBtn')) {
  $('#callStopAgentBtn').onclick = async () => {
    stopVoiceAudio();
    if (sessionId && running) {
      await api('/api/stop', { session: sessionId }).catch(() => {});
    }
    setCallToolPill('');
    setCallState('idle', 'Остановлено');
  };
}
if ($('#callTextSendBtn')) {
  $('#callTextSendBtn').onclick = () => {
    const inp = $('#callTextInput');
    if (!inp || !inp.value.trim()) return;
    const val = inp.value.trim();
    inp.value = '';
    callSendPrompt(val);
  };
}
if ($('#callTextInput')) {
  $('#callTextInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.value.trim()) {
      e.preventDefault();
      const val = e.target.value.trim();
      e.target.value = '';
      callSendPrompt(val);
    }
  });
}

function syncExtendedState(st) {
  if (!st) return;
  if (st.version && $('#updateVerChip')) $('#updateVerChip').textContent = 'v' + st.version;
  if (st.theme && !localStorage.getItem('lh.theme')) applyTheme(st.theme, false);
  if ($('#personaSelect') && st.personas) {
    const sel = $('#personaSelect');
    if (sel.options.length !== Object.keys(st.personas).length) {
      sel.innerHTML = '';
      Object.keys(st.personas).forEach((k) => {
        const p = st.personas[k];
        const opt = document.createElement('option');
        opt.value = k;
        opt.textContent = (p.icon ? p.icon + ' ' : '') + (p.name || k);
        sel.appendChild(opt);
      });
    }
    sel.value = st.persona || 'default';
  }
  if ($('#toastNotifToggle')) $('#toastNotifToggle').checked = st.toast_notifications !== false;
  if (st.rag) renderRagState(st.rag);
  if (st.mcp_servers) renderMcpState(st.mcp_servers);
  if (st.voice) {
    if ($('#voiceEngineChip')) $('#voiceEngineChip').textContent = st.voice.engine || 'auto';
    if ($('#voiceEngineSelect')) $('#voiceEngineSelect').value = st.voice.engine || 'auto';
    if ($('#voiceLangSelect')) $('#voiceLangSelect').value = st.voice.voice_language || 'ru';
    if ($('#voiceSpeedRange') && st.voice.voice_speed != null) {
      $('#voiceSpeedRange').value = st.voice.voice_speed;
      if ($('#voiceSpeedVal')) $('#voiceSpeedVal').textContent = Number(st.voice.voice_speed).toFixed(2).replace(/\.00$/, '.0') + '×';
    }
    if ($('#voicePitchRange') && st.voice.voice_pitch != null) {
      $('#voicePitchRange').value = st.voice.voice_pitch;
      const pv = Number(st.voice.voice_pitch) || 0;
      if ($('#voicePitchVal')) $('#voicePitchVal').textContent = (pv > 0 ? '+' : '') + pv + 'Hz';
    }
    if ($('#voiceXttsUrl') && document.activeElement !== $('#voiceXttsUrl')) {
      $('#voiceXttsUrl').value = st.voice.xtts_url || 'http://127.0.0.1:8020';
    }
    if (st.voice.presets) fillVoicePresets(st.voice.presets, st.voice.voice_preset);
  }
}

const AUTO_GOAL_RE = /(?:^\s*\/goal\b|поставь(?:\s+себе)?\s+цель|задай\s+цель|установи\s+цель|новая\s+цель|включи\s+режим\s+цели|в\s+режиме\s+цели|режим\s+цели\s*[:—\-]|^\s*цель\s*[:—\-]|set\s+(?:a\s+)?goal)/i;
if ($('#input')) {
  $('#input').addEventListener('input', () => {
    const hint = $('#modeHint');
    if (!hint) return;
    const val = $('#input').value || '';
    if (AUTO_GOAL_RE.test(val)) {
      hint.textContent = '🎯 Режим цели (авто)';
      hint.style.color = 'var(--ok-soft)';
    } else if (hint.textContent === '🎯 Режим цели (авто)') {
      hint.textContent = '';
      hint.style.color = '';
    }
  });
}

/* ---------- LoRA & Unsloth Fine-Tuning Studio 3.0 ---------- */
let trainTimer = null;

function trainCollectParams() {
  const custom = ($('#trainModelCustom') && $('#trainModelCustom').value.trim()) || '';
  const preset = ($('#trainModelPreset') && $('#trainModelPreset').value) || 'unsloth/Qwen3-4B-bnb-4bit';
  const checkedLayers = Array.from(document.querySelectorAll('#trainLayersChecks input[type="checkbox"]:checked')).map((el) => el.value);
  return {
    backend: ($('#trainBackend') && $('#trainBackend').value) || 'auto',
    model: custom || preset,
    method: ($('#trainMethod') && $('#trainMethod').value) || 'sft',
    dataset_path: ($('#trainDatasetSelect') && $('#trainDatasetSelect').value) || '',
    lora_r: Number($('#trainLoraR') && $('#trainLoraR').value) || 16,
    lora_alpha: Number($('#trainLoraAlpha') && $('#trainLoraAlpha').value) || 16,
    max_steps: Number($('#trainMaxSteps') && $('#trainMaxSteps').value) || 60,
    epochs: Number($('#trainEpochs') && $('#trainEpochs').value) || 1,
    batch_size: Number($('#trainBatchSize') && $('#trainBatchSize').value) || 2,
    grad_accum: Number($('#trainGradAccum') && $('#trainGradAccum').value) || 4,
    learning_rate: parseFloat(($('#trainLr') && $('#trainLr').value) || '0.0002') || 0.0002,
    max_seq_length: Number($('#trainMaxSeq') && $('#trainMaxSeq').value) || 2048,
    chat_template: ($('#trainChatTemplate') && $('#trainChatTemplate').value) || 'alpaca',
    lr_scheduler: ($('#trainScheduler') && $('#trainScheduler').value) || 'cosine',
    use_rslora: $('#trainUseRslora') ? !!$('#trainUseRslora').checked : false,
    packing: $('#trainPacking') ? !!$('#trainPacking').checked : false,
    thermal_guard: $('#trainThermalGuard') ? !!$('#trainThermalGuard').checked : true,
    gguf_quant: ($('#trainGgufQuant') && $('#trainGgufQuant').value) || 'q4_k_m',
    export_gguf: $('#trainExportGguf') ? !!$('#trainExportGguf').checked : true,
    auto_import_lmstudio: $('#trainAutoImport') ? !!$('#trainAutoImport').checked : true,
    target_modules: checkedLayers.length ? checkedLayers : undefined,
  };
}

function renderTrainLossChart(points) {
  const svg = $('#trainLossChart');
  if (!svg) return;
  const pts = Array.isArray(points) ? points : [];
  if (pts.length < 2) {
    svg.innerHTML = '<text x="300" y="64" text-anchor="middle" fill="var(--text-cap)" font-size="13">График Loss появится автоматически после первых шагов обучения</text>';
    return;
  }
  const losses = pts.map((p) => Number(p.loss)).filter((v) => !isNaN(v));
  const minL = Math.min(...losses);
  const maxL = Math.max(...losses);
  const span = Math.max(0.001, maxL - minL);
  const W = 600, H = 120, padX = 12, padY = 14;
  const coords = pts.map((p, idx) => {
    const x = padX + (idx / Math.max(1, pts.length - 1)) * (W - padX * 2);
    const y = padY + (1 - ((Number(p.loss) - minL) / span)) * (H - padY * 2);
    return [x.toFixed(1), y.toFixed(1)];
  });
  const poly = coords.map((c) => c.join(',')).join(' ');
  const area = padX + ',' + (H - padY) + ' ' + poly + ' ' + coords[coords.length - 1][0] + ',' + (H - padY);
  const last = coords[coords.length - 1];
  svg.innerHTML =
    '<line x1="0" y1="' + padY + '" x2="' + W + '" y2="' + padY + '" stroke="rgba(255,255,255,0.08)" stroke-dasharray="4 4"/>' +
    '<line x1="0" y1="' + (H - padY) + '" x2="' + W + '" y2="' + (H - padY) + '" stroke="rgba(255,255,255,0.08)" stroke-dasharray="4 4"/>' +
    '<polygon points="' + area + '" fill="rgba(86,134,254,0.16)"/>' +
    '<polyline fill="none" stroke="var(--accent)" stroke-width="2.5" points="' + poly + '"/>' +
    '<circle cx="' + last[0] + '" cy="' + last[1] + '" r="4" fill="var(--ok)"/>' +
    '<text x="8" y="13" fill="var(--text-cap)" font-size="11">max ' + maxL.toFixed(3) + '</text>' +
    '<text x="8" y="' + (H - 3) + '" fill="var(--text-cap)" font-size="11">min ' + minL.toFixed(3) + '</text>' +
    '<text x="' + (W - 8) + '" y="13" text-anchor="end" fill="var(--ok-soft)" font-size="11">step ' + pts[pts.length - 1].step + ' · loss ' + Number(pts[pts.length - 1].loss).toFixed(4) + '</text>';
}

async function updateTrainVramEstimate() {
  const chip = $('#trainVramChip');
  if (!chip) return;
  try {
    const p = trainCollectParams();
    const res = await api('/api/train/estimate', p);
    if (!res || !res.ok) return;
    chip.className = 'sec-chip ' + (res.fit === 'ok' ? 'ok' : (res.fit === 'tight' ? 'warn' : 'err'));
    chip.textContent = '~' + res.estimated_vram_gb + ' / ' + (res.gpu_vram_gb || '?') + ' ГБ VRAM';
    chip.title = res.note || '';
  } catch (e) {}
}

async function refreshTrainEnv(force) {
  const chip = $('#trainEnvChip');
  const sum = $('#trainEnvSummary');
  if (chip) chip.textContent = 'проверка…';
  try {
    const env = await api('/api/train/env' + (force ? '?force=1' : ''));
    if (!env) return;
    if (chip) {
      chip.className = 'sec-chip ' + (env.ready ? 'ok' : 'warn');
      chip.textContent = env.ready ? 'готово (CUDA + Unsloth)' : 'нужна установка';
    }
    if (sum) {
      const p = env.packages || {};
      const gpuStr = env.has_gpu ? (env.gpu_name + ' (' + env.vram_gb + ' ГБ VRAM)') : 'GPU не найдена';
      sum.textContent =
        'GPU: ' + gpuStr +
        ' · PyTorch: ' + (p.torch || 'нет') + (p.cuda ? ' (CUDA ' + (p.cuda_ver || '') + ')' : ' (без CUDA)') +
        ' · Unsloth: ' + (p.unsloth || 'не установлен') +
        (env.using_venv ? ' · venv активен' : '') +
        ' — ' + (env.hint || '');
    }
    if ($('#trainInstallBtn')) {
      $('#trainInstallBtn').textContent = env.ready
        ? '🔄 Обновить Unsloth в изолированном venv'
        : '⬇️ Установить Unsloth + PyTorch CUDA (в изолированный venv)';
    }
    updateTrainVramEstimate();
  } catch (e) {
    if (sum) sum.textContent = 'Ошибка диагностики: ' + e.message;
  }
}

async function refreshTrain(checkEnv) {
  if (checkEnv) refreshTrainEnv(false);
  try {
    const data = await api('/api/train/status');
    if (!data || !data.ok) return;

    const mSel = $('#trainModelPreset');
    const localModels = data.local_models || [];
    const presetSig = (data.presets || []).length + ':' + localModels.length;
    if (mSel && data.presets && mSel.dataset.sig !== presetSig) {
      const prevM = mSel.value;
      mSel.dataset.sig = presetSig;
      mSel.innerHTML = '';
      if (localModels.length) {
        const gLoc = document.createElement('optgroup');
        gLoc.label = 'Скачанные локальные модели (data/models)';
        localModels.forEach((lm) => {
          const opt = document.createElement('option');
          opt.value = lm.path;
          opt.textContent = '💾 ' + lm.name + ' (' + lm.size_gb + ' ГБ)';
          gLoc.appendChild(opt);
        });
        mSel.appendChild(gLoc);
      }
      const gPre = document.createElement('optgroup');
      gPre.label = 'Рекомендованные пресеты (Unsloth / HF)';
      data.presets.forEach((m) => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.label + ' · ~' + m.vram_gb + ' ГБ';
        gPre.appendChild(opt);
      });
      mSel.appendChild(gPre);
      if (prevM) mSel.value = prevM;
      updateTrainVramEstimate();
    }
    if ($('#trainQuantChip')) {
      $('#trainQuantChip').className = 'sec-chip ' + (data.llama_quantize_available ? 'ok' : 'warn');
      $('#trainQuantChip').textContent = data.llama_quantize_available ? 'llama-quantize готов' : 'llama-quantize не найден';
    }

    const dsRows = data.datasets || [];
    const hfRows = data.hf_datasets || [];
    const dsList = $('#trainDatasetsList');
    const dsSel = $('#trainDatasetSelect');
    const prevDs = dsSel ? dsSel.value : '';
    if (dsSel) {
      dsSel.innerHTML = '';
      if (dsRows.length) {
        const g1 = document.createElement('optgroup');
        g1.label = 'Локальные датасеты (data/datasets)';
        dsRows.forEach((d) => {
          const opt = document.createElement('option');
          opt.value = d.path;
          opt.textContent = '📄 ' + d.name + ' (' + d.rows + ' прим., ' + d.size_kb + ' КБ)';
          g1.appendChild(opt);
        });
        dsSel.appendChild(g1);
      }
      if (hfRows.length) {
        const g2 = document.createElement('optgroup');
        g2.label = 'HuggingFace Hub (скачаются сами)';
        hfRows.forEach((h) => {
          const opt = document.createElement('option');
          opt.value = h.id;
          opt.textContent = h.label;
          g2.appendChild(opt);
        });
        dsSel.appendChild(g2);
      }
      if (prevDs) {
        const allVals = dsRows.map((d) => d.path).concat(hfRows.map((h) => h.id));
        if (allVals.includes(prevDs)) dsSel.value = prevDs;
      }
    }
    if (dsList) {
      dsList.innerHTML = '';
      if (!dsRows.length) {
        dsList.append(node('div', 'page-sub', 'Локальных датасетов пока нет — собери из чатов, загрузи файл, сгенерируй ИИ ниже или выбери HuggingFace датасет в п.3.'));
      } else {
        dsRows.forEach((d) => {
          const row = node('div', 'cllm-row');
          const info = node('div', 'cllm-info');
          info.append(node('div', 'cllm-title', '📄 ' + esc(d.name)));
          info.append(node('div', 'cllm-sub', d.rows + ' примеров · ' + d.size_kb + ' КБ'));
          row.append(info);
          const acts = node('div', 'sched-actions');
          const prevBtn = node('button', 'ghost small-btn', '👁️ Просмотр');
          prevBtn.type = 'button';
          prevBtn.onclick = async () => {
            const pv = await api('/api/train/dataset/preview?name=' + encodeURIComponent(d.name));
            if (pv && pv.ok && $('#codeDialog')) {
              openCodeDialog('Датасет: ' + pv.name, pv.total + ' строк' + (pv.bad_lines ? (' · битых: ' + pv.bad_lines) : ''));
              $('#codeDialogText').textContent = JSON.stringify(pv.samples, null, 2);
            }
          };
          const cleanBtn = node('button', 'ghost small-btn', '🧹 Очистка');
          cleanBtn.type = 'button';
          cleanBtn.title = 'Проверить JSONL, удалить дубликаты и посчитать статистику токенов';
          cleanBtn.onclick = async () => {
            const cr = await api('/api/train/dataset/clean', { name: d.name });
            if (cr && cr.ok) {
              pushNotice('🧹 ' + cr.name + ' (' + cr.format + '): ' + cr.before + ' → ' + cr.after + ' строк (дублей: ' + cr.removed_duplicates + ', ср. ~' + cr.avg_tokens + ' ток.)', 'ok');
              refreshTrain(false);
            } else {
              pushNotice((cr && cr.error) || 'Ошибка очистки датасета', 'bad');
            }
          };
          const delBtn = node('button', 'ghost small-btn danger-text', '✕');
          delBtn.type = 'button';
          delBtn.onclick = async () => {
            await api('/api/train/dataset/delete', { name: d.name });
            refreshTrain(false);
          };
          acts.append(prevBtn, cleanBtn, delBtn);
          row.append(acts);
          dsList.append(row);
        });
      }
    }

    const job = data.job || {};
    const isRun = job.status === 'running';
    if ($('#trainJobChip')) {
      $('#trainJobChip').className = 'sec-chip ' + (isRun ? 'warn' : (job.status === 'done' ? 'ok' : (job.status === 'failed' ? 'err' : '')));
      $('#trainJobChip').textContent = job.status || 'idle';
    }
    if ($('#trainStartBtn')) $('#trainStartBtn').disabled = isRun;
    if ($('#trainInstallBtn')) $('#trainInstallBtn').disabled = isRun;
    if ($('#trainStopBtn')) $('#trainStopBtn').disabled = !isRun;
    if ($('#trainMetStep')) $('#trainMetStep').textContent = (job.step || 0) + ' / ' + (job.max_steps || 0);
    if ($('#trainMetLoss')) $('#trainMetLoss').textContent = job.loss != null ? String(job.loss) : '—';
    if ($('#trainMetEpoch')) $('#trainMetEpoch').textContent = job.epoch != null ? String(job.epoch) : '0.0';
    if ($('#trainMetSpeed')) $('#trainMetSpeed').textContent = job.speed_it_s != null ? (job.speed_it_s + ' шаг/с') : '—';
    if ($('#trainMetEta')) $('#trainMetEta').textContent = job.eta_min != null ? (job.eta_min + ' мин') : '—';
    if ($('#trainMetGpu')) $('#trainMetGpu').textContent = job.gpu_temp != null ? (job.gpu_temp + '°C') : '—';
    const pct = job.max_steps > 0 ? Math.min(100, Math.round(((job.step || 0) / job.max_steps) * 100)) : (job.status === 'done' ? 100 : 0);
    if ($('#trainProgressFill')) $('#trainProgressFill').style.width = pct + '%';
    renderTrainLossChart(job.loss_history || []);
    const logEl = $('#trainLogBox');
    if (logEl && job.lines && job.lines.length) {
      logEl.textContent = job.lines.join('\n');
      logEl.scrollTop = logEl.scrollHeight;
    }

    const hList = $('#trainHistoryList');
    if (hList) {
      hList.innerHTML = '';
      const hist = data.history || [];
      if (!hist.length) {
        hList.append(node('div', 'page-sub', 'Завершённых обучений пока нет.'));
      } else {
        hist.forEach((h) => {
          const row = node('div', 'cllm-row');
          const info = node('div', 'cllm-info');
          const ico = h.status === 'done' ? '✅' : (h.status === 'stopped' ? '⏹️' : '❌');
          info.append(node('div', 'cllm-title', ico + ' ' + esc(h.model || '') + ' (' + esc(String(h.method || '').toUpperCase()) + ')'));
          info.append(node('div', 'cllm-sub', 'датасет: ' + esc(h.dataset || '—') + ' · loss: ' + (h.loss != null ? h.loss : '—') + ' · ' + esc((h.gguf_files && h.gguf_files[0]) || h.output_dir || '')));
          row.append(info);
          const acts = node('div', 'sched-actions');
          if (h.output_dir) {
            const evalPick = node('button', 'ghost small-btn', '🧪 Тест');
            evalPick.type = 'button';
            evalPick.onclick = () => {
              if ($('#trainEvalDir')) $('#trainEvalDir').value = h.output_dir;
              if ($('#trainEvalPrompt')) $('#trainEvalPrompt').focus();
            };
            acts.append(evalPick);
          }
          if (h.gguf_files && h.gguf_files.length) {
            const impBtn = node('button', 'ghost small-btn', '📦 В LM Studio');
            impBtn.type = 'button';
            impBtn.onclick = async () => {
              const r = await api('/api/lms/import', { path: h.gguf_files[0], mode: 'copy' });
              pushNotice(r && r.ok ? '📦 Импорт GGUF в LM Studio запущен' : ((r && r.error) || 'Ошибка импорта'), r && r.ok ? 'ok' : 'bad');
            };
            acts.append(impBtn);
          }
          row.append(acts);
          hList.append(row);
        });
      }
    }

    if (trainTimer) clearTimeout(trainTimer);
    if (currentView === 'train') {
      trainTimer = setTimeout(() => refreshTrain(false), isRun ? 1800 : 6000);
    }
  } catch (e) {
    console.warn('train refresh failed:', e);
  }
}

['#trainModelPreset', '#trainModelCustom', '#trainMethod', '#trainLoraR', '#trainMaxSeq', '#trainBatchSize'].forEach((sel) => {
  const el = $(sel);
  if (el) el.addEventListener('change', updateTrainVramEstimate);
});

if ($('#trainAutotuneBtn')) {
  $('#trainAutotuneBtn').onclick = async () => {
    const p = trainCollectParams();
    const res = await api('/api/train/autotune', { model: p.model, method: p.method });
    if (res && res.ok && res.recommended) {
      const r = res.recommended;
      if ($('#trainBatchSize')) $('#trainBatchSize').value = r.batch_size;
      if ($('#trainGradAccum')) $('#trainGradAccum').value = r.grad_accum;
      if ($('#trainMaxSeq')) $('#trainMaxSeq').value = r.max_seq_length;
      if ($('#trainLoraR')) $('#trainLoraR').value = r.lora_r;
      if ($('#trainLoraAlpha')) $('#trainLoraAlpha').value = r.lora_alpha;
      if ($('#trainLr')) $('#trainLr').value = String(r.learning_rate);
      if ($('#trainScheduler')) $('#trainScheduler').value = r.lr_scheduler;
      if ($('#trainUseRslora')) $('#trainUseRslora').checked = !!r.use_rslora;
      if ($('#trainPacking')) $('#trainPacking').checked = !!r.packing;
      if ($('#trainGgufQuant')) $('#trainGgufQuant').value = r.gguf_quant;
      updateTrainVramEstimate();
      pushNotice('⚡ Параметры подобраны под GPU (' + res.gpu_vram_gb + ' ГБ VRAM): batch=' + r.batch_size + ', seq=' + r.max_seq_length + ', r=' + r.lora_r, 'ok');
    }
  };
}
if ($('#trainEnvCheckBtn')) $('#trainEnvCheckBtn').onclick = () => refreshTrainEnv(true);
if ($('#trainInstallBtn')) {
  $('#trainInstallBtn').onclick = async () => {
    try {
      const res = await api('/api/train/install', {});
      if (res && res.ok) {
        pushNotice('⬇️ Установка Unsloth + PyTorch CUDA запущена в data/unsloth_venv (смотри лог ниже)', 'ok');
        refreshTrain(false);
      } else {
        pushNotice((res && res.error) || 'Не удалось запустить установку', 'bad');
      }
    } catch (err) {
      pushNotice(err.message, 'bad');
    }
  };
}
if ($('#trainFromSessionsBtn')) {
  $('#trainFromSessionsBtn').onclick = async () => {
    try {
      const res = await api('/api/train/dataset/from_sessions', { name: 'sessions_sft.jsonl' });
      if (res && res.ok) {
        pushNotice('📥 Собрано ' + res.count + ' примеров из ' + res.sessions_scanned + ' сессий в ' + res.name, 'ok');
        refreshTrain(false);
      }
    } catch (err) {
      pushNotice(err.message, 'bad');
    }
  };
}
if ($('#trainDsUpload')) {
  $('#trainDsUpload').onchange = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const res = await api('/api/train/dataset/import', { filename: file.name, text });
      if (res && res.ok) {
        pushNotice('📂 Импортировано ' + res.count + ' примеров в ' + res.name, 'ok');
        refreshTrain(false);
      } else {
        pushNotice((res && res.error) || 'Ошибка импорта', 'bad');
      }
    } catch (err) {
      pushNotice(err.message, 'bad');
    } finally {
      e.target.value = '';
    }
  };
}
if ($('#trainSynthBtn')) {
  $('#trainSynthBtn').onclick = async () => {
    const topic = ($('#trainSynthTopic') && $('#trainSynthTopic').value.trim()) || '';
    const count = Number($('#trainSynthCount') && $('#trainSynthCount').value) || 10;
    const stEl = $('#trainSynthStatus');
    if (!topic) { pushNotice('Введи тему для генерации синтетического датасета', 'warn'); return; }
    if (stEl) stEl.textContent = '✨ Локальная модель генерирует ' + count + ' примеров по теме «' + topic + '»…';
    $('#trainSynthBtn').disabled = true;
    try {
      const res = await api('/api/train/dataset/synthesize', { topic, count, name: 'synthetic_sft.jsonl' });
      if (res && res.ok) {
        if (stEl) stEl.textContent = '✅ Сгенерировано ' + res.added + ' примеров в ' + res.name;
        $('#trainSynthTopic').value = '';
        refreshTrain(false);
      } else if (stEl) {
        stEl.textContent = '❌ ' + ((res && res.error) || 'Ошибка генерации');
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    } finally {
      $('#trainSynthBtn').disabled = false;
    }
  };
}
if ($('#trainDsAddBtn')) {
  $('#trainDsAddBtn').onclick = async () => {
    const name = ($('#trainDsAddName') && $('#trainDsAddName').value.trim()) || 'custom_sft.jsonl';
    const instruction = ($('#trainDsAddInst') && $('#trainDsAddInst').value.trim()) || '';
    const output = ($('#trainDsAddOut') && $('#trainDsAddOut').value.trim()) || '';
    if (!instruction || !output) { pushNotice('Заполни инструкцию и ответ', 'warn'); return; }
    const res = await api('/api/train/dataset/append', { name, instruction, output });
    if (res && res.ok) {
      $('#trainDsAddInst').value = '';
      $('#trainDsAddOut').value = '';
      pushNotice('✅ Пример добавлен в ' + res.name, 'ok');
      refreshTrain(false);
    }
  };
}
if ($('#trainScriptBtn')) {
  $('#trainScriptBtn').onclick = async () => {
    const params = trainCollectParams();
    const res = await api('/api/train/script', params);
    if (res && res.ok && $('#codeDialog')) {
      openCodeDialog('Скрипт обучения (' + (params.backend || 'auto') + ' · ' + params.method.toUpperCase() + ')', params.model);
      $('#codeDialogText').textContent = res.script || '';
    }
  };
}
if ($('#trainInspectLayersBtn')) {
  $('#trainInspectLayersBtn').onclick = async () => {
    const p = trainCollectParams();
    const box = $('#trainLayersBox');
    const wrap = $('#trainLayersChecks');
    const archEl = $('#trainLayersArch');
    if (!box || !wrap) return;
    box.hidden = false;
    wrap.innerHTML = '<span class="page-sub">🔍 Сканирование архитектуры слоёв ' + esc(p.model) + '…</span>';
    try {
      const res = await api('/api/train/layers', { model: p.model });
      wrap.innerHTML = '';
      if (!res || !res.ok) {
        wrap.innerHTML = '<span class="page-sub">❌ ' + esc((res && res.error) || 'Ошибка') + '</span>';
        return;
      }
      if (archEl) archEl.textContent = 'Слои LoRA (' + (res.architecture || 'auto') + ' · ' + (res.source || '') + '):';
      const recSet = new Set(res.recommended || []);
      (res.layers || []).forEach((layer) => {
        const lbl = document.createElement('label');
        lbl.className = 'check-pill';
        const chk = document.createElement('input');
        chk.type = 'checkbox';
        chk.value = layer;
        chk.checked = recSet.has(layer);
        const sp = document.createElement('span');
        sp.textContent = layer;
        lbl.append(chk, sp);
        wrap.appendChild(lbl);
      });
    } catch (err) {
      wrap.innerHTML = '<span class="page-sub">❌ ' + esc(err.message) + '</span>';
    }
  };
}
if ($('#trainLayersAllBtn')) {
  $('#trainLayersAllBtn').onclick = () => {
    document.querySelectorAll('#trainLayersChecks input[type="checkbox"]').forEach((c) => { c.checked = true; });
  };
}
if ($('#trainLayersAttnBtn')) {
  $('#trainLayersAttnBtn').onclick = () => {
    document.querySelectorAll('#trainLayersChecks input[type="checkbox"]').forEach((c) => {
      c.checked = /^(q_proj|k_proj|v_proj|o_proj|qkv_proj|c_attn|query_key_value)$/.test(c.value);
    });
  };
}
if ($('#trainHfSearchBtn')) {
  $('#trainHfSearchBtn').onclick = async () => {
    const query = ($('#trainHfQuery') && $('#trainHfQuery').value.trim()) || 'Qwen2.5 Instruct';
    const max_size = parseFloat(($('#trainHfMaxB') && $('#trainHfMaxB').value) || '0') || 0;
    const out = $('#trainHfResults');
    if (!out) return;
    out.innerHTML = '<div class="page-sub">🔎 Поиск на HuggingFace Hub…</div>';
    try {
      const res = await api('/api/train/hf/search', { query, max_size, limit: 12 });
      out.innerHTML = '';
      const models = (res && res.models) || [];
      if (!models.length) {
        out.append(node('div', 'page-sub', 'Ничего не найдено по запросу «' + query + '».'));
        return;
      }
      models.forEach((m) => {
        const row = node('div', 'cllm-row');
        const info = node('div', 'cllm-info');
        info.append(node('div', 'cllm-title', '🤗 ' + esc(m.id) + (m.params_b ? (' · ' + m.params_b + 'B') : '')));
        info.append(node('div', 'cllm-sub', '⬇ ' + (m.downloads || 0).toLocaleString('ru-RU') + ' · ❤ ' + (m.likes || 0)));
        row.append(info);
        const acts = node('div', 'sched-actions');
        const useBtn = node('button', 'ghost small-btn', 'Выбрать');
        useBtn.type = 'button';
        useBtn.onclick = () => {
          if ($('#trainModelCustom')) $('#trainModelCustom').value = m.id;
          updateTrainVramEstimate();
          pushNotice('Выбрана модель: ' + m.id, 'ok');
        };
        const dlBtn = node('button', 'primary small-btn', '⬇️ Скачать в data/models');
        dlBtn.type = 'button';
        dlBtn.onclick = async () => {
          const r = await api('/api/train/hf/download', { repo_id: m.id });
          pushNotice(r && r.ok ? ('⬇️ Многопоточное скачивание запущено: ' + m.id + ' (см. лог ниже)') : ((r && r.error) || 'Ошибка'), r && r.ok ? 'ok' : 'bad');
          refreshTrain(false);
        };
        acts.append(useBtn, dlBtn);
        row.append(acts);
        out.append(row);
      });
    } catch (err) {
      out.innerHTML = '<div class="page-sub">❌ ' + esc(err.message) + '</div>';
    }
  };
}
if ($('#trainQuantBtn')) {
  $('#trainQuantBtn').onclick = async () => {
    const input_gguf = ($('#trainQuantInput') && $('#trainQuantInput').value.trim()) || '';
    const quant_type = ($('#trainQuantType') && $('#trainQuantType').value) || 'Q4_K_M';
    const stEl = $('#trainQuantStatus');
    if (!input_gguf) { pushNotice('Укажи путь к .gguf файлу для квантования', 'warn'); return; }
    if (stEl) stEl.textContent = '⏳ Выполняется llama-quantize (' + quant_type + ')…';
    $('#trainQuantBtn').disabled = true;
    try {
      const res = await api('/api/train/quantize', { input_gguf, quant_type });
      if (res && res.ok) {
        if (stEl) stEl.textContent = '✅ Готово за ' + res.elapsed_sec + 'с: ' + res.output_gguf + ' (' + res.input_size_mb + ' МБ → ' + res.output_size_mb + ' МБ)';
        pushNotice('✅ GGUF квантован: ' + res.output_size_mb + ' МБ', 'ok');
      } else if (stEl) {
        stEl.textContent = '❌ ' + ((res && res.error) || 'Ошибка квантования');
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    } finally {
      $('#trainQuantBtn').disabled = false;
    }
  };
}
if ($('#trainBenchGgufBtn')) {
  $('#trainBenchGgufBtn').onclick = async () => {
    const gguf_path = ($('#trainQuantInput') && $('#trainQuantInput').value.trim()) || '';
    const stEl = $('#trainQuantStatus');
    if (!gguf_path) { pushNotice('Укажи путь к .gguf файлу для замера скорости', 'warn'); return; }
    if (stEl) stEl.textContent = '⚡ Запуск llama-bench на ' + gguf_path + '…';
    $('#trainBenchGgufBtn').disabled = true;
    try {
      const r = await api('/api/train/benchmark_gguf', { gguf_path });
      if (r && r.ok) {
        if (stEl) stEl.textContent = '⚡ ' + r.size_mb + ' МБ · Prompt (pp128): ' + (r.pp128_tps || '—') + ' tok/s · Генерация (tg64): ' + (r.tg64_tps || '—') + ' tok/s' + (r.gpu_info ? (' · ' + r.gpu_info) : '');
        pushNotice('⚡ Скорость генерации: ' + (r.tg64_tps || '—') + ' tok/s', 'ok');
      } else if (stEl) {
        stEl.textContent = '❌ ' + ((r && r.error) || 'Ошибка бенчмарка');
      }
    } catch (err) {
      if (stEl) stEl.textContent = '❌ ' + err.message;
    } finally {
      $('#trainBenchGgufBtn').disabled = false;
    }
  };
}

/* ---------- IDE Workspace File Explorer (#viewFiles) & Slash-Command Palette ---------- */
let ideCurDir = '';
let ideCurFile = '';
let ideCurRev = null;
let ideRootAbs = '';

async function refreshIdeFiles(dirPath) {
  if (dirPath !== undefined) ideCurDir = dirPath;
  try {
    const st = await api('/api/files/status');
    if (st && st.ok) {
      ideRootAbs = st.root || '';
      if ($('#filesRootLabel')) $('#filesRootLabel').textContent = 'рабочая папка: ' + (st.root || 'work');
      if ($('#filesPairChip')) {
        if (st.lan_access && st.display) {
          $('#filesPairChip').className = 'sec-chip ok';
          $('#filesPairChip').textContent = '📱 Код: ' + st.display;
          $('#filesPairChip').style.cursor = 'pointer';
          $('#filesPairChip').onclick = () => {
            if (st.pair_url && navigator.clipboard) navigator.clipboard.writeText(st.pair_url);
            pushNotice('Ссылка с кодом для телефона скопирована: ' + (st.pair_url || st.display), 'ok');
          };
          if ($('#filesRotateCodeBtn')) $('#filesRotateCodeBtn').hidden = false;
        } else {
          $('#filesPairChip').className = 'sec-chip';
          $('#filesPairChip').textContent = '📱 Wi-Fi редактор: включи LAN в Настройках';
          if ($('#filesRotateCodeBtn')) $('#filesRotateCodeBtn').hidden = true;
        }
      }
    }
    if ($('#filesCrumbs')) {
      $('#filesCrumbs').textContent = '📁 /' + ideCurDir + (ideCurDir ? '  (клик — вверх ↑)' : '');
    }
    const listing = await api('/api/files/list?path=' + encodeURIComponent(ideCurDir));
    const box = $('#filesList');
    if (!box) return;
    box.innerHTML = '';
    if (!listing || !listing.ok) {
      box.append(node('div', 'page-sub', (listing && listing.error) || 'Ошибка чтения папки'));
      return;
    }
    const entries = listing.entries || [];
    if (!entries.length) {
      box.append(node('div', 'page-sub', 'Папка пуста — нажми +📄 чтобы создать файл.'));
      return;
    }
    entries.forEach((e) => {
      const item = node('div', 'ide-item' + (e.path === ideCurFile ? ' active' : ''));
      const ico = e.type === 'dir' ? '📁 ' : '📄 ';
      const sz = e.type === 'file' ? (e.size > 1024 ? (e.size / 1024).toFixed(1) + ' КБ' : e.size + ' Б') : '';
      item.append(node('span', null, ico + e.name), node('span', 'page-sub', sz));
      item.onclick = () => {
        if (e.type === 'dir') refreshIdeFiles(e.path);
        else openIdeFile(e.path);
      };
      box.append(item);
    });
  } catch (err) {
    if ($('#filesList')) $('#filesList').innerHTML = '<div class="page-sub">❌ ' + esc(err.message) + '</div>';
  }
}

async function openIdeFile(relPath) {
  try {
    const r = await api('/api/files/read?path=' + encodeURIComponent(relPath));
    if (!r || !r.ok) {
      pushNotice((r && r.error) || 'Не удалось открыть файл', 'warn');
      return;
    }
    ideCurFile = r.path;
    ideCurRev = r.revision;
    if ($('#filesCurrentPath')) $('#filesCurrentPath').textContent = r.path;
    const linesCnt = (r.text || '').split('\n').length;
    if ($('#filesMetaInfo')) $('#filesMetaInfo').textContent = linesCnt + ' стр. · ' + r.size + ' Б';
    const ta = $('#filesEditorArea');
    if (ta) { ta.disabled = false; ta.value = r.text; }
    ['#filesSaveBtn', '#filesDeleteBtn', '#filesAskAiBtn', '#filesIndexRagBtn'].forEach((s) => {
      if ($(s)) $(s).disabled = false;
    });
    refreshIdeFiles();
  } catch (err) {
    pushNotice(err.message, 'bad');
  }
}

if ($('#filesCrumbs')) {
  $('#filesCrumbs').onclick = () => {
    if (!ideCurDir) return;
    const parts = ideCurDir.split('/');
    parts.pop();
    refreshIdeFiles(parts.join('/'));
  };
}
if ($('#filesRefreshBtn')) $('#filesRefreshBtn').onclick = () => refreshIdeFiles();
if ($('#filesRotateCodeBtn')) {
  $('#filesRotateCodeBtn').onclick = async () => {
    await api('/api/files/rotate', {});
    pushNotice('🔄 Код сопряжения мобильного редактора обновлён', 'ok');
    refreshIdeFiles();
  };
}
if ($('#filesSaveBtn')) {
  $('#filesSaveBtn').onclick = async () => {
    if (!ideCurFile || !$('#filesEditorArea')) return;
    const text = $('#filesEditorArea').value;
    const r = await api('/api/files/save', { path: ideCurFile, text, revision: ideCurRev });
    if (r && r.ok) {
      ideCurRev = r.revision;
      if ($('#filesMetaInfo')) $('#filesMetaInfo').textContent = '✓ сохранено · ' + r.size + ' Б';
      pushNotice('💾 Файл сохранён: ' + ideCurFile, 'ok');
    } else {
      pushNotice((r && r.error) || 'Ошибка сохранения', 'bad');
    }
  };
}
if ($('#filesNewFileBtn')) {
  $('#filesNewFileBtn').onclick = async () => {
    const name = prompt('Имя нового файла:', 'script.py');
    if (!name || !name.trim()) return;
    const rel = (ideCurDir ? ideCurDir + '/' : '') + name.trim();
    const r = await api('/api/files/save', { path: rel, text: '', revision: null });
    if (r && r.ok) {
      await refreshIdeFiles();
      openIdeFile(rel);
    } else {
      pushNotice((r && r.error) || 'Ошибка создания файла', 'bad');
    }
  };
}
if ($('#filesNewDirBtn')) {
  $('#filesNewDirBtn').onclick = async () => {
    const name = prompt('Имя новой папки:', 'new_folder');
    if (!name || !name.trim()) return;
    const rel = (ideCurDir ? ideCurDir + '/' : '') + name.trim();
    const r = await api('/api/files/mkdir', { path: rel });
    if (r && r.ok) refreshIdeFiles(rel);
    else pushNotice((r && r.error) || 'Ошибка создания папки', 'bad');
  };
}
if ($('#filesDeleteBtn')) {
  $('#filesDeleteBtn').onclick = async () => {
    if (!ideCurFile || !confirm('Удалить файл ' + ideCurFile + '?')) return;
    const r = await api('/api/files/delete', { path: ideCurFile });
    if (r && r.ok) {
      ideCurFile = '';
      ideCurRev = null;
      if ($('#filesEditorArea')) { $('#filesEditorArea').value = ''; $('#filesEditorArea').disabled = true; }
      if ($('#filesCurrentPath')) $('#filesCurrentPath').textContent = 'Выберите файл слева';
      ['#filesSaveBtn', '#filesDeleteBtn', '#filesAskAiBtn', '#filesIndexRagBtn'].forEach((s) => { if ($(s)) $(s).disabled = true; });
      refreshIdeFiles();
    } else {
      pushNotice((r && r.error) || 'Ошибка удаления', 'bad');
    }
  };
}
if ($('#filesAskAiBtn')) {
  $('#filesAskAiBtn').onclick = () => {
    if (!ideCurFile || !$('#filesEditorArea')) return;
    const snippet = $('#filesEditorArea').value.slice(0, 6000);
    setView('chat');
    if ($('#input')) {
      $('#input').value = 'Разбери и улучши файл `' + ideCurFile + '`:\n```\n' + snippet + '\n```\n';
      $('#input').focus();
    }
  };
}
if ($('#filesIndexRagBtn')) {
  $('#filesIndexRagBtn').onclick = async () => {
    if (!ideCurFile || !ideRootAbs) return;
    const fullPath = ideRootAbs.replace(/[\\/]+$/, '') + '/' + ideCurFile;
    const r = await api('/api/rag/index', { path: fullPath });
    pushNotice(r && r.ok ? ('📚 Файл добавлен в базу знаний RAG (' + r.chunks_count + ' чанков)') : ((r && r.error) || 'Ошибка RAG'), r && r.ok ? 'ok' : 'bad');
  };
}
if ($('#filesSearchInput')) {
  $('#filesSearchInput').oninput = async (e) => {
    const q = e.target.value.trim();
    if (!q) { refreshIdeFiles(); return; }
    const r = await api('/api/files/search?q=' + encodeURIComponent(q));
    const box = $('#filesList');
    if (!box) return;
    box.innerHTML = '';
    (r.matches || []).forEach((m) => {
      const item = node('div', 'ide-item');
      item.append(node('span', null, '📄 ' + m.path));
      item.onclick = () => openIdeFile(m.path);
      box.append(item);
    });
  };
}

// Global Ctrl+S in IDE Files view + Spacebar Push-to-Talk in Voice Call
document.addEventListener('keydown', (e) => {
  if (currentView === 'files' && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    if ($('#filesSaveBtn') && !$('#filesSaveBtn').disabled) $('#filesSaveBtn').click();
    return;
  }
  if (voiceCallActive && e.code === 'Space' && document.activeElement && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
    e.preventDefault();
    callStartListening();
  }
});

// Interactive Slash-Command Palette when typing "/" in #input
const SLASH_COMMANDS = [
  { cmd: '/goal ', desc: '🎯 Режим автономной цели', run: (inp) => { inp.value = '/goal '; inp.focus(); } },
  { cmd: '/rag', desc: '📚 Открыть поиск по базе знаний', run: () => { if ($('#gearBtn')) $('#gearBtn').click(); setTimeout(() => $('#ragQueryInput')?.focus(), 200); } },
  { cmd: '/voice', desc: '📞 Начать голосовой созвон 2.0', run: () => { $('#input').value = ''; openVoiceCall(); } },
  { cmd: '/train', desc: '🧬 Открыть LoRA & Unsloth Studio 3.0', run: () => { $('#input').value = ''; setView('train'); } },
  { cmd: '/files', desc: '📁 Открыть встроенный IDE Редактор', run: () => { $('#input').value = ''; setView('files'); } },
  { cmd: '/cluster', desc: '📡 Открыть Wi-Fi Кластер и RPC', run: () => { $('#input').value = ''; setView('cluster'); } },
  { cmd: '/clear', desc: '✨ Создать чистую сессию', run: () => { $('#input').value = ''; $('#newChat')?.click(); } },
];

if ($('#input') && $('#slashMenu')) {
  $('#input').addEventListener('input', () => {
    const menu = $('#slashMenu');
    const val = $('#input').value;
    if (!val.startsWith('/') || val.includes(' ') || val.length > 14) {
      menu.hidden = true;
      return;
    }
    const q = val.toLowerCase();
    const hits = SLASH_COMMANDS.filter((c) => c.cmd.trim().startsWith(q));
    if (!hits.length) { menu.hidden = true; return; }
    menu.innerHTML = '';
    hits.forEach((c) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'slash-item';
      btn.innerHTML = '<code>' + c.cmd.trim() + '</code><span>' + c.desc + '</span>';
      btn.onclick = () => {
        menu.hidden = true;
        c.run($('#input'));
      };
      menu.appendChild(btn);
    });
    menu.hidden = false;
  });
}
if ($('#trainStartBtn')) {
  $('#trainStartBtn').onclick = async () => {
    const params = trainCollectParams();
    if (!params.dataset_path) {
      pushNotice('Сначала выбери датасет или собери/сгенерируй его выше', 'warn');
      return;
    }
    try {
      const res = await api('/api/train/start', params);
      if (res && res.ok) {
        pushNotice('🧬 Обучение Unsloth запущено: ' + params.model, 'ok');
        refreshTrain(false);
      } else {
        pushNotice((res && res.error) || 'Не удалось запустить обучение', 'bad');
      }
    } catch (err) {
      pushNotice(err.message, 'bad');
    }
  };
}
if ($('#trainStopBtn')) {
  $('#trainStopBtn').onclick = async () => {
    await api('/api/train/stop', {}).catch(() => {});
    pushNotice('🛑 Задача обучения остановлена', 'warn');
    refreshTrain(false);
  };
}
if ($('#trainEvalBtn')) {
  $('#trainEvalBtn').onclick = async () => {
    const adapter_dir = ($('#trainEvalDir') && $('#trainEvalDir').value.trim()) || '';
    const prompt = ($('#trainEvalPrompt') && $('#trainEvalPrompt').value.trim()) || '';
    const outEl = $('#trainEvalOut');
    if (!adapter_dir || !prompt) { pushNotice('Укажи папку адаптера и тестовый промпт', 'warn'); return; }
    if (outEl) { outEl.hidden = false; outEl.textContent = '🧪 Загружаю адаптер Unsloth и генерирую ответ…'; }
    $('#trainEvalBtn').disabled = true;
    try {
      const res = await api('/api/train/eval', { adapter_dir, prompt });
      if (outEl) outEl.textContent = res && res.ok ? res.answer : ('❌ ' + ((res && res.error) || 'Ошибка'));
    } catch (err) {
      if (outEl) outEl.textContent = '❌ ' + err.message;
    } finally {
      $('#trainEvalBtn').disabled = false;
    }
  };
}

/* ---------- LM Studio Native Load Modal (Context Window, GPU Offload, Vulkan, KV Quant) ---------- */
let lmsModalEstTimer = null;
let lmsModalPollTimer = null;

function _gpuPctFromVal(val) {
  const raw = String(val == null ? 'max' : val).trim().toLowerCase();
  if (raw === 'max' || raw === 'auto' || raw === '' || raw === '100%') return 100;
  if (raw === 'off' || raw === 'none' || raw === 'cpu' || raw === '0' || raw === '0%') return 0;
  const num = parseFloat(raw.replace('%', ''));
  if (!Number.isFinite(num)) return 100;
  return num > 1 ? Math.round(Math.min(100, Math.max(0, num))) : Math.round(Math.min(100, Math.max(0, num * 100)));
}

function _gpuFlagFromPct(pct) {
  const n = Math.round(Number(pct));
  if (!Number.isFinite(n) || n >= 100) return 'max';
  if (n <= 0) return 'off';
  return String(Math.round(n) / 100);
}

function _updateGpuSliderUi(pct) {
  const p = Math.min(100, Math.max(0, Math.round(Number(pct) || 0)));
  const range = $('#lmsLoadGpuRange');
  if (range) range.value = String(p);
  const lbl = $('#lmsLoadGpuLabel');
  if (lbl) {
    if (p >= 100) lbl.textContent = 'Max (100% слоёв в VRAM)';
    else if (p <= 0) lbl.textContent = 'Off (0% — только CPU / RAM)';
    else lbl.textContent = p + '% слоёв в VRAM (гибрид GPU+RAM)';
  }
  document.querySelectorAll('#lmsLoadGpuPresets .ctx-preset-pill').forEach((btn) => {
    btn.classList.toggle('active', Number(btn.dataset.gpu) === p);
  });
}

function _selectedModalDiskModel() {
  const sel = $('#lmsLoadModelSelect');
  const wanted = sel ? sel.value : '';
  const disk = (lmsStateCache && lmsStateCache.disk) || [];
  return disk.find((m) => (m.modelKey || m.path) === wanted) || disk[0] || null;
}

function _syncModalModelBounds(preferredCtx) {
  const m = _selectedModalDiskModel();
  const maxCtx = Math.max(512, Math.min(262144, Number((m && m.maxContextLength) || 32768)));
  const range = $('#lmsLoadCtxRange');
  const numIn = $('#lmsLoadCtxInput');
  const maxLbl = $('#lmsLoadCtxMaxLabel');
  const metaEl = $('#lmsLoadModalMeta');

  if (range) range.max = String(maxCtx);
  if (numIn) numIn.max = String(maxCtx);
  if (maxLbl) maxLbl.textContent = '/ ' + maxCtx.toLocaleString('ru-RU') + ' ток.';

  if (metaEl && m) {
    const q = m.quantization && m.quantization.name ? m.quantization.name : '';
    metaEl.textContent = [
      q,
      fmtSize(m.sizeBytes),
      m.paramsString || '',
      'макс. контекст ' + maxCtx.toLocaleString('ru-RU'),
      m.vision ? 'Vision' : '',
      m.trainedForToolUse ? 'Tools' : '',
    ].filter(Boolean).join(' · ');
  }

  const preset = (m && m.saved_preset) || {};
  const loadedList = (lmsStateCache && lmsStateCache.loaded) || [];
  const loadedMatch = m ? loadedList.find((x) => x.modelKey === m.modelKey || x.path === m.path) : null;

  let targetCtx = preferredCtx
    || (loadedMatch && loadedMatch.contextLength)
    || preset.context_length
    || (stateCache && (stateCache.lms_context_length || stateCache.context_tokens))
    || 16384;
  targetCtx = Math.max(512, Math.min(maxCtx, Math.round(Number(targetCtx) || 16384)));

  if (range) range.value = String(targetCtx);
  if (numIn) numIn.value = String(targetCtx);

  document.querySelectorAll('#lmsLoadCtxPresets .ctx-preset-pill').forEach((btn) => {
    const raw = btn.dataset.ctx;
    if (raw === 'max') {
      btn.disabled = false;
      btn.textContent = 'Max (' + fmtTok(maxCtx) + ')';
      btn.classList.toggle('active', targetCtx === maxCtx);
    } else {
      const cVal = Number(raw);
      btn.disabled = cVal > maxCtx;
      btn.classList.toggle('active', cVal === targetCtx);
    }
  });

  if (preset.gpu_offload != null) _updateGpuSliderUi(_gpuPctFromVal(preset.gpu_offload));
  if (preset.kv_quant && $('#lmsLoadKvSelect')) $('#lmsLoadKvSelect').value = preset.kv_quant;
  if (preset.flash_attention != null && $('#lmsLoadFlashCheck')) $('#lmsLoadFlashCheck').checked = !!preset.flash_attention;
  if (preset.parallel && $('#lmsLoadParallelInput')) $('#lmsLoadParallelInput').value = String(preset.parallel);

  scheduleModalEstimate(false);
}

function _setModalCtxValue(val) {
  const m = _selectedModalDiskModel();
  const maxCtx = Math.max(512, Math.min(262144, Number((m && m.maxContextLength) || 262144)));
  const clamped = Math.max(512, Math.min(maxCtx, Math.round(Number(val) || 8192)));
  if ($('#lmsLoadCtxRange')) $('#lmsLoadCtxRange').value = String(clamped);
  if ($('#lmsLoadCtxInput') && document.activeElement !== $('#lmsLoadCtxInput')) {
    $('#lmsLoadCtxInput').value = String(clamped);
  }
  document.querySelectorAll('#lmsLoadCtxPresets .ctx-preset-pill').forEach((btn) => {
    const raw = btn.dataset.ctx;
    const target = raw === 'max' ? maxCtx : Number(raw);
    btn.classList.toggle('active', target === clamped);
  });
  scheduleModalEstimate(false);
}

function renderModalEstimate(est, isPrecise) {
  if (!est) return;
  const verdict = $('#lmsLoadEstVerdict');
  const bar = $('#lmsLoadEstBar');
  const stats = $('#lmsLoadEstStats');
  const hw = est.hardware || (lmsStateCache && lmsStateCache.hardware) || {};
  const vramTotal = Number(hw.vram_total_gb || 0);
  const ramTotal = Number(hw.ram_total_gb || 16);
  const estGpu = Number(est.est_gpu_gb != null ? est.est_gpu_gb : est.total_req_gb || 0);
  const estTot = Number(est.total_req_gb || 0);

  if (verdict) {
    const tag = isPrecise && est.cli_estimate ? '✓ Точный расчёт lms: ' : '⚡ Оценка: ';
    const cliVerdict = est.cli_estimate && est.cli_estimate.verdict ? ' · ' + est.cli_estimate.verdict : '';
    verdict.textContent = tag + (est.fit_label || '') + (est.tok_per_sec ? ' · ' + est.tok_per_sec : '') + cliVerdict;
  }
  if (bar) {
    const denom = vramTotal > 0 && estGpu > 0 ? vramTotal : ramTotal;
    const numer = vramTotal > 0 && estGpu > 0 ? estGpu : estTot;
    const pct = Math.min(100, Math.max(4, Math.round((numer / Math.max(1, denom)) * 100)));
    bar.style.width = pct + '%';
    bar.className = 'lms-est-bar-fill' + (est.fit === 'oom' ? ' bad' : (est.fit === 'tight' || est.fit === 'hybrid' ? ' warn' : ''));
  }
  if (stats) {
    const vramPart = vramTotal > 0
      ? `VRAM: ${estGpu.toFixed(2)} / ${vramTotal.toFixed(2)} ГБ`
      : `GPU не используется`;
    stats.textContent = `${vramPart} · Общая память: ${estTot.toFixed(2)} ГБ (веса ${est.weights_gb || '—'} ГБ + KV-кэш ${est.kv_cache_gb || '—'} ГБ)`;
  }
}

function scheduleModalEstimate(precise) {
  if (lmsModalEstTimer) clearTimeout(lmsModalEstTimer);
  lmsModalEstTimer = setTimeout(async () => {
    const m = _selectedModalDiskModel();
    if (!m) return;
    const ctx = clampContext($('#lmsLoadCtxInput') && $('#lmsLoadCtxInput').value, 16384);
    const gpuFlag = _gpuFlagFromPct($('#lmsLoadGpuRange') && $('#lmsLoadGpuRange').value);
    const kv = ($('#lmsLoadKvSelect') && $('#lmsLoadKvSelect').value) || 'f16';
    const btn = $('#lmsLoadPreciseBtn');
    if (precise && btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Считаю…';
    }
    try {
      const res = await api('/api/lms/estimate', {
        model: m.modelKey || m.path,
        context_length: ctx,
        gpu: gpuFlag,
        kv_quant: kv,
        precise: !!precise,
      });
      if (res && res.estimate) renderModalEstimate(res.estimate, !!precise);
    } catch (err) {
      if (precise) pushNotice('Ошибка расчёта памяти: ' + err.message, 'warn');
    } finally {
      if (precise && btn) {
        btn.disabled = false;
        btn.textContent = '🔍 Точный расчёт LM Studio';
      }
    }
  }, precise ? 10 : 120);
}

async function openLmsLoadModal(preferredModelKey) {
  const dlg = $('#lmsLoadDialog');
  if (!dlg) return;
  if (lmsModalPollTimer) { clearTimeout(lmsModalPollTimer); lmsModalPollTimer = null; }
  const liveLog = $('#lmsLoadLiveLog');
  if (liveLog) { liveLog.hidden = true; liveLog.textContent = ''; }
  const submitBtn = $('#lmsLoadSubmitBtn');
  if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = '🚀 Загрузить модель в память'; }

  try {
    lmsStateCache = await api('/api/lms/state');
  } catch (err) {
    pushNotice('Не удалось опросить LM Studio: ' + err.message, 'bad');
    return;
  }

  const diskAll = (lmsStateCache && lmsStateCache.disk) || [];
  const chatDisk = diskAll.filter((m) => m.type !== 'embedding');
  const modelsToShow = chatDisk.length ? chatDisk : diskAll;
  const selModel = $('#lmsLoadModelSelect');
  if (!modelsToShow.length) {
    pushNotice('В LM Studio пока нет скачанных моделей на диске — скачай GGUF модель на вкладке LM Studio.', 'warn');
    setView('lm');
    return;
  }

  selModel.innerHTML = '';
  modelsToShow.forEach((m) => {
    const key = m.modelKey || m.path;
    const opt = document.createElement('option');
    opt.value = key;
    const q = m.quantization && m.quantization.name ? ' (' + m.quantization.name + ')' : '';
    opt.textContent = (m.displayName || key) + q + (m.sizeBytes ? ' · ' + fmtSize(m.sizeBytes) : '');
    if (preferredModelKey && (key === preferredModelKey || m.path === preferredModelKey || m.displayName === preferredModelKey)) {
      opt.selected = true;
    }
    selModel.appendChild(opt);
  });

  // Заполняем селектор Runtime движков (Vulkan / CUDA / ROCm / CPU)
  const rtSel = $('#lmsLoadRuntimeSelect');
  const rtInfo = (lmsStateCache && lmsStateCache.runtimes) || {};
  if (rtSel) {
    rtSel.innerHTML = '';
    const installedRts = (rtInfo.runtimes || []).filter((r) => r.installed);
    if (!installedRts.length) {
      const defOpt = document.createElement('option');
      defOpt.value = '';
      defOpt.textContent = 'По умолчанию LM Studio';
      rtSel.appendChild(defOpt);
    } else {
      installedRts.forEach((r) => {
        const opt = document.createElement('option');
        opt.value = r.name;
        const ico = r.kind === 'vulkan' ? '🌋 ' : (r.kind === 'cuda' ? '🟢 ' : (r.kind === 'rocm' ? '🔴 ' : '💻 '));
        const compatWarn = r.compatible ? '' : ' [⚠️ ошибка опроса GPU]';
        opt.textContent = ico + (r.label || r.name) + (r.version ? ' v' + r.version : '') + compatWarn;
        if (r.selected) opt.selected = true;
        rtSel.appendChild(opt);
      });
    }
  }

  const defs = (lmsStateCache && lmsStateCache.defaults) || {};
  _updateGpuSliderUi(_gpuPctFromVal(defs.gpu_offload || (stateCache && stateCache.lms_gpu_offload) || 'max'));
  if ($('#lmsLoadKvSelect')) $('#lmsLoadKvSelect').value = defs.kv_quant || (stateCache && stateCache.lms_kv_quant) || 'f16';
  if ($('#lmsLoadFlashCheck')) $('#lmsLoadFlashCheck').checked = defs.flash_attention !== false;
  if ($('#lmsLoadParallelInput')) $('#lmsLoadParallelInput').value = String(defs.parallel || 1);
  if ($('#lmsLoadTtlInput')) $('#lmsLoadTtlInput').value = String(defs.ttl || 0);

  _syncModalModelBounds();
  if (!dlg.open) dlg.showModal();
  const bodyEl = dlg.querySelector('.modal-body');
  if (bodyEl) bodyEl.scrollTop = 0;
}

(function wireLmsLoadModal() {
  const dlg = $('#lmsLoadDialog');
  if (!dlg) return;
  const closeModal = () => {
    if (lmsModalPollTimer) { clearTimeout(lmsModalPollTimer); lmsModalPollTimer = null; }
    if (dlg.open) dlg.close();
  };
  $('#lmsLoadCloseBtn')?.addEventListener('click', closeModal);
  $('#lmsLoadCancelBtn')?.addEventListener('click', closeModal);
  dlg.addEventListener('click', (ev) => { if (ev.target === dlg) closeModal(); });

  $('#lmsLoadModelSelect')?.addEventListener('change', () => _syncModalModelBounds());
  $('#lmsLoadCtxRange')?.addEventListener('input', (e) => _setModalCtxValue(e.target.value));
  $('#lmsLoadCtxInput')?.addEventListener('change', (e) => {
    _setModalCtxValue(e.target.value);
    e.target.value = $('#lmsLoadCtxRange').value;
  });
  $('#lmsLoadCtxInput')?.addEventListener('input', (e) => {
    const n = Number(e.target.value);
    if (Number.isFinite(n) && n >= 512) _setModalCtxValue(n);
  });
  document.querySelectorAll('#lmsLoadCtxPresets .ctx-preset-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      const m = _selectedModalDiskModel();
      const maxCtx = Math.max(512, Math.min(262144, Number((m && m.maxContextLength) || 32768)));
      const raw = btn.dataset.ctx;
      _setModalCtxValue(raw === 'max' ? maxCtx : Number(raw));
    });
  });

  $('#lmsLoadGpuRange')?.addEventListener('input', (e) => {
    _updateGpuSliderUi(e.target.value);
    scheduleModalEstimate(false);
  });
  document.querySelectorAll('#lmsLoadGpuPresets .ctx-preset-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      _updateGpuSliderUi(Number(btn.dataset.gpu));
      scheduleModalEstimate(false);
    });
  });
  $('#lmsLoadKvSelect')?.addEventListener('change', () => scheduleModalEstimate(false));
  $('#lmsLoadPreciseBtn')?.addEventListener('click', () => scheduleModalEstimate(true));

  $('#lmsLoadSubmitBtn')?.addEventListener('click', async () => {
    const m = _selectedModalDiskModel();
    if (!m) return;
    const submitBtn = $('#lmsLoadSubmitBtn');
    const liveLog = $('#lmsLoadLiveLog');
    const ctx = clampContext($('#lmsLoadCtxInput').value, 16384);
    const gpu = _gpuFlagFromPct($('#lmsLoadGpuRange').value);
    const runtime = ($('#lmsLoadRuntimeSelect') && $('#lmsLoadRuntimeSelect').value) || '';
    const kv_quant = ($('#lmsLoadKvSelect') && $('#lmsLoadKvSelect').value) || 'f16';
    const flash_attention = !!($('#lmsLoadFlashCheck') && $('#lmsLoadFlashCheck').checked);
    const parallel = Math.max(1, Math.min(16, Number($('#lmsLoadParallelInput').value) || 1));
    const ttl = Math.max(0, Number($('#lmsLoadTtlInput').value) || 0);
    const select_for_chat = !!($('#lmsLoadSyncChatCheck') && $('#lmsLoadSyncChatCheck').checked);

    submitBtn.disabled = true;
    submitBtn.textContent = '⏳ Загружаю в память…';
    if (liveLog) {
      liveLog.hidden = false;
      liveLog.textContent = `Запуск lms load ${m.modelKey || m.path} --context-length ${ctx} --gpu ${gpu}…`;
    }

    try {
      const res = await api('/api/lms/load', {
        model: m.modelKey || m.path,
        runtime,
        context_length: ctx,
        gpu,
        kv_quant,
        flash_attention,
        parallel,
        ttl,
        select_for_chat,
      });
      if (!res.ok) throw new Error(res.error || 'Не удалось запустить загрузку');
      (res.warnings || []).forEach((w) => pushNotice(w, 'warn'));

      const pollJob = async () => {
        try {
          const job = await api('/api/lms/job');
          if (liveLog && job) {
            liveLog.textContent = (job.kind || 'load') + ' [' + (job.status || '') + ']\n' + (job.lines || []).join('\n');
            liveLog.scrollTop = liveLog.scrollHeight;
          }
          if (job && job.running) {
            lmsModalPollTimer = setTimeout(pollJob, 900);
            return;
          }
          await refreshState();
          await refreshReadiness();
          if (currentView === 'lm') await refreshLM();
          if (job && job.rc === 0) {
            pushNotice(`✅ Модель «${m.displayName || m.modelKey}» загружена (контекст ${ctx.toLocaleString('ru-RU')} токенов)!`, 'ok');
            closeModal();
          } else {
            submitBtn.disabled = false;
            submitBtn.textContent = '🔄 Повторить загрузку';
            pushNotice('Загрузка модели завершилась с ошибкой — проверь лог в окне и попробуй уменьшить контекст или GPU Offload.', 'bad');
          }
        } catch (pollErr) {
          submitBtn.disabled = false;
          submitBtn.textContent = '🚀 Загрузить модель в память';
        }
      };
      lmsModalPollTimer = setTimeout(pollJob, 600);
    } catch (err) {
      if (liveLog) liveLog.textContent = '❌ Ошибка: ' + err.message;
      pushNotice(err.message, 'bad');
      submitBtn.disabled = false;
      submitBtn.textContent = '🚀 Загрузить модель в память';
    }
  });
})();

/* ---------- Screenshot-1 Post-Run / Post-Goal Summary Bar & Review Diff Modal ---------- */
const _REVIEW_DOC_SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 2h5.5L13 5.5V13a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/><path d="M9.5 2v3.5H13"/><path d="M6 9h4M8 7v4"/></svg>';

function renderChangesSummaryBar(changes, goalInfo, steps, rounds) {
  const filesChanged = Number((changes && changes.files_changed) || 0);
  const ins = Number((changes && changes.insertions) || 0);
  const del = Number((changes && changes.deletions) || 0);
  if (filesChanged <= 0 && !goalInfo) return null;

  ensureTurn();
  const bar = node('div', 'changes-summary-bar');
  const left = node('button', 'changes-summary-left');
  left.type = 'button';

  if (filesChanged > 0) {
    const fLabel = filesChanged === 1 ? '1 file changed' : `${filesChanged} files changed`;
    const goalTag = goalInfo ? (goalInfo.achieved ? ' · ✓ цель достигнута' : ' · цель завершена') : '';
    left.innerHTML = `<span class="changes-label">${esc(fLabel + goalTag)}</span>`
      + `<span class="changes-plus">+${ins}</span>`
      + `<span class="changes-minus">-${del}</span>`
      + `<span class="changes-chev">›</span>`;
  } else {
    const pts = (goalInfo && goalInfo.points) || [];
    const donePts = pts.filter((p) => p.done).length;
    const statusIcon = goalInfo && goalInfo.achieved ? '✓ Цель достигнута' : 'Итог цели';
    const ptsStr = pts.length ? ` · пунктов ${donePts}/${pts.length}` : '';
    const stStr = steps != null ? ` · шагов ${steps}` : '';
    left.innerHTML = `<span class="changes-label">${esc(statusIcon + ptsStr + stStr)}</span>`
      + `<span class="changes-chev">›</span>`;
  }

  const revBtn = node('button', 'changes-review-btn');
  revBtn.type = 'button';
  revBtn.innerHTML = `${_REVIEW_DOC_SVG}<span>Review</span>`;

  const openFn = () => openReviewModal(changes, goalInfo, steps, rounds);
  left.onclick = openFn;
  revBtn.onclick = openFn;

  bar.append(left, revBtn);
  add(bar);
  return bar;
}

function openReviewModal(changes, goalInfo, steps, rounds) {
  const dlg = $('#reviewChangesDialog');
  if (!dlg) return;
  const files = (changes && changes.files) || [];
  const ins = Number((changes && changes.insertions) || 0);
  const del = Number((changes && changes.deletions) || 0);

  const titleEl = $('#reviewModalTitle');
  const metaEl = $('#reviewModalMeta');
  const statChip = $('#reviewModalStatChip');
  const goalBox = $('#reviewGoalSummaryBox');
  const layoutEl = $('#reviewFilesLayout');
  const listEl = $('#reviewFileList');
  const diffFname = $('#reviewDiffFname');
  const diffCode = $('#reviewDiffCode');
  const openIdeBtn = $('#reviewOpenInIdeBtn');

  if (titleEl) {
    titleEl.textContent = goalInfo
      ? (goalInfo.achieved ? '✅ Итог цели и обзор изменений' : '📋 Отчёт по цели и изменения')
      : '📄 Обзор изменённых файлов (Review)';
  }
  if (metaEl) {
    const bits = [];
    if (files.length) bits.push(`Файлов изменено: ${files.length}`);
    if (steps != null) bits.push(`Шагов агента: ${steps}`);
    if (rounds) bits.push(`Подходов: ${rounds}`);
    metaEl.textContent = bits.join(' · ') || 'Сводка прогона';
  }
  if (statChip) {
    if (files.length) {
      statChip.hidden = false;
      statChip.innerHTML = `<span class="changes-plus">+${ins}</span><span class="changes-minus">-${del}</span>`;
    } else {
      statChip.hidden = true;
    }
  }

  if (goalBox) {
    if (goalInfo) {
      goalBox.hidden = false;
      const pts = goalInfo.points || [];
      const ptsHtml = pts.length
        ? '<div class="review-goal-pts">' + pts.map((p, i) =>
            `<div>${p.done ? '✅' : '⬜'} <b>${i + 1}.</b> ${esc(p.text || '')}`
            + (p.evidence ? ` <span style="color:var(--text-cap)">— ${esc(p.evidence)}</span>` : '')
            + `</div>`
          ).join('') + '</div>'
        : '';
      goalBox.innerHTML = `<div class="review-goal-title">${goalInfo.achieved ? '🎯 Цель успешно достигнута' : '⚠️ Цель остановлена'}: ${esc(goalInfo.objective || '')}</div>`
        + (goalInfo.summary ? `<div class="review-goal-summary">${esc(goalInfo.summary)}</div>` : '')
        + (goalInfo.evidence ? `<div class="page-sub">Проверка: ${esc(goalInfo.evidence)}</div>` : '')
        + ptsHtml;
    } else {
      goalBox.hidden = true;
      goalBox.innerHTML = '';
    }
  }

  if (!files.length) {
    if (layoutEl) layoutEl.hidden = true;
  } else {
    if (layoutEl) layoutEl.hidden = false;
    listEl.innerHTML = '';

    const selectFile = (idx) => {
      const f = files[idx];
      if (!f) return;
      listEl.querySelectorAll('.review-file-item').forEach((el, i) => el.classList.toggle('active', i === idx));
      if (diffFname) {
        diffFname.textContent = `${f.rel_path || f.name || f.path} (${_fmtShortBytes(f.size_bytes)})`;
      }
      if (diffCode) {
        diffCode.innerHTML = _renderDiffHtml(f.diff);
      }
      if (openIdeBtn) {
        openIdeBtn.hidden = f.status === 'deleted';
        openIdeBtn.onclick = () => {
          dlg.close();
          setView('files');
          if (typeof openIdeFile === 'function' && f.rel_path && !f.rel_path.startsWith('..')) {
            setTimeout(() => openIdeFile(f.rel_path), 150);
          }
        };
      }
    };

    files.forEach((f, idx) => {
      const st = f.status || 'modified';
      const badge = st === 'added' ? 'A' : (st === 'deleted' ? 'D' : 'M');
      const btn = node('button', 'review-file-item' + (idx === 0 ? ' active' : ''));
      btn.type = 'button';
      btn.title = f.path || f.rel_path || '';
      btn.innerHTML = `<span class="rf-status ${esc(st)}">${badge}</span>`
        + `<span class="rf-name">${esc(f.rel_path || f.name || f.path)}</span>`
        + `<span class="rf-counts"><span class="dive-plus">+${f.added || 0}</span><span class="dive-minus">-${f.removed || 0}</span></span>`;
      btn.onclick = () => selectFile(idx);
      listEl.appendChild(btn);
    });

    selectFile(0);
  }

  if (!dlg.open) dlg.showModal();
}

(function wireReviewModal() {
  const dlg = $('#reviewChangesDialog');
  if (!dlg) return;
  const close = () => { if (dlg.open) dlg.close(); };
  $('#reviewModalCloseBtn')?.addEventListener('click', close);
  dlg.addEventListener('click', (ev) => { if (ev.target === dlg) close(); });
})();

/* ---------- Workspace Manager (Optional Workspaces & Per-Chat Assignment) ---------- */
let wsModalTargetSession = null;

function syncHeaderWorkspaceChip(sessionWsPath) {
  const chip = $('#headWorkspaceChip');
  if (!chip) return;
  const st = stateCache || {};
  const curRow = (st.sessions || []).find((r) => r.id === sessionId);
  const wsPath = sessionWsPath !== undefined
    ? String(sessionWsPath || '')
    : String((curRow && curRow.workspace) || st.workspace || '');
  const saved = (st.workspaces || []).find((w) => _wsKey(w.path) === _wsKey(wsPath));
  if (wsPath) {
    const name = _wsShortName(wsPath, saved && saved.name);
    chip.textContent = '📁 ' + name;
    chip.title = `Рабочее пространство чата: ${wsPath}\nНажми, чтобы сменить папку или переключить в режим «Без пространства»`;
    chip.classList.add('active-ws');
  } else {
    chip.textContent = '🌐 Без папки';
    chip.title = 'Чат без привязки к папке проекта (по желанию). Нажми, чтобы выбрать рабочее пространство';
    chip.classList.remove('active-ws');
  }
}

function syncWorkspaceSidebarUi(st) {
  const ws = String((st && st.workspace) || '').trim();
  const saved = ((st && st.workspaces) || []).find((w) => _wsKey(w.path) === _wsKey(ws));
  const btn = $('#wsPickerBtn');
  const ico = $('#wsPickerIco');
  const nameEl = $('#wsPickerName');
  const clearBtn = $('#wsClearQuickBtn');

  if (btn) {
    btn.classList.toggle('has-ws', !!ws);
    btn.title = ws
      ? `Активное рабочее пространство: ${ws}\nНажми для переключения или добавления папки`
      : 'Рабочее пространство не выбрано (по желанию) — нажми, чтобы выбрать папку проекта';
  }
  if (ico) ico.textContent = ws ? '📁' : '🌐';
  if (nameEl) nameEl.textContent = ws ? _wsShortName(ws, saved && saved.name) : 'Без пространства';
  if (clearBtn) clearBtn.hidden = !ws;

  const curRow = ((st && st.sessions) || []).find((r) => r.id === sessionId);
  syncHeaderWorkspaceChip(curRow ? (curRow.workspace || '') : ws);
}

async function createNewChatInWorkspace(wsPath) {
  try {
    const cleanWs = String(wsPath || '').trim();
    await api('/api/workspace/select', { path: cleanWs });
    const res = await api('/api/new', { title: '', workspace: cleanWs });
    await openSession(res.session);
    if (currentView !== 'chat') setView('chat');
    $('#sessionTitle').textContent = 'новая сессия';
    await refreshState();
    $('#input')?.focus();
  } catch (err) {
    pushNotice(err.message, 'bad');
  }
}

async function applyWorkspaceChoice(wsPath, customName, createDir) {
  const dlg = $('#workspaceDialog');
  const cleanWs = String(wsPath || '').trim();
  try {
    if (wsModalTargetSession) {
      // Перемещение конкретного чата в выбранное пространство (или «Без пространства»)
      if (cleanWs) {
        await api('/api/workspace/select', {
          path: cleanWs,
          name: customName || '',
          create_dir: !!createDir,
        });
      }
      await api('/api/session/workspace', { session: wsModalTargetSession, workspace: cleanWs });
      if (wsModalTargetSession === sessionId && !cleanWs) {
        await api('/api/workspace/select', { path: '' });
      }
      pushNotice(
        cleanWs
          ? `Чат перемещён в пространство «${_wsShortName(cleanWs, customName)}»`
          : 'Чат отвязан от папки — режим «Без рабочего пространства»',
        'ok'
      );
    } else {
      await api('/api/workspace/select', {
        path: cleanWs,
        name: customName || '',
        create_dir: !!createDir,
        session: sessionId || '',
      });
      pushNotice(
        cleanWs
          ? `Выбрано рабочее пространство: ${_wsShortName(cleanWs, customName)} (${cleanWs})`
          : 'Включён режим «Без рабочего пространства» (общие чаты)',
        'ok'
      );
    }
    if (dlg && dlg.open) dlg.close();
    await refreshState();
    if (currentView === 'files' && typeof refreshIdeFiles === 'function') refreshIdeFiles();
  } catch (err) {
    pushNotice(err.message, 'bad');
  }
}

function renderWorkspaceModalList() {
  const st = stateCache || {};
  const sessions = st.sessions || [];
  const targetRow = wsModalTargetSession ? sessions.find((r) => r.id === wsModalTargetSession) : null;
  const currentWs = wsModalTargetSession
    ? String((targetRow && targetRow.workspace) || '')
    : String(st.workspace || '');
  const currentKey = _wsKey(currentWs);

  const titleEl = $('#wsModalTitle');
  const metaEl = $('#wsModalMeta');
  if (titleEl) {
    titleEl.textContent = wsModalTargetSession
      ? `📁 Пространство для чата «${(targetRow && targetRow.title) || wsModalTargetSession}»`
      : '📁 Рабочие пространства';
  }
  if (metaEl) {
    metaEl.textContent = wsModalTargetSession
      ? 'Привяжи чат к папке проекта или оставь его в разделе «Без пространства»'
      : 'Выбор рабочего пространства по желанию — можно работать с папкой проекта или свободно без привязки';
  }

  const noneCard = $('#wsChooseNoneBtn');
  const noneBadge = $('#wsNoneActiveBadge');
  if (noneCard) noneCard.classList.toggle('active', currentKey === '__none__');
  if (noneBadge) noneBadge.textContent = currentKey === '__none__' ? '✓ Выбрано' : 'Выбрать';

  const list = $('#wsSavedList');
  if (!list) return;
  list.innerHTML = '';
  const workspaces = st.workspaces || [];
  if (!workspaces.length) {
    list.appendChild(node('div', 'empty-note', 'Сохранённых пространств пока нет — выбери папку кнопкой «📂 Обзор…» ниже или работай без пространства.'));
    return;
  }

  const countByKey = {};
  sessions.forEach((s) => {
    const k = _wsKey(s.workspace);
    countByKey[k] = (countByKey[k] || 0) + 1;
  });

  workspaces.forEach((w) => {
    const k = _wsKey(w.path);
    const isSel = k === currentKey;
    const chatCnt = countByKey[k] || 0;
    const row = node('div', 'ws-saved-row' + (isSel ? ' active' : ''));
    const info = node('div', 'ws-saved-info');
    const warnMissing = w.exists === false ? ' <span style="color:var(--warn);font-size:11px;">(папка не найдена)</span>' : '';
    info.innerHTML = `<div class="ws-saved-name">📁 ${esc(w.name || _wsShortName(w.path))}`
      + `<span class="ws-group-count">${chatCnt} чат.</span>${warnMissing}</div>`
      + `<div class="ws-saved-path" title="${esc(w.path)}">${esc(w.path)}</div>`;

    const selBtn = node('button', isSel ? 'ghost small-btn' : 'primary small-btn', isSel ? '✓ Активно' : 'Выбрать');
    selBtn.type = 'button';
    selBtn.onclick = () => applyWorkspaceChoice(w.path, w.name, false);

    const newChatBtn = node('button', 'ghost small-btn', '+ Чат');
    newChatBtn.type = 'button';
    newChatBtn.title = 'Создать новый чат сразу в этом пространстве';
    newChatBtn.onclick = async () => {
      const dlg = $('#workspaceDialog');
      if (dlg && dlg.open) dlg.close();
      await createNewChatInWorkspace(w.path);
    };

    const rmBtn = node('button', 'ghost small-btn danger-text', '✕');
    rmBtn.type = 'button';
    rmBtn.title = 'Убрать пространство из списка (файлы на диске НЕ удаляются)';
    rmBtn.onclick = async () => {
      try {
        await api('/api/workspace/remove', { path: w.path, detach_sessions: true });
        await refreshState();
        renderWorkspaceModalList();
        pushNotice('Рабочее пространство убрано из списка (файлы не затронуты)', 'ok');
      } catch (err) {
        pushNotice(err.message, 'bad');
      }
    };

    row.append(info, selBtn, newChatBtn, rmBtn);
    list.appendChild(row);
  });
}

function openWorkspaceModal(forSessionId) {
  wsModalTargetSession = forSessionId || null;
  const dlg = $('#workspaceDialog');
  if (!dlg) return;
  if ($('#wsPathInput')) $('#wsPathInput').value = '';
  if ($('#wsNameInput')) $('#wsNameInput').value = '';
  if ($('#wsCreateDirCheck')) $('#wsCreateDirCheck').checked = false;
  renderWorkspaceModalList();
  if (!dlg.open) dlg.showModal();
}

(function wireWorkspaceUi() {
  const dlg = $('#workspaceDialog');
  const close = () => { if (dlg && dlg.open) dlg.close(); };

  $('#wsPickerBtn')?.addEventListener('click', () => openWorkspaceModal(null));
  $('#headWorkspaceChip')?.addEventListener('click', () => openWorkspaceModal(sessionId || null));
  $('#wsClearQuickBtn')?.addEventListener('click', async () => {
    await applyWorkspaceChoice('', '', false);
  });
  $('#wsFilterToggleBtn')?.addEventListener('click', () => {
    wsFilterOnlyActive = !wsFilterOnlyActive;
    localStorage.setItem('lh.wsFilterOnlyActive', wsFilterOnlyActive ? '1' : '0');
    renderSessions();
  });

  $('#wsModalCloseBtn')?.addEventListener('click', close);
  dlg?.addEventListener('click', (ev) => { if (ev.target === dlg) close(); });

  $('#wsChooseNoneBtn')?.addEventListener('click', () => applyWorkspaceChoice('', '', false));

  $('#wsBrowseBtn')?.addEventListener('click', async () => {
    const btn = $('#wsBrowseBtn');
    btn.disabled = true;
    btn.textContent = '⏳ Открываю…';
    try {
      const res = await api('/api/workspace/browse', {});
      if (res && res.ok && !res.cancelled && res.path) {
        $('#wsPathInput').value = res.path;
        if ($('#wsNameInput') && !$('#wsNameInput').value.trim()) {
          $('#wsNameInput').value = _wsShortName(res.path);
        }
      }
    } catch (err) {
      pushNotice(err.message, 'bad');
    } finally {
      btn.disabled = false;
      btn.textContent = '📂 Обзор…';
    }
  });

  $('#wsAddSelectBtn')?.addEventListener('click', () => {
    const p = ($('#wsPathInput') && $('#wsPathInput').value.trim()) || '';
    if (!p) {
      $('#wsPathInput')?.focus();
      pushNotice('Укажи путь к папке или нажми «📂 Обзор…» (либо выбери «Без рабочего пространства» выше)', 'warn');
      return;
    }
    const name = ($('#wsNameInput') && $('#wsNameInput').value.trim()) || '';
    const createDir = !!($('#wsCreateDirCheck') && $('#wsCreateDirCheck').checked);
    applyWorkspaceChoice(p, name, createDir);
  });

  $('#wsPathInput')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('#wsAddSelectBtn')?.click();
    }
  });
})();

(async function boot() {
  if (localStorage.getItem('lh.rail') === '1') app.classList.add('rail');
  if (localStorage.getItem('lh.noright') === '1') app.classList.add('no-right');
  if (localStorage.getItem('lh.thinkoff') === '1') {
    app.classList.add('thinkoff');
    $('#thinkBtn').classList.add('off');
    $('#thinkBtn').setAttribute('aria-pressed', 'false');
  }
  syncQuickControls();
  refreshDesktop();
  refreshRemote();
  await refreshState();
  const st = stateCache || {};
  const first = (st.sessions || [])[0];
  if (first) await openSession(first.id);
  else {
    const res = await api('/api/new', { title: '' });
    await openSession(res.session);
    await refreshState();
  }
  $('#input').focus();
  if (!st.provider_status || !st.provider_status.alive) {
    pushNotice('локальный сервер моделей молчит — нажми «разбудить» на вкладке LM Studio или запусти LM Studio сам', 'warn');
  }
  setView(localStorage.getItem('lh.view') || 'chat');
})();
