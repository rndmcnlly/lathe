import assert from "node:assert/strict";
import test from "node:test";
import { runScenario, validateScenario } from "./interpreter.mjs";

test("rejects unsupported instructions and stale editor routing before execution", () => {
  assert.throws(() => validateScenario({ id: "x", version: 1, steps: [{ id: "a", do: "editor.open" }] }, new Set()), /Unknown instruction/);
  assert.throws(() => validateScenario({ id: "x", version: 1, steps: [{ id: "a", do: "open", fallback: ["retry"] }] }, new Set(["open"])), /Unknown step field/);
});

test("a failed contract stops the take, preserves its domain, and never advances", async () => {
  let advanced = false;
  await assert.rejects(runScenario({ id: "x", version: 1, steps: [
    { id: "native", do: "proof", failureDomain: "native_subagent_behavior" },
    { id: "publish", do: "next" },
  ] }, {
    proof: async () => { throw new Error("no completion"); },
    next: async () => { advanced = true; },
  }), (error) => error.report.status === "rejected"
    && error.report.failed_step === "native" && error.report.failure_domain === "native_subagent_behavior");
  assert.equal(advanced, false);
});

test("streams linear progress and screenshot markers", async () => {
  const events = [];
  const report = await runScenario({ id: "x", version: 1, steps: [{ id: "send", do: "send", screenshot: "sent" }] },
    { send: async () => ({ observations: ["actual result"] }) },
    { onEvent: async (event) => events.push(event) });
  assert.deepEqual(events.map((event) => event.phase), ["scenario-start", "step-start", "step-passed", "scenario-accepted"]);
  assert.equal(events[2].screenshot, "sent");
  assert.equal(report.status, "accepted");
  assert.deepEqual(report.steps[0].observations, ["actual result"]);
});
