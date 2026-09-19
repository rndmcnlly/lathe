// Short-lived Cloudflare Worker proxy for cookie-isolated preview origins.

const RESERVED = new Set([
  "www", "auth", "chat", "api", "mail", "mx", "ns1", "ns2", "cdn", "app",
]);

const LABEL_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const TAG_RE = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;
const NONCE_LENGTH = 16;
const DEFAULT_TTL = 24 * 60 * 60;
const MIN_TTL = 60;
const MAX_TTL = 24 * 60 * 60;
const AUTH_STATE_TTL = 10 * 60;
const AUTH_CALLBACK_PATH = "/_lathe/auth/callback";
const SESSION_COOKIE = "__Host-lathe_session";
const STATE_PREFIX = "_lathe:state:";
const SESSION_PREFIX = "_lathe:session:";

let oidcConfigurationPromise;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const host = url.hostname;

    if (host === env.ZONE) {
      if (url.pathname === "/register" && request.method === "POST") {
        return handleRegister(request, env);
      }
      if (url.pathname.startsWith("/register/") && request.method === "DELETE") {
        return handleRevoke(request, env, url.pathname.slice("/register/".length));
      }
      return page(
        "preview wrapper",
        "<p>Known subdomains only.</p>" +
          `<p><code>POST /register</code> with credentials serves new hosts.</p>`,
        200,
        env.ZONE,
      );
    }

    if (host.endsWith("." + env.ZONE)) {
      const label = host.slice(0, -(env.ZONE.length + 1));
      if (label.includes(".") || !LABEL_RE.test(label)) {
        return page("malformed host", "<p>The requested host is malformed.</p>", 400, env.ZONE);
      }

      const registration = await getRegistration(env, host);
      if (!registration) {
        return page(
          "unknown or expired",
          `<p><code>${escapeHtml(host)}</code> is not registered, or its lease expired.</p>`,
          404,
          env.ZONE,
        );
      }

      if (registration.access === "private") {
        if (url.pathname === AUTH_CALLBACK_PATH) {
          return handleAuthCallback(request, url, registration, host, env);
        }
        if (!(await hasAuthorizedSession(request, registration, host, env))) {
          return beginAuthentication(url, registration, host, env);
        }
      }

      return proxy(request, url, registration.target, host, env.ZONE);
    }

    return page("off territory", "<p>This host is outside the zone.</p>", 421, env.ZONE);
  },

  // Registration, auth-state, and session records all carry expiration TTLs.
  async scheduled(event, env, ctx) {
    const nowS = Math.floor(Date.now() / 1000);
    let live = 0;
    let deleted = 0;
    let cursor;
    do {
      const result = await env.SUBDOMAINS.list({ cursor });
      for (const key of result.keys) {
        if (key.expiration && key.expiration <= nowS) {
          await env.SUBDOMAINS.delete(key.name);
          deleted++;
        } else {
          live++;
        }
      }
      cursor = result.list_complete ? undefined : result.cursor;
    } while (cursor);
    console.log(`sweep: live=${live} deleted=${deleted} at ${new Date().toISOString()}`);
  },
};

