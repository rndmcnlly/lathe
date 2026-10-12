// Real workerd + KV + loopback HTTP/WebSocket peers. No fetch stubs or credentials.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { once } from "node:events";
import { createServer } from "node:http";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { WebSocketServer } from "ws";

const sessionName = "__Host-lathe_session";
const now = () => Math.floor(Date.now() / 1000);

async function fixture(t) {
  const codes = new Map();
  const tokens = new Map();
  const requests = [];
  const errors = [];
  let origin;
  let mf;
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url, origin);
      const reply = (body, status = 200) => {
        res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify(body));
      };
      if (url.pathname === "/.well-known/openid-configuration") {
        return reply({ issuer: origin, authorization_endpoint: `${origin}/authorize`,
          token_endpoint: `${origin}/token`, userinfo_endpoint: `${origin}/userinfo` });
      }
      if (url.pathname === "/authorize") {
        assert.equal(url.searchParams.get("client_id"), "local-client");
        assert.equal(url.searchParams.get("response_type"), "code");
        assert.equal(url.searchParams.get("code_challenge_method"), "S256");
        const code = randomUUID();
        codes.set(code, { challenge: url.searchParams.get("code_challenge"),
          redirect: url.searchParams.get("redirect_uri"),
          claims: { sub: "local-user", email: url.searchParams.get("test_email") || "owner@example.org",
            email_verified: url.searchParams.get("test_verified") !== "false" } });
        const callback = new URL(url.searchParams.get("redirect_uri"));
        callback.searchParams.set("code", code);
        callback.searchParams.set("state", url.searchParams.get("state"));
        res.writeHead(302, { location: callback.href });
        return res.end();
      }
      if (url.pathname === "/token") {
        let body = "";
        for await (const chunk of req) body += chunk;
        const form = new URLSearchParams(body);
        const issued = codes.get(form.get("code"));
        codes.delete(form.get("code"));
        assert.ok(issued, "code must have been issued by the local provider");
        assert.equal(form.get("client_secret"), "local-secret");
        assert.equal(form.get("client_id"), "local-client");
        assert.equal(form.get("grant_type"), "authorization_code");
        assert.equal(form.get("redirect_uri"), issued.redirect);
        assert.equal(createHash("sha256").update(form.get("code_verifier")).digest("base64url"), issued.challenge);
        const token = randomUUID();
        tokens.set(token, issued.claims);
        return reply({ access_token: token });
      }
      if (url.pathname === "/userinfo") {
        const claims = tokens.get(req.headers.authorization?.replace(/^Bearer /, ""));
        assert.ok(claims);
        return reply(claims);
      }
      let body = "";
      for await (const chunk of req) body += chunk;
      requests.push({ path: req.url, headers: req.headers, body });
      res.setHeader("set-cookie", [
        "application=response; Domain=preview.test; Path=/",
        `${sessionName}=forged; Path=/; Secure; HttpOnly`,
      ]);
      if (url.pathname === "/redirect") {
        res.writeHead(302, { location: `${origin}/next?q=1` });
        return res.end();
      }
      return reply(requests.at(-1));
    } catch (error) {
      errors.push(error);
      res.writeHead(500);
      res.end("local fixture assertion failed");
    }
  });
  const sockets = new WebSocketServer({ noServer: true });
  t.after(async () => {
    await mf?.dispose();
    for (const ws of sockets.clients) ws.terminate();
    await new Promise((resolve) => sockets.close(resolve));
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    assert.deepEqual(errors, [], "fixture assertions must survive Worker error handling");
  });
  sockets.on("headers", (headers) => {
    headers.push("Set-Cookie: application=upgrade; Domain=preview.test; Path=/");
    headers.push(`Set-Cookie: ${sessionName}=forged; Path=/; Secure; HttpOnly`);
  });
  server.on("upgrade", (req, socket, head) => {
    requests.push({ path: req.url, headers: req.headers });
    sockets.handleUpgrade(req, socket, head, (ws) => {
      ws.on("message", (data, binary) => ws.send(data, { binary }));
      ws.on("error", (error) => errors.push(error));
    });
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  origin = `http://127.0.0.1:${server.address().port}`;
  mf = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    scriptPath: fileURLToPath(new URL("../src/index.js", import.meta.url)),
    compatibilityDate: "2026-09-01", // Same behavioral floor as wrangler.toml.example.
    kvNamespaces: ["SUBDOMAINS"],
    bindings: { ZONE: "preview.test", REGISTER_TOKEN: "local-register",
      OIDC_ISSUER: origin, OIDC_CLIENT_ID: "local-client", OIDC_CLIENT_SECRET: "local-secret",
      REGISTRATION_ENCRYPTION_KEY: Buffer.alloc(32, 17).toString("base64") },
  }));
  const kv = await mf.getKVNamespace("SUBDOMAINS");
  const dispatch = (url, init) => mf.dispatchFetch(url, { redirect: "manual", ...init });
  const register = async (access = "private", extra = {}) => {
    const response = await dispatch("https://preview.test/register", {
      method: "POST", headers: { authorization: "Bearer local-register", "content-type": "application/json" },
      body: JSON.stringify({ owner: { subject: "owui-user", email: " Owner@Example.org " },
        upstream_url: origin, access, ttl: 3600, ...extra }),
    });
    assert.equal(response.status, 200, await response.clone().text());
    return response.json();
  };
  const authorize = async (registration, overrides = {}) => {
    const gate = await dispatch(`${registration.url}app?q=1`);
    assert.equal(gate.status, 302, await gate.clone().text());
    const authorization = new URL(gate.headers.get("location"));
    for (const [key, value] of Object.entries(overrides)) authorization.searchParams.set(key, value);
    const provider = await fetch(authorization, { redirect: "manual" });
    assert.equal(provider.status, 302);
    return provider.headers.get("location");
  };
  const login = async (registration) => {
    const response = await dispatch(await authorize(registration));
    assert.equal(response.status, 302, await response.clone().text());
    const cookie = response.headers.get("set-cookie");
    assert.match(cookie, /; Path=\/; Max-Age=\d+; Secure; HttpOnly; SameSite=Lax$/);
    assert.doesNotMatch(cookie, /Domain=/i);
    return cookie.split(";", 1)[0];
  };
  return { dispatch, register, authorize, login, kv, requests };
}

