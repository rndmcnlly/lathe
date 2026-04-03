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
  Title, Text, Chat, User, Agent, CodeBlock, ToolCall, Scene, Callout,
  Conversation, Inspiration, Clip, ArchDiagramTag, LifecycleDiagramTag,
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
        <Conversation
          userMessage="Can you clone it and get oriented?"
          toolName="bash"
          toolArgs={'"git clone https://github.com/..."'}
          toolResultPreview={`⠋ Spinning up sandbox...
✓ Sandbox ready (4.2s)
Cloning into '/home/daytona/workspace'...
✓ Done`}
          toolIcon="terminal"
          agentReply="Cloned. A sandbox spun up automatically — let me look around."
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
        <Conversation
          userMessage="What's in the repo?"
          toolName="bash"
          toolArgs={'"ls -la /home/daytona/workspace/"'}
          toolResultPreview={`README.md  AGENTS.md  src/
package.json  tests/`}
          toolIcon="terminal"
          agentReply="It's a Node project with source in src/, tests in tests/, and an AGENTS.md with project conventions."
        />
        The agent clones the repo and explores the project structure.
        Shell commands, file reads, edits — the full surface of a coding
        agent, running in the sandbox.
      </Slide>

      <Slide id="onboards">
        <Conversation
          userMessage="Get familiar with how this project works."
          toolName="onboard"
          toolArgs=""
          toolResultPreview={`Loaded project context from AGENTS.md
• Test with: npm test
• Style: Prettier, no semicolons
• Deploy: push to main triggers CI`}
          toolIcon="book"
          agentReply="Got it — Prettier formatting, no semicolons, tests via npm test. I'll follow these conventions."
        />
        Then it calls onboard. This loads project-specific instructions,
        conventions, and context — all in one shot. The agent picks up
        the project's norms the same way a new teammate would read the
        contributing guide.
      </Slide>

      <Slide id="builds-something">
        <Conversation
          userMessage="Build out the API endpoint we discussed."
          toolName="bash"
          toolArgs={'"npm install && npm test"'}
          toolResultPreview={`added 47 packages in 3.1s
Server listening on port 3000
✓ 12 tests passed`}
          toolIcon="terminal"
          agentReply="API is up on port 3000 and all 12 tests pass. Ready for you to try it."
        />
        The agent gets to work. It writes code, installs dependencies,
        starts a dev server. Each step is a tool call — visible in the
        conversation as it happens. Packages stay installed across
        conversations because the sandbox is persistent.
      </Slide>

      <Slide id="expose-moment" section>
        <Conversation
          userMessage="Give me a VS Code editor for this project."
          toolName="expose"
          toolArgs={'target="code-server"'}
          toolResultPreview={`IDE URL (valid ~1 hour):
https://8080-sandbox-abc123.proxy...`}
          toolIcon="globe"
          agentReply="Here's your editor — VS Code in the browser, connected to the sandbox. Full terminal and extensions included."
        />
        Now here's where it gets interesting. The agent calls expose —
        and the user gets a public URL to whatever's running in the
        sandbox. A web app. A file browser. A full VS Code instance. One
        tool call installs, starts, and signs the URL.
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

      <Slide id="delegate-mention">
        <Callout
          icon="git-branch"
          heading="Delegation"
          body="The agent can hand off multi-step work to a sub-agent that shares the same sandbox. Long-running jobs move to the background automatically, so the main agent stays responsive."
        />
        For bigger tasks, the agent can delegate. A sub-agent picks up
        the work in its own context window, and if it takes a while, the
        job backgrounds automatically so the conversation keeps flowing.
        With multiple delegates running at once, this becomes a kind of
        agent swarm — but most of the time, it's just a convenient way to
        offload a messy task.
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
        <Conversation
          userMessage="I'm back — where did we leave off?"
          toolName="onboard"
          toolArgs=""
          toolResultPreview={`✓ Sandbox ready (1.8s)
Loaded project context from AGENTS.md
12 files in workspace, git history intact`}
          toolIcon="book"
          agentReply="Welcome back. Your workspace is intact — last commit was the auth middleware, three days ago."
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
