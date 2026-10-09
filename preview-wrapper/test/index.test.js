import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import worker from "../src/index.js";

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

class MemoryKV {
  constructor() {
    this.values = new Map();
    this.options = new Map();
  }

  async get(key) {
    return this.values.get(key) ?? null;
  }

  async put(key, value, options) {
    this.values.set(key, value);
    this.options.set(key, options);
  }

  async delete(key) {
    this.values.delete(key);
  }

  async list() {
    return {
      keys: [...this.values.keys()].map((name) => ({ name })),
      list_complete: true,
    };
  }
}

function environment() {
  return {
    ZONE: "preview.test",
    REGISTER_TOKEN: "register-secret",
    OIDC_ISSUER: "https://auth.test",
    OIDC_CLIENT_ID: "preview-client",
    OIDC_CLIENT_SECRET: "oidc-secret",
    REGISTRATION_ENCRYPTION_KEY: Buffer.alloc(32, 17).toString("base64"),
    SUBDOMAINS: new MemoryKV(),
  };
}

async function register(env, access = "private", tag = "", extra = {}) {
  const response = await worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.REGISTER_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      owner: { subject: "owui-user", email: " Owner@Example.org " },
      upstream_url: "https://signed-upstream.test/credential",
      access,
      ...(tag ? { tag } : {}),
      ttl: 3600,
      ...extra,
    }),
  }), env);
  assert.equal(response.status, 200);
  return response.json();
}

test("Lathe registration requires an explicit access policy", async () => {
  const env = environment();
  const response = await worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.REGISTER_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      owner: { subject: "owui-user", email: "owner@example.org" },
      upstream_url: "https://signed-upstream.test/credential",
      requested_access: "private",
    }),
  }), env);
  assert.equal(response.status, 400);
  assert.match(await response.text(), /access/);
  assert.equal(env.SUBDOMAINS.values.size, 0);
});

test("public registrations proxy without OIDC", async () => {
  const env = environment();
  const registration = await register(env, "public");
  assert.deepEqual(Object.keys(registration).sort(), ["expires_at", "host", "url"]);
  assert.match(new URL(registration.url).hostname, /^lathe-public-[0-9a-f]{16}\.preview\.test$/);

  globalThis.fetch = async (input) => {
    assert.equal(input.toString(), "https://signed-upstream.test/path?q=1");
    return new Response("public content");
  };
  const response = await worker.fetch(new Request(`${registration.url}path?q=1`), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "public content");
});

