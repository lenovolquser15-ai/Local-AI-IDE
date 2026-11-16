// Runs the Cloudflare relay Worker in plain Node with a fake Durable Object.
// Usage: node tests/test_worker.mjs <secret>
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const secret = process.argv[2];
if (!secret) {
  console.error("secret argument is required");
  process.exit(2);
}

const tmp = mkdtempSync(join(tmpdir(), "lh-worker-"));
const modulePath = join(tmp, "worker.mjs");
copyFileSync(new URL("../cloudflare/worker/worker.js", import.meta.url), modulePath);

const worker = (await import(pathToFileURL(modulePath).href)).default;
const { Rooms } = await import(pathToFileURL(modulePath).href);

const storage = new Map();
const state = {
  storage: {
    async get(key) { return storage.get(key); },
    async put(key, value) { storage.set(key, value); },
  },
};
const rooms = new Rooms(state, {});
const env = {
  LH_SECRET: secret,
  ROOMS: {
    idFromName: (name) => name,
    get: () => ({ fetch: (url, init) => rooms.fetch(new Request(url, init)) }),
  },
  ASSETS: {
    fetch: (url) => new Response(String(url), { status: 200, headers: { "content-type": "text/html" } }),
  },
};

const base = "https://relay.test";
function signed(ts, body) {
  return createHmac("sha256", secret).update(`${ts}.${body}`).digest("hex");
}
async function call(method, path, { body, headers = {} } = {}) {
  return worker.fetch(new Request(base + path, { method, body, headers }), env);
}
function now() { return Math.floor(Date.now() / 1000); }
async function register(code, url, ttl = 3600, relay = false, epoch = 1) {
  const ts = String(now());
  const body = JSON.stringify({ code, url, exp: now() + ttl, epoch, relay });
  return call("POST", "/api/rooms", {
    body,
    headers: { "content-type": "application/json", "X-LH-Ts": ts, "X-LH-Sign": signed(ts, body) },
  });
}
function normalizeHex(hex) { return String(hex || "").toLowerCase(); }

