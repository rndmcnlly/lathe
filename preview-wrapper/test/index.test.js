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
  }

  async get(key) {
    return this.values.get(key) ?? null;
  }

  async put(key, value) {
    this.values.set(key, value);
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
    SUBDOMAINS: new MemoryKV(),
  };
}

async function register(env, requestedAccess = "private") {
  const response = await worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.REGISTER_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      owner: { subject: "owui-user", email: " Owner@Example.org " },
      slot: "5000",
      upstream_url: "https://signed-upstream.test/credential",
      requested_access: requestedAccess,
      ttl: 3600,
    }),
  }), env);
  assert.equal(response.status, 200);
  return response.json();
}

test("Lathe registration requires an explicit access mode", async () => {
  const env = environment();
  const response = await worker.fetch(new Request("https://preview.test/register", {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.REGISTER_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      owner: { subject: "owui-user", email: "owner@example.org" },
      slot: "5000",
      upstream_url: "https://signed-upstream.test/credential",
    }),
  }), env);
  assert.equal(response.status, 400);
  assert.match(await response.text(), /requested_access/);
  assert.equal(env.SUBDOMAINS.values.size, 0);
});

test("public registrations proxy without OIDC", async () => {
  const env = environment();
  const registration = await register(env, "public");
  assert.equal(registration.access_mode, "public-wrapped");

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
  assert.equal(registration.access_mode, "owner-authenticated");
  const host = new URL(registration.url).hostname;
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
