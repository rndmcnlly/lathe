export function matchesToolExpectation(event, expected) {
  const contains = (value, text) => !text || JSON.stringify(value).toLowerCase().includes(text.toLowerCase());
  return expected.tools.includes(event.tool)
    && contains(event.arguments, expected.argumentContains)
    && contains(event.output, expected.outputContains)
    && (!expected.outputLineEquals || event.output.split(/\r?\n/).some((line) =>
      line.replace(/^\d+: /, "") === expected.outputLineEquals));
}

export function normalizedToolEvents(payload) {
  const calls = [];
  const results = new Map();
  const seen = new Set();

  function asText(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(asText).join("\n");
    if (value && typeof value === "object") {
      return asText(value.text ?? value.output ?? value.content ?? value.result ?? "");
    }
    return value == null ? "" : String(value);
  }

  function visit(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) visit(item);
      return;
    }
    for (const call of node.tool_calls || []) {
      const fn = call?.function || call;
      const name = fn?.name || call?.tool_name;
      if (!name) continue;
      let args = fn.arguments ?? call.arguments ?? {};
      if (typeof args === "string") {
        try { args = JSON.parse(args); } catch {}
      }
      calls.push({
        id: call.id || call.tool_call_id || null,
        tool: name,
        arguments: args,
        output: asText(call.output ?? call.result ?? call.content ?? ""),
      });
    }
    if (node.type === "function_call" && node.name) {
      let args = node.arguments ?? {};
      if (typeof args === "string") {
        try { args = JSON.parse(args); } catch {}
      }
      calls.push({
        id: node.call_id || node.id || null,
        tool: node.name,
        arguments: args,
        output: "",
      });
    }
    const resultId = node.tool_call_id || node.call_id;
    if (resultId && node.type !== "function_call") {
      results.set(resultId, asText(node.content ?? node.output ?? node.result ?? ""));
    }
    if (node.tool_name && !node.tool_calls) {
      calls.push({
        id: resultId || null,
        tool: node.tool_name,
        arguments: node.arguments || node.args || {},
        output: asText(node.output ?? node.result ?? node.content ?? ""),
      });
    }
    for (const value of Object.values(node)) visit(value);
  }

  visit(payload);
  return calls.filter((event) => {
    if (event.id && results.has(event.id)) event.output = results.get(event.id);
    const key = JSON.stringify([event.id, event.tool, event.arguments, event.output]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