test("private flow matches verified email and isolates the wrapper session", async () => {
  const env = environment();
  const registration = await register(env);
  assert.deepEqual(Object.keys(registration).sort(), ["expires_at", "host", "url"]);
  const host = new URL(registration.url).hostname;
  assert.match(host, /^lathe-private-[0-9a-f]{16}\.preview\.test$/);
  let forwardedCookie;

  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url === "https://auth.test/.well-known/openid-configuration") {
      return Response.json({
        issuer: "https://auth.test",
        authorization_endpoint: "https://auth.test/authorize",
        token_endpoint: "https://auth.test/token",
        userinfo_endpoint: "https://auth.test/userinfo",
      });
    }
    if (url === "https://auth.test/token") {
      const body = new URLSearchParams(init.body);
      assert.equal(body.get("client_id"), env.OIDC_CLIENT_ID);
      assert.equal(body.get("client_secret"), env.OIDC_CLIENT_SECRET);
      assert.ok(body.get("code_verifier"));
      return Response.json({ access_token: "access-token" });
    }
    if (url === "https://auth.test/userinfo") {
      assert.equal(new Headers(init.headers).get("authorization"), "Bearer access-token");
      return Response.json({
        sub: "pocket-id-user",
        email: "owner@example.org",
        email_verified: true,
      });
    }
    if (url === "https://signed-upstream.test/private?q=1") {
      forwardedCookie = new Headers(init.headers).get("cookie");
      const headers = new Headers();
      headers.append("set-cookie", "application=value; Domain=signed-upstream.test; Path=/");
      headers.append("set-cookie", "__Host-lathe_session=forged; Secure; Path=/");
      return new Response("private content", { headers });
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  const initial = await worker.fetch(new Request(`${registration.url}private?q=1`), env);
  assert.equal(initial.status, 302);
  const authorization = new URL(initial.headers.get("location"));
  assert.equal(authorization.origin, "https://auth.test");
  assert.equal(authorization.searchParams.get("redirect_uri"), `https://${host}/_lathe/auth/callback`);
  assert.equal(authorization.searchParams.get("code_challenge_method"), "S256");

  const callback = await worker.fetch(new Request(
    `https://${host}/_lathe/auth/callback?code=code&state=${authorization.searchParams.get("state")}`,
  ), env);
  assert.equal(callback.status, 302);
  assert.equal(callback.headers.get("location"), `https://${host}/private?q=1`);
  const setCookie = callback.headers.get("set-cookie");
  assert.match(setCookie, /^__Host-lathe_session=/);
  assert.match(setCookie, /Secure; HttpOnly; SameSite=Lax/);
  assert.doesNotMatch(setCookie, /Domain=/i);
  const sessionCookie = setCookie.split(";", 1)[0];

  const proxied = await worker.fetch(new Request(`${registration.url}private?q=1`, {
    headers: { cookie: `${sessionCookie}; application=request` },
  }), env);
  assert.equal(proxied.status, 200);
  assert.equal(await proxied.text(), "private content");
  assert.equal(forwardedCookie, "application=request");
  const returnedCookies = proxied.headers.getSetCookie();
  assert.equal(returnedCookies.length, 1);
  assert.match(returnedCookies[0], /^application=value;/);
  assert.doesNotMatch(returnedCookies[0], /Domain=/i);
});

test("private flow rejects a different verified email", async () => {
  const env = environment();
  const registration = await register(env);
  const host = new URL(registration.url).hostname;

  globalThis.fetch = async (input) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith("/.well-known/openid-configuration")) {
      return Response.json({
        issuer: "https://auth.test",
        authorization_endpoint: "https://auth.test/authorize",
        token_endpoint: "https://auth.test/token",
        userinfo_endpoint: "https://auth.test/userinfo",
      });
    }
    if (url === "https://auth.test/token") return Response.json({ access_token: "token" });
    if (url === "https://auth.test/userinfo") {
      return Response.json({ email: "attacker@example.org", email_verified: true });
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  const initial = await worker.fetch(new Request(registration.url), env);
  const authorization = new URL(initial.headers.get("location"));
  const callback = await worker.fetch(new Request(
    `https://${host}/_lathe/auth/callback?code=code&state=${authorization.searchParams.get("state")}`,
  ), env);
  assert.equal(callback.status, 403);
  assert.equal(callback.headers.get("set-cookie"), null);
});

test("hostname templates expose selected hints and sanitize the final label", async () => {
  const env = environment();
  env.HOSTNAME_TEMPLATE = "Demo.{access}_{tag}.{email_user}";
  const registration = await register(env, "private", "vscode");
  const host = new URL(registration.url).hostname;
  const stored = JSON.parse(await env.SUBDOMAINS.get(host));
  assert.match(host, /^demo-private-vscode-owner-[0-9a-f]{16}\.preview\.test$/);
  assert.equal(stored.access, "private");
});

test("hostname text never determines access policy", async () => {
  const env = environment();
  env.HOSTNAME_TEMPLATE = "private-{email}-{user_id}";
  const registration = await register(env, "public");
  assert.match(new URL(registration.url).hostname,
    /^private-owner-example-org-owui-user-[0-9a-f]{16}\.preview\.test$/);
  globalThis.fetch = async () => new Response("public content");
  const response = await worker.fetch(new Request(registration.url), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "public content");
});

