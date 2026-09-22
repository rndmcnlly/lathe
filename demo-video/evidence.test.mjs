import assert from "node:assert/strict";
import test from "node:test";

import { normalizedToolEvents, matchesToolExpectation } from "./evidence.mjs";

test("read-back rejects trailing text left by a wrapped-row edit", () => {
  const expected = { tools: ["read", "bash"], argumentContains: "RELAY.md",
    outputLineEquals: "You: A shared state of mind." };
  const event = { tool: "read", arguments: { path: "/tmp/RELAY.md" } };
  for (const output of ["2: You: A shared state of mind.\n", "You: A shared state of mind."]) {
    assert.equal(matchesToolExpectation({ ...event, output }, expected), true);
  }
  for (const output of ["2: You: A shared state of mind.model do, where the work runs",
    "The file says You: A shared state of mind.", "You: A shared state of mind. extra"]) {
    assert.equal(matchesToolExpectation({ ...event, output }, expected), false);
  }
});

test("normalizes OpenAI tool calls and paired results", () => {
  const payload = {
    chat: { history: { messages: {
      a: { role: "assistant", tool_calls: [{
        id: "call-1",
        function: { name: "read", arguments: "{\"path\":\"/tmp/RELAY.md\"}" },
      }] },
      b: { role: "tool", tool_call_id: "call-1", content: "You: A shared state of mind." },
    } } },
  };
  assert.deepEqual(normalizedToolEvents(payload), [{
    id: "call-1",
    tool: "read",
    arguments: { path: "/tmp/RELAY.md" },
    output: "You: A shared state of mind.",
  }]);
});

test("normalizes embedded Open WebUI tool observations", () => {
  const payload = { tool_name: "delegate", args: { foreground_seconds: 0 }, result: "Backgrounded" };
  assert.deepEqual(normalizedToolEvents(payload), [{
    id: null,
    tool: "delegate",
    arguments: { foreground_seconds: 0 },
    output: "Backgrounded",
  }]);
});

test("normalizes Responses API function calls and outputs", () => {
  const payload = { output: [
    {
      type: "function_call",
      id: "tool-record",
      call_id: "call-2",
      name: "bash",
      arguments: "{\"command\":\"cat RELAY.md\"}",
      status: "completed",
    },
    {
      type: "function_call_output",
      call_id: "call-2",
      output: [{ type: "input_text", text: "You: A shared state of mind." }],
      status: "completed",
    },
  ] };
  assert.deepEqual(normalizedToolEvents(payload), [{
    id: "call-2",
    tool: "bash",
    arguments: { command: "cat RELAY.md" },
    output: "You: A shared state of mind.",
  }]);
});
