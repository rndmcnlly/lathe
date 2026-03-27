/**
 * The Lathe explainer video script — single source of truth.
 *
 * This file IS the script. The JSX tags define both the visual content
 * and the narration for each slide. The evaluate() function walks the
 * element tree to produce the typed Part[] data that Remotion renders
 * and prepare.ts sends to TTS.
 *
 * Narrative: spin up, get to work, spin down.
 */

import React from "react";
import {
  Video, Part, Slide,
  Title, Text, Chat, User, Agent, CodeBlock, ToolCall, Scene,
  Inspiration, Clip, ArchDiagramTag, LifecycleDiagramTag,
  extractNarrations, toComposition, getTotalFrames, extractTimeline,
} from "./tags";

// ── The Script ────────────────────────────────────────────────────

const script = (
  <Video>

    {/* ── Act 1: Spin Up ──────────────────────────────────────── */}

    <Part id="spin-up" label="Spin Up">

      <Slide id="title" section>
        <Title main="Lathe" sub="An agent harness for Open WebUI" />
        Lathe is an agent harness for Open WebUI. Inspired by coding
        agents like Pi and OpenCode — but built for the browser.
      </Slide>

      <Slide id="the-landscape">
        <Inspiration tagline="Lathe brings this to the browser — any model, no local setup.">
          <Clip label="Pi" file="pi-demo.mp4" url="pi.dev" />
          <Clip label="OpenCode" file="opencode-demo.mp4" url="opencode.ai" />
        </Inspiration>
        Terminal coding agents are everywhere now. They're fast, capable,
        and deeply integrated with developer workflows. But they assume
        you're at a workstation with a shell. Lathe brings that same
        agent surface to Open WebUI — any model, any browser, no local
        setup.
      </Slide>

      <Slide id="first-message" section>
        <Chat>
          <User>I have a project repo I need to work on. Can you clone it and get oriented?</User>
          <Agent>Sure — let me pull that down and take a look.</Agent>
        </Chat>
        A user opens their browser and starts a conversation. Nothing
        special yet — just a chat window. But the moment the model
        reaches for a tool, something happens.
      </Slide>

      <Slide id="sandbox-spins-up">
        <CodeBlock
          language="text"
          code={`⠋ Spinning up sandbox...
  Creating VM for user@example.com
  Starting sandbox instance
  Waiting for toolbox daemon...
✓ Sandbox ready (4.2s)

$ bash("git clone https://github.com/...")
Cloning into '/home/daytona/workspace'...
✓ Done`}
          caption="First tool call triggers sandbox creation"
        />
        A sandbox spins up. A full Linux VM, provisioned on demand,
        dedicated to this user. The model didn't ask for it. The user
        didn't configure it. Lathe handled the lifecycle transparently —
        create, start, wait for ready.
      </Slide>

      <Slide id="arch-overview">
        <ArchDiagramTag />
        The architecture is simple. The user talks to a model in Open
        WebUI. The model calls Lathe's tools. Each tool executes in a
        Daytona sandbox. Results flow back as tool output the model
        reasons about.
      </Slide>

    </Part>

    {/* ── Act 2: Get to Work ──────────────────────────────────── */}

    <Part id="work" label="Get to Work">

      <Slide id="clones-repo" section>
        <ToolCall
          tool="bash"
          args={'"ls -la /home/daytona/workspace/"'}
          result={`total 48
drwxr-xr-x  8 daytona daytona 4096 Mar 26 09:14 .
-rw-r--r--  1 daytona daytona 1247 Mar 26 09:14 README.md
-rw-r--r--  1 daytona daytona  892 Mar 26 09:14 AGENTS.md
drwxr-xr-x  4 daytona daytona 4096 Mar 26 09:14 src/
-rw-r--r--  1 daytona daytona  340 Mar 26 09:14 package.json
drwxr-xr-x  2 daytona daytona 4096 Mar 26 09:14 tests/`}
          icon="terminal"
        />
        The agent clones the repo and explores the project structure.
        Shell commands, file reads, edits — the full surface of a coding
        agent, running in the sandbox.
      </Slide>

      <Slide id="onboards">
        <ToolCall
          tool="onboard"
          args=""
          result={`Loaded project context from AGENTS.md:

• Test with: npm test
• Style: Prettier, no semicolons
• Branch convention: feature/<name>
• Deploy: push to main triggers CI

3 files indexed, 1 convention file loaded.`}
          icon="book"
        />
        Then it calls onboard. This loads project-specific instructions,
        conventions, and context — all in one shot. The agent picks up
        the project's norms the same way a new teammate would read the
        contributing guide.
      </Slide>

      <Slide id="builds-something">
        <CodeBlock
          language="text"
          code={`$ write("/home/daytona/workspace/src/api.ts",
    "import express from 'express'\\n...")
✓ Wrote 1.2 KB

$ bash("npm install && npm run dev")
added 47 packages in 3.1s
Server listening on port 3000

$ bash("npm test")
✓ 12 tests passed`}
          caption="Agent writes, installs, runs, tests"
        />
        The agent gets to work. It writes code, installs dependencies,
        starts a dev server. Each step is a tool call — visible in the
        conversation as it happens. Packages stay installed across
        conversations because the sandbox is persistent.
      </Slide>

      <Slide id="expose-moment" section>
        <ToolCall
          tool="expose"
          args="port=8080"
          result={`Public URL (valid ~1 hour):
https://8080-sandbox-abc123.proxy.daytona.io/
  ?token=eyJ...

VS Code in the browser — full terminal,
extensions, file editing.`}
          icon="globe"
        />
        Now here's where it gets interesting. The agent calls expose —
        and the user gets a public URL to whatever's running in the
        sandbox. A web app. A file browser. A full VS Code instance. The
        interaction breaks out of the chat window.
      </Slide>

      <Slide id="beyond-chat">
        <Scene
          leftLabel="User in VS Code"
          leftLines={[
            "Opens exposed URL in new tab",
            "Edits src/api.ts directly",
            "Saves — dev server hot-reloads",
          ]}
          rightLabel="Agent in chat"
          rightLines={[
            'edit("src/routes.ts", old, new)',
            "Adds authentication middleware",
            "Runs test suite after each change",
          ]}
        />
        The user opens VS Code in one tab and the running app in
        another. They edit code directly while the agent makes structural
        changes through chat. Both work on the same filesystem, in the
        same sandbox, at the same time.
      </Slide>

    </Part>

    {/* ── Act 3: Spin Down ────────────────────────────────────── */}

    <Part id="spin-down" label="Spin Down">

      <Slide id="session-ends" section>
        <Chat>
          <User>That's good for today, thanks.</User>
          <Agent>Your sandbox will sleep automatically when idle. Files, packages, git history — all still there next time.</Agent>
        </Chat>
        When the user is done, they just close the tab. The sandbox
        idles for a few minutes, then sleeps on its own. No teardown.
        No save button.
      </Slide>

      <Slide id="any-device">
        <Scene
          leftLabel="Laptop"
          leftLines={[
            "Open chat.example.com",
            "Start a project, write code",
            "Close the lid and leave",
          ]}
          rightLabel="Phone"
          rightLines={[
            "Open chat.example.com",
            '"Pick up where I left off"',
            "Same sandbox, same files",
          ]}
        />
        And because the sandbox is in the cloud, it's not tied to a
        machine — it's tied to you. Start a conversation on your laptop
        at the office, then pick it up on your phone on the bus. Anywhere
        you can reach your Open WebUI server, your sandbox is there.
      </Slide>

      <Slide id="comes-back">
        <CodeBlock
          language="text"
          code={`⠋ Waking sandbox...
✓ Sandbox ready (1.8s)

$ onboard()
Loaded project context from AGENTS.md
  Last modified: 3 days ago
  12 files in workspace, git history intact

$ bash("git log --oneline -3")
a1b2c3d Add auth middleware
e4f5g6h Initial API scaffold
i7j8k9l First commit`}
          caption="New conversation, same workspace"
        />
        Days later, the user starts a new conversation. The sandbox
        wakes transparently on the first tool call. Files, packages, git
        history — all still there. The agent calls onboard and picks up
        the project context immediately.
      </Slide>

      <Slide id="the-cycle" section>
        <LifecycleDiagramTag />
        That's the cycle. Spin up, get to work, spin down. The sandbox
        is infrastructure that appears when you need it and disappears
        when you don't. One toolkit. One API key. Every model on the
        server gets a coding agent surface.
      </Slide>

      <Slide id="outro">
        <Title main="lathe.tools" sub="github.com/rndmcnlly/lathe" />
        Lathe. An agent harness for Open WebUI.
      </Slide>

    </Part>

  </Video>
);

// ── Export the tree and walkers ────────────────────────────────────

export { script, extractNarrations, toComposition, getTotalFrames, extractTimeline };