test("legacy registration records are rejected", async () => {
  const env = environment();
  const host = "lathe-0123456789abcdef.preview.test";
  await env.SUBDOMAINS.put(host, JSON.stringify({
    version: 1,
    target: "https://signed-upstream.test/credential",
    access_mode: "public-wrapped",
    owner: null,
  }));

  globalThis.fetch = async () => {
    throw new Error("legacy registration reached the network");
  };
  const response = await worker.fetch(new Request(`https://${host}/legacy`), env);
  assert.equal(response.status, 404);
});

test("generic registrations cannot claim the Lathe hostname namespace", async () => {
  const env = environment();
  const response = await worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.REGISTER_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      subdomain: "lathe-private-forged",
      target: "https://service.test/",
    }),
  }), env);
  assert.equal(response.status, 400);
  assert.equal(env.SUBDOMAINS.values.size, 0);
});

async function registrationRequest(env, body, token = env.REGISTER_TOKEN) {
  return worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  }), env);
}

const appHeaders = { Authorization: "Basic YXBwOnNlY3JldA==", "X-App-Assertion": "configured-assertion" };

for (const access of ["public", "private"]) {
  for (const websocket of [false, true]) {
    test(`${access} configured headers replace browser values on ${websocket ? "WS" : "HTTP"}`, async () => {
      const env = environment();
      const registration = await register(env, access, "", { upstream_headers: appHeaders });
      assert.equal(registration.upstream_headers_applied, true);
      const host = new URL(registration.url).hostname;
      const stored = await env.SUBDOMAINS.get(host);
      assert.equal(JSON.parse(stored).version, 3);
      for (const secret of [...Object.values(appHeaders), "signed-upstream.test", "credential"]) {
        assert.ok(!stored.includes(secret));
        assert.ok(!JSON.stringify(env.SUBDOMAINS.options.get(host)).includes(secret));
        assert.ok(!JSON.stringify(registration).includes(secret));
      }
      assert.equal(env.SUBDOMAINS.options.get(host).expirationTtl, 3600);
      if (access === "private") {
        await env.SUBDOMAINS.put("_lathe:session:owner", JSON.stringify({
          host, owner_email: "owner@example.org",
        }));
      }
      const upgraded = { status: 101, webSocket: { canary: true } };
      let forwarded = 0;
      globalThis.fetch = async (input, init) => {
        forwarded++;
        assert.equal(input.toString(), "https://signed-upstream.test/app");
        const headers = new Headers(init.headers);
        assert.equal(headers.get("authorization"), appHeaders.Authorization);
        assert.equal(headers.get("x-app-assertion"), appHeaders["X-App-Assertion"]);
        assert.equal(headers.get("origin"), "https://other-origin.test");
        assert.equal(headers.get("x-unconfigured-assertion"), "ordinary-browser-value");
        assert.equal(headers.get("cookie"), "app=cookie");
        assert.equal(headers.get("connection"), websocket ? "Upgrade" : null);
        if (websocket) assert.equal(headers.get("sec-websocket-protocol"), "app-protocol");
        return websocket ? upgraded : new Response("application content");
      };
      const response = await worker.fetch(new Request(`${registration.url}app`, {
        headers: {
          aUtHoRiZaTiOn: "Bearer browser-spoof", "X-APP-ASSERTION": "browser-spoof",
          "X-Unconfigured-Assertion": "ordinary-browser-value", origin: "https://other-origin.test",
          cookie: `${access === "private" ? "__Host-lathe_session=owner; " : ""}app=cookie`,
          connection: "Upgrade, Authorization, X-App-Assertion",
          ...(websocket ? { upgrade: "websocket", "sec-websocket-protocol": "app-protocol" } : {}),
        },
      }), env);
      assert.equal(forwarded, 1);
      if (websocket) assert.equal(response, upgraded);
      else assert.equal(await response.text(), "application content");
    });
  }
}

