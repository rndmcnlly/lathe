import assert from "node:assert/strict";
import test from "node:test";
import { normalizedToolEvents, matchesToolExpectation, privatePreviewUrl, assertSuccessfulTurn } from "./evidence.mjs";

test("persisted turn errors reject otherwise useful output, including exhausted tool iterations", () => {
  const payload = (message) => ({ chat: { history: { currentId: "current", messages: {
    old: { role: "assistant", done: true, error: { content: "old error" } }, current: message,
  } } } });
  const completed = { role: "assistant", done: true, content: "Your infographic is ready." };
  assert.doesNotThrow(() => assertSuccessfulTurn(payload(completed)));
  for (const content of ["Tool-call limit reached (12 iterations).", "Provider unavailable"]) {
    assert.throws(() => assertSuccessfulTurn(payload({ ...completed, error: { content } })), /turn error/);
  }
  for (const message of [null, { ...completed, done: false }, { ...completed, role: "user" }]) {
    assert.throws(() => assertSuccessfulTurn(payload(message)), /missing/);
  }
});

test("analytics evidence requires an interpreter result, not just a call or prose", () => {
  const expected = { tools: ["interpret"] };
  assert.equal(matchesToolExpectation({ tool: "interpret", output: "12 items, 7 repaired" }, expected), true);
  assert.equal(matchesToolExpectation({ tool: "interpret", output: "" }, expected), false);
  assert.equal(matchesToolExpectation({ tool: "bash", output: "12 items, 7 repaired" }, expected), false);
});

test("pairs persisted tool calls with their actual results and deduplicates history copies", () => {
  const output = [
    { type: "function_call", call_id: "one", name: "write", arguments: '{"path":"/workspace/impact.html"}' },
    { type: "function_call_output", call_id: "one", output: [{ type: "input_text", text: "Wrote 5000 bytes" }] },
  ];
  const events = normalizedToolEvents({ history: { output }, messages: [{ output }] });
  assert.deepEqual(events, [{ id: "one", tool: "write", arguments: { path: "/workspace/impact.html" }, output: "Wrote 5000 bytes" }]);
});

test("normalizes OpenAI call/result pairs", () => {
  const events = normalizedToolEvents([
    { tool_calls: [{ id: "one", function: { name: "read", arguments: '{"path":"/workspace/data.csv"}' } }] },
    { role: "tool", tool_call_id: "one", content: "12 rows" },
  ]);
  assert.equal(events.length, 1);
  assert.equal(events[0].output, "12 rows");
});

test("tool evidence does not accept an assistant's claim of private exposure", () => {
  const expected = { tools: ["expose"], argumentsEqual: { access: "private" }, outputContains: "Copying the URL does not grant access." };
  assert.equal(matchesToolExpectation({ tool: "expose", arguments: { access: "private" }, output: "I'll make it private." }, expected), false);
  assert.equal(matchesToolExpectation({ tool: "expose", arguments: { access: "public" }, output: "Copying the URL does not grant access." }, expected), false);
});

test("preview follows the literal completed tool URL, not an assistant-rewritten hostname", () => {
  const event = { tool: "expose", arguments: { access: "private" }, output:
    "Service URL: https://lathe-private-actual.lathe-preview.test/\nOwner-authenticated private preview" };
  assert.equal(privatePreviewUrl(event, ["lathe-preview.test"]), "https://lathe-private-actual.lathe-preview.test/");
  for (const output of ["Here is your link: https://invented.lathe-preview.test/",
    "Service URL: https://lathe-preview.test.attacker.invalid/", "Service URL: https://secret@lathe-preview.test/"]) {
    assert.throws(() => privatePreviewUrl({ ...event, output }, ["lathe-preview.test"]));
  }
});
