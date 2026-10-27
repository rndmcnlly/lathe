# Preview Wrapper Contract

Normative protocol between Lathe and any preview wrapper: the reference Worker
in this directory, BayLeaf's preview gateway, or an independent implementation.
This file is written for agents assisting the small group of administrators who
own the contract. It favours completeness over readability. Human-facing
deployment guidance stays in [README.md](README.md).

Key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.
Anything this file does not constrain is implementation-defined, and Lathe MUST
NOT depend on it.

- Contract revision: 2 (draft, [Lathe #111](https://github.com/rndmcnlly/lathe/issues/111)).
  Revision 2 makes browser-header forwarding normative (section 5). Revision 1
  was the registration and `upstream_headers` contract of [Lathe #98](https://github.com/rndmcnlly/lathe/issues/98).
- Known implementations: the reference Worker (`src/index.js`); BayLeaf
  (`bayleaf-ucsc/bayleaf`, `api/src/routes/previews.ts`, tracked in
  [bayleaf#118](https://github.com/bayleaf-ucsc/bayleaf/issues/118)).
  `demo_local.py` is a local demonstration and is **not** a conforming wrapper.

## 0. How to change this contract

1. Edit this file first, then implementations, then Lathe. Every normative
   change bumps the revision and adds a line to section 11.
2. Classify the change against the deployed wire protocol:
   - **Wrapper-only semantics** (for example, forwarding rules): wrappers may
     ship before or after Lathe, independently.
   - **New optional request field**: wrappers MUST accept and ignore it before
     Lathe sends it. Making it required is a separate, later revision.
   - **New reply field Lathe requires**: every wrapper Lathe may be configured
     against must emit it before Lathe requires it.
3. Update the case table (section 9) in the same change. A rule without a case
   is not checkable and SHOULD NOT be added.
4. Run the conformance process (section 10) against every implementation you
   control, and record known nonconformance in section 12 instead of weakening
   the rule.

## 1. Roles and trust

- **Lathe** (the producer) runs inside Open WebUI. Owner identity comes only
  from OWUI's injected user context. The model controls `access`, `tag`, the
  sandbox port and `upstream_headers`, and nothing else.
- **Wrapper** owns the registration control plane, the browser data plane,
  browser sessions and hostname policy. Lathe trusts the configured wrapper:
  nothing in the reply proves enforcement (see section 10 for what conformance
  testing can and cannot establish).
- **Upstream** is the sandbox's signed preview URL. It is a bearer capability;
  a wrapper MUST treat its full authority component as secret.
- **Browser** is untrusted. Headers that page script sets carry only what the
  application's own origin already knows. Headers the browser attaches by
  itself (ambient headers) can carry state the application never chose to
  send. This distinction drives section 5.

## 2. Registration

### 2.1 Request

```http
POST {registration endpoint}
Authorization: Bearer {installation credential}
Content-Type: application/json
```

```json
{"owner": {"subject": "owui-user-id", "email": "owner@example.org"},
 "upstream_url": "https://signed-upstream.example/",
 "access": "private",
 "tag": "vscode",
 "upstream_headers": {"Authorization": "Basic YXBwOnNlY3JldA=="}}
```

- `owner.subject`, `owner.email`: non-empty strings after trimming. Lathe
  sends OWUI's user id and email. The wrapper MUST NOT accept owner identity
  from any other source.
- `upstream_url`: absolute URL. Lathe sends only `https`. A wrapper MAY accept
  `http` for non-Lathe producers. Path and query of `upstream_url` MUST NOT be
  relied on: the browser's path and query replace them (section 5.1), so any
  upstream credential MUST live in the authority component.
- `access`: exactly `"public"` or `"private"`. Required. It is a requirement,
  not a preference: a wrapper that cannot enforce the requested policy MUST
  reject the registration, never downgrade it.
- `tag`: optional, empty or `^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$`. An
  untrusted display hint. A wrapper MAY ignore it and MUST NOT treat it as an
  authorization claim.
- `upstream_headers`: optional. Section 4.
- Lathe sends no `ttl`. Wrappers MUST NOT require one.
- Unknown fields: a wrapper SHOULD ignore them (forward compatibility, section 0).

### 2.2 Authentication

- The bearer credential is an installation secret, distinct from any sandbox,
  provider or user credential.
- A wrapper SHOULD compare it in constant time.
- Failure: 401. The body MUST NOT echo the presented credential.

### 2.3 Success reply

Status 200, `Content-Type: application/json`, `Cache-Control: no-store`:

```json
{"url": "https://{label}.{zone}/",
 "expires_at": "2026-09-20T01:00:00Z",
 "upstream_headers_applied": true}
```

- `url` (required): `https`, has a hostname, no userinfo, no fragment, no
  whitespace. Its hostname MUST differ from the upstream hostname, and neither
  the upstream hostname nor the installation credential may appear anywhere in
  `url` after percent-decoding.
- `expires_at` (required): RFC 3339 with an explicit offset or `Z`, strictly in
  the future. It is the absolute lease end; section 3.
- `upstream_headers_applied`: MUST be exactly JSON `true` when the request
  carried a nonempty `upstream_headers`, and MUST be absent otherwise.
- `host` and other extra fields: MAY be present; Lathe ignores them.
- The reply MUST NOT contain `upstream_url`, injected header names or values,
  or the installation credential.

### 2.4 Errors

- Any non-2xx is a refusal. Lathe follows no redirects; 3xx is a refusal.
- Bodies SHOULD be `{"error": "..."}` and MUST NOT echo `upstream_url`,
  header values, or credentials.
- Recommended statuses: 400 for malformed input, 401 for credentials, 403 for
  policy refusal (for example, owner domain or upstream not allowed), 409 for
  conflict, 503 when a required capability (OIDC, encryption key) is not
  configured. Lathe does not distinguish them.

### 2.5 Hostnames

- Each Lathe registration MUST yield a fresh hostname containing at least 64
  bits of randomness. Registrations are independent leases; a new registration
  MUST NOT revoke or replace an earlier one.
- The hostname MUST be a single DNS label under the wrapper zone, on a
  registrable domain distinct from OWUI's (cookie isolation).
- Hostname text is never an authorization input. Policy comes from the stored
  record.
- Wrappers MAY include access, tag or owner-derived text. Owner-derived text
  publishes identifiers to DNS and certificate-transparency logs; that choice
  belongs to the wrapper administrator.

### 2.6 Revocation

- A wrapper MAY offer revocation. Lathe currently never calls it.
- After revocation or expiry, new HTTP requests MUST fail, and the wrapper
  SHOULD close established WebSockets.

## 3. Lease and storage

- The lease MUST end at `expires_at`, checked at request time against the
  stored absolute expiry, not only by storage eviction.
- Browser sessions MUST NOT outlive the lease.
- If `upstream_headers` is nonempty, the destination and headers MUST be stored
  encrypted with authenticated encryption bound to the hostname, so a record
  copied to another hostname fails. Missing encryption configuration MUST
  refuse header-bearing registrations (503), never store plaintext.
- Owner identity MAY be stored in plaintext metadata. If it is, say so in the
  wrapper's privacy documentation.
- The wrapper MUST NOT store or log request or response bodies.

## 4. `upstream_headers` (injection)

### 4.1 Validation (both Lathe and wrapper)

- Object of string to string; at most 16 entries.
- Names match ``^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$``.
- Values: printable ASCII `[\x20-\x7e]*`, at most 4096 bytes; empty allowed.
- Sum of name and value lengths at most 8192.
- Case-insensitive duplicates rejected.
- **Injection-forbidden names**, compared case-insensitively:
  `host`, `cookie`, `origin`, `referer`, `forwarded`, `x-real-ip`,
  `true-client-ip`, `connection`, `upgrade`, `keep-alive`, `te`, `trailer`,
  `transfer-encoding`, `content-length`, `expect`, `http2-settings`,
  `proxy-authorization`, `proxy-authenticate`.
- **Injection-forbidden prefixes**: `x-forwarded-`, `sec-`, `cf-`, `daytona-`,
  `x-daytona-`, `x-lathe-`.
- A wrapper MAY additionally reject values it recognizes as platform
  credentials (BayLeaf does). It MUST reject such a registration, never
  silently drop the header.
- Errors MUST NOT echo names or values. Invalid input MUST NOT be stored.

### 4.2 Semantics

- Applied to every proxied HTTP request and WebSocket handshake for the
  registration, in either access mode, as the **last** step of header
  construction (section 5.2). Injected values replace browser values
  case-insensitively.
- In private mode, injection happens only after the owner gate passes. Anonymous
  and wrong-owner requests MUST NOT reach the upstream at all.
- In public mode, every visitor exercises injected credentials. Lathe SHOULD
  require an explicit acknowledgement from the model before sending
  `upstream_headers` with `access: "public"` (client-side; no wire field in
  revision 2).
- Purpose: values the browser cannot hold, such as fixed application auth.
  Injection is **not** a forwarding mechanism. Browser headers already pass
  (section 5). Injecting a CSRF marker disables the application's own check
  and SHOULD NOT be recommended.

## 5. Proxied requests

### 5.1 Target

- Scheme and authority come from the stored upstream. Path and query come from
  the browser request, assigned as components, never resolved as a relative
  URL (so `//evil.example/x` cannot change the authority).
- Method and body pass through unchanged. GET and HEAD have no body.
- The wrapper MUST fetch with manual redirect handling and MUST NOT follow
  upstream redirects itself.
- A wrapper SHOULD bound upstream request time.

### 5.2 Request headers

Build the upstream header set in this order. The order is normative: a later
step wins over an earlier one.

1. **Start from all browser request headers.**
2. **Remove `Connection`-nominated fields.** For every token in `Connection`,
   remove the header of that name, except `upgrade` and `sec-websocket-*` on a
   WebSocket handshake. Apply this unconditionally, not only when injection is
   configured.
3. **Remove wrapper-controlled headers.** These are never taken from the
   browser:
   - routing and identity: `host`, `forwarded`, `x-real-ip`, `true-client-ip`,
     and the prefixes `x-forwarded-`, `cf-`;
   - provider and wrapper namespaces: prefixes `daytona-`, `x-daytona-`,
     `x-lathe-`;
   - transport: `connection`, `keep-alive`, `te`, `trailer`,
     `transfer-encoding`, `content-length`, `expect`, `http2-settings`,
     `proxy-authorization`, `proxy-authenticate`, `accept-encoding` (the
     wrapper's fetch negotiates and decodes content itself); and `upgrade`
     except on a WebSocket handshake.
4. **Mediate ambient headers.**
   - `cookie`: remove every cookie in the wrapper's own namespace (for example
     `__Host-lathe_session`, BayLeaf's gateway cookies). A wrapper that rewrites
     application cookie names MUST present them to the upstream under the
     application's original names. Drop the header if empty.
   - `authorization`: forward, except a wrapper MAY drop values it recognizes
     as platform credentials. It MUST NOT forward its own session credential.
   - `referer`: MAY be dropped or reduced to the origin. Preview hostnames can
     be capabilities.
   - `origin`: MUST be forwarded unchanged when present. Applications need it
     for their own cross-origin checks.
   - `sec-fetch-*`: MAY be forwarded. They are browser-set and truthful.
   - `sec-websocket-*`, `upgrade: websocket`, `connection: Upgrade`: MUST be
     forwarded on WebSocket handshakes.
5. **Forward everything else unchanged.** Any header not removed or mediated
   above MUST reach the upstream with its browser value. A wrapper MUST NOT
   use an allowlist of application header names.
6. **Set wrapper headers.** `X-Forwarded-Host: {public host}`,
   `X-Forwarded-Proto: https`. Provider headers the wrapper itself needs (for
   example `X-Daytona-Skip-Preview-Warning`) MAY be set here.
7. **Apply `upstream_headers`** (section 4.2), overwriting.

A wrapper MAY apply further size or count limits to forwarded headers. It MUST
reject an over-limit request (431 or 400), not silently drop headers.

Rationale for step 5: page script on the preview origin belongs to the
application, so a script-set header carries nothing the application doesn't
already know. Cross-origin script can't attach custom headers to a non-simple
request without a CORS preflight, which the application controls. The
dangerous classes are routing, provider-interpreted and ambient headers, which
steps 2 to 4 enumerate structurally.

Note that the forwarding removal set (step 3) is deliberately **narrower**
than the injection-forbidden set (section 4.1). `origin`, `cookie`, `referer`
and `sec-*` are browser-owned: forwarded or mediated from the browser, never
forged by injection.

### 5.3 Access gate

- **Private**: every request, including non-GET requests and WebSocket
  handshakes, MUST be authenticated as the registered owner before contacting
  the upstream. Owner match: normalized (trimmed, lowercased) email equal to the
  registered owner email, with the identity provider asserting the email is
  verified. Unauthenticated top-level navigations SHOULD redirect to sign-in;
  other unauthenticated requests MAY be redirected or rejected with 401. A
  signed-in non-owner MUST receive a refusal without a session cookie.
- **Public**: possession of the URL is the only requirement.
- A wrapper MAY apply stricter request checks (for example, rejecting
  cross-origin non-GET requests by `Origin` or `Sec-Fetch-Site`, as BayLeaf
  does). Such checks MUST NOT remove headers that pass them.
- A wrapper MAY reserve a single path prefix on preview hosts for its own
  endpoints (reference Worker: `/_lathe/`; BayLeaf: `/__preview`). It MUST
  document the prefix, and requests under it MUST NOT reach the upstream. On
  public hosts the reference Worker currently reserves nothing (section 12).

## 6. Responses

- Status MUST pass through unchanged, including 3xx, 4xx and 5xx.
- `Set-Cookie`: drop any cookie in the wrapper's namespace. Strip `Domain` so
  application cookies are host-only. A wrapper MAY rename application cookies
  (section 5.2 step 4 then applies on the way back).
- `Location`: if it resolves to the upstream origin, rewrite to
  `https://{public host}` with the same path and query. Other locations pass
  unchanged.
- Error bodies: for 4xx and 5xx, a wrapper SHOULD pass the body through. It
  MUST replace the body when it contains the upstream hostname or any upstream
  credential, and it MAY replace bodies above a size bound (recommended
  64 KiB). Replaced bodies SHOULD say they were replaced, so agents debugging
  through the wrapper know the application's message was hidden. Match
  case-insensitively on the hostname, its first label, and the token portion
  of the label (for IP-literal upstreams, the full address only). "Upstream
  credential" here means the stored upstream capability, not configured
  `upstream_headers` values: those belong to the application, and a wrapper
  MAY additionally mask them above a minimum length. Buffering 4xx/5xx bodies
  means streamed error responses (for example server-sent events on a 4xx) are
  not streamed.
- Why this matters with Daytona: its preview proxy **appends** a second
  `X-Forwarded-Host` carrying the signed capability hostname after the
  wrapper's value (observed 2026-10-27; documented by Daytona's custom preview
  proxy guide). Applications can therefore learn and echo the capability.
  `X-Daytona-Trust-Forwarded-Host: true` does **not** suppress this on signed
  preview URLs (verified live 2026-10-27); Daytona documents it only for
  standard URLs with `X-Daytona-Preview-Token`.
- Masking is an accident guard, not a security boundary (the framing GitHub,
  GitLab, CircleCI and Vercel use for their log masking). An application that
  knows the capability can always transform it past a matcher. Successful (2xx)
  bodies are not scanned by either known implementation; a streaming,
  known-value redactor in the style of GitHub Actions' `SecretMasker`
  (registered encodings, in-place same-length replacement, carry-over across
  chunks) is a possible future revision, not a requirement.
- Response headers MUST NOT disclose the upstream hostname or credentials. A
  wrapper MAY drop or rewrite any header for that reason and MAY add security
  headers (CSP, `X-Frame-Options`, HSTS).
- WebSocket 101 responses follow the same `Set-Cookie` rules.

## 7. Wrapper-generated responses

- Unknown or expired hostname: 404. A wrapper MAY redirect top-level
  navigations to a stable explanation page.
- Upstream unreachable: 502. The body MUST NOT disclose the upstream.
- These pages are wrapper-authored. Agents SHOULD distinguish them from
  application responses by their documented text or a wrapper-specific header.

## 8. Lathe client obligations

- Fail closed: `access: "private"` or a nonempty `upstream_headers` MUST NOT
  fall back to a direct upstream URL on any registration failure.
- Public headerless registrations MAY fall back to the direct signed URL. That
  URL is itself a bearer credential; label it as such to the user.
- Never expose raw wrapper status codes, bodies or the installation credential
  to the model.
- Validate the reply exactly as section 2.3 states, including the strict
  `upstream_headers_applied === true` check.
- Do not send fields the configured wrapper may not accept (section 0).

## 9. Case table

Each case is checkable black-box with the echo upstream from section 10. "Seen"
means the echo reports the header with that value; "absent" means it does not
report it at all. Run every case on HTTP POST and on a WebSocket handshake
unless marked otherwise; run in both access modes unless marked.

| ID | Request or registration | Expected |
|----|------------------------|----------|
| F1 | `X-Pocket: 1` | seen `1` |
| F2 | `X-App-Csrf: <random>` on POST | seen, same value |
| F3 | `X-Some-Unknown-Header-Name: v` | seen `v` |
| F4 | `Origin: https://{public host}` | seen unchanged |
| F5 | `Accept`, `Content-Type`, `If-None-Match` | seen unchanged |
| F6 | `Authorization: Bearer app-token-123` | seen (unless wrapper documents a platform-credential screen that matches it) |
| R1 | `X-Forwarded-For: 203.0.113.9` | absent, or the wrapper's own value |
| R2 | `x-FORWARDED-host: evil.example` | value is `{public host}` |
| R3 | `CF-Connecting-IP: 203.0.113.9` | absent, or the edge's own value |
| R4 | `X-Daytona-Preview-Token: x` | absent |
| R5 | `Daytona-Anything: x` | absent |
| R6 | `X-Lathe-Anything: x` | absent |
| R7 | `True-Client-IP`, `X-Real-IP`, `Forwarded` | absent, or wrapper-set |
| R8 | `Proxy-Authorization: Basic eA==` | absent |
| C1 | `Connection: X-Pocket` plus `X-Pocket: 1` | `X-Pocket` absent (HTTP only) |
| C2 | `Connection: X-Injected` with `X-Injected` configured for injection | seen with injected value |
| C3 | WebSocket handshake with `Connection: Upgrade, X-Foo` and `X-Foo: 1` | handshake succeeds; `X-Foo` absent |
| K1 | Cookie with the wrapper session cookie plus `app=1` | `cookie` seen as `app=1` only |
| K2 | Only the wrapper session cookie | `cookie` absent |
| I1 | Registration injects `X-App-Mode: demo`; browser sends `x-app-mode: forged` | seen `demo` |
| I2 | Registration injects `Authorization: Basic YTpi`; browser sends `Authorization: Bearer forged` | seen `Basic YTpi` |
| I3 | Private: anonymous request with injection configured | upstream receives nothing |
| I4 | Private: signed-in non-owner with injection configured | upstream receives nothing; no session cookie set |
| V1 | Each injection-forbidden name and prefix, case-varied | registration rejected; nothing stored |
| V2 | 17 entries; 65-byte name; 4097-byte value; non-ASCII value; total 8193 | rejected |
| V3 | Exactly 16 entries; 64-byte name; 4096-byte value; empty value | accepted, `upstream_headers_applied: true` |
| V4 | `upstream_headers: {}` | accepted, no `upstream_headers_applied` |
| P1 | Browser path `//evil.example/x?y=1` | upstream sees authority unchanged, path `//evil.example/x`, query `y=1` |
| P2 | `upstream_url` with path `/secret` | upstream sees browser path, not `/secret` |
| E1 | Upstream returns 403, body `pocket: csrf header missing` | status 403, body passed through |
| E2 | Upstream returns 500 whose body contains the upstream hostname | status 500, body replaced |
| E3 | Upstream returns `Location: {upstream origin}/next` | `Location: https://{public host}/next` |
| E4 | Upstream sets `Set-Cookie: s=1; Domain=example; Path=/` | browser receives cookie without `Domain` |
| E5 | Upstream sets the wrapper's session cookie name | dropped |
| L1 | Request after `expires_at` even if the record is still in storage | refused |
| L2 | Registration reply | `url` and `expires_at` valid per section 2.3; no upstream host, credential or header values anywhere in the body |

## 10. Conformance process

This is deliberately exhaustive. Run all of it for any normative change, any
change to a wrapper's request or response path, and before claiming a new
implementation conforms.

### 10.1 Echo upstream

Run a small HTTP and WebSocket server the wrapper can reach as an upstream:

- For HTTP, respond 200 with JSON `{method, path, query, headers}` where
  `headers` is every received header, names lowercased, values verbatim.
- `/status/{code}?body=...` returns that status and body (cases E1, E2).
- `/redirect?to=...` returns 302 with that `Location` (E3).
- `/cookie?set=...` returns the given `Set-Cookie` (E4, E5).
- For a WebSocket handshake, accept it and send the same JSON as the first
  text frame.

Where to run it:

- Reference Worker: a loopback server under Miniflare, as `test/runtime.test.js`
  already does.
- BayLeaf: inside a real sandbox, because BayLeaf only accepts Daytona upstream
  suffixes. Expose it through the normal path, then drive the cases from
  outside. Private cases need an owner browser session; use a real signed-in
  browser (an agent-driven browser is fine) or the gateway's local harness
  (`npm run test:previews`) with the same case table.

### 10.2 Driving the cases

1. Register the echo upstream twice per access mode: once without
   `upstream_headers`, once with the injection set used by I1, I2 and C2.
2. For each case in section 9, send the request through the wrapper with a raw
   HTTP client that does not add or normalize headers (not a browser, except
   for private-session setup and K1). Record the full request, wrapper
   response, and echo report.
3. Repeat every HTTP case that the table does not mark "HTTP only" as a
   WebSocket handshake and read the first frame.
4. Repeat with header names case-varied (all lower, all upper, mixed). Any
   difference in outcome is a failure.
5. Repeat F1 to F3 with values at the wrapper's documented size limits and one
   byte over. Over-limit must be rejected, not truncated or dropped.

### 10.3 Proving the kit detects faults

Before trusting a passing run, show that the kit fails on broken wrappers. On
a disposable local copy (never a deployed wrapper), inject each fault and
confirm at least one case fails:

- forwarding through an application-name allowlist (fails F1 to F3);
- copying all browser headers (fails R1 to R8);
- applying `Connection` stripping only when injection is configured (fails C1);
- injecting before forwarding instead of after (fails I1, I2, C2);
- dropping `Origin` (fails F4);
- resolving the browser path as a relative URL (fails P1);
- masking all error bodies (fails E1);
- passing all error bodies (fails E2).

Record which case caught each fault. A fault no case catches is a gap in
section 9; add a case.

### 10.4 What conformance cannot establish

- That a deployed wrapper runs the code that passed. An administrator can
  redeploy at any time. Conformance is evidence about a revision, not a
  guarantee about production.
- Provider-side behavior beyond the wrapper, such as which headers Daytona's
  preview proxy interprets. Audit that separately and record it in section 12.
- Browser behavior. A raw client can send headers no browser would send; that
  is the point of the R cases, but it means passing the kit does not prove
  browser compatibility for any particular application.

## 11. Revision history

- **2** (draft, Lathe #111): browser-header forwarding becomes normative
  (section 5.2), with structural removal of routing, provider, transport and
  wrapper headers and mandatory forwarding of everything else. Error bodies
  pass through unless they contain upstream secrets (section 6). The contract
  moves from `README.md` to this file. No wire change; wrappers and Lathe may
  ship in either order.
- **1** (Lathe #98): registration, `access`, `upstream_headers` validation,
  `upstream_headers_applied`, encrypted storage.

## 12. Known nonconformance and open questions

Reference Worker (`src/index.js`), as of contract revision 2:

- Conforms to section 5.2, constant-time bearer comparison and section 6 error
  bodies as of the revision 2 implementation (Lathe #111).
- Forwards `sec-*` browser headers (allowed by section 5.2 step 4).
- Applies no upstream request timeout.
- Reserves `/_lathe/auth/callback` only on private hosts; no prefix on public
  hosts.
- Wrong-owner or stale private sessions are redirected to sign-in again
  instead of refused, which can loop.
- Revocation does not close established WebSockets (section 2.6 SHOULD).

BayLeaf (`api/src/routes/previews.ts`), as of contract revision 2:

- Conforms to sections 5.2 and 6 since bayleaf commit `8abe2d3` (deployed
  2026-10-27, bayleaf#118). Live qualification against production passed the
  F, R, E1, E2 and navigation cases with a public exposure
  (`api/scripts/qualify-preview-passthrough.py`); private mode is covered by
  its synthetic harness.
- Stricter than required, all allowed: forwards script-set headers only on
  same-origin non-navigation requests; drops `sec-*`; drops browser
  `Authorization` unless it is a single Bearer token; screens every forwarded
  value for platform credentials; rejects cross-origin non-GET requests.
- Cloudflare's edge rejects client requests carrying `CF-Connecting-IP` before
  the Worker runs (R3 is satisfied by refusal).
- Reply omits `host` (allowed).

Open questions:

- Which headers Daytona's preview proxy interprets outside the `x-daytona-`
  prefix. Until audited, R4 and R5 cover only the documented namespace.
- Whether to move from signed preview URLs to standard URLs with
  `X-Daytona-Preview-Token` plus `X-Daytona-Trust-Forwarded-Host`, which would
  stop the hostname being a credential. Tradeoff: that token covers the whole
  sandbox (every port), cannot be revoked, and would need the wrapper to
  enforce a port allowlist itself. Two behaviours to verify first: whether
  Daytona strips `X-Daytona-Preview-Token` before the app, and whether the
  trust header then fully replaces the appended value.
- Whether the public-injection acknowledgement should become a wire field
  (section 4.2). If it does, follow the optional-then-required sequence in
  section 0.
- Whether `sec-fetch-*` forwarding should become MUST, so applications can
  rely on it for their own CSRF defence.
