const STEP_KEYS = new Set([
  "id", "do", "required", "intent", "with", "fallback", "failureDomain", "screenshot",
]);
const SCENARIO_KEYS = new Set(["id", "version", "steps"]);

export function validateScenario(scenario, operations) {
  if (!scenario || typeof scenario !== "object" || Array.isArray(scenario)) {
    throw new Error("Scenario must be an object");
  }
  for (const key of Object.keys(scenario)) {
    if (!SCENARIO_KEYS.has(key)) throw new Error(`Unknown scenario field ${key}`);
  }
  if (typeof scenario.id !== "string" || !scenario.id
      || !Number.isInteger(scenario.version) || scenario.version < 1
      || !Array.isArray(scenario.steps) || scenario.steps.length === 0) {
    throw new Error("Scenario requires a non-empty id, positive integer version, and non-empty steps array");
  }

  const ids = new Set();
  for (const step of scenario.steps) {
    if (!step || typeof step !== "object" || Array.isArray(step)) {
      throw new Error("Every scenario step must be an object");
    }
    for (const key of Object.keys(step)) {
      if (!STEP_KEYS.has(key)) throw new Error(`Unknown field ${key} in step ${step.id || "<unknown>"}`);
    }
    if (typeof step.id !== "string" || !step.id || ids.has(step.id)) {
      throw new Error(`Step ids must be non-empty and unique: ${step.id || "<missing>"}`);
    }
    ids.add(step.id);
    if (typeof step.do !== "string" || !operations.has(step.do)) {
      throw new Error(`Unknown instruction ${step.do || "<missing>"} in step ${step.id}`);
    }
    if (step.required !== undefined && typeof step.required !== "boolean") {
      throw new Error(`Step ${step.id} required must be boolean`);
    }
    if (step.screenshot !== undefined && (typeof step.screenshot !== "string" || !step.screenshot)) {
      throw new Error(`Step ${step.id} screenshot must be a non-empty string`);
    }
    if (step.with !== undefined && (!step.with || typeof step.with !== "object" || Array.isArray(step.with))) {
      throw new Error(`Step ${step.id} with must be an object`);
    }
    if (step.fallback !== undefined && !Array.isArray(step.fallback)) {
      throw new Error(`Step ${step.id} fallback must be an array`);
    }
    for (const fallback of step.fallback || []) {
      if (typeof fallback !== "string" || !operations.has(fallback)) {
        throw new Error(`Unknown fallback ${fallback} in step ${step.id}`);
      }
    }
  }
  return scenario;
}

export function resolveTemplates(value, variables) {
  if (typeof value === "string") {
    return value.replace(/\{\{([A-Za-z][A-Za-z0-9_]*)\}\}/g, (_, name) => {
      if (!(name in variables)) throw new Error(`Unknown scenario variable ${name}`);
      return String(variables[name]);
    });
  }
  if (Array.isArray(value)) return value.map((item) => resolveTemplates(item, variables));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, resolveTemplates(item, variables)]),
    );
  }
  return value;
}

function resultRecord(step, operation, started, result, error) {
  return {
    step: step.id,
    intent: step.intent || null,
    required: step.required !== false,
    instruction: operation,
    result: error ? "failed" : result?.result || "passed",
    observations: result?.observations || [],
    failure_domain: error ? step.failureDomain || "infrastructure" : null,
    error: error ? error.message : null,
    elapsed_ms: Date.now() - started,
  };
}

export async function runScenario(scenario, adapter, context = {}) {
  const operations = new Set(Object.keys(adapter));
  validateScenario(scenario, operations);
  const report = {
    schema_version: 1,
    scenario: scenario.id,
    scenario_version: scenario.version,
    started_at: new Date().toISOString(),
    status: "running",
    failure_domain: null,
    steps: [],
  };
  if (context.onEvent) await context.onEvent({ phase: "scenario-start" }, report);

  for (const step of scenario.steps) {
    const params = resolveTemplates(step.with || {}, context.variables || {});
    const routes = [step.do, ...(step.fallback || [])];
    let passed = false;
    let lastError;

    for (let attempt = 0; attempt < routes.length; attempt++) {
      const operation = routes[attempt];
      const started = Date.now();
      if (context.onEvent) {
        await context.onEvent({
          phase: "step-start", step: step.id, operation, attempt: attempt + 1,
        }, report);
      }
      try {
        const result = await adapter[operation](params, context, step);
        report.steps.push({
          ...resultRecord(step, operation, started, result),
          attempt: attempt + 1,
          fallback: attempt > 0,
        });
        if (context.onEvent) {
          await context.onEvent({
            phase: "step-passed", step: step.id, operation, attempt: attempt + 1,
            screenshot: step.screenshot,
          }, report);
        }
        passed = true;
        break;
      } catch (error) {
        lastError = error instanceof Error ? error : new Error(String(error));
        report.steps.push({
          ...resultRecord(step, operation, started, null, lastError),
          attempt: attempt + 1,
          fallback: attempt > 0,
        });
        if (context.onEvent) {
          await context.onEvent({
            phase: "step-failed", step: step.id, operation, attempt: attempt + 1,
            error: lastError.message, screenshot: step.screenshot,
          }, report);
        }
      }
    }

    if (!passed && step.required !== false) {
      report.status = "rejected";
      report.failure_domain = step.failureDomain || "infrastructure";
      report.failed_step = step.id;
      report.completed_at = new Date().toISOString();
      if (context.onEvent) {
        await context.onEvent({ phase: "scenario-rejected", step: step.id }, report);
      }
      const error = new Error(`Required step ${step.id} failed: ${lastError?.message || "unknown error"}`);
      error.report = report;
      throw error;
    }
  }

  report.status = "accepted";
  report.completed_at = new Date().toISOString();
  if (context.onEvent) await context.onEvent({ phase: "scenario-accepted" }, report);
  return report;
}