try {
  let res = await call("GET", "/api/health");
  assert.equal(res.status, 200);
  const health = await res.json();
  assert.equal(health.relay, true);
  assert.equal(health.v, 2);

  const code = "AB12CD34EF";
  res = await register(code, "https://test-abc.trycloudflare.com");
  assert.equal(res.status, 200, "register should succeed");
  res = await call("GET", "/api/rooms/" + code, { headers: { "CF-Connecting-IP": "1.1.1.1" } });
  const tunnel = await res.json();
  assert.equal(tunnel.ok, true);
  assert.equal(tunnel.url, "https://test-abc.trycloudflare.com");
  assert.equal(tunnel.relay, false);
  assert.equal(tunnel.token.split(".")[2], code, "token sid carries the code");

  res = await call("GET", "/api/remote/claim?rt=" + tunnel.token,
    { headers: { "CF-Connecting-IP": "1.1.1.4" } });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("location"),
    "https://test-abc.trycloudflare.com/api/remote/claim?rt=" + tunnel.token);

  const relayCode = "RX12CD34EF";
  res = await register(relayCode, "", 3600, true);
  assert.equal(res.status, 200, "relay register accepts empty url");
  res = await call("GET", "/api/rooms/" + relayCode, { headers: { "CF-Connecting-IP": "2.2.2.1" } });
  const relayRoom = await res.json();
  assert.equal(relayRoom.ok, true);
  assert.equal(relayRoom.relay, true);
  const parts = relayRoom.token.split(".");
  assert.equal(parts.length, 5);
  assert.equal(parts[0], "v1");
  assert.equal(parts[2], relayCode, "relay token must carry the code");
  const expected = createHmac("sha256", secret).update(`v1|${parts[1]}|${parts[2]}|${parts[3]}`).digest("hex");
  assert.equal(normalizeHex(parts[4]), expected, "token signature must verify");

  res = await call("GET", "/api/remote/claim?rt=" + relayRoom.token,
    { headers: { "CF-Connecting-IP": "2.2.2.2" } });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("location"), "/");
  const cookie = res.headers.get("set-cookie") || "";
  assert.match(cookie, /lh_remote=v1\./);
  assert.match(cookie, /HttpOnly/);
  assert.match(cookie, /Secure/);

  res = await call("GET", "/");
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") || "", /text\/html/,
    "anonymous visit shows the connect page");

  res = await call("GET", "/", { headers: { "Cookie": cookie.split(";")[0], "Accept": "text/html" } });
  assert.equal(res.status, 502, "valid cookie reaches the app proxy");
  assert.match(res.headers.get("content-type") || "", /text\/html/);
  assert.match(await res.text(), /не в сети/, "offline app proxy returns an HTML page");

  const code2 = "KX12CD34EF";
  await register(code2, "", 600, true);
  res = await call("GET", "/api/rooms/" + code2, { headers: { "CF-Connecting-IP": "3.3.3.1" } });
  const shortRoom = await res.json();
  await register(code2, "", 7200, true);
  res = await call("GET", "/api/rooms/" + code2, { headers: { "CF-Connecting-IP": "3.3.3.2" } });
  const extended = await res.json();
  assert.ok(extended.exp > shortRoom.exp, "re-registering the same code extends its expiry");
  assert.notEqual(extended.token, shortRoom.token, "extended room mints a new token");

  res = await register(relayCode, "", 3600, true, 2);
  assert.equal(res.status, 200);
  res = await call("GET", "/", { headers: { "Cookie": cookie.split(";")[0] } });
  assert.equal(res.status, 200, "stale cookie falls back to the connect page");
  assert.match(res.headers.get("content-type") || "", /text\/html/);
  assert.match(res.headers.get("set-cookie") || "", /lh_remote=;.*Max-Age=0/,
    "stale cookie is cleared");

  res = await call("GET", "/connect", { headers: { "Cookie": cookie.split(";")[0] } });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") || "", /text\/html/);
  res = await call("GET", "/api/remote/claim?rt=" + relayRoom.token,
    { headers: { "CF-Connecting-IP": "2.2.2.3" } });
  assert.equal(res.status, 403, "claim after epoch bump must fail");

  res = await call("GET", "/api/remote/claim?rt=v1.1.2.3", { headers: { "CF-Connecting-IP": "2.2.2.4" } });
  assert.equal(res.status, 403);

  const ts = String(now());
  const body = JSON.stringify({ code: "ZZ12CD34EF", url: "https://x.trycloudflare.com",
    exp: now() + 600, epoch: 1 });
  res = await call("POST", "/api/rooms", { body, headers: { "X-LH-Ts": ts, "X-LH-Sign": "0".repeat(64) } });
  assert.equal(res.status, 401, "bad signature must be rejected");

  res = await register("QQ12CD34EF", "https://evil.example.com");
  assert.equal(res.status, 400, "non-tunnel url must be rejected");
  res = await register("QQ12CD34EG", "", 3600, false);
  assert.equal(res.status, 400, "empty url must be rejected without relay flag");

  res = await call("GET", "/agent", { headers: { "X-LH-Code": relayCode, "X-LH-Ts": ts,
    "X-LH-Sign": "0".repeat(64), "Upgrade": "websocket" } });
  assert.equal(res.status, 401, "agent without a signature must be rejected");

  const delSig = createHmac("sha256", secret).update(`${ts}.${relayCode}`).digest("hex");
  res = await call("DELETE", "/api/rooms/" + relayCode, { headers: { "X-LH-Ts": ts, "X-LH-Sign": delSig } });
  assert.equal(res.status, 200);
  res = await call("GET", "/api/rooms/" + relayCode, { headers: { "CF-Connecting-IP": "2.2.2.5" } });
  assert.equal(res.status, 404, "deleted code must not resolve");

  await register("RX12CD34EH", "", 3600, true);
  let last = 0;
  for (let i = 0; i < 13; i += 1) {
    res = await call("GET", "/api/rooms/RX12CD34EH", { headers: { "CF-Connecting-IP": "9.9.9.9" } });
    last = res.status;
  }
  assert.equal(last, 429, "rate limit should kick in");

  console.log(JSON.stringify({ ok: true, token: relayRoom.token, exp: Number(parts[1]), epoch: Number(parts[3]) }));
} catch (err) {
  console.error("FAIL:", err && err.stack ? err.stack : err);
  process.exit(1);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
