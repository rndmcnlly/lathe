# Security Policy

## Supported versions

Only the latest version of `lathe.py` on the `main` branch is supported. There are no versioned releases of the toolkit itself — deployments pull from `main`.

## Reporting a vulnerability

If you find a security issue in Lathe, please report it privately rather than opening a public issue.

**Email**: adam@adamsmith.as

Include:

- A description of the vulnerability
- Steps to reproduce it (or a proof of concept)
- The impact as you understand it

I'll acknowledge receipt within 48 hours and aim to resolve confirmed vulnerabilities within 7 days. If you'd like to be credited in the fix, let me know.

## Scope

Lathe is a toolkit that runs inside an Open WebUI server process and makes API calls to Daytona's sandbox infrastructure. Security-relevant areas include:

- **Sandbox isolation** — each user gets one sandbox, identified by email. Cross-user access would be a critical issue.
- **Secret handling** — the `env_vars` UserValve gives credentials to every model-controlled shell command, including delegate commands. These values are masked in the OWUI settings UI but are not hidden from the model, which can read its command environment. Treat them as credentials entrusted to the model and use narrowly scoped, revocable tokens. Cross-user disclosure or retention outside the ephemeral command lifetime would be a critical issue.
- **API key exposure** — the Daytona API key is an admin Valve. If a model or user could extract it, they'd have control over all sandboxes.
- **Sandbox escape** — Lathe executes arbitrary commands inside Daytona sandboxes. This is by design. A vulnerability would be if sandbox commands could affect the OWUI host server or other users' sandboxes.

## Out of scope

- **Anything the model does inside its own sandbox** — users grant models shell access by enabling Lathe. The model can `rm -rf /` its own sandbox. This is expected.
- **Daytona platform vulnerabilities** — report these to [Daytona](https://www.daytona.io/) directly.
- **Open WebUI vulnerabilities** — report these to [Open WebUI](https://github.com/open-webui/open-webui/security).
