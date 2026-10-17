const STEP_KEYS = new Set(["id", "do", "intent", "with", "failureDomain", "screenshot"]);

export function validateScenario(scenario, operations) {
  if (!scenario || typeof scenario !== "object" || Array.isArray(scenario)) throw new Error("Scenario must be an object");
  for (const key of Object.keys(scenario)) {
    if (!["id", "version", "steps"].includes(key)) throw new Error(`Unknown scenario field ${key}`);
  }
  if (typeof scenario.id !== "string" || !scenario.id
      || !Number.isInteger(scenario.version) || scenario.version < 1
      || !Array.isArray(scenario.steps) || !scenario.steps.length) {
    throw new Error("Scenario requires an id, positive integer version, and non-empty steps");
  }
  const ids = new Set();
  for (const step of scenario.steps) {
    for (const key of Object.keys(step)) {
      if (!STEP_KEYS.has(key)) throw new Error(`Unknown step field ${key}`);
    }
    if (typeof step.id !== "string" || !step.id || ids.has(step.id)) throw new Error("Step ids must be unique non-empty strings");
    ids.add(step.id);
    if (!operations.has(step.do)) throw new Error(`Unknown instruction ${step.do}`);
    if (step.with !== undefined && (!step.with || typeof step.with !== "object" || Array.isArray(step.with))) {
      throw new Error(`Step ${step.id} with must be an object`);
    }
  }
  return scenario;
}

// One linear take. No editor routes, template bindings, optional contracts, or retries.
export async function runScenario(scenario, adapter, context = {}) {
  validateScenario(scenario, new Set(Object.keys(adapter)));
  const report = { schema_version: 1, scenario: scenario.id, scenario_version: scenario.version,
    started_at: new Date().toISOString(), status: "running", failure_domain: null, steps: [] };
  const emit = async (event) => context.onEvent?.(event, report);
  await emit({ phase: "scenario-start" });
  for (const step of scenario.steps) {
    const started = Date.now();
    await emit({ phase: "step-start", step: step.id, operation: step.do });
    try {
      const result = await adapter[step.do](step.with || {});
      report.steps.push({ step: step.id, intent: step.intent || null, instruction: step.do,
        result: "passed", observations: result?.observations || [], elapsed_ms: Date.now() - started });
      await emit({ phase: "step-passed", step: step.id, operation: step.do, screenshot: step.screenshot });
    } catch (cause) {
      report.steps.push({ step: step.id, instruction: step.do, result: "failed", error: cause.message,
        elapsed_ms: Date.now() - started });
      Object.assign(report, { status: "rejected", failure_domain: step.failureDomain || "infrastructure",
        failed_step: step.id, completed_at: new Date().toISOString() });
      await emit({ phase: "step-failed", step: step.id, operation: step.do, error: cause.message });
      const error = new Error(`Required step ${step.id} failed: ${cause.message}`, { cause });
      error.report = report;
      throw error;
    }
  }
  report.status = "accepted";
  report.completed_at = new Date().toISOString();
  await emit({ phase: "scenario-accepted" });
  return report;
}
