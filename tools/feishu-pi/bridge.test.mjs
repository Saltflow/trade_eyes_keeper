import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import test from "node:test";
import { FrameDecoder, encodeFrames, MAX_FRAME_BYTES } from "./framing.mjs";

const tool = {
  type: "function",
  function: {
    name: "search_repository", description: "Search approved source files",
    parameters: { type: "object", properties: { query: { type: "string" } },
      required: ["query"], additionalProperties: false },
  },
};
const start = {
  type: "start", messages: [{ role: "system", content: "Use evidence." },
    { role: "user", content: "Where is the scheduler?" }],
  tools: [tool], max_tool_rounds: 4, max_tool_calls: 8, max_context_chars: 20000,
};

test("Preflight loads the real SDK and exits without model or tool requests", () => {
  const bridgePath = new URL("bridge.mjs", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  const result = spawnSync(process.execPath, [bridgePath, "--check"], { encoding: "utf8", timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(result.stdout);
  assert.equal(status.type, "ready");
  assert.equal(status.backend, "pi");
  assert.equal(status.version, "0.87.1");
  assert.equal(status.direct_model_access, false);
  assert.deepEqual(status.builtin_tools, []);
});

async function session(input, handle, closeEarly = false) {
  const child = spawn(process.execPath, [new URL("bridge.mjs", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")],
    { stdio: ["pipe", "pipe", "pipe"], env: { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot } });
  const timer = setTimeout(() => child.kill(), 10000);
  const exited = once(child, "exit");
  let errors = "";
  // Oversized-input rejection may close stdin before its queued write drains.
  child.stdin.on("error", (error) => { if (error.code !== "EPIPE") throw error; });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (data) => { errors += data; });
  const values = [];
  const lines = createInterface({ input: child.stdout });
  child.stdin.write(`${JSON.stringify(input)}\n`);
  try {
    for await (const line of lines) {
      const value = JSON.parse(line);
      values.push(value);
      if (value.type === "request") {
        if (closeEarly) child.stdin.end();
        else child.stdin.write(`${JSON.stringify({ reply_to: value.id, ...await handle(value) })}\n`);
      }
    }
    const [code] = await exited;
    assert.equal(code, 0, errors);
    return values;
  } finally {
    clearTimeout(timer);
    child.kill();
    lines.close();
  }
}

function call(name = "search_repository", args = { query: "scheduler" }, id = "call_1") {
  return { role: "assistant", content: "", tool_calls: [{ id, type: "function",
    function: { name, arguments: JSON.stringify(args) } }] };
}

test("Pi executes tool round and preserves real tool evidence in final model request", async () => {
  let rounds = 0;
  const result = await session(start, (request) => {
    if (request.kind === "tool") {
      assert.equal(request.name, "search_repository");
      assert.deepEqual(request.arguments, { query: "scheduler" });
      return { result: { path: "src/core/scheduler.py", lines: "schedule.run_pending()" } };
    }
    rounds += 1;
    assert.deepEqual(request.tools, [tool]);
    if (rounds === 1) return { result: call() };
    assert.equal(request.messages.at(-1).role, "tool");
    assert.match(request.messages.at(-1).content, /src\/core\/scheduler.py/);
    return { result: { role: "assistant", content: "See src/core/scheduler.py." } };
  });
  assert.deepEqual(result.at(-1), { type: "done", answer: "See src/core/scheduler.py.", model_rounds: 2, tool_calls: 1 });
});

test("Unknown tool cannot reach Python executor", async () => {
  let rounds = 0;
  const result = await session(start, (request) => {
    assert.equal(request.kind, "model");
    if (++rounds === 1) return { result: call("bash", { command: "cat .env" }) };
    assert.match(request.messages.at(-1).content, /not found/i);
    return { result: { role: "assistant", content: "Only approved tools are available." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Invalid parameters are rejected by Pi before Python executor", async () => {
  let rounds = 0;
  const result = await session(start, (request) => {
    assert.equal(request.kind, "model");
    if (++rounds === 1) return { result: call("search_repository", { query: 3 }) };
    assert.match(request.messages.at(-1).content, /valid|query/i);
    return { result: { role: "assistant", content: "Parameter error received." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Tool failure is visible to the next model round", async () => {
  let rounds = 0;
  const result = await session(start, (request) => {
    if (request.kind === "tool") return { error: "Approved file unavailable" };
    if (++rounds === 1) return { result: call() };
    assert.match(request.messages.at(-1).content, /Approved file unavailable/);
    return { result: { role: "assistant", content: "The file is unavailable." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Last model round has no tools and further tool calls cannot execute", async () => {
  const result = await session({ ...start, max_tool_rounds: 1 }, (request) => {
    assert.equal(request.kind, "model");
    assert.deepEqual(request.tools, []);
    return { result: call() };
  });
  assert.equal(result.at(-1).type, "error");
  assert.match(result.at(-1).message, /budget/);
});

test("Tool call cap blocks the next execution", async () => {
  let rounds = 0;
  let executed = 0;
  const result = await session({ ...start, max_tool_calls: 1 }, (request) => {
    if (request.kind === "tool") { executed += 1; return { result: { found: true } }; }
    return { result: call("search_repository", { query: "scheduler" }, `call_${++rounds}`) };
  });
  assert.equal(executed, 1);
  assert.equal(result.at(-1).type, "error");
});

test("Model transport failure ends without tool execution", async () => {
  const result = await session(start, () => ({ error: "DeepSeek timeout" }));
  assert.equal(result.at(-1).type, "error");
  assert.equal(result.at(-1).message, "DeepSeek timeout");
});

test("Python owns model-context compaction rather than Node truncation", async () => {
  const result = await session({ ...start, max_context_chars: 8 }, (request) => {
    assert.deepEqual(request.messages, start.messages);
    return { result: { role: "assistant", content: "Python applies the budget." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Existing retrieval tool history survives conversion", async () => {
  const input = structuredClone(start);
  input.messages.push(call());
  input.messages.push({ role: "tool", tool_call_id: "call_1", content: "source lines" });
  const result = await session(input, (request) => {
    assert.deepEqual(request.messages, input.messages);
    return { result: { role: "assistant", content: "From source lines." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Parent EOF cancels outstanding model request and exits", async () => {
  const result = await session(start, () => assert.fail(), true);
  assert.equal(result.at(-1).type, "error");
  assert.equal(result.at(-1).message, "Parent input closed");
});

test("Protocol byte limit rejects oversized input without model execution", async () => {
  const input = structuredClone(start);
  input.messages[1].content = "x".repeat(2 * 1024 * 1024);
  const result = await session(input, () => assert.fail("No requests allowed"));
  assert.equal(result.at(-1).type, "error");
  assert.match(result.at(-1).message, /too large/);
});

test("Malformed model tool arguments terminate with explicit error", async () => {
  const response = call();
  response.tool_calls[0].function.arguments = "not-json";
  const result = await session(start, (request) => {
    assert.equal(request.kind, "model");
    return { result: response };
  });
  assert.equal(result.at(-1).type, "error");
});

test("Tool schema forbids extra parameters rather than silently stripping them", async () => {
  let rounds = 0;
  const result = await session(start, (request) => {
    assert.equal(request.kind, "model");
    if (++rounds === 1) return { result: call("search_repository", { query: "scheduler", command: "rm" }) };
    assert.match(request.messages.at(-1).content, /Invalid tool parameters/);
    return { result: { role: "assistant", content: "Rejected extra arguments." } };
  });
  assert.equal(result.at(-1).type, "done");
});

test("Local definitions and refs preserve nested values and reject wrong types", async () => {
  const referencedTool = {
    type: "function", function: { name: "referenced_tool", description: "Local schema refs",
      parameters: { type: "object", properties: { item: { $ref: "#/definitions/Item" } },
        required: ["item"], additionalProperties: false,
        definitions: { Item: { type: "object", properties: { name: { type: "string" }, value: {} },
          required: ["name", "value"], additionalProperties: false } } } },
  };
  let rounds = 0;
  let executions = 0;
  const args = { item: { name: "budget", value: { nested: [1, false, null, "三"] } } };
  const result = await session({ ...start, tools: [referencedTool] }, (request) => {
    if (request.kind === "tool") {
      executions += 1;
      assert.deepEqual(request.arguments, args);
      return { result: { accepted: true } };
    }
    if (++rounds === 1) return { result: call("referenced_tool", args) };
    if (rounds === 2) return { result: call("referenced_tool", { item: { name: 4, value: null } }, "bad_ref") };
    assert.match(request.messages.at(-1).content, /Invalid tool parameters/);
    return { result: { role: "assistant", content: "Nested schema validated." } };
  });
  assert.equal(executions, 1);
  assert.equal(result.at(-1).type, "done");
});

// Export tool_schema(SimpleNamespace(capabilities=None)) from FeishuAssistant
// to an ignored JSON file first; no assistant instance or real tools are started.
// This checks the deployed Python schema rather than a handwritten copy.
test("Every real Feishu service tool schema accepts its valid RPC fixture", {
  skip: !process.env.FEISHU_PI_SCHEMA_FILE,
}, async () => {
  const schemas = JSON.parse(await readFile(process.env.FEISHU_PI_SCHEMA_FILE, "utf8"));
  const argumentsByName = {
    list_datasets: { limit: 10, offset: 0 },
    get_dataset_details: { dataset_id: "ds_0123456789abcdef" },
    list_dataset_files: { dataset_id: "ds_0123456789abcdef", code: "000001", limit: 10 },
    query_dataset_documents: { code: "000001", limit: 10, offset: 0 },
    read_dataset_text: { file_id: "file_0123456789abcdef", start_line: 1, line_count: 20 },
    repository_map: {},
    list_repository_files: { prefix: "src/", limit: 10, offset: 0 },
    search_repository: { query: "scheduler", prefix: "src/", limit: 3 },
    read_repository_file: { path: "src/core/scheduler.py", start_line: 1, line_count: 20 },
    repository_symbols: { path: "src/core/scheduler.py", limit: 20, offset: 0 },
    list_project_docs: {},
    search_project_docs: { query: "回测", limit: 3 },
    read_project_doc: { path: "docs/configuration.md", start_line: 1, line_count: 20 },
    read_configuration: { prefix: "solver" },
    research_catalog: {},
    query_research_data: { codes: ["000001", "0700.HK"] },
    propose_configuration: { changes: [{ key: "solver_config.budget", value: { CNY: 1000, enabled: true, options: [null, "值"] } }] },
    prepare_research: { script_id: "current_strategy", codes: ["000001"], start: "2022-01-01", end: "2024-01-01",
      parameters: { confidence: 0.95, enabled: true, label: "研究" } },
    query_tasks: { action_id: "abcdef012345" },
    fetch_a_share_report: { code: "000001", report_type: "annual", year: 2025 },
    search_report_text: { document_id: "cninfo_" + "a".repeat(64), query: "现金流", limit: 5 },
    query_stock_fundamentals: { code: "000001", as_of: "2026-10-29" },
    search_web: { query: "公开公告", limit: 3 },
    calculate_gordon_ke_sensitivity: { dataset_id: "ds_fixture", codes: ["000001"], as_of: "2026-10-29", growth_rates: [0, 0.03] },
    personal_monitor_status: {},
    personal_monitor_image_evidence: { batch_id: "fixture-batch" },
    personal_monitor_account: { account: "fixture-account" },
    personal_monitor_import_image: { account: "fixture-account", batch_id: "fixture-batch", use_message_time: true },
    personal_monitor_import_manual: { account: "fixture-account", expected_revision: 0, rows: [{ instrument: "000001", quantity: 100 }] },
    personal_monitor_undo_positions: { account: "fixture-account", expected_revision: 1 },
    personal_monitor_redo_positions: { account: "fixture-account", expected_revision: 2 },
    personal_monitor_rule: { account: "fixture-account", rule_id: "fixture-rule", definition: { metric: "price", tiers: [{ id: "first", operator: "lt", value: 1 }] } },
    personal_monitor_exception: { account: "fixture-account", code: "000001", paused: true },
    personal_monitor_reset: { account: "fixture-account", rule_id: "fixture-rule", code: "000001" },
    personal_monitor_resend: { outbox_id: "fixture-outbox" },
    personal_monitor_create_reminder: { text: "fixture reminder", after_days: 1 },
    personal_monitor_edit_reminder: { reminder_id: "fixture-reminder", expected_revision: 1, text: "updated fixture" },
    personal_monitor_cancel_reminder: { reminder_id: "fixture-reminder", expected_revision: 2 },
  };
  assert.deepEqual(schemas.map((entry) => entry.function.name).sort(), Object.keys(argumentsByName).sort());
  const seen = new Set();
  let rounds = 0;
  const result = await session({ ...start, tools: schemas, max_tool_calls: schemas.length, max_tool_rounds: Math.ceil(schemas.length / 7) + 1 }, (request) => {
    if (request.kind === "tool") {
      assert.deepEqual(request.arguments, argumentsByName[request.name]);
      seen.add(request.name);
      return { result: { fixture_only: true, name: request.name } };
    }
    rounds += 1;
    if (rounds <= Math.ceil(schemas.length / 7)) {
      const batch = schemas.slice((rounds - 1) * 7, rounds * 7);
      return { result: { role: "assistant", content: "", tool_calls: batch.map((entry) =>
        call(entry.function.name, argumentsByName[entry.function.name], entry.function.name).tool_calls[0]) } };
    }
    assert.equal(seen.size, schemas.length);
    return { result: { role: "assistant", content: "All real tool schemas accepted." } };
  });
  assert.equal(result.at(-1).type, "done");
  assert.equal(seen.size, schemas.length);
});


test("Research framing carries full logical content and rejects dropped/reordered frames", () => {
  const original = { content: "原文".repeat(800000) };
  const frames = encodeFrames(original);
  assert.ok(frames.length > 1);
  assert.ok(frames.every((line) => Buffer.byteLength(line) <= MAX_FRAME_BYTES));
  const decoder = new FrameDecoder();
  let result;
  for (const line of frames) result = decoder.accept(JSON.parse(line));
  assert.deepEqual(result, original);
  assert.throws(() => new FrameDecoder().accept(JSON.parse(frames[1])), /Missing initial/);
  const broken = new FrameDecoder();
  broken.accept(JSON.parse(frames[0]));
  assert.throws(() => broken.accept(JSON.parse(frames[2])), /reordered/);
});

test("Research preflight advertises separate protocol while retaining Python-owned access", () => {
  const bridgePath = new URL("bridge.mjs", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  const result = spawnSync(process.execPath, [bridgePath, "--profile", "research", "--check"], { encoding: "utf8", timeout: 10000 });
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(result.stdout);
  assert.equal(status.protocol, "research-ndjson/1");
  assert.equal(status.direct_model_access, false);
  assert.deepEqual(status.builtin_tools, []);
});
