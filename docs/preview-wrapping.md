# Owner-authenticated preview wrapping

An administrator can configure a wrapping service that turns upstream bearer
URLs into owner-authenticated browser URLs. This is enforced inside `expose()`;
there is no second tool for the model to remember. Arbitrary HTTP services,
dufs, and code-server use the same path. SSH is separate and still returns a
credential-bearing command.

## Wire contract

Lathe makes an HTTPS POST to the configured `preview_wrapper_url` with
`Authorization: Bearer <preview_wrapper_key>` and JSON:

```json
{
  "owner": {"subject": "deployment-local OWUI user ID", "email": "owner@example.edu"},
  "slot": "5000",
  "upstream_url": "https://temporary-upstream.example/"
}
```

The subject and email come exclusively from injected `__user__` context.
They are not model-supplied arguments. The opaque slot is the resolved port.
Lathe sends no sandbox-management credential to the wrapper. The installation
credential establishes the registrar's authority; the wrapper determines its
identity namespace, destination restrictions, public naming, replacement,
revocation, retention, and browser-login policy.

A successful response contains:

```json
{
  "url": "https://owner-5000.previews.example/",
  "access_mode": "owner-authenticated",
  "expires_at": "2026-09-18T19:00:00Z"
}
```

Lathe verifies a distinct HTTPS destination, owner-authenticated access mode,
and a future timezone-qualified expiry. It rejects a response that reflects
the upstream hostname or installation credential in the returned URL. It never
follows registration redirects or exposes raw response/error text. Missing
configuration or identity, registration failure, and malformed responses all
fail closed. The configured wrapper remains trusted to implement its claims;
valid JSON alone cannot prove that it enforces browser ownership.

## Lifetime and user experience

`preview_expiry_seconds` controls the upstream signed URL (default 86400,
maximum 24 hours). The wrapper chooses its own registration lifetime. Lathe
does not force those two clocks to coincide. Both are reported distinctly in
the tool result; sandbox sleep or a stopped application can end availability
earlier. Calling `expose()` again prepares the service as needed and replaces
or renews the registration. The wrapper may invalidate existing browser sessions
and WebSocket connections on replacement.

A wrapper can assign a fresh browser origin on each registration. Do not promise
bookmark stability: a transient URL can intentionally change on every exposure,
isolating service workers and browser storage from earlier registrations.

Owner-authenticated URLs can be shown in screen recordings without sharing a
bearer credential. They do not hide the application's visible content, nor do
they grant collaborators access. Opening one may require a separate browser
login even if the owner is already signed into Open WebUI.

Browser-application compatibility belongs to the wrapping service, including
uploads, redirects, cookies, workers, WebSockets, and expiry of open connections.
Lathe embeds no deployment domains, identity-provider protocol, or proprietary
proxy protocol in this interface.
