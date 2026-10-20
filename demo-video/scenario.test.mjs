import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { matchesToolExpectation } from "./evidence.mjs";
import { runScenario } from "./interpreter.mjs";

const scenario = JSON.parse(readFileSync(new URL("./scenario.json", import.meta.url)));
const exposure = scenario.steps.find((step) => step.id === "preview.prove").with;

test("private preview accepts the model's chosen port but never public access", () => {
  const event = { tool: "expose", arguments: { target: "http:8765", access: "private" },
    output: "Owner-only wrapped preview: Copying the URL does not grant access." };
  assert.equal(matchesToolExpectation(event, exposure), true);
  assert.equal(matchesToolExpectation({ ...event, arguments: { target: "http:8765", access: "public" } }, exposure), false);
});

test("missing artifact evidence stops the storyboard before the private preview", async () => {
  let previewed = false;
  const adapter = Object.fromEntries(scenario.steps.map((step) => [step.do, async () => ({})]));
  adapter["chat.find-artifact"] = async () => { throw new Error("no saved infographic"); };
  adapter["preview.open"] = async () => { previewed = true; };
  await assert.rejects(runScenario(scenario, adapter), (error) => error.report.failed_step === "infographic.prove");
  assert.equal(previewed, false);
});

test("analytics must demonstrate interpretation before commissioning the infographic", async () => {
  let infographic = false;
  const adapter = Object.fromEntries(scenario.steps.map((step) => [step.do, async () => ({})]));
  adapter["chat.send"] = async ({ dataFile }) => { if (!dataFile) infographic = true; };
  adapter["chat.await-tool-result"] = async (expected) => {
    if (!matchesToolExpectation({ tool: "bash", output: "7 repairs" }, expected)) {
      throw new Error("no interpreter result");
    }
  };
  await assert.rejects(runScenario(scenario, adapter), (error) => error.report.failed_step === "analytics.prove");
  assert.equal(infographic, false);
});
