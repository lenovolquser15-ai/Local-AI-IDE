// LocalHarness relay. The desktop keeps an outbound WebSocket to this Worker and
// serves its HTTP API back through it, so no port forwarding or tunnel binary is
// needed. The connect page hands out a short code that becomes a signed cookie.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,X-LH-Ts,X-LH-Sign",
  "Access-Control-Max-Age": "86400",
};
const CODE_RE = /^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{10}$/;
const HOST_RE = /^[a-z0-9-]+\.trycloudflare\.com$/;
const CONNECT_FILES = new Set(["/connect.css", "/connect.js", "/robots.txt"]);
const HOP = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
  "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
  "cf-connecting-ip", "cf-ray", "cf-visitor", "cdn-loop", "x-forwarded-for",
  "x-forwarded-proto", "x-real-ip",
]);
const RESP_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "trailer", "upgrade"]);
const CHUNK = 384 * 1024;
const SINGLE = 384 * 1024;
const HEAD_TIMEOUT = 180000;
const COOKIE_CLEAR = "lh_remote=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Secure";
const enc = new TextEncoder();

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...CORS,
      ...extra,
    },
  });
}

function page(title, text, status = 200) {
  const body = `<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LocalHarness</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f17;
color:#e8eefc;font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:24px}
.box{max-width:420px;background:#131a26;border:1px solid #24314a;border-radius:18px;padding:26px}
h1{font-size:18px;margin:0 0 10px}p{margin:0;color:#9fb0cc}a{color:#8fd0ff}</style>
<div class="box"><h1>${title}</h1><p>${text}</p></div></html>`;
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
               "X-Robots-Tag": "noindex" },
  });
}

function normalize(raw) {
  return String(raw || "").toUpperCase().replace(/[^0-9A-Z]/g, "")
    .replace(/O/g, "0").replace(/[IL]/g, "1").replace(/U/g, "V");
}

function cookieToken(raw) {
  for (const chunk of String(raw || "").split(";")) {
    const at = chunk.indexOf("=");
    if (at > 0 && chunk.slice(0, at).trim() === "lh_remote") return chunk.slice(at + 1).trim();
  }
  return "";
}

