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
- **The agent operates ordinary sandbox programs:** installation, startup,
  readiness, reuse, and recovery belong to the agent's inspected commands and
  user-approved practices, rather than a Lathe-managed app installer.

`expose` publishes a running service with an explicit access policy. It does not
need to know how an editor, terminal, file browser, or website was installed.
Keeping the access boundary in Lathe is compatible with moving application
management out of it.

Daytona's built-in desktop is a provider capability, not a Lathe-installed
application. `peek(command, args)` observes its screen, accessibility state and
diagnostics; `poke(command, args)` sends input or changes desktop lifecycle.
Commands are a closed, strictly validated vocabulary, not arbitrary API routes.
`expose(target="vnc", access=...)` supplies the human's noVNC connection to the
same screen, through the ordinary preview policy. Private access is strongly
advised, not enforced by target; explicit public access remains an escape hatch.
Browser installation and launch remain ordinary sandbox program work.

The desktop is shared across conversations and people. Human login requires an
explicit conversational handoff: stop input and screenshots until the human
says ready, then observe afresh. This is coordination, not an enforced control
lock or credential-isolation boundary. Screenshots delivered to OWUI enter
conversation history; logging in grants sandbox software access to that session.

## Optional applications and reference material

File browsers, terminals, IDEs, and static servers are ordinary optional
applications. Lathe ships no app installer, pinned support bundle, cache receipts,
or prepackaged app skills. The agent can use preinstalled programs, reachable
intranet repositories, or other approved sources. Dependencies such as ripgrep
can be supplied that way too; public Internet access is not a core prerequisite.

The internal services manual teaches installation checks, readiness, process
identity, safe shutdown, port negotiation, and private/public proxy boundaries.
Application-specific details live in the mutable agent reference `handbook/`.
The administrator may set `handbook_url` to a public, intranet, or test Markdown
reference. Its default is empty. Lathe displays configured guidance but never
fetches or executes it. Agents consult references when needed and adapt them to
installed versions and actual network access. `docs/` remains exclusively the
public landing page, not an agent handbook.

## Skills grown with consent

Successful procedures can become skills only when the user requests or approves
preserving them. Transagency includes both the capacity developed through
human–agent collaboration and the participant's expanded experience of agency.
Agentic skills support independent sandbox work; transagentic skills support
coordination through Open WebUI. Both develop with the user's choices about
what to remember, automate, share, revise, or forget.

Discovery reads existing `SKILL.md` frontmatter from three scopes. Shared
`~/.agents/skills` provides sandbox-wide defaults; Lathe-specific
`~/.local/share/lathe/skills` overrides those defaults; project
`<project>/.agents/skills` overrides both on a name collision. `XDG_DATA_HOME`
replaces `~/.local/share` when configured. Global and project `AGENTS.md`
discovery is unchanged. Shared skills must not assume other agents have Lathe's
`expose` tool or communicate through Open WebUI.

Discovery neither installs nor creates skills. A first successful app setup is
an opportunity to offer a skill recording the tested version, paths, process
identity, start/stop checks, port-reuse decisions, and upgrade preferences.
Existing skills are read before use, not silently overwritten from the handbook.
Deleting a skill does not trigger regeneration. A later successful use may
prompt another offer, still requiring approval. Binaries, configuration, logs,
and service state stay outside skill directories; secrets and expiring preview
URLs do not belong in saved procedures.

## State

Files in an ordinary sandbox survive stop/archive. Destroying the sandbox
destroys that workspace; Lathe does not provision an additional persistent
volume. Important results can be exported or committed to a remote repository.

`interpret` deliberately retains conversation-scoped Python state. Native
subagents share the user's filesystem but have their own chat identities and
therefore separate interpreter contexts. File exchange is the explicit bridge
between those computations.