test("private injected credentials never reach upstream for anonymous or wrong-owner sessions", async () => {
  const env = environment();
  const registration = await register(env, "private", "", { upstream_headers: appHeaders });
  await env.SUBDOMAINS.put("_lathe:session:attacker", JSON.stringify({
    host: registration.host, owner_email: "attacker@example.org",
  }));
  globalThis.fetch = async (input) => {
    assert.equal(input.toString(), "https://auth.test/.well-known/openid-configuration");
    return Response.json({ issuer: "https://auth.test", authorization_endpoint: "https://auth.test/authorize",
      token_endpoint: "https://auth.test/token", userinfo_endpoint: "https://auth.test/userinfo" });
  };
  for (const cookie of ["", "__Host-lathe_session=attacker"]) {
    const response = await worker.fetch(new Request(registration.url, {
      headers: { cookie, origin: "https://attacker.test", upgrade: "websocket" },
    }), env);
    assert.equal(response.status, 302);
    assert.ok(!response.headers.get("location").includes(appHeaders.Authorization));
  }
});

const invalidHeaders = [null, [], "headers", { Authorization: true },
  { Host: "secret-canary" }, { cOoKiE: "secret-canary" }, { Origin: "secret-canary" },
  { "X-Forwarded-Owner": "secret-canary" }, { Connection: "secret-canary" }, { "Content-Length": "12" },
  { "Sec-WebSocket-Protocol": "secret-canary" }, { "X-Daytona-Token": "secret-canary" },
  { "Bad Name": "secret-canary" }, { "": "secret-canary" }, { ["X".repeat(65)]: "value" },
  { "X-App": "secret-canary\r\nHost: attacker" }, { "X-App": "\x00" }, { "X-App": "\t" },
  { "X-App": "é" }, { "X-App": "\x7f" }, { "X-App": "x".repeat(4097) },
  Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`X-${i}`, "value"])),
  { "X-A": "x".repeat(4096), "X-B": "x".repeat(4096) },
  { Authorization: "secret-canary", authorization: "collision" },
];
for (const [index, upstream_headers] of invalidHeaders.entries()) {
  test(`invalid header dictionary ${index} fails without storage or disclosure`, async () => {
    const env = environment();
    const response = await registrationRequest(env, {
      subdomain: "demo", target: "https://service.test", upstream_headers,
    });
    assert.equal(response.status, 400);
    assert.ok(!(await response.text()).includes("secret-canary"));
    assert.equal(env.SUBDOMAINS.values.size, 0);
  });
}

test("boundary-sized names and values, empty values, and empty dictionaries are supported", async () => {
  const env = environment();
  const headers = { ["X".repeat(64)]: "x".repeat(4096), "X-Empty": "" };
  const response = await registrationRequest(env, {
    subdomain: "demo", target: "https://service.test", upstream_headers: headers,
  });
  assert.equal(response.status, 200);
  globalThis.fetch = async (input, init) => {
    assert.equal(init.headers.get("x".repeat(64)).length, 4096);
    assert.equal(init.headers.get("x-empty"), "");
    return new Response("ok");
  };
  assert.equal((await worker.fetch(new Request("https://demo.preview.test"), env)).status, 200);
  const empty = await register(env, "public", "", { upstream_headers: {} });
  assert.equal(empty.upstream_headers_applied, undefined);
  assert.equal(JSON.parse(await env.SUBDOMAINS.get(empty.host)).version, 2);
  for (const upstream_headers of [
    Object.fromEntries(Array.from({ length: 16 }, (_, i) => [`X-${i}`, "value"])),
    { "X-A": "x".repeat(4096), "X-B": "x".repeat(4090) },
  ]) {
    assert.equal((await registrationRequest(env, {
      subdomain: "demo", target: "https://service.test", upstream_headers,
    })).status, 200);
  }
});

test("control-plane authority and encryption configuration fail closed", async () => {
  const env = environment();
  const body = { subdomain: "demo", target: "https://service.test", upstream_headers: appHeaders };
  assert.equal((await registrationRequest(env, body, "attacker")).status, 401);
  for (const key of [undefined, "bad-key", Buffer.alloc(16).toString("base64")]) {
    env.REGISTRATION_ENCRYPTION_KEY = key;
    const response = await registrationRequest(env, body);
    assert.equal(response.status, 503);
    assert.ok(!(await response.text()).includes(appHeaders.Authorization));
    assert.equal(env.SUBDOMAINS.values.size, 0);
  }
});