async function handleRegister(request, env) {
  const auth = request.headers.get("authorization") ?? "";
  if (auth !== `Bearer ${env.REGISTER_TOKEN}`) {
    return json({ error: "invalid bearer token" }, 401);
  }

  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return json({ error: "body must be JSON" }, 400);
  }

  const latheRegistration = body.owner !== undefined || body.upstream_url !== undefined;
  if (latheRegistration
      && (!body.owner || typeof body.owner !== "object"
        || typeof body.owner.subject !== "string"
        || typeof body.owner.email !== "string"
        || typeof body.upstream_url !== "string")) {
    return json({ error: "Lathe registrations require owner and upstream_url" }, 400);
  }
  const access = body.access;
  if (latheRegistration && !["public", "private"].includes(access)) {
    return json({ error: "Lathe registrations require access public or private" }, 400);
  }
  if (!latheRegistration && access !== undefined && access !== "public") {
    return json({ error: "generic registrations support only public access" }, 409);
  }

  const tag = body.tag === undefined ? "" : body.tag;
  if (latheRegistration && (typeof tag !== "string" || (tag && !TAG_RE.test(tag)))) {
    return json({ error: "tag must be empty or a lowercase DNS label of at most 32 characters" }, 400);
  }
  if (!latheRegistration && body.tag !== undefined) {
    return json({ error: "tag is only supported for Lathe registrations" }, 400);
  }

  let label;
  if (latheRegistration) {
    try {
      label = renderLatheLabel(env.HOSTNAME_TEMPLATE || "lathe-{access}", {
        access,
        tag: tag || "preview",
        email_user: safeLabelPart(body.owner.email.split("@", 1)[0], "user"),
        email: safeLabelPart(body.owner.email, "user"),
        user_id: safeLabelPart(body.owner.subject, "user"),
      });
    } catch {
      return json({ error: "hostname template is invalid for this registration" }, 500);
    }
  } else {
    label = typeof body.subdomain === "string" ? body.subdomain.trim().toLowerCase() : "";
  }
  if (!LABEL_RE.test(label) || RESERVED.has(label)
      || (!latheRegistration && label.startsWith("lathe-"))) {
    return json({ error: `subdomain must match ${LABEL_RE} and avoid reserved names` }, 400);
  }

  let target;
  try {
    target = new URL(latheRegistration ? body.upstream_url : body.target);
  } catch {
    return json({ error: "target must be an http(s) URL" }, 400);
  }
  if (target.protocol !== "http:" && target.protocol !== "https:") {
    return json({ error: "target scheme must be http or https" }, 400);
  }

  let ttl = DEFAULT_TTL;
  if (body.ttl !== undefined) {
    if (!Number.isInteger(body.ttl) || body.ttl < MIN_TTL || body.ttl > MAX_TTL) {
      return json({ error: `ttl must be an integer in [${MIN_TTL}, ${MAX_TTL}] seconds` }, 400);
    }
    ttl = body.ttl;
  }

  let owner = null;
  if (latheRegistration) {
    const subject = typeof body.owner.subject === "string" ? body.owner.subject.trim() : "";
    const email = normalizeEmail(body.owner.email);
    if (!subject || !email) {
      return json({ error: "owner subject and email are required" }, 400);
    }
    owner = { subject, email };
  }

  if (access === "private" && !oidcConfigured(env)) {
    return json({ error: "private access is not configured" }, 503);
  }

  const host = `${label}.${env.ZONE}`;
  const nowS = Math.floor(Date.now() / 1000);
  const expiresS = nowS + ttl;
  const registration = {
    version: 2,
    target: target.toString(),
    access: latheRegistration ? access : "public",
    owner,
    registered_at: new Date(nowS * 1000).toISOString(),
    expires_at: expiresS,
  };
  await env.SUBDOMAINS.put(host, JSON.stringify(registration), {
    expirationTtl: ttl,
    metadata: {
      producer: latheRegistration ? "lathe" : "generic",
      access: latheRegistration ? access : "public",
      owner_email: owner?.email ?? null,
    },
  });

  return json({
    url: `https://${host}/`,
    host,
    expires_at: new Date(expiresS * 1000).toISOString(),
  });
}

async function handleRevoke(request, env, rawLabel) {
  const auth = request.headers.get("authorization") ?? "";
  if (auth !== `Bearer ${env.REGISTER_TOKEN}`) {
    return json({ error: "invalid bearer token" }, 401);
  }
  const label = decodeURIComponent(rawLabel).trim().toLowerCase();
  if (!LABEL_RE.test(label)) return json({ error: "invalid subdomain" }, 400);
  await env.SUBDOMAINS.delete(`${label}.${env.ZONE}`);
  return json({ deleted: label });
}

async function getRegistration(env, host) {
  const stored = await env.SUBDOMAINS.get(host);
  if (!stored) return null;
  try {
    const registration = JSON.parse(stored);
    if (registration?.version === 2 && typeof registration.target === "string"
        && ["public", "private"].includes(registration.access)) {
      return registration;
    }
  } catch {
    return null;
  }
  return null;
}

function oidcConfigured(env) {
  return Boolean(env.OIDC_ISSUER && env.OIDC_CLIENT_ID && env.OIDC_CLIENT_SECRET);
}

async function getOidcConfiguration(env) {
  if (!oidcConfigured(env)) throw new Error("OIDC is not configured");
  if (!oidcConfigurationPromise) {
    const issuer = env.OIDC_ISSUER.replace(/\/$/, "");
    oidcConfigurationPromise = fetch(`${issuer}/.well-known/openid-configuration`)
      .then((response) => {
        if (!response.ok) throw new Error("OIDC discovery failed");
        return response.json();
      })
      .then((configuration) => {
        if (configuration.issuer !== issuer || !configuration.authorization_endpoint
            || !configuration.token_endpoint || !configuration.userinfo_endpoint) {
          throw new Error("OIDC discovery response is invalid");
        }
        return configuration;
      })
      .catch((error) => {
        oidcConfigurationPromise = undefined;
        throw error;
      });
  }
  return oidcConfigurationPromise;
}

