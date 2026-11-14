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

function md(text) {
  let html = esc(text);
  const fences = [];
  html = html.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    fences.push('<pre>' + code.replace(/\n$/, '') + '</pre>');
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

function pushUser(text, images) {
  ensureTurn();
  const box = node('div', 'msg user');
  box.appendChild(node('div', 'body', md(text)));
  if (images) box.appendChild(node('div', 'attach', 'прикреплено изображений: ' + images));
  add(box);
}

function pushAssistant(html, label) {
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

/* ---------- reasoning: inline "глубокие раздумья" line ---------- */
function diveStart() {
  if (diveEl) return diveEl;
  ensureTurn();
  const wrap = node('div', 'dive');
  const head = node('div', 'dive-head live');
  head.innerHTML = '<span class="mark">⌁</span><span class="label">Глубокие раздумья</span>' +
    '<span class="dive-dur"></span>';
  const body = node('div', 'dive-body');
  const meta = node('div', 'dive-meta', 'идёт поток · нажми, чтобы свернуть');
  head.onclick = () => { body.hidden = !body.hidden; meta.textContent = body.hidden ? 'свернуто · нажми, чтобы открыть' : 'идёт поток · нажми, чтобы свернуть'; if (!body.hidden) body.scrollTop = body.scrollHeight; };
  wrap.append(head, meta, body);
  body.hidden = !expandReasoning;
  if (!showReasoning) wrap.hidden = true;
  add(wrap);
  diveEl = { wrap: wrap, head: head, body: body, text: '', t0: Date.now(), live: !replaying };
  return diveEl;
}

function showDive(piece) {
  const d = diveStart();
  d.wrap.hidden = !showReasoning;
  d.text += piece;
  d.body.textContent = d.text.length > 8000 ? d.text.slice(-8000) : d.text;
  d.body.scrollTop = d.body.scrollHeight;
}

function diveFinal(text) {
  const d = diveEl || diveStart();
  diveEl = null;
  const full = (text || d.text || '').trim();
  const secs = d.live ? ((Date.now() - d.t0) / 1000).toFixed(1) + ' с' : full.length + ' зн.';
  d.head.classList.remove('live');
  d.live = false;
  d.head.querySelector('.dive-dur').textContent = '· ' + secs + ' · ' + full.length.toLocaleString('ru-RU') + ' знаков';
  d.body.textContent = full.length > 12000 ? full.slice(-12000) : full;
  d.body.hidden = !expandReasoning;
  d.wrap.hidden = !showReasoning;
  d.body.scrollTop = 0;
}

function dropDive() { diveEl = null; }

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

function finishAnswer(html, label) {
  if (answerEl) {
    const box = answerEl.parentNode;
    const who = box.querySelector('.who');
    if (label && who) who.textContent = label;
    else if (label) box.insertBefore(node('div', 'who', esc(label)), box.firstChild);
    answerEl.innerHTML = html;
    answerEl = null;
    return;
  }
  pushAssistant(html, label);
}

function resetStream() { answerEl = null; }

/* ---------- tool calls ---------- */
function toolLine(ev, open) {
  ensureTurn();
  const wrap = node('div', 'call');
  const line = node('div', 'call-line ' + (ev.ok ? 'good' : 'bad'));
  const args = typeof ev.args === 'string' ? ev.args : JSON.stringify(ev.args || {});
  line.innerHTML = '<span class="chev">' + (open ? '▾' : '▸') + '</span>' +
    '<span class="name">' + esc(ev.tool) + '</span>' +
    '<span class="arg">' + esc(String(args).slice(0, 160)) + '</span>' +
    '<span class="when">' + at(ev.ts) + '</span>';
  const card = node('div', 'call-card');
  card.hidden = !open;
  if (args) card.appendChild(node('div', 'cmd', esc(shorten(args, 900))));
  card.appendChild(node('pre', null, esc(shorten(ev.result, 24000))));
  if (ev.image) {
    const img = node('img');
    img.src = '/shot?path=' + encodeURIComponent(ev.image);
    img.alt = 'скриншот';
    img.addEventListener('click', () => window.open(img.src));
    card.appendChild(img);
  }
  line.onclick = () => {
    card.hidden = !card.hidden;
    line.querySelector('.chev').textContent = card.hidden ? '▸' : '▾';
  };
  wrap.append(line, card);
  add(wrap);
  traceRow(ev.ok ? 'good' : 'bad', ev.tool, String(args));
  return wrap;
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
  $('#speed').textContent = bits.join(' · ');
  paintUsage(lastSpeed, null, null);
}

/* ---------- context meter: сколько контекста съедено и где начнётся сжатие ---------- */
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
  const threshold = Number(ev.threshold) || 0;
  const pct = Math.max(0, Math.min(100, Number(ev.pct) || 0));
  const markPct = win ? Math.max(0, Math.min(100, threshold * 100 / win)) : 0;
  bar.hidden = false;
  const fill = $('#ctxFill');
  fill.style.width = pct + '%';
  fill.className = 'ctxbar-fill' + (pct >= markPct ? ' over' : (pct >= markPct * 0.8 ? ' warn' : ''));
  const mark = $('#ctxMark');
  mark.style.left = markPct + '%';
  mark.title = 'сжатие при ~' + fmtTok(threshold) + ' токенов';
  $('#ctxLabel').textContent = 'контекст ' + pct + '% · ' + fmtTok(used) + '/' + fmtTok(win);
  $('#ctxLimit').textContent = 'сжатие при ' + fmtTok(threshold) + ' ток'
    + (ev.answer ? ' · резерв ' + fmtTok(ev.answer) : '');
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
  switch (ev.type) {
    case 'user':
      turn = null; dropDive(); resetStream(); closeBand();
      pushUser(ev.text, ev.images);
      break;
    case 'plan': pushNotice('→ ' + ev.text); break;
    case 'reason': showDive(ev.text); break;
    case 'thinking': diveFinal(ev.text); break;
    case 'delta': streamPiece(ev.text); break;
    case 'speed': showSpeed(ev); break;
    case 'goal': goalBand(ev); break;
    case 'tool_start': traceRow('', ev.tool, 'вызов…'); break;
    case 'tool': resetStream(); toolLine(ev, false); break;
    case 'question': case 'approve': resetStream(); dropDive(); askBand(ev); break;
    case 'notice': pushNotice(ev.text, 'warn'); break;
    case 'context': paintContext(ev); break;
    case 'compact': pushNotice('контекст сжат: свёрнуто ходов — ' + ev.folded_turns, 'warn compact'); break;
    case 'error': resetStream(); dropDive(); pushNotice('сбой: ' + ev.text, 'bad'); break;
    case 'stopped':
      resetStream(); dropDive(); pushNotice('остановлено', 'warn');
      finishRun(ev || {});
      break;
    case 'done':
      dropDive();
      finishAnswer(md(ev.text), 'модель · ' + (ev.protocol === 'json' ? 'json-инструменты' : 'native'));
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
  if (streamAbort) streamAbort.abort();
  running = false;
  sessionId = id;
  since = 0;
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
  const title = (data.meta && data.meta.title) || id;
  $('#sessionTitle').textContent = title;
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
}

/* ---------- view tabs ---------- */
function setView(name) {
  currentView = name;
  document.querySelectorAll('.view-tab').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  const chat = name === 'chat';
  log.hidden = !chat;
  $('#viewPlugins').hidden = name !== 'plugins';
  $('#viewLM').hidden = name !== 'lm';
  const composer = document.querySelector('.composer');
  if (composer) composer.style.display = chat ? '' : 'none';
  const quick = document.querySelector('.quick-controls');
  if (quick) quick.style.display = chat ? '' : 'none';
  $('#right').style.display = chat ? '' : 'none';
  localStorage.setItem('lh.view', name);
  if (name === 'plugins') refreshPlugins();
  if (name === 'lm') refreshLM();
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

async function refreshState() {
  const st = await api('/api/state');
  stateCache = st;
  const ps = st.provider_status || {};
  $('#provDot').className = 'dot ' + (ps.alive ? 'ok' : 'bad');
  $('#dnsChip').textContent = 'dns-обход: ' + (st.dns_probe && st.dns_probe.ok ? st.dns_probe.ip : 'не работает');
  $('#dnsChip').title = st.dns_probe && st.dns_probe.ok ? '' : (st.dns_probe || {}).error || '';
  $('#modelCount').textContent = (ps.label || st.provider) + ' · моделей: ' + ((ps.models || []).length);
  $('#lmContext').value = st.lms_context_length || st.context_tokens || 8192;
  $('#lmTtl').value = st.lms_ttl || 0;
  if (document.activeElement !== $('#systemPrompt')) $('#systemPrompt').value = st.system_prompt || '';
  $('#searxngEnabled').checked = !!st.search_searxng;
  if (document.activeElement !== $('#searxngUrl')) $('#searxngUrl').value = st.search_searxng_url || 'http://127.0.0.1:8080';
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
  $('#toolsChip').textContent = 'инструментов: ' + (st.tools || []).length + ' · профиль ' + (st.tool_profile || 'core');
  $('#toolsChip').title = (st.tools || []).join(', ');
  fillSelect($('#provider'), Object.keys(st.providers || {}).map((k) => [k, st.providers[k]]), st.provider);
  const models = ps.models || [];
  const visionSet = new Set(ps.vision_models || []);
  fillSelect($('#model'), models.map((m) => [m, shortModel(m) + (visionSet.has(m) ? ' · vision' : '')]), st.model || models[0] || '');
  fillSelect($('#profile'), [['core', 'core'], ['full', 'full']], st.tool_profile);
  fillSelect($('#protocol'), [['auto', 'tools auto'], ['native', 'tools native'], ['json', 'tools json']], st.tool_protocol);
  fillSelect($('#perms'), [['auto', 'права: авто'], ['unrestricted', 'права: всё'], ['ask', 'права: спрос']],
    (st.permissions && st.permissions.mode) || 'auto');
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
        finally { headUnload.disabled = false; headUnload.textContent = '⏏ Выгрузить модель'; }
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

function renderSessions() {
  if ($('#sessions .session-edit')) return;
  const filter = $('#sessionSearch').value.trim().toLocaleLowerCase();
  const rows = ((stateCache || {}).sessions || []).filter((row) =>
    (row.title + ' ' + row.id).toLocaleLowerCase().includes(filter));
  $('#sessions').replaceChildren();
  if (!rows.length) $('#sessions').append(node('div', 'page-sub', filter ? 'Ничего не найдено' : 'Пока нет сессий'));
  rows.forEach((row) => {
    const el = node('div', 'session' + (row.id === sessionId ? ' active' : ''));
    el.dataset.id = row.id;
    const label = node('button', 'session-title', esc(row.title || row.id));
    label.title = row.title || row.id;
    label.onclick = () => openSession(row.id).catch((err) => pushNotice(err.message, 'bad'));
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
    el.append(label, rename, download, del);
    $('#sessions').appendChild(el);
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

async function patchConfig(key, value) {
  await api('/api/config', { config: { [key]: value } });
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
    const [st, cat] = await Promise.all([api('/api/plugins'), api('/api/plugins/catalog')]);
    plugCache = { st, cat };
    renderPlugins();
  } catch (err) {
    $('#plugInstalled').innerHTML = '';
    $('#plugInstalled').append(node('div', 'empty-note', 'не удалось получить список: ' + err.message));
  }
}

function renderPlugins() {
  if (!plugCache) return;
  const st = plugCache.st;
  const cat = plugCache.cat;
  $('#plugDir').textContent = 'папка: ' + (st.dir || '…') + ' · загружено ' + (st.loaded || 0) +
    ' · инструментов ' + (st.tools || []).length;
  const q = plugFilter.trim().toLowerCase();
  const inst = $('#plugInstalled');
  inst.innerHTML = '';
  const files = (st.plugins || []).filter((p) => !q || (p.name + ' ' + p.file + ' ' + (p.tools || []).join(' ')).toLowerCase().includes(q));
  if (!files.length) inst.append(node('div', 'empty-note', 'пока пусто — поставь что-нибудь из каталога'));
  files.forEach((p) => {
    const on = p.enabled !== false;
    const actions = [];
    if (p.error) actions.push(node('span', 'pcard-file', 'ошибка'));
    actions.push(switchEl(on, async () => {
      try {
        await api('/api/plugins/toggle', { file: p.file, enabled: !on });
        refreshPlugins();
        refreshState();
      } catch (err) { alert(err.message); }
    }));
    actions.push(delBtn('удалить файл плагина', async () => {
      if (!confirm('Удалить ' + p.file + ' безвозвратно?')) return;
      try {
        await api('/api/plugins/remove', { file: p.file });
        refreshPlugins();
        refreshState();
      } catch (err) { alert(err.message); }
    }));
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

/* ---------- LM Studio view: two plates ---------- */
function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  const gb = bytes / 1073741824;
  if (gb >= 0.5) return gb.toFixed(1) + ' ГБ';
  return Math.round(bytes / 1048576) + ' МБ';
}

function renderLM(data) {
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
  const loaded = $('#lmLoaded');
  loaded.innerHTML = '';
  if (!(data.loaded || []).length) loaded.append(node('div', 'empty-note', 'в памяти пусто — загрузи модель с диска'));
  (data.loaded || []).forEach((m) => {
    const actions = [node('button', 'ghost', 'выгрузить')];
    actions[0].onclick = async () => {
      actions[0].disabled = true;
      actions[0].textContent = 'выгружается…';
      try {
        await api('/api/lms/unload', { model: m.modelKey || m.path || m.identifier });
        pushNotice('Модель выгружена из памяти', 'ok');
        await refreshLM();
        await refreshState();
      } catch (err) { alert(err.message); }
      finally { actions[0].disabled = false; actions[0].textContent = 'выгрузить'; }
    };
    const est = m.estimate || {};
    const card = plugCard({
      name: m.displayName || m.modelKey || m.path,
      desc: [m.status, m.contextLength ? 'контекст ' + m.contextLength : '', m.paramsString || '',
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
  const loadedKeys = new Set(
    (data.loaded || []).flatMap((m) => [m.modelKey, m.path, m.identifier, m.displayName].filter(Boolean))
  );
  (data.disk || []).forEach((m) => {
    const isLlm = m.type !== 'embedding';
    const isLoaded = loadedKeys.has(m.modelKey) || loadedKeys.has(m.path) || loadedKeys.has(m.identifier);
    const actions = [];
    if (isLoaded) {
      const unloadBtn = node('button', 'ghost', 'Выгрузить');
      unloadBtn.onclick = async () => {
        unloadBtn.disabled = true;
        unloadBtn.textContent = 'выгружается…';
        try {
          await api('/api/lms/unload', { model: m.modelKey || m.path || m.identifier });
          pushNotice('Модель выгружена из памяти', 'ok');
          await refreshLM();
          await refreshState();
        } catch (err) { alert(err.message); }
      };
      actions.push(unloadBtn);
    }
    const loadBtn = node('button', isLoaded ? 'ghost' : 'primary', isLoaded ? 'Перезагрузить' : 'Загрузить');
    loadBtn.onclick = async () => {
      loadBtn.disabled = true;
      loadBtn.textContent = 'грузится…';
      try { await api('/api/lms/load', { model: m.modelKey || m.path, context_length: Number($('#lmContext').value) || 8192, ttl: Number($('#lmTtl').value) || 0 }); refreshLM(); }
      catch (err) { alert(err.message); loadBtn.disabled = false; loadBtn.textContent = isLoaded ? 'Перезагрузить' : 'Загрузить'; }
    };
    actions.push(loadBtn);
    const est = m.estimate || {};
    const card = plugCard({
      name: m.displayName || m.modelKey || m.path,
      desc: [m.type, m.paramsString || '', m.architecture || '', m.trainedForToolUse ? 'tools' : '',
        m.vision ? 'vision' : '', 'контекст до ' + (m.maxContextLength || '?'),
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

async function refreshLM() {
  try {
    const data = await api('/api/lms/state');
    renderLM(data);
    const job = await api('/api/lms/job');
    renderJob(job);
    if (job && job.running && !lmJobTimer) {
      lmJobTimer = setTimeout(async () => { lmJobTimer = null; refreshLM(); }, 2000);
    }
  } catch (err) {
    $('#lmBase').textContent = 'сервер: ошибка ' + err.message;
  }
}

async function startDownload() {
  const q = $('#lmQuery').value.trim();
  if (!q) return;
  $('#lmDownload').disabled = true;
  try {
    await api('/api/lms/download', { query: q });
    $('#lmJobLog').hidden = false;
    $('#lmJobLog').textContent = 'запустил скачивание: ' + q;
    refreshLM();
  } catch (err) { alert(err.message); }
  $('#lmDownload').disabled = false;
}

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
const settingsDrawer = () => $('#settingsDrawer');
function setSettingsDrawer(open) {
  const d = settingsDrawer();
  if (!d) return;
  if (!open && d.contains(document.activeElement)) $('#gearBtn').focus();
  d.classList.toggle('open', open);
  d.setAttribute('aria-hidden', open ? 'false' : 'true');
  $('#gearBtn').classList.toggle('on', open);
}
$('#gearBtn').onclick = () => setSettingsDrawer(!settingsDrawer().classList.contains('open'));
$('#drawerClose').onclick = () => setSettingsDrawer(false);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setSettingsDrawer(false); });
$('#showReasoning').onchange = (e) => {
  showReasoning = e.target.checked;
  localStorage.setItem('lh.showReasoning', showReasoning ? '1' : '0');
  if (diveEl) diveEl.wrap.hidden = !showReasoning;
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
};
document.querySelector('.right-head').onclick = () => {
  app.classList.toggle('no-right');
  localStorage.setItem('lh.noright', app.classList.contains('no-right') ? '1' : '0');
};

document.querySelectorAll('.view-tab').forEach((b) => {
  b.onclick = () => setView(b.dataset.view);
});
$('#plugSearch').oninput = (e) => { plugFilter = e.target.value; renderPlugins(); };
$('#plugReload').onclick = async () => {
  try {
    await api('/api/plugins/reload', {});
    refreshPlugins();
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
$('#lmContext').onchange = (e) => patchConfig('lms_context_length', Number(e.target.value) || 8192);
$('#lmTtl').onchange = (e) => patchConfig('lms_ttl', Number(e.target.value) || 0);
$('#searxngEnabled').onchange = (e) => patchConfig('search_searxng', e.target.checked);
$('#searxngUrl').onchange = (e) => patchConfig('search_searxng_url', e.target.value.trim() || 'http://127.0.0.1:8080');

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
    await api('/api/memory/clear');
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
  patchConfig('lan_access', e.target.checked);
  $('#lanSection').hidden = !e.target.checked;
  if (e.target.checked) {
    pushNotice('Доступ по локальной сети включён. Перезапустите сервер, чтобы открыть порт для других устройств.', 'warn');
  }
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

(async function boot() {
  if (localStorage.getItem('lh.rail') === '1') app.classList.add('rail');
  if (localStorage.getItem('lh.noright') === '1') app.classList.add('no-right');
  if (localStorage.getItem('lh.thinkoff') === '1') {
    app.classList.add('thinkoff');
    $('#thinkBtn').classList.add('off');
  }
  syncQuickControls();
  refreshDesktop();
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
