/**
 * The Lathe explainer video script: the single source of truth.
 *
 * The JSX tree defines each slide's visual and narration. The walkers exported
 * by tags.tsx extract narration, build the Remotion composition, and calculate
 * the timeline used by TTS, captions, and the progress bar.
 *
 * Narrative: where work runs, how people and agents share it, what persists.
 */

import React from "react";
import {
  Video, Part, Slide,
  Title, Chat, User, Agent, Scene, Callout,
  Conversation, DemoClip, ArchDiagramTag, LifecycleDiagramTag,
  extractNarrations, toComposition, getTotalFrames, extractTimeline,
} from "./tags";

const script = (
  <Video>

    {/* Act 1: Where the work happens */}
    <Part id="spin-up" label="Where It Runs">

      <Slide id="title" section>
        <Title main="From chat to workspace" sub="Where does the work run, and what survives?" />
        A chat can describe code. Lathe gives it somewhere to inspect, run,
        and change code. But where is that workspace, and what survives when
        the conversation ends?
      </Slide>

      <Slide id="first-tool-proof">
        <DemoClip file="demo.webm" startSeconds={10} />
        Here is the transition. A model available through Open WebUI calls a
        tool. Lathe finds or creates the user's sandbox, runs the command
        there, and returns the result to the conversation.
      </Slide>

      <Slide id="architecture" section>
        <ArchDiagramTag />
        The browser talks to Open WebUI. An administrator has installed Lathe
        there and configured the Daytona credential. Commands and project
        files stay in a Daytona-hosted sandbox associated with this Open WebUI
        account by an email label.
      </Slide>

    </Part>

    {/* Act 2: One project, several interfaces */}
    <Part id="work" label="One Workspace">

      <Slide id="project-work" section>
        <DemoClip file="demo.webm" startSeconds={22} />
        Follow one project. The agent clones Lathe and inspects the repository.
        From there it can read instructions, edit files by absolute path,
        install dependencies, and run tests against the same sandbox filesystem.
      </Slide>

      <Slide id="shared-filesystem">
        <Scene
          leftLabel="You: browser IDE"
          leftLines={[
            "/home/daytona/workspace/lathe",
            "Edit docs/getting-started.md",
            "Save the file",
          ]}
          rightLabel="Agent: Open WebUI"
          rightLines={[
            "read(absolute path)",
            "edit(the same file)",
            "bash(run the tests)",
          ]}
        />
        You can open VS Code in the browser while the agent reads and tests
        the same absolute paths. A delegate can work there too. These are not
        copies being synchronized. They are interfaces onto one filesystem.
      </Slide>

      <Slide id="expose-proof">
        <DemoClip file="demo.webm" startSeconds={53} />
        Lathe can start code-server and request a signed preview URL. That URL
        is a bearer credential: possession grants terminal and workspace
        access until it expires, so the friendly link is private and should
        not be shared.
      </Slide>

      <Slide id="handoff">
        <Scene
          leftLabel="Fresh conversation"
          leftLines={[
            "Goal and decisions",
            "Work completed",
            "Absolute paths to continue",
          ]}
          rightLabel="Existing sandbox"
          rightLines={[
            "/home/daytona/workspace/lathe",
            "Files and Git history",
            "Installed packages",
          ]}
        />
        If the conversation grows long, handoff writes a compact document for
        a fresh chat. The document transfers conversational context. It does
        not move the project: the next agent returns to the existing workspace.
      </Slide>

    </Part>

    {/* Act 3: What persists */}
    <Part id="spin-down" label="What Persists">

      <Slide id="conversation-ends" section>
        <Chat>
          <User>That's enough for today.</User>
          <Agent>The sandbox can stop when idle. Your files remain, but running servers and interpreter state do not.</Agent>
        </Chat>
        Closing the tab does not save or delete anything. After the configured
        idle interval, Daytona may stop and later archive the sandbox. Its
        filesystem remains, while its processes end.
      </Slide>

      <Slide id="persistence-model">
        <LifecycleDiagramTag />
        A later tool call restarts the sandbox. Files, installed packages, and
        Git history return. Servers and interpreters must restart. Configured
        deletion or explicit destruction removes the sandbox; an optional
        persistent volume has its own retention boundary.
      </Slide>

      <Slide id="return">
        <Conversation
          userMessage="Continue from the handoff and check the last commit."
          toolName="onboard"
          toolArgs={'path="/home/daytona/workspace/lathe"'}
          toolResultPreview={`[Sandbox was restarted — running processes were lost]
# Directory: /home/daytona/workspace/lathe
…`}
          toolIcon="book"
          agentReply="The workspace and Git history are intact. I'll restart anything the project needs before continuing."
        />
        That closes the loop. A fresh conversation can recover the context,
        and its first tool call can wake the same account-associated workspace.
        The durable unit is the filesystem, not the running session.
      </Slide>

      <Slide id="answer" section>
        <Title main="Chat coordinates the work" sub="The sandbox holds it" />
        Lathe turns Open WebUI's tool surface into a real coding workspace,
        without user-side local setup. The chat can change; the device can
        change; the workspace remains until its configured retention boundary.
      </Slide>

      <Slide id="outro">
        <Title main="lathe.tools" sub="github.com/rndmcnlly/lathe" />
        Lathe is an agent harness for Open WebUI.
      </Slide>

    </Part>
  </Video>
);

export { script, extractNarrations, toComposition, getTotalFrames, extractTimeline };