async function beginAuthentication(url, registration, host, env) {
  try {
    const configuration = await getOidcConfiguration(env);
    const remaining = registrationRemaining(registration);
    if (remaining <= 0) throw new Error("registration expired");

    const state = randomToken(24);
    const verifier = randomToken(48);
    const challenge = await sha256Base64Url(verifier);
    const redirectUri = `https://${host}${AUTH_CALLBACK_PATH}`;
    await env.SUBDOMAINS.put(STATE_PREFIX + state, JSON.stringify({
      host,
      return_to: url.pathname + url.search,
      verifier,
      redirect_uri: redirectUri,
    }), { expirationTtl: Math.min(AUTH_STATE_TTL, remaining) });

    const authorization = new URL(configuration.authorization_endpoint);
    authorization.searchParams.set("client_id", env.OIDC_CLIENT_ID);
    authorization.searchParams.set("redirect_uri", redirectUri);
    authorization.searchParams.set("response_type", "code");
    authorization.searchParams.set("scope", "openid email");
    authorization.searchParams.set("state", state);
    authorization.searchParams.set("code_challenge", challenge);
    authorization.searchParams.set("code_challenge_method", "S256");
    return Response.redirect(authorization.toString(), 302);
  } catch {
    return page("sign-in unavailable", "<p>Private preview sign-in is temporarily unavailable.</p>", 503, env.ZONE);
  }
}

async function handleAuthCallback(request, url, registration, host, env) {
  const state = url.searchParams.get("state") ?? "";
  const code = url.searchParams.get("code") ?? "";
  if (!state || !code || url.searchParams.has("error")) {
    return page("sign-in failed", "<p>The identity provider did not complete sign-in.</p>", 401, env.ZONE);
  }

  const stateKey = STATE_PREFIX + state;
  const stored = await env.SUBDOMAINS.get(stateKey);
  if (!stored) {
    return page("sign-in expired", "<p>This sign-in request expired or was already used.</p>", 401, env.ZONE);
  }
  await env.SUBDOMAINS.delete(stateKey);

  let authState;
  try {
    authState = JSON.parse(stored);
  } catch {
    return page("sign-in failed", "<p>The sign-in state was invalid.</p>", 401, env.ZONE);
  }
  if (authState.host !== host || authState.redirect_uri !== `https://${host}${AUTH_CALLBACK_PATH}`
      || typeof authState.verifier !== "string" || !safeReturnPath(authState.return_to)) {
    return page("sign-in failed", "<p>The sign-in state did not match this preview.</p>", 401, env.ZONE);
  }

  try {
    const configuration = await getOidcConfiguration(env);
    const tokenResponse = await fetch(configuration.token_endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: authState.redirect_uri,
        client_id: env.OIDC_CLIENT_ID,
        client_secret: env.OIDC_CLIENT_SECRET,
        code_verifier: authState.verifier,
      }),
    });
    if (!tokenResponse.ok) throw new Error("token exchange failed");
    const tokens = await tokenResponse.json();
    if (typeof tokens.access_token !== "string" || !tokens.access_token) {
      throw new Error("access token missing");
    }

    const userResponse = await fetch(configuration.userinfo_endpoint, {
      headers: { authorization: `Bearer ${tokens.access_token}` },
    });
    if (!userResponse.ok) throw new Error("userinfo failed");
    const claims = await userResponse.json();
    const authenticatedEmail = normalizeEmail(claims.email);
    const ownerEmail = normalizeEmail(registration.owner?.email);
    if (claims.email_verified !== true || !authenticatedEmail || authenticatedEmail !== ownerEmail) {
      return page(
        "not authorized",
        "<p>You signed in successfully, but this private preview belongs to another user.</p>",
        403,
        env.ZONE,
      );
    }

    const remaining = registrationRemaining(registration);
    if (remaining <= 0) throw new Error("registration expired");
    const session = randomToken(32);
    await env.SUBDOMAINS.put(SESSION_PREFIX + session, JSON.stringify({
      host,
      owner_email: ownerEmail,
      subject: claims.sub,
    }), { expirationTtl: remaining });

    return new Response(null, {
      status: 302,
      headers: {
        location: `https://${host}${authState.return_to}`,
        "set-cookie": `${SESSION_COOKIE}=${session}; Path=/; Max-Age=${remaining}; Secure; HttpOnly; SameSite=Lax`,
        "cache-control": "no-store",
      },
    });
  } catch {
    return page("sign-in failed", "<p>Could not complete private preview sign-in.</p>", 502, env.ZONE);
  }
}