function applicationCookies(response, expected) {
  assert.deepEqual(response.headers.getSetCookie(), [`application=${expected}; Path=/`]);
}

test("workerd HTTP: private identity gate, host-bound state/session, and cookie isolation", { timeout: 20000 }, async (t) => {
  const f = await fixture(t);
  const privatePreview = await f.register();
  const sibling = await f.register();
  assert.equal((await f.dispatch(privatePreview.url)).status, 302);
  assert.equal(f.requests.length, 0, "anonymous requests must not reach the application");
  for (const claims of [{ test_email: "attacker@example.org" }, { test_verified: "false" }]) {
    const result = await f.dispatch(await f.authorize(privatePreview, claims));
    assert.equal(result.status, 403);
    assert.equal(result.headers.get("set-cookie"), null);
  }
  const callback = await f.authorize(privatePreview);
  const swapped = new URL(callback);
  swapped.host = sibling.host;
  assert.equal((await f.dispatch(swapped.href)).status, 401);
  const validCallback = await f.authorize(privatePreview);
  assert.equal((await f.dispatch(validCallback)).status, 302);
  assert.equal((await f.dispatch(validCallback)).status, 401, "used state cannot be replayed locally");
  const cookie = await f.login(privatePreview);
  assert.equal((await f.dispatch(sibling.url, { headers: { cookie } })).status, 302);
  assert.equal(f.requests.length, 0, "a sibling cannot borrow the session even if sent manually");
  const response = await f.dispatch(`${privatePreview.url}app?q=1`, {
    method: "POST", headers: { cookie: `${cookie}; application=request` }, body: "payload",
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), f.requests.at(-1));
  assert.equal(f.requests.at(-1).path, "/app?q=1");
  assert.equal(f.requests.at(-1).body, "payload");
  assert.equal(f.requests.at(-1).headers.cookie, "application=request");
  applicationCookies(response, "response");
  const redirect = await f.dispatch(`${privatePreview.url}redirect`, { headers: { cookie } });
  assert.equal(redirect.headers.get("location"), `${privatePreview.url}next?q=1`);
  const publicPreview = await f.register("public");
  assert.equal((await f.dispatch(publicPreview.url)).status, 200);
});

