# Lathe Preview Wrapper

A small Cloudflare Worker for short-lived preview hostnames on a registrable
domain distinct from Open WebUI. Public previews use URL possession; private
previews authenticate their registered owner through OIDC.

## Security Model

The wrapper separates two concerns:

- **Registration control plane**: trusted producers authenticate with one
  `REGISTER_TOKEN` bearer secret and register an upstream URL for at most 24
  hours. Models and browsers never receive this secret.
- **Browser data plane**: public registrations authorize possession of the
  generated URL. Private registrations redirect through OIDC authorization code
  + PKCE and require an exact normalized match between the provider's verified
  email claim and the owner email registered by Lathe.

Run this Worker on a different registrable domain from OWUI. A subdomain of the
OWUI domain does not provide the same cookie boundary: sibling applications can
share parent-domain cookies.

Lathe supplies the owner email from OWUI's trusted injected user context. URL
parameters, browser-submitted email, and the model are never identity
authorities. OIDC state and opaque browser sessions are short-lived KV records
scoped to one random preview hostname. The browser session uses a Secure,
HttpOnly, SameSite=Lax, host-only `__Host-lathe_session` cookie.

Public mode is not a confidentiality boundary: anyone possessing a public URL
can access its service. Private mode is owner-authenticated. In both modes the
upstream bearer URL stays server-side. The proxy strips its auth cookie before
forwarding to the sandbox and drops upstream attempts to set that cookie.

## Identity Compatibility

Private previews do not reuse or validate an Open WebUI browser session. The
Worker is a separate OIDC relying party with its own confidential client and
session. When OWUI already authenticates users through OIDC, the same provider
is usually the right authority for the Worker: an existing provider session may
make the second sign-in seamless, but the two applications remain isolated.

The integration requires all of the following:

- OWUI's injected user email and the provider's `email` claim identify the same
  person and match after trimming and lowercase normalization.
- The provider emits `email_verified: true`. A matching but unverified address
  is rejected.
- The provider supports OIDC discovery, authorization code flow with PKCE,
  token exchange, and userinfo with the `openid email` scopes.
- The provider accepts a single-label wildcard callback of
  `https://*.WRAPPER_DOMAIN/_lathe/auth/callback`. This lets every random preview
  origin receive its own host-only session cookie. Providers that prohibit
  wildcard callbacks are not compatible with the current helper service.

An OIDC client restriction may limit sign-in to the same group or community
that can access OWUI. The Worker then applies the narrower per-preview rule:
the authenticated email must equal the owner email registered by Lathe.

OWUI installations using only local users do not have an external browser
identity authority for this service to consult, so they cannot offer private
previews through this helper without first adding an OIDC provider or broker.
Public wrapped previews remain available without OIDC.

## Registration Contract

Lathe uses the existing wrapper protocol:

```http
POST /register
Authorization: Bearer $REGISTER_TOKEN
Content-Type: application/json

{"owner":{"subject":"owui-user-id","email":"owner@example.org"},
 "slot":"5000","upstream_url":"https://signed-upstream.example/",
 "requested_access":"private"}
```

The Worker generates a `lathe-{nonce}` label and returns:

```json
{"url":"https://lathe-{nonce}.previews.example.org/",
 "host":"lathe-{nonce}.previews.example.org",
 "access_mode":"owner-authenticated",
 "expires_at":"2026-09-20T01:00:00Z"}
```

The sensitive upstream URL is never echoed. Early revocation is
`DELETE /register/{label}` with the same bearer token.

Other trusted producers may register an explicit label:

```json
{"subdomain":"demo-abc123","target":"https://service.example/","ttl":86400}
```

`ttl` is optional and constrained to 60–86400 seconds. Lathe registrations must
request exactly `public` or `private`; the response reports `public-wrapped` or
`owner-authenticated` respectively. Generic explicit-label registrations remain
public-only.

## Deploy

Requirements: a Cloudflare-managed domain, Wrangler, and permission to create a
Worker, KV namespace, route, secret, and DNS records.

1. Copy the example configuration and replace every placeholder:

   ```bash
   cp wrangler.toml.example wrangler.toml
   wrangler kv namespace create SUBDOMAINS
   ```

2. Paste the returned namespace ID into `wrangler.toml`.

3. Confirm that the identity system satisfies the compatibility contract above,
   then create a confidential OIDC client with:

   - Callback URL: `https://*.WRAPPER_DOMAIN/_lathe/auth/callback`
   - Authorization code flow with PKCE enabled
   - Scopes/claims: `openid email`, including boolean `email_verified`
   - A confidential client secret

   Pocket ID supports the required single-label wildcard callback. The Worker
   additionally binds every OIDC state record to the exact random hostname, so
   one preview cannot complete another preview's sign-in.

4. Set `OIDC_ISSUER` and `OIDC_CLIENT_ID` in `wrangler.toml`. Create both Worker
   secrets, then deploy:

   ```bash
   wrangler secret put REGISTER_TOKEN
   wrangler secret put OIDC_CLIENT_SECRET
   wrangler deploy
   ```

5. In Cloudflare DNS, create proxied `A` records for `@` and `*`, both pointing
   to reserved TEST-NET address `192.0.2.1`. Worker routes own the requests; no
   origin exists at that address.

6. Configure Lathe's `preview_wrapper_url` as
   `https://WRAPPER_DOMAIN/register` and `preview_wrapper_key` as the same
   registration secret.

KV's `expirationTtl` performs actual expiry. The daily cron is a cleanup canary
and logs live/deleted record counts. No per-preview DNS record is created.

## Proxy Behavior

- Preserves method, body, path, and query string.
- Adds `X-Forwarded-Host` and `X-Forwarded-Proto`.
- Removes `Domain=` from upstream `Set-Cookie`, making cookies host-only.
- Removes `__Host-lathe_session` before proxying request cookies upstream and
  discards upstream attempts to set it.
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