async function hasAuthorizedSession(request, registration, host, env) {
  const token = readCookie(request.headers.get("cookie") ?? "", SESSION_COOKIE);
  if (!token) return false;
  const stored = await env.SUBDOMAINS.get(SESSION_PREFIX + token);
  if (!stored) return false;
  try {
    const session = JSON.parse(stored);
    return session.host === host
      && normalizeEmail(session.owner_email) === normalizeEmail(registration.owner?.email);
  } catch {
    return false;
  }
}

function registrationRemaining(registration) {
  if (!Number.isInteger(registration.expires_at)) return DEFAULT_TTL;
  return Math.max(0, registration.expires_at - Math.floor(Date.now() / 1000));
}

function normalizeEmail(value) {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function safeReturnPath(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    && !value.startsWith(AUTH_CALLBACK_PATH);
}

function readCookie(header, name) {
  for (const part of header.split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) return value.join("=");
  }
  return "";
}

function stripSessionCookie(header) {
  return header.split(";")
    .map((part) => part.trim())
    .filter((part) => part && part.split("=", 1)[0] !== SESSION_COOKIE)
    .join("; ");
}

function isSessionSetCookie(value) {
  return value.trim().toLowerCase().startsWith(`${SESSION_COOKIE.toLowerCase()}=`);
}

function randomLabel() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function safeLabelPart(value, fallback) {
  const safe = String(value ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return safe || fallback;
}

function renderLatheLabel(template, values) {
  if (typeof template !== "string" || !template) throw new Error("empty template");
  const rendered = template.replace(/\{([a-z_]+)\}/g, (match, name) => {
    if (!(name in values)) throw new Error("unknown template variable");
    return values[name];
  });
  if (/[{}]/.test(rendered)) throw new Error("invalid template expression");
  const prefix = safeLabelPart(rendered, "");
  if (!prefix || !LABEL_RE.test(prefix)) throw new Error("invalid prefix");
  if (prefix.length + 1 + NONCE_LENGTH > 63) throw new Error("prefix too long");
  return `${prefix}-${randomLabel()}`;
}

function randomToken(byteLength) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function sha256Base64Url(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return base64Url(new Uint8Array(digest));
}

function base64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function proxy(request, url, target, publicHost, zone) {
  let upstream;
  try {
    upstream = new URL(target);
  } catch {
    return page("bad mapping", "<p>The stored mapping is invalid. Re-register the host.</p>", 502, zone);
  }
  upstream.pathname = url.pathname;
  upstream.search = url.search;

  const headers = new Headers(request.headers);
  headers.set("X-Forwarded-Host", publicHost);
  headers.set("X-Forwarded-Proto", "https");
  const cookies = stripSessionCookie(headers.get("cookie") ?? "");
  if (cookies) headers.set("cookie", cookies);
  else headers.delete("cookie");

  const resp = await fetch(upstream, {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    redirect: "manual",
  });

  // Reconstructing the response would discard Cloudflare's WebSocket handle.
  if (resp.status === 101) return resp;

  const out = new Headers();
  for (const [name, value] of resp.headers.entries()) {
    if (name.toLowerCase() === "set-cookie") continue;
    out.append(name, value);
  }

  // Keep application cookies host-only, and prevent untrusted upstreams from
  // setting the wrapper's authentication cookie.
  const setCookies = resp.headers.getAll
    ? resp.headers.getAll("set-cookie")
    : resp.headers.getSetCookie ? resp.headers.getSetCookie() : [];
  for (const cookie of setCookies) {
    if (!isSessionSetCookie(cookie)) {
      out.append("set-cookie", cookie.replace(/;\s*domain=[^;]*/gi, ""));
    }
  }

  const location = out.get("location");
  if (location) out.set("location", unwrapLocation(location, upstream, publicHost));

  return new Response(resp.body, { status: resp.status, headers: out });
}

function unwrapLocation(location, upstream, publicHost) {
  try {
    const parsed = new URL(location, upstream);
    if (parsed.origin === upstream.origin) {
      parsed.protocol = "https:";
      parsed.host = publicHost;
      return parsed.toString();
    }
    return location;
  } catch {
    return location;
  }
}

const escapeHtml = (s) =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function page(title, bodyHtml, status, zone) {
  return new Response(
    `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)} · ${escapeHtml(zone)}</title>` +
      `<style>body{font-family:ui-monospace,monospace;margin:4rem auto;max-width:40rem;color:#222}` +
      `code{background:#eee;padding:0 .3em;border-radius:.2em}</style></head>` +
      `<body><h1>${escapeHtml(title)}</h1>${bodyHtml}</body></html>`,
    { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } },
  );
}
