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
  Conversation, Inspiration, Clip, DemoClip, ArchDiagramTag, LifecycleDiagramTag,
  extractNarrations, toComposition, getTotalFrames, extractTimeline,
} from "./tags";

// ── The Script ────────────────────────────────────────────────────

const script = (
  <Video>

    {/* ── Act 1: Spin Up ──────────────────────────────────────── */}

    <Part id="spin-up" label="Spin Up">

      <Slide id="title" section>
        <Title main="Lathe" sub="An agent harness for Open WebUI" />
        Here's Lathe, an agent harness for Open WebUI. Same idea as Pi
        or OpenCode — but built for the browser.
      </Slide>

      <Slide id="the-landscape">
        <Inspiration tagline="Lathe brings this to the browser — any model, no local setup.">
          <Clip label="Pi" file="pi-demo.mp4" url="pi.dev" />
          <Clip label="OpenCode" file="opencode-demo.mp4" url="opencode.ai" />
        </Inspiration>
        Terminal coding agents are everywhere. Fast. Capable. But they
        assume you're at a workstation with a shell.
      </Slide>

      <Slide id="first-message" section>
        <DemoClip file="demo.webm" startSeconds={10} />
        Lathe brings that to Open WebUI. Any model, any browser, no
        local setup. The user opens a conversation. Just a chat window —
        until the model reaches for a tool.
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
        A sandbox spins up. Provisioned on demand. Dedicated to this
        user. The model didn't ask for it. The user didn't configure it.
        Lathe handles the lifecycle transparently.
      </Slide>

      <Slide id="arch-overview">
        <ArchDiagramTag />
        User talks to a model in Open WebUI. Model calls Lathe's tools.
        Tools execute in a Daytona sandbox. Results flow back as tool
        output.
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
        The agent clones the repo and explores. Shell commands, file
        reads, edits — all running in the sandbox.
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
        Then it calls onboard. Project instructions, conventions,
        context — loaded in one shot.
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
        The agent writes code, installs dependencies, starts a dev
        server. Packages persist across conversations. The sandbox is
        stateful.
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
        The agent calls expose. The user gets a signed URL to whatever's
        running in the sandbox. A web app. A file browser. A full VS
        Code instance.
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
        User edits code in VS Code. Agent makes structural changes
        through chat. Same filesystem. Same sandbox. Same time.
      </Slide>

      <Slide id="delegate-mention">
        <Callout
          icon="git-branch"
          heading="Delegation"
          body="The agent can hand off multi-step work to a sub-agent that shares the same sandbox. Long-running jobs move to the background automatically, so the main agent stays responsive."
        />
        For bigger tasks, the agent delegates. A sub-agent picks up the
        work in its own context. Long jobs background automatically. The
        conversation keeps flowing.
      </Slide>

    </Part>

    {/* ── Act 3: Spin Down ────────────────────────────────────── */}

    <Part id="spin-down" label="Spin Down">

      <Slide id="session-ends" section>
        <Chat>
          <User>That's good for today, thanks.</User>
          <Agent>Your sandbox will sleep automatically when idle. Files, packages, git history — all still there next time.</Agent>
        </Chat>
        The user closes the tab. The sandbox sleeps on its own. No
        teardown. No save button.
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
        The sandbox is in the cloud. Not tied to a machine — tied to
        you. Start on your laptop. Pick up on your phone. Same sandbox
        wherever you reach the server.
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
        New conversation. The sandbox wakes on the first tool call.
        Files, packages, git history — all intact. Onboard picks up the
        project context immediately.
      </Slide>

      <Slide id="the-cycle" section>
        <LifecycleDiagramTag />
        Spin up, get to work, spin down. Infrastructure that appears
        when you need it. One toolkit. One API key. Every model on the
        server gets a coding agent surface.
      </Slide>

      <Slide id="outro">
        <Title main="lathe.tools" sub="github.com/rndmcnlly/lathe" />
        That's Lathe. An agent harness for Open WebUI.
      </Slide>

    </Part>

  </Video>
);

// ── Export the tree and walkers ────────────────────────────────────

export { script, extractNarrations, toComposition, getTotalFrames, extractTimeline };
