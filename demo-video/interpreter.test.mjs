import assert from "node:assert/strict";
import test from "node:test";

import { resolveTemplates, runScenario, validateScenario } from "./interpreter.mjs";

test("validates instructions before execution", () => {
  assert.throws(
    () => validateScenario({ id: "x", version: 1, steps: [{ id: "a", do: "browser.css" }] }, new Set(["chat.send"])),
    /Unknown instruction browser\.css/,
  );
});

test("rejects unknown top-level fields and missing versions", () => {
  const operations = new Set(["chat.send"]);
  assert.throws(
    () => validateScenario({ id: "x", version: 1, steps: [{ id: "a", do: "chat.send" }], css: "#x" }, operations),
    /Unknown scenario field css/,
  );
  assert.throws(
    () => validateScenario({ id: "x", steps: [{ id: "a", do: "chat.send" }] }, operations),
    /positive integer version/,
  );
});

test("resolves serializable scenario variables", () => {
  assert.deepEqual(
    resolveTemplates({ path: "{{root}}/RELAY.md", values: ["{{model}}"] }, { root: "/tmp/demo", model: "m1" }),
    { path: "/tmp/demo/RELAY.md", values: ["m1"] },
  );
});

test("takes a bounded fallback and journals both attempts", async () => {
  const scenario = {
    id: "fallback",
    version: 1,
    steps: [{ id: "open", do: "editor.explorer", fallback: ["editor.quick-open"] }],
  };
  const report = await runScenario(scenario, {
    "editor.explorer": async () => { throw new Error("folder hidden"); },
    "editor.quick-open": async () => ({ observations: ["editor.open-path=/tmp/RELAY.md"] }),
  });
  assert.equal(report.status, "accepted");
  assert.deepEqual(report.steps.map((step) => step.result), ["failed", "passed"]);
  assert.equal(report.steps[1].fallback, true);
});

test("rejects a required failed contract with its domain", async () => {
  const scenario = {
    id: "required",
    version: 1,
    steps: [{ id: "tool", do: "chat.await-tool", failureDomain: "model_behavior" }],
  };
  await assert.rejects(
    () => runScenario(scenario, { "chat.await-tool": async () => { throw new Error("missing"); } }),
    (error) => error.report.status === "rejected"
      && error.report.failure_domain === "model_behavior"
      && error.report.failed_step === "tool",
  );
});

test("optional failures do not reject a take", async () => {
  const report = await runScenario(
    { id: "optional", version: 1, steps: [{ id: "hold", do: "camera.hold", required: false }] },
    { "camera.hold": async () => { throw new Error("animation unavailable"); } },
  );
  assert.equal(report.status, "accepted");
  assert.equal(report.steps[0].result, "failed");
});

test("streams step events for live progress reporting", async () => {
  const events = [];
  await runScenario(
    { id: "events", version: 1, steps: [{ id: "send", do: "chat.send" }] },
    { "chat.send": async () => ({ observations: ["sent"] }) },
    { onEvent: async (event) => events.push(event.phase) },
  );
  assert.deepEqual(events, ["scenario-start", "step-start", "step-passed", "scenario-accepted"]);
});