test("workerd WebSocket: gated real upgrade, injected headers, sanitized cookies, and frame forwarding", { timeout: 20000 }, async (t) => {
  const f = await fixture(t);
  for (const access of ["public", "private"]) {
    const registration = await f.register(access, { upstream_headers: { Authorization: "Basic local-app" } });
    const headers = { upgrade: "websocket", connection: "Upgrade, Authorization",
      authorization: "Bearer browser-spoof", "sec-websocket-protocol": "local-echo" };
    if (access === "private") {
      const blocked = await f.dispatch(`${registration.url}socket`, { headers });
      assert.equal(blocked.status, 302);
      assert.equal(f.requests.length, 1, "only the previous public handshake reached upstream");
      headers.cookie = `${await f.login(registration)}; application=request`;
    } else headers.cookie = "application=request";
    const response = await f.dispatch(`${registration.url}socket?q=1`, { headers });
    assert.equal(response.status, 101);
    assert.ok(response.webSocket, "actual workerd WebSocket handle");
    assert.equal(response.headers.get("sec-websocket-protocol"), "local-echo");
    applicationCookies(response, "upgrade");
    assert.equal(f.requests.at(-1).headers.cookie, "application=request");
    assert.equal(f.requests.at(-1).headers.authorization, "Basic local-app");
    assert.equal(f.requests.at(-1).path, "/socket?q=1");
    const ws = response.webSocket;
    ws.accept();
    const echo = new Promise((resolve, reject) => {
      ws.addEventListener("message", (event) => resolve(event.data), { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    ws.send(`echo-${access}`);
    assert.equal(await echo, `echo-${access}`);
    const binaryEcho = new Promise((resolve) => ws.addEventListener("message", (event) => resolve(event.data), { once: true }));
    ws.send(new Uint8Array([0, 255, 7]));
    assert.deepEqual(new Uint8Array(await binaryEcho), new Uint8Array([0, 255, 7]));
    ws.close(1000, "done");
  }
});

test("workerd KV: short remaining leases can sign in without extending authorization", { timeout: 20000 }, async (t) => {
  const f = await fixture(t);
  const registration = await f.register();
  const record = JSON.parse(await f.kv.get(registration.host));
  record.expires_at = now() + 30;
  await f.kv.put(registration.host, JSON.stringify(record), { expirationTtl: 60 });
  const cookie = await f.login(registration);
  const token = cookie.split("=", 2)[1];
  const session = JSON.parse(await f.kv.get(`_lathe:session:${token}`));
  assert.equal(session.expires_at, record.expires_at);
  assert.equal((await f.dispatch(registration.url, { headers: { cookie } })).status, 200);
  // Keep a stale KV value readable: absolute expiry must still fail closed.
  session.expires_at = now() - 1;
  await f.kv.put(`_lathe:session:${token}`, JSON.stringify(session), { expirationTtl: 60 });
  assert.equal((await f.dispatch(registration.url, { headers: { cookie } })).status, 302);
  const callback = await f.authorize(registration);
  const stateKey = `_lathe:state:${new URL(callback).searchParams.get("state")}`;
  const state = JSON.parse(await f.kv.get(stateKey));
  state.expires_at = now() - 1;
  await f.kv.put(stateKey, JSON.stringify(state), { expirationTtl: 60 });
  assert.equal((await f.dispatch(callback)).status, 401);
  record.expires_at = now() - 1;
  await f.kv.put(registration.host, JSON.stringify(record), { expirationTtl: 60 });
  assert.equal((await f.dispatch(registration.url, { headers: { cookie } })).status, 404);
  assert.equal(f.requests.length, 1);
});

test("workerd KV actually expires registration, state, and session; rejects sub-minute TTLs", { timeout: 90000 }, async (t) => {
  const f = await fixture(t);
  await assert.rejects(f.kv.put("invalid-ttl", "value", { expirationTtl: 30 }), /60/);
  const registration = await f.register("private", { ttl: 60 });
  const abandonedCallback = await f.authorize(registration);
  const stateKey = `_lathe:state:${new URL(abandonedCallback).searchParams.get("state")}`;
  const cookie = await f.login(registration);
  const sessionKey = `_lathe:session:${cookie.split("=", 2)[1]}`;
  assert.ok(await f.kv.get(registration.host));
  assert.ok(await f.kv.get(stateKey));
  assert.ok(await f.kv.get(sessionKey));
  assert.equal((await f.dispatch(registration.url, { headers: { cookie } })).status, 200);
  await delay(61000);
  assert.equal(await f.kv.get(registration.host), null, "KV storage expiry, not only application expiry");
  assert.equal(await f.kv.get(stateKey), null);
  assert.equal(await f.kv.get(sessionKey), null);
  assert.equal((await f.dispatch(registration.url, { headers: { cookie } })).status, 404);
  assert.equal((await f.dispatch(abandonedCallback)).status, 404);
  assert.equal(f.requests.length, 1);
});
