# Lathe: a legible sandbox interface

## Tools tell a human-facing story

Tool calls are part of the user interface, not only an API for the model.
As `glob`, `read`, `grep`, `edit`, `bash`, and `view` scroll past, their names
tell the person following the work what the agent is doing: discovering,
inspecting, searching, changing, executing, and checking the result.

Collapsing those operations into `bash` would reduce the tool vocabulary but
hide that story inside shell commands. Preserve useful, recognizable operations
even when their implementations become thin adapters to standard programs.
Optimize for a small number of concepts and bespoke behaviors, not the smallest
possible number of tool names.

The tool contracts should also make mistakes legible: explicit paths, bounded
inspection, unambiguous edits, and clear failure results. These remain useful
even when models have large context windows and can manage their own intake.

## Responsibility boundaries

- **Open WebUI owns conversations and agent orchestration:** model routing,
  native subagents, conversation compaction, and continuation.
- **Lathe owns the authenticated sandbox connection and tool contracts:**
  per-user sandbox lifecycle, file operations, command execution, interpreter
  identity, and explicit access to running services.
- **Sandbox programs own application behavior:** installation, startup,
  readiness, reuse, and recovery of particular applications belong in ordinary
  scripts and discoverable recipes, rather than the OWUI adapter.

`expose` publishes a running service with an explicit access policy. It does not
need to know how an editor, terminal, file browser, or website was installed.
Keeping the access boundary in Lathe is compatible with moving application
management out of it.

## Optional recipes

Repository-hosted recipes are fetched on demand, pinned to a Git commit, and
cached by that revision in the sandbox. Their source is inspectable and their
invocation works through `bash`. First acquisition needs network access;
reusing a cached script need not. Application dependencies may still need the
network.

Extracting scripts makes the core smaller and gives application maintenance a
clear home. It is not counted as eliminating those scripts from the whole
system. Local trials may preseed the exact pinned source before its commit is
published.

## State

Files in an ordinary sandbox survive stop/archive. Destroying the sandbox
destroys that workspace; Lathe does not provision an additional persistent
volume. Important results can be exported or committed to a remote repository.

`interpret` deliberately retains conversation-scoped Python state. Native
subagents share the user's filesystem but have their own chat identities and
therefore separate interpreter contexts. File exchange is the explicit bridge
between those computations.
