# Contributing to Lathe

Contributions are welcome. Before opening a PR, please read this document — the project has an unusual constraint that matters.

## Lathe is agent-written

Every line of code in this repo was written by an AI coding agent, directed by a human. We'd like to keep it that way. This isn't a gimmick — it's a forcing function that keeps the codebase within the reach of the tools it provides. Lathe is maintained using Lathe.

**What this means for contributors:**

- Use a coding agent (Lathe itself, Claude, Cursor, Copilot, OpenCode, etc.) to write your changes. The agent does the typing; you do the thinking.
- If you need to hand-edit a line to fix something an agent can't get right, that's fine — pragmatism over purity. But if you find yourself hand-writing a whole feature, consider whether the friction is telling you something about the design.
- Mention which agent you used in your PR description. Not for gatekeeping — for curiosity.

## Before you open a PR

1. **Read `AGENTS.md`** — it has architecture notes, test procedures, and contribution rules that apply to both humans and agents working on the repo.
2. **Run the unit tests**: `uv run --script test_harness.py unit`
3. **Don't break the single-file constraint** — `lathe.py` is one file, deliberately. Resist the urge to split it.
4. **Route documentation to the right home** — user-facing docs go in `docs/`, admin docs in `README.md`, agent/contributor docs in `AGENTS.md`. See the "Three audiences, three homes" section in `AGENTS.md`.

## Good first contributions

- New recipes for the `lathe(manpage="recipes")` system — tested bootstrap scripts for tools that work within Daytona's sandbox constraints.
- Bug reports with reproduction steps (even better: a failing test case).
- Documentation improvements routed to the right audience.
- Improvements to the demo or explainer video pipelines.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
