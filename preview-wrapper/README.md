# Lathe Preview Wrapper

A small Cloudflare Worker for short-lived preview hostnames on a registrable
domain distinct from Open WebUI. It provides branding and structural cookie
isolation today, with an owner-email authorization seam for private previews.

## Security Model

The wrapper separates two concerns:

- **Registration control plane**: trusted producers authenticate with one
  `REGISTER_TOKEN` bearer secret and register an upstream URL for at most 24
  hours. Models and browsers never receive this secret.
- **Browser data plane**: public registrations currently authorize possession
  of the generated URL. Private registrations are not yet implemented and are
  rejected rather than silently downgraded.

Run this Worker on a different registrable domain from OWUI. A subdomain of the
OWUI domain does not provide the same cookie boundary: sibling applications can
share parent-domain cookies.

Lathe supplies the owner email from OWUI's trusted injected user context. The
Worker stores that email in KV metadata. A future private mode will authenticate
the browser through OAuth/OIDC and authorize only when the verified email claim
matches the registered owner email. URL parameters, browser-submitted email,
and the model are never identity authorities.

This first implementation is not a confidentiality boundary. It protects
cookie namespaces and hides the upstream URL, but anyone possessing a public
wrapped URL can access its service.

## Registration Contract

Lathe uses the existing wrapper protocol:

```http
POST /register
Authorization: Bearer $REGISTER_TOKEN
Content-Type: application/json

{"owner":{"subject":"owui-user-id","email":"owner@example.org"},
 "slot":"5000","upstream_url":"https://signed-upstream.example/"}
```

The Worker generates a `lathe-{nonce}` label and returns:

```json
{"url":"https://lathe-{nonce}.previews.example.org/",
 "host":"lathe-{nonce}.previews.example.org",
 "access_mode":"public-wrapped",
 "expires_at":"2026-09-20T01:00:00Z"}
```

The sensitive upstream URL is never echoed. Early revocation is
`DELETE /register/{label}` with the same bearer token.

Other trusted producers may register an explicit label:

```json
{"subdomain":"demo-abc123","target":"https://service.example/","ttl":86400}
```

`ttl` is optional and constrained to 60–86400 seconds. The current service can
satisfy only public access. An optional `requested_access` value other than
`public` receives HTTP 409 and creates no mapping. Lathe omits that field for
compatibility with existing wrapper services; coordinated protocol negotiation
is tracked in [issue #68](https://github.com/rndmcnlly/lathe/issues/68).

## Deploy

Requirements: a Cloudflare-managed domain, Wrangler, and permission to create a
Worker, KV namespace, route, secret, and DNS records.

1. Copy the example configuration and replace every placeholder:

   ```bash
   cp wrangler.toml.example wrangler.toml
   wrangler kv namespace create SUBDOMAINS
   ```

2. Paste the returned namespace ID into `wrangler.toml`.

3. Create a long random registration secret and deploy:

   ```bash
   wrangler secret put REGISTER_TOKEN
   wrangler deploy
   ```

4. In Cloudflare DNS, create proxied `A` records for `@` and `*`, both pointing
   to reserved TEST-NET address `192.0.2.1`. Worker routes own the requests; no
   origin exists at that address.

5. Configure Lathe's `preview_wrapper_url` as
   `https://WRAPPER_DOMAIN/register` and `preview_wrapper_key` as the same
   registration secret.

KV's `expirationTtl` performs actual expiry. The daily cron is a cleanup canary
and logs live/deleted record counts. No per-preview DNS record is created.

## Proxy Behavior

- Preserves method, body, path, and query string.
- Adds `X-Forwarded-Host` and `X-Forwarded-Proto`.
- Removes `Domain=` from upstream `Set-Cookie`, making cookies host-only.
- Rewrites same-origin absolute redirects to the public wrapper hostname.
- Passes WebSocket upgrade responses through without reconstruction.

The upstream target must be reachable from Cloudflare's edge. Do not treat the
wrapper as a private network tunnel.

## Development

```bash
npm run check
wrangler dev
wrangler tail
```

Use `.dev.vars.example` for local development. `.dev.vars`, `wrangler.toml`,
and Wrangler state are gitignored.
