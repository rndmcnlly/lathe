// Short-lived Cloudflare Worker proxy for cookie-isolated preview origins.
//
// Two roles in one worker:
//   1. Apex: trusted producers POST /register with a bearer token to map a
//      hostname to an upstream URL for up to 24 hours. That KV entry is the
//      only per-service record.
//   2. Wildcard hosts: proxy to the mapped target, stripping upstream Cookie
//      Domain attributes so cookies stay host-only for the wrapping host.

const RESERVED = new Set([
  "www", "auth", "chat", "api", "mail", "mx", "ns1", "ns2", "cdn", "app",
]);

const LABEL_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const DEFAULT_TTL = 24 * 60 * 60;
const MIN_TTL = 60;
const MAX_TTL = 24 * 60 * 60;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: { "content-type": "application/json" },
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
      const target = await env.SUBDOMAINS.get(host);
      if (!target) {
        return page(
          "unknown or expired",
          `<p><code>${escapeHtml(host)}</code> is not registered, or its lease expired.</p>`,
          404,
          env.ZONE,
        );
      }
      return proxy(request, url, target, host, env.ZONE);
    }

    return page("off territory", "<p>This host is outside the zone.</p>", 421, env.ZONE);
  },

  // KV entries carry their own expiration TTL; this job is a cleanup canary.
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

  const latheRegistration = body.owner && typeof body.owner === "object"
    && typeof body.slot === "string" && typeof body.upstream_url === "string";
  const requestedAccess = typeof body.requested_access === "string"
    ? body.requested_access : "public";
  if (requestedAccess !== "public") {
    return json({
      error: `requested access ${JSON.stringify(requestedAccess)} is unavailable`,
      available_access: ["public"],
    }, 409);
  }

  const label = latheRegistration
    ? `lathe-${randomLabel()}`
    : typeof body.subdomain === "string" ? body.subdomain.trim().toLowerCase() : "";
  if (!LABEL_RE.test(label) || RESERVED.has(label)) {
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

  const host = `${label}.${env.ZONE}`;
  const expiresS = Math.floor(Date.now() / 1000) + ttl;
  await env.SUBDOMAINS.put(host, target.toString(), {
    expirationTtl: ttl,
    metadata: {
      registered: new Date().toISOString(),
      producer: latheRegistration ? "lathe" : "generic",
      owner_email: latheRegistration ? body.owner.email : null,
      slot: latheRegistration ? body.slot : null,
    },
  });

  return json({
    url: `https://${host}/`,
    host,
    access_mode: "public-wrapped",
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

function randomLabel() {
  const bytes = new Uint8Array(10);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(36).padStart(2, "0")).join("").slice(0, 16);
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

  // Dropping Domain makes each cookie host-only for the public wrapper host.
  const cookies = resp.headers.getAll("set-cookie");
  for (const cookie of cookies) {
    out.append("set-cookie", cookie.replace(/;\s*domain=[^;]*/gi, ""));
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
    { status, headers: { "content-type": "text/html; charset=utf-8" } },
  );
}