test("replacement removes old injections, encrypted records are host-bound, and revocation is enforced", async () => {
  const env = environment();
  const body = { subdomain: "demo", target: "https://service.test", upstream_headers: appHeaders };
  assert.equal((await registrationRequest(env, body)).status, 200);
  const encrypted = await env.SUBDOMAINS.get("demo.preview.test");
  await env.SUBDOMAINS.put("copied.preview.test", encrypted);
  assert.equal((await worker.fetch(new Request("https://copied.preview.test"), env)).status, 404);
  assert.equal((await registrationRequest(env, { subdomain: "demo", target: "https://new-service.test" })).status, 200);
  globalThis.fetch = async (input, init) => {
    assert.equal(input.toString(), "https://new-service.test/");
    assert.equal(init.headers.get("authorization"), "Bearer browser-value");
    return new Response("ok");
  };
  assert.equal((await worker.fetch(new Request("https://demo.preview.test", {
    headers: { authorization: "Bearer browser-value" },
  }), env)).status, 200);
  assert.equal((await worker.fetch(new Request("https://preview.test/register/demo", {
    method: "DELETE", headers: { authorization: `Bearer ${env.REGISTER_TOKEN}` },
  }), env)).status, 200);
  assert.equal((await worker.fetch(new Request("https://demo.preview.test"), env)).status, 404);
});

test("token rotation preserves leases; encryption key rotation invalidates them", async () => {
  const env = environment();
  const registration = await register(env, "public", "", { upstream_headers: appHeaders });
  env.REGISTER_TOKEN = "rotated-token";
  globalThis.fetch = async (input, init) => {
    assert.equal(init.headers.get("authorization"), appHeaders.Authorization);
    return new Response("ok");
  };
  assert.equal((await worker.fetch(new Request(registration.url), env)).status, 200);
  env.REGISTRATION_ENCRYPTION_KEY = Buffer.alloc(32, 18).toString("base64");
  assert.equal((await worker.fetch(new Request(registration.url), env)).status, 404);
});

test("absolute expiry, ciphertext tampering, and plaintext header records cannot proxy", async () => {
  const env = environment();
  const registration = await register(env, "public", "", { upstream_headers: appHeaders });
  const envelope = JSON.parse(await env.SUBDOMAINS.get(registration.host));
  envelope.payload = (envelope.payload[0] === "A" ? "B" : "A") + envelope.payload.slice(1);
  await env.SUBDOMAINS.put(registration.host, JSON.stringify(envelope));
  globalThis.fetch = async () => { throw new Error("invalid record reached upstream"); };
  assert.equal((await worker.fetch(new Request(registration.url), env)).status, 404);
  for (const record of [
    { version: 2, target: "https://service.test", access: "public", expires_at: 1 },
    { version: 2, target: "https://service.test", access: "public", expires_at: 4102444800, upstream_headers: appHeaders },
  ]) {
    await env.SUBDOMAINS.put(registration.host, JSON.stringify(record));
    assert.equal((await worker.fetch(new Request(registration.url), env)).status, 404);
  }
});

test("encrypted registrations enforce absolute expiry even before KV eviction", async (t) => {
  const env = environment();
  const registration = await register(env, "public", "", { upstream_headers: appHeaders });
  const future = Date.now() + 3601 * 1000;
  t.mock.method(Date, "now", () => future);
  globalThis.fetch = async () => { throw new Error("expired registration reached network"); };
  assert.equal((await worker.fetch(new Request(registration.url), env)).status, 404);
});

test("upstream transport failures cannot disclose target or credentials", async () => {
  const env = environment();
  const registration = await register(env, "public", "", { upstream_headers: appHeaders });
  globalThis.fetch = async () => { throw new Error("signed-upstream.test " + appHeaders.Authorization); };
  const response = await worker.fetch(new Request(registration.url), env);
  assert.equal(response.status, 502);
  const body = await response.text();
  assert.ok(!body.includes(appHeaders.Authorization));
  assert.ok(!body.includes("signed-upstream.test"));
});
