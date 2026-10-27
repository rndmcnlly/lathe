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
scoped to one random preview hostname. The browser session uses a
Secure, HttpOnly, SameSite=Lax, host-only `__Host-lathe_session` cookie.

The Lathe/OWUI administrator and preview-wrapper administrator are distinct
roles, even when one person performs both. The Lathe admin configures the
registration endpoint and credential. The wrapper admin controls
`HOSTNAME_TEMPLATE` and therefore decides whether access policy, model-supplied
tags, or trusted owner identifiers become public hostname text. Lathe and its
model cannot override that template.

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

The authoritative, normative protocol for Lathe and every wrapper (this Worker,
BayLeaf's gateway, or an independent implementation) is
[CONTRACT.md](CONTRACT.md): registration request and reply, `upstream_headers`
validation and semantics, request-header forwarding, response handling, a
black-box case table, and the conformance process. Change that file first.

This section records only what is specific to the reference Worker.

### Reference Worker labels

Besides Lathe registrations, other trusted producers may register an explicit
label:

```json
{"subdomain":"demo-abc123","target":"https://service.example/","ttl":86400}
```

`ttl` is optional and constrained to 60–86400 seconds; Lathe registrations
default to 86400. Generic explicit-label registrations are public-only, cannot
use the reserved `lathe-` prefix, and replace any existing registration for the
same label, including its injected headers. Early revocation is
`DELETE /register/{label}` with the registration bearer token. Successful
replies also include `host`, which Lathe ignores.

### Upstream header storage and rotation

Header-bearing registrations require `REGISTRATION_ENCRYPTION_KEY`: a base64
encoded, random 32-byte secret. The complete registration, including destination,
headers, owner, access, and absolute expiry, is encrypted using AES-256-GCM with a
fresh 96-bit nonce and the exact hostname as authenticated additional data. KV
stores a version-3 envelope (`iv`, `payload`), with the existing expiry TTL and
ordinary producer/access/owner metadata, but no destination or header values in
metadata. Existing headerless version-2 records remain compatible. Plaintext
records containing nonempty headers are rejected. Missing/invalid encryption
configuration refuses new header-bearing registrations with a sanitized 503.

KV expiry and DELETE revocation remain authoritative. Absolute expiry is also
checked on read. Generic re-registration replaces the complete record, including
headers: omission removes old injections. Lathe registrations use fresh random
hostnames, so a new exposure does not revoke previous live exposures. Revocation
does not terminate already-established WebSockets.

The encryption secret is separate from `REGISTER_TOKEN`: rotating the latter
preserves encrypted leases. Replacing the encryption secret makes old encrypted
records unreadable (404), until re-registered; there is no old-key fallback or
automatic migration. KV ciphertext may remain until TTL/deletion. Encryption
does not hide data from the running Worker, administrators holding its key, or
the upstream application, and is not a zero-access claim. Values supplied by a
model already exist in OWUI tool arguments; applications may echo them in bodies
or response headers. This proxy does not sanitize successful application bodies.

An app treating a header as an authentication assertion must independently
verify trusted peer addresses and prevent spoofing via direct or alternate
upstream routes. The wrapper is not a private network tunnel and cannot prove
that network boundary. Protocol reference: [Lathe #98](https://github.com/rndmcnlly/lathe/issues/98),
related independent BayLeaf implementation: [BayLeaf #86](https://github.com/bayleaf-ucsc/bayleaf/issues/86).

### Hostname templates

`HOSTNAME_TEMPLATE` may contain literal text and these variables:

- `{access}`: required policy, `public` or `private`.
- `{tag}`: model-supplied tag, or `preview` when omitted.
- `{email_user}`: owner email before `@`.
- `{email}`: complete owner email.
- `{user_id}`: OWUI's injected owner subject.

After substitution, the complete prefix is normalized to lowercase ASCII:
non-alphanumeric runs (including dots, `@`, spaces, and underscores) become
hyphens, and boundary hyphens are removed. The Worker rejects unknown variables,
an empty result, or a rendered prefix too long to leave room for `-{nonce}` in a
63-character DNS label. It never permits the template to control or omit the
nonce suffix.

DNS-safe is not private. `{email}`, `{email_user}`, and `{user_id}` can publish
personal identifiers in DNS queries, browser history, logs, and certificate
transparency systems. The preview-wrapper administrator, not the Lathe admin or
model, decides whether those variables are appropriate for the deployment's
population and threat model.

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

4. Set `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and the desired `HOSTNAME_TEMPLATE` in
   `wrangler.toml`. Create both Worker secrets, then deploy:

   ```bash
   wrangler secret put REGISTER_TOKEN
   wrangler secret put OIDC_CLIENT_SECRET
    wrangler deploy
    ```

   To enable upstream headers, also generate and install a separate secret:

   ```bash
   openssl rand -base64 32 | wrangler secret put REGISTRATION_ENCRYPTION_KEY
   ```

   Deploying source does not create this secret. Headerless previews do not
   require it. Treat encryption-key rotation as invalidation of encrypted leases.

5. In Cloudflare DNS, create proxied `A` records for `@` and `*`, both pointing
   to reserved TEST-NET address `192.0.2.1`. Worker routes own the requests; no
   origin exists at that address.

6. Configure Lathe's `preview_wrapper_url` as
   `https://WRAPPER_DOMAIN/register` and `preview_wrapper_key` as the same
   registration secret.

KV's `expirationTtl` performs actual expiry. Auth state and sessions also carry
absolute expiry: KV requires at least 60 seconds of storage TTL, even when the
preview lease has less time remaining. Longer storage never extends authorization.
State/session records from older code without absolute expiry fail closed and
require a fresh sign-in. The daily cron is a cleanup canary and logs live/deleted
record counts. No per-preview DNS record is created.

## Proxy Behavior

Request and response handling is specified in [CONTRACT.md](CONTRACT.md)
(sections 5 and 6). Its section 12 lists where this Worker does not yet conform.

The upstream target must be reachable from Cloudflare's edge. Do not treat the
wrapper as a private network tunnel.

## Development

```bash
npm ci
npm run check
npm test
wrangler dev
wrangler tail
```

Use `.dev.vars.example` for local development. `.dev.vars`, `wrangler.toml`,
and Wrangler state are gitignored.

Tests require Node 22 or newer. PR CI runs both wrapper and demo-video Node
suites. `npm test` includes `test/runtime.test.js`; `npm run test:runtime` selects
only that qualification. It takes about a minute because it observes real KV
expiry rather than replacing the clock.

The runtime suite executes the shipped Worker in workerd via pinned Miniflare,
with real local KV bindings and loopback HTTP/OIDC/WebSocket servers. The local
identity provider deterministically issues codes, validates PKCE and callback
binding, and supplies owner/wrong-owner/unverified claims. No fetch mocks,
production credentials, real user login, cloud deployment, or browser download
are involved. It checks private gating, host-bound state/session rejection,
request/response cookie isolation (including 101 headers), injected application
credentials, real upgrade handles with text/binary frame echo, redirects, KV's
minimum TTL, sub-minute remaining leases, stale-record absolute expiry, and
actual storage eviction.

This is not qualification of Cloudflare DNS/TLS/routes, Pocket ID configuration,
OWUI identity injection, or browser cookie enforcement. Cookie attributes are
checked on HTTP responses and cross-host session replay is attempted explicitly,
not through a browser cookie jar. Local KV does not model multi-edge eventual
consistency or make read/delete an atomic one-time-state primitive. The fast
unit suite uses mocked fetch and a TTL-aware in-memory KV; it does not claim to
prove Worker upgrades or edge propagation. Miniflare is currently pinned to its
5.x alpha (and its workerd build) to avoid the known vulnerable dependencies in
the available 4.x release; the lockfile makes this qualification reproducible.
