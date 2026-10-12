import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { matchesToolExpectation } from "./evidence.mjs";
import { runScenario } from "./interpreter.mjs";

const scenario = JSON.parse(readFileSync(new URL("./scenario.json", import.meta.url)));
const exposure = scenario.steps.find((step) => step.id === "editor.prove-exposed").with;

test("shipped scenario rejects public IDE and generic port substitutes", () => {
  const event = { tool: "expose", arguments: { target: "code-server:/home/daytona/workspace", access: "private" },
    output: "Owner-only wrapped preview: Copying the URL does not grant access." };
  assert.equal(matchesToolExpectation(event, exposure), true);
  for (const arguments_ of [{ target: "code-server", access: "public" },
    { target: "http:8080", access: "private" },
    { target: "http:8080", access: "private", tag: "code-server" },
    { target: "code-server:relative", access: "private" },
    { target: "not-code-server", access: "private" }, { target: "code-server", access: "private" }]) {
    assert.equal(matchesToolExpectation({ ...event, arguments: arguments_ }, exposure), false);
  }
  assert.equal(matchesToolExpectation({ ...event, output: "Public wrapped preview: https://example.invalid" }, exposure), false);
});

test("shipped scenario executes with independent tool and file-read evidence", async () => {
  const events = [
    { tool: "lathe", arguments: { manpage: "version" }, output: "Lathe toolkit version: fixture" },
    { tool: "bash", arguments: { command: "git clone https://github.com/rndmcnlly/lathe" }, output: "Cloned" },
    { tool: "write", arguments: { path: "/workspace/RELAY.md" }, output: "Saved" },
    { tool: "expose", arguments: { target: "code-server:/workspace", access: "private" }, output: "Copying the URL does not grant access." },
    { tool: "read", arguments: { path: "/workspace/RELAY.md" }, output: "2: You: A shared state of mind." },
    { tool: "delegate", arguments: {}, output: "Backgrounded" },
    { tool: "bash", arguments: { command: "ss -ltn | grep 8080" }, output: "Stopped" },
  ];
  const adapter = Object.fromEntries(scenario.steps.flatMap((step) => [step.do, ...(step.fallback || [])])
    .map((operation) => [operation, async () => ({})]));
  adapter["chat.await-tool-result"] = async (expected) => {
    assert.ok(events.some((event) => matchesToolExpectation(event, expected)));
  };
  assert.equal((await runScenario(scenario, adapter, { variables: { checkout: "/workspace" } })).status, "accepted");
  events.find((event) => event.tool === "read").output = "Assistant says the edit worked";
  await assert.rejects(runScenario(scenario, adapter, { variables: { checkout: "/workspace" } }),
    (error) => error.report.failed_step === "relay.prove-read");
});