async function importKey(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

async function verify(secret, message, hex) {
  if (!/^[0-9a-f]{64}$/i.test(String(hex || ""))) return false;
  const key = await importKey(secret);
  const bytes = new Uint8Array(hex.match(/.{2}/g).map((h) => parseInt(h, 16)));
  return crypto.subtle.verify("HMAC", key, bytes, enc.encode(message));
}

async function mint(secret, exp, epoch, sid) {
  const key = await importKey(secret);
  const message = `v1|${exp}|${sid}|${epoch}`;
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
  const hex = [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `v1.${exp}.${sid}.${epoch}.${hex}`;
}

async function tokenInfo(secret, token) {
  const parts = String(token || "").split(".");
  if (parts.length !== 5 || parts[0] !== "v1") return null;
  const exp = Number(parts[1]);
  if (!Number.isFinite(exp) || exp <= Math.floor(Date.now() / 1000)) return null;
  if (!CODE_RE.test(parts[2])) return null;
  if (!(await verify(secret, `v1|${parts[1]}|${parts[2]}|${parts[3]}`, parts[4]))) return null;
  return { exp, sid: parts[2], epoch: Number(parts[3]) || 1 };
}

function fresh(ts) {
  const now = Math.floor(Date.now() / 1000);
  // сутки — с запасом на сбитые часы на домашних ПК; replay прикрывает короткий срок кода
  return /^\d{9,12}$/.test(String(ts || "")) && Math.abs(now - Number(ts)) <= 86400;
}

function b64encode(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(out);
}

function b64decode(text) {
  const raw = atob(String(text || ""));
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

async function hub(env, op, payload) {
  const stub = env.ROOMS.get(env.ROOMS.idFromName("hub"));
  return stub.fetch("https://room" + op, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function asset(env, request, path) {
  const url = new URL(request.url);
  return env.ASSETS.fetch(new Request(new URL(path, url.origin), {
    method: "GET",
    headers: { "Accept": "text/html,application/javascript,text/css,*/*" },
  }));
}

async function roomState(env, code) {
  const res = await hub(env, "/peek", { code });
  const data = await res.json().catch(() => ({}));
  return data.ok ? data.room : null;
}

async function connectPage(env, request, stale) {
  const res = await asset(env, request, "/index.html");
  const headers = new Headers(res.headers);
  headers.set("Cache-Control", "no-store");
  headers.set("Vary", "Cookie");
  if (stale) headers.append("Set-Cookie", COOKIE_CLEAR);
  return new Response(res.body, { status: res.status, headers });
}

async function agentEntry(request, env) {
  const code = normalize(request.headers.get("X-LH-Code"));
  const ts = request.headers.get("X-LH-Ts");
  if (request.headers.get("Upgrade") !== "websocket") {
    return json({ ok: false, error: "нужен websocket" }, 426);
  }
  if (!CODE_RE.test(code) || !fresh(ts) ||
      !(await verify(env.LH_SECRET, `${ts}.${code}`, request.headers.get("X-LH-Sign")))) {
    return json({ ok: false, error: "подпись не принята" }, 401);
  }
  return env.ROOMS.get(env.ROOMS.idFromName(code)).fetch(request);
}

async function claim(request, env) {
  const url = new URL(request.url);
  const token = String(url.searchParams.get("rt") || "");
  const info = await tokenInfo(env.LH_SECRET, token);
  if (!info) {
    return page("Код недействителен",
      "Срок действия истёк или ссылка повреждена. Запроси новый код в приложении LocalHarness.", 403);
  }
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const res = await hub(env, "/get", { code: info.sid, ip });
  const data = await res.json().catch(() => ({}));
  if (!data.ok || Number(data.room.epoch) !== info.epoch) {
    return page("Код не найден",
      "Код устарел или удалён на компьютере. Запроси новый в разделе «Удалённый доступ».", 403);
  }
  if (!data.room.relay) {
    return new Response(null, {
      status: 302,
      headers: { "Location": String(data.room.url) + "/api/remote/claim?rt=" + encodeURIComponent(token),
                 "Cache-Control": "no-store", "X-Robots-Tag": "noindex" },
    });
  }
  const maxAge = Math.max(60, info.exp - Math.floor(Date.now() / 1000));
  return new Response(null, {
    status: 302,
    headers: {
      "Location": "/",
      "Set-Cookie": `lh_remote=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}; Secure`,
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex",
    },
  });
}

async function roomPut(request, env) {
  const body = await request.text();
  const ts = request.headers.get("X-LH-Ts");
  if (!fresh(ts) || !(await verify(env.LH_SECRET, `${ts}.${body}`, request.headers.get("X-LH-Sign")))) {
    return json({ ok: false, error: "подпись не принята" }, 401);
  }
  let data;
  try { data = JSON.parse(body); } catch { return json({ ok: false, error: "битый JSON" }, 400); }
  const code = normalize(data.code);
  const now = Math.floor(Date.now() / 1000);
  const exp = Number(data.exp) || 0;
  if (!CODE_RE.test(code)) return json({ ok: false, error: "код должен быть из 10 символов" }, 400);
  if (!(exp > now + 30 && exp <= now + 7 * 86400)) return json({ ok: false, error: "неверный срок кода" }, 400);
  const relay = Boolean(data.relay);
  let url = String(data.url || "").replace(/\/+$/, "");
  if (!relay) {
    let host = "";
    try { host = new URL(url).hostname; } catch { /* invalid */ }
    if (!HOST_RE.test(host)) return json({ ok: false, error: "разрешены только адреса trycloudflare.com" }, 400);
  }
  const room = { code, url, exp, epoch: Number(data.epoch) || 1, relay };
  const res = await hub(env, "/put", room);
  if (!res.ok) return json({ ok: false, error: "реле не сохранило код" }, 500);
  return json({ ok: true, code, exp, relay });
}

async function roomDel(request, env) {
  const code = normalize(new URL(request.url).pathname.split("/").pop());
  const ts = request.headers.get("X-LH-Ts");
  if (!fresh(ts) || !(await verify(env.LH_SECRET, `${ts}.${code}`, request.headers.get("X-LH-Sign")))) {
    return json({ ok: false, error: "подпись не принята" }, 401);
  }
  await hub(env, "/del", { code });
  return json({ ok: true });
}

async function roomGet(request, env) {
  const code = normalize(new URL(request.url).pathname.split("/").pop());
  if (!CODE_RE.test(code)) return json({ ok: false, error: "код должен быть из 10 символов" }, 400);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const res = await hub(env, "/get", { code, ip });
  const data = await res.json().catch(() => ({}));
  if (!data.ok) return json({ ok: false, error: data.error || "код не найден или истёк" }, res.status || 404);
  const token = await mint(env.LH_SECRET, data.room.exp, data.room.epoch, code);
  return json({ ok: true, url: data.room.url, token, exp: data.room.exp,
                epoch: data.room.epoch, relay: Boolean(data.room.relay) });
}

async function appProxy(request, env) {
  const token = cookieToken(request.headers.get("Cookie"));
  const info = token ? await tokenInfo(env.LH_SECRET, token) : null;
  if (!info) {
    if (request.method === "GET" && String(request.headers.get("Accept") || "").includes("text/html")) {
      return asset(env, request, "/index.html");
    }
    return json({ ok: false, error: "нужен временный код доступа" }, 401);
  }
  const stub = env.ROOMS.get(env.ROOMS.idFromName(info.sid));
  return stub.fetch(request);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (url.pathname === "/agent") return agentEntry(request, env);
    if (url.pathname === "/api/health") {
      return json({ ok: true, service: "lh-router", relay: true, v: 2 });
    }
    if (url.pathname === "/api/rooms") {
      if (request.method === "POST") return roomPut(request, env);
    }
    if (url.pathname.startsWith("/api/rooms/")) {
      if (request.method === "DELETE") return roomDel(request, env);
      if (request.method === "GET") return roomGet(request, env);
    }
    if (url.pathname === "/api/remote/claim") return claim(request, env);
    if (url.pathname === "/" || url.pathname === "/connect" || url.pathname === "/connect/") {
      const token = cookieToken(request.headers.get("Cookie"));
      const info = token ? await tokenInfo(env.LH_SECRET, token) : null;
      if (!info) return connectPage(env, request, Boolean(token));
      const room = await roomState(env, info.sid);
      if (!room || Number(room.epoch) !== info.epoch) return connectPage(env, request, true);
      return appProxy(request, env);
    }
    if (CONNECT_FILES.has(url.pathname)) return asset(env, request, url.pathname);
    return appProxy(request, env);
  },
};

export class Rooms {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.hits = new Map();
    this.agent = null;
    this.next = 0;
    this.pending = new Map();
    this.acks = new Map();
  }

  sendAgent(payload) {
    const ws = this.agent;
    if (!ws) return false;
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      this.dropAgent("отправка не удалась");
      return false;
    }
  }

  dropAgent(reason) {
    const ws = this.agent;
    this.agent = null;
    if (ws) {
      try { ws.close(1011, String(reason || "closed").slice(0, 100)); } catch (err) { /* gone */ }
    }
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      if (p.headed && p.controller) {
        try { p.controller.error(new Error("компьютер отключился")); } catch (err) { /* closed */ }
      } else {
        p.reject(new Error("компьютер отключился"));
      }
      this.pending.delete(id);
    }
    this.acks.clear();
  }

  acceptAgent(request) {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    if (this.agent) {
      try { this.agent.close(1000, "заменено новым подключением"); } catch (err) { /* gone */ }
    }
    this.agent = server;
    server.addEventListener("message", (event) => this.onAgentMessage(event.data));
    server.addEventListener("close", () => {
      if (this.agent === server) this.dropAgent("закрыто");
    });
    server.addEventListener("error", () => {
      if (this.agent === server) this.dropAgent("ошибка websocket");
    });
    return new Response(null, { status: 101, webSocket: client });
  }

  onAgentMessage(raw) {
    if (typeof raw !== "string") return;
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.t === "ping") { this.sendAgent({ t: "pong" }); return; }
    if (msg.t === "ack") {
      const done = this.acks.get(String(msg.id) + ":" + String(msg.n));
      if (done) done();
      return;
    }
    const id = String(msg.id || "");
    const p = id ? this.pending.get(id) : null;
    if (!p) return;
    if (msg.t === "head") {
      if (p.headed) return;
      p.headed = true;
      clearTimeout(p.timer);
      const headers = new Headers();
      for (const pair of msg.headers || []) {
        const key = String(pair[0] || "").toLowerCase();
        if (!key || RESP_HOP.has(key)) continue;
        try { headers.append(key, String(pair[1])); } catch (err) { /* bad header */ }
      }
      const stream = new ReadableStream({
        start: (controller) => { p.controller = controller; },
        cancel: () => {
          this.pending.delete(id);
          this.sendAgent({ t: "cancel", id });
        },
      });
      p.resolve(new Response(stream, { status: Number(msg.status) || 200, headers }));
      return;
    }
    if (msg.t === "chunk") {
      if (p.controller && msg.data) {
        try { p.controller.enqueue(b64decode(msg.data)); } catch (err) { /* closed */ }
      }
      return;
    }
    if (msg.t === "end") {
      clearTimeout(p.timer);
      this.pending.delete(id);
      if (p.controller) {
        try { p.controller.close(); } catch (err) { /* closed */ }
      }
      return;
    }
    if (msg.t === "err") {
      clearTimeout(p.timer);
      this.pending.delete(id);
      const error = new Error(String(msg.error || "ошибка на компьютере").slice(0, 200));
      if (p.controller) {
        try { p.controller.error(error); } catch (err) { /* closed */ }
      } else {
        p.reject(error);
      }
    }
  }

  waitAck(id, n) {
    return new Promise((resolve) => {
      const key = id + ":" + n;
      const timer = setTimeout(() => { this.acks.delete(key); resolve(); }, 20000);
      this.acks.set(key, () => { clearTimeout(timer); this.acks.delete(key); resolve(); });
    });
  }

  async pump(request, id) {
    const url = new URL(request.url);
    const headers = {};
    for (const [name, value] of request.headers) {
      const key = name.toLowerCase();
      if (!HOP.has(key)) headers[key] = value;
    }
    const raw = request.body ? new Uint8Array(await request.arrayBuffer()) : new Uint8Array(0);
    const base = { t: "req", id, method: request.method, path: url.pathname + url.search, headers };
    if (raw.length === 0) { this.sendAgent({ ...base, size: 0 }); return; }
    if (raw.length <= SINGLE) { this.sendAgent({ ...base, body: b64encode(raw) }); return; }
    this.sendAgent({ ...base, size: raw.length });
    for (let i = 0, n = 0; i < raw.length; i += CHUNK, n += 1) {
      this.sendAgent({ t: "body", id, n, data: b64encode(raw.subarray(i, i + CHUNK)) });
      await this.waitAck(id, n);
    }
    this.sendAgent({ t: "bodyend", id });
  }

  proxy(request) {
    if (!this.agent) {
      if (String(request.headers.get("Accept") || "").includes("text/html")) {
        return page("Компьютер не в сети",
          "Реле работает, но приложение на компьютере сейчас не отвечает. " +
          "Проверь, что LocalHarness открыт, и обнови страницу.", 502);
      }
      return json({ ok: false, error: "компьютер сейчас не в сети — включи удалённый доступ" }, 502);
    }
    const id = String(++this.next);
    const head = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("компьютер не ответил вовремя"));
      }, HEAD_TIMEOUT);
      this.pending.set(id, { resolve, reject, controller: null, headed: false, timer });
    });
    this.pump(request, id).catch((err) => {
      const p = this.pending.get(id);
      if (p && !p.headed) {
        clearTimeout(p.timer);
        this.pending.delete(id);
        p.reject(err);
      }
    });
    return head.catch((err) => json({ ok: false, error: String(err.message || err) }, 504));
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.headers.get("Upgrade") === "websocket" && url.pathname === "/agent") {
      return this.acceptAgent(request);
    }
    if (url.pathname === "/put") {
      const body = await request.json();
      const rooms = (await this.state.storage.get("rooms")) || {};
      const now = Math.floor(Date.now() / 1000);
      for (const [key, value] of Object.entries(rooms)) {
        if (!value || Number(value.exp) <= now) delete rooms[key];
      }
      rooms[normalize(body.code)] = { url: String(body.url || ""), exp: Number(body.exp) || 0,
                                      epoch: Number(body.epoch) || 1, relay: Boolean(body.relay) };
      await this.state.storage.put("rooms", rooms);
      return json({ ok: true });
    }
    if (url.pathname === "/del") {
      const body = await request.json();
      const rooms = (await this.state.storage.get("rooms")) || {};
      if (rooms[normalize(body.code)]) {
        delete rooms[normalize(body.code)];
        await this.state.storage.put("rooms", rooms);
      }
      return json({ ok: true });
    }
    if (url.pathname === "/peek") {
      const body = await request.json();
      const now = Math.floor(Date.now() / 1000);
      const rooms = (await this.state.storage.get("rooms")) || {};
      const room = rooms[normalize(body.code)];
      if (!room || Number(room.exp) <= now) return json({ ok: false }, 404);
      return json({ ok: true, room });
    }
    if (url.pathname === "/get") {
      const now = Math.floor(Date.now() / 1000);
      const body = await request.json();
      const ip = String(body.ip || "unknown").slice(0, 64);
      const bucket = Math.floor(now / 60);
      const seen = this.hits.get(ip);
      const count = seen && seen.bucket === bucket ? seen.count + 1 : 1;
      this.hits.set(ip, { bucket, count });
      if (this.hits.size > 500) {
        for (const [key, value] of this.hits) if (value.bucket < bucket - 5) this.hits.delete(key);
      }
      if (count > 12) return json({ ok: false, error: "слишком много попыток, подожди минуту" }, 429);
      const rooms = (await this.state.storage.get("rooms")) || {};
      let dirty = false;
      for (const [key, value] of Object.entries(rooms)) {
        if (!value || value.exp <= now) { delete rooms[key]; dirty = true; }
      }
      if (dirty) await this.state.storage.put("rooms", rooms);
      const room = rooms[normalize(body.code)];
      if (!room) return json({ ok: false, error: "код не найден или истёк" }, 404);
      return json({ ok: true, room });
    }
    return this.proxy(request);
  }
}
