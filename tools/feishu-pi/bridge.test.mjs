import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline";
import test from "node:test";
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
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

async function session(input, handle, closeEarly = false, timeout = 10000) {
  const child = spawn(process.execPath, [new URL("bridge.mjs", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")],
    { stdio: ["pipe", "pipe", "pipe"], env: { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot } });
  const timer = setTimeout(() => child.kill(), timeout);
  const exited = once(child, "exit");
  let errors = "";
  // Oversized-input rejection may close stdin before its queued write drains.
  child.stdin.on("error", (error) => { if (!["EPIPE", "EOF"].includes(error.code)) throw error; });
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

test("Native Pi marks thrown tools as errors and business error fields as data", async () => {
  for (const throws of [true, false]) {
    let rounds = 0;
    let executions = 0;
    const receipts = [];
    const model = { id: "offline", provider: "python-bridge", api: "openai-completions",
      name: "Offline fixture", reasoning: false, input: ["text"], contextWindow: 10000,
      maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    const agent = new Agent({
      initialState: { model, messages: [{ role: "user", content: "Read source", timestamp: 0 }],
        tools: [{ name: tool.function.name, label: "Read source", description: "Read source",
          parameters: tool.function.parameters,
          async execute() {
            executions += 1;
            if (throws) throw new Error("source unavailable");
            return { content: [{ type: "text", text: JSON.stringify({ error: "business field", rows: [1] }) }], details: {} };
          } }] },
      streamFn(_model, context) {
        rounds += 1;
        if (rounds === 2) {
          const receipt = context.messages.find((message) => message.role === "toolResult");
          assert.equal(receipt.isError, throws);
          assert.match(receipt.content[0].text, throws ? /source unavailable/ : /business field/);
        }
        assert.ok(rounds <= 2, "No model retry or repeated tool loop");
        const response = { role: "assistant", api: model.api, provider: model.provider,
          model: model.id, timestamp: 0,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: rounds === 1 ? "toolUse" : "stop",
          content: rounds === 1
            ? [{ type: "toolCall", id: "native-error", name: tool.function.name, arguments: { query: "source" } }]
            : [{ type: "text", text: "Result received" }] };
        const stream = createAssistantMessageEventStream();
        stream.push({ type: "done", reason: response.stopReason, message: response });
        stream.end(response);
        return stream;
      },
    });
    agent.subscribe((event) => {
      if (event.type === "message_end" && event.message.role === "toolResult") receipts.push(event.message);
    });
    await agent.continue();
    assert.equal(executions, 1);
    assert.equal(rounds, 2);
    assert.equal(receipts.length, 1);
    assert.equal(receipts[0].isError, throws);
  }
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
  input.messages[1].content = "x".repeat(48 * 1024 * 1024);
  const result = await session(input, () => assert.fail("No requests allowed"), false, 60000);
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
