/** Pi owns the agent loop; Python owns every model request and tool execution. */
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { Value } from "typebox/value";
import { FrameDecoder, encodeFrames } from "./framing.mjs";

const research = process.argv.includes("research");

const [nodeMajor, nodeMinor] = process.versions.node.split(".").map(Number);
if (nodeMajor < 22 || (nodeMajor === 22 && nodeMinor < 19)) {
  process.stdout.write(`${JSON.stringify({ type: "error", message: "Pi requires Node.js >=22.19.0" })}\n`);
  process.exit(1);
}
if (process.argv.includes("--check")) {
  process.stdout.write(`${JSON.stringify({
    type: "ready", backend: "pi", version: "0.87.1", node_version: process.versions.node,
    protocol: research ? "research-ndjson/1" : "ndjson/1", direct_model_access: false, builtin_tools: [],
  })}\n`);
  process.exit(0);
}

const MAX_LINE_BYTES = 2 * 1024 * 1024;
const model = {
  id: "python-deepseek",
  name: "Python-owned DeepSeek transport",
  api: "openai-completions",
  provider: "python-bridge",
  baseUrl: "",
  reasoning: true,
  input: research ? ["text", "image"] : ["text"],
  contextWindow: 1000000,
  maxTokens: 32768,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
const usage = {
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};
let started = false;
let finished = false;
let agent;
let sequence = 0;
let modelRounds = 0;
let toolCalls = 0;
let fatalError;
const pending = new Map();

function send(value) {
  if (research) {
    for (const line of encodeFrames(value)) process.stdout.write(line);
  } else {
    const line = `${JSON.stringify(value)}\n`;
    if (Buffer.byteLength(line) > MAX_LINE_BYTES) throw new Error("Protocol frame is too large");
    process.stdout.write(line);
  }
}

function finish(value) {
  if (finished) return;
  finished = true;
  try {
    send({ ...value, model_rounds: modelRounds, tool_calls: toolCalls });
  } catch {
    send({ type: "error", message: "Protocol frame is too large",
      model_rounds: modelRounds, tool_calls: toolCalls });
  }
  for (const waiter of pending.values()) waiter.reject(new Error("Bridge closed"));
  pending.clear();
  process.stdin.destroy();
}

function stop(message) {
  fatalError = new Error(message);
  agent?.abort();
  for (const waiter of pending.values()) waiter.reject(fatalError);
  pending.clear();
  finish({ type: "error", message });
}

function rpc(kind, payload, signal) {
  if (finished || signal?.aborted) return Promise.reject(new Error("Operation aborted"));
  const id = String(++sequence);
  return new Promise((resolve, reject) => {
    const abort = () => {
      pending.delete(id);
      reject(new Error("Operation aborted"));
    };
    pending.set(id, {
      resolve(value) { signal?.removeEventListener("abort", abort); resolve(value); },
      reject(error) { signal?.removeEventListener("abort", abort); reject(error); },
    });
    signal?.addEventListener("abort", abort, { once: true });
    send({ type: "request", id, kind, ...payload });
  });
}

function assistant(message) {
  if (!message || message.role !== "assistant" ||
      (message.content != null && typeof message.content !== "string")) {
    throw new Error("Invalid model assistant response");
  }
  const calls = message.tool_calls ?? [];
  if (!Array.isArray(calls) || calls.length > (research ? 256 : 8)) throw new Error("Invalid tool call list");
  const ids = new Set();
  const content = message.content ? [{ type: "text", text: message.content }] : [];
  for (const call of calls) {
    if (!call || typeof call.id !== "string" || !call.id || ids.has(call.id) ||
        call.type !== "function" || typeof call.function?.name !== "string") {
      throw new Error("Invalid model tool call");
    }
    ids.add(call.id);
    const args = typeof call.function.arguments === "string"
      ? JSON.parse(call.function.arguments) : call.function.arguments;
    if (!args || typeof args !== "object" || Array.isArray(args)) {
      throw new Error("Tool arguments must be an object");
    }
    content.push({ type: "toolCall", id: call.id, name: call.function.name, arguments: args });
  }
  return {
    role: "assistant", content, api: model.api, provider: model.provider,
    model: model.id, usage: structuredClone(usage),
    ...(typeof message.reasoning_content === "string"
      ? { reasoning_content: message.reasoning_content } : {}),
    stopReason: calls.length ? "toolUse" : "stop", timestamp: Date.now(),
  };
}

function contentParts(value) {
  if (typeof value === "string") return [{ type: "text", text: value }];
  if (!research || !Array.isArray(value)) throw new Error("Only text messages are supported");
  return value.map((part) => {
    if (part.type === "text" && typeof part.text === "string") return part;
    const match = part.type === "image_url" && /^data:(image\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)$/.exec(part.image_url?.url);
    if (!match) throw new Error("Only inline original page images are supported");
    return { type: "image", mimeType: match[1], data: match[2] };
  });
}
function exportedParts(parts) {
  return parts.map((part) => part.type === "image"
    ? { type: "image_url", image_url: { url: `data:${part.mimeType};base64,${part.data}` } }
    : { type: "text", text: part.text });
}
function toolContent(result) {
  if (!research || !Array.isArray(result?._pi_images)) return [{ type: "text", text: JSON.stringify(result) }];
  const { _pi_images: images, ...data } = result;
  return [{ type: "text", text: JSON.stringify(data) }, ...images.map((image) => {
    if (!/^image\/[a-zA-Z0-9.+-]+$/.test(image.mimeType) || typeof image.data !== "string" || !/^[A-Za-z0-9+/=]+$/.test(image.data)) throw new Error("Invalid original page image");
    return { type: "image", data: image.data, mimeType: image.mimeType };
  })];
}
function importMessages(messages, tools) {
  const names = new Map();
  return messages.map((message, index) => {
    if (message.role === "assistant") {
      const value = assistant(message);
      for (const call of value.content.filter((x) => x.type === "toolCall")) names.set(call.id, call.name);
      return value;
    }
    if (!research && typeof message.content !== "string") throw new Error("Only text messages are supported");
    if (message.role === "system" || message.role === "user") {
      return {
        role: message.role, content: Array.isArray(message.content) ? contentParts(message.content) : message.content, timestamp: Date.now(),
        ...(index === 0 && message.role === "system" ? { toolsAdded: tools } : {}),
      };
    }
    if (message.role === "tool" && names.has(message.tool_call_id)) {
      return {
        role: "toolResult", toolCallId: message.tool_call_id,
        toolName: names.get(message.tool_call_id),
        content: contentParts(message.content), isError: Boolean(message.is_error),
        timestamp: Date.now(),
      };
    }
    throw new Error("Invalid input message role or orphan tool result");
  });
}

function exportMessages(messages) {
  return messages.map((message) => {
    const text = typeof message.content === "string" ? message.content
      : message.content.filter((x) => x.type === "text").map((x) => x.text).join("\n");
    if (message.role === "toolResult") {
      return { role: "tool", tool_call_id: message.toolCallId, content: research && message.content.some((x) => x.type === "image") ? exportedParts(message.content) : text };
    }
    if (message.role === "assistant") {
      const calls = message.content.filter((x) => x.type === "toolCall");
      return {
        role: "assistant", content: text,
        ...(typeof message.reasoning_content === "string"
          ? { reasoning_content: message.reasoning_content } : {}),
        ...(calls.length ? { tool_calls: calls.map((call) => ({
          id: call.id, type: "function",
          function: { name: call.name, arguments: JSON.stringify(call.arguments) },
        })) } : {}),
      };
    }
    if (research && Array.isArray(message.content) && message.content.some((x) => x.type === "image")) return { role: message.role, content: exportedParts(message.content) };
    const sections = Object.values(message.sections ?? {}).filter((x) => typeof x === "string");
    return { role: message.role, content: [text, ...sections].join("\n") };
  });
}

function integer(value, name, max) {
  if (!Number.isSafeInteger(value) || value < 1 || value > max) throw new Error(`Invalid ${name}`);
  return value;
}

async function run(start) {
  const rounds = integer(start.max_tool_rounds, "max_tool_rounds", research ? 10000 : 32);
  const callLimit = integer(start.max_tool_calls, "max_tool_calls", 256);
  // Python applies the model-context budget after compacting old tool output.
  if (!research) integer(start.max_context_chars, "max_context_chars", 4000000);
  if (!Array.isArray(start.messages) || !start.messages.length ||
      !Array.isArray(start.tools) || start.tools.length > 64) throw new Error("Invalid start payload");
  const schemaNames = new Set();
  const tools = start.tools.map((entry) => {
    const definition = entry?.function;
    if (entry?.type !== "function" || !definition ||
        !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(definition.name) ||
        schemaNames.has(definition.name) || definition.parameters?.type !== "object") {
      throw new Error("Invalid or duplicate registered tool schema");
    }
    schemaNames.add(definition.name);
    return {
      name: definition.name, label: definition.name,
      description: definition.description ?? "", parameters: definition.parameters,
      executionMode: "sequential", replay: "never",
      prepareArguments(args) {
        // Pi's default validation coerces types. Preserve the Python strict contract.
        if (!Value.Check(definition.parameters, args)) throw new Error("Invalid tool parameters");
        return args;
      },
      async execute(_id, args, signal) {
        const result = await rpc("tool", { name: definition.name, arguments: args, ...(research ? { call_id: _id } : {}) }, signal);
        return { content: toolContent(result), details: {} };
      },
    };
  });
  const declarations = tools.map(({ name, description, parameters }) => ({ name, description, parameters }));
  const messages = importMessages(start.messages, declarations);
  // Crash recovery executes only missing receipts. Already checkpointed results
  // remain in history and never reach the executor again.
  if (research) {
    const lastAssistant = messages.findLastIndex((message) => message.role === "assistant");
    const tail = messages.slice(lastAssistant + 1);
    if (lastAssistant >= 0 && tail.every((message) => message.role === "toolResult")) {
      const completed = new Set(tail.map((message) => message.toolCallId));
      for (const call of messages[lastAssistant].content.filter((part) => part.type === "toolCall")) {
        if (completed.has(call.id)) continue;
        const tool = tools.find((entry) => entry.name === call.name);
        if (!tool) throw new Error("Cannot resume unknown tool");
        tool.prepareArguments(call.arguments);
        if (++toolCalls > callLimit) throw new Error("Agent tool budget exhausted");
        const result = await rpc("tool", { name: call.name, arguments: call.arguments, call_id: call.id });
        messages.push({ role: "toolResult", toolCallId: call.id, toolName: call.name,
          content: toolContent(result), isError: false, timestamp: Date.now() });
        await rpc("checkpoint", { messages: exportMessages(messages) });
      }
    }
  }
  agent = new Agent({
    initialState: { model, thinkingLevel: "high", tools, messages },
    toolExecution: "sequential",
    ...(research ? { prepareRequest: async ({ context }, signal) => {
      const prepared = await rpc("prepare_context", { messages: exportMessages(context.messages) }, signal);
      if (!Array.isArray(prepared) || !prepared.length) throw new Error("Invalid prepared context");
      const replacement = importMessages(prepared, declarations);
      agent.state.messages = replacement;
      await rpc("checkpoint", { messages: exportMessages(replacement) }, signal);
      return { context: { ...context, messages: replacement.slice() } };
    } } : {}),
    beforeToolCall() {
      toolCalls += 1;
      if (toolCalls > callLimit || modelRounds >= rounds) {
        fatalError = new Error("Agent tool budget exhausted");
        return { block: true, reason: fatalError.message, terminate: true };
      }
      return undefined;
    },
    finishTurn() { return fatalError ? { action: "end" } : undefined; },
    streamFn(_model, context, options) {
      const stream = createAssistantMessageEventStream();
      void (async () => {
        try {
          if (++modelRounds > rounds) throw new Error("Agent model budget exhausted");
          const outbound = exportMessages(context.messages);
          const result = await rpc("model", {
            messages: outbound,
            tools: modelRounds === rounds ? [] : start.tools,
          }, options?.signal);
          const response = assistant(result);
          stream.push({ type: "done", reason: response.stopReason, message: response });
          stream.end(response);
        } catch (error) {
          fatalError = error;
          const response = { ...assistant({ role: "assistant", content: "" }),
            stopReason: options?.signal?.aborted ? "aborted" : "error", errorMessage: error.message };
          stream.push({ type: "error", reason: response.stopReason, error: response });
          stream.end(response);
        }
      })();
      return stream;
    },
  });
  if (research) agent.subscribe(async (event, signal) => {
    if (event.type === "message_end") await rpc("checkpoint", { messages: exportMessages(agent.state.messages) }, signal);
  });
  await agent.continue();
  if (fatalError) throw fatalError;
  const last = [...agent.state.messages].reverse().find((item) => item.role === "assistant");
  if (!last || last.stopReason !== "stop") throw new Error(last?.errorMessage ?? "Agent ended without a complete answer");
  const answer = last.content.filter((item) => item.type === "text").map((item) => item.text).join("\n");
  if (!answer.trim()) throw new Error("Agent returned an empty answer");
  finish({ type: "done", answer, ...(research ? { messages: exportMessages(agent.state.messages) } : {}) });
}

const decoder = new FrameDecoder();
let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  if (finished) return;
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0 && !finished) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (Buffer.byteLength(line) + 1 > MAX_LINE_BYTES) return stop("Input line is too large");
    try {
      const value = research ? decoder.accept(JSON.parse(line)) : JSON.parse(line);
      if (value === undefined) continue;
      if (!started) {
        if (value.type !== "start") throw new Error("First message must be start");
        started = true;
        void run(value).catch((error) => finish({ type: "error", message: error.message }));
      } else {
        const waiter = pending.get(value.reply_to);
        if (!waiter) throw new Error("Unknown or repeated reply id");
        pending.delete(value.reply_to);
        if (typeof value.error === "string") waiter.reject(new Error(value.error));
        else if (Object.hasOwn(value, "result")) waiter.resolve(value.result);
        else throw new Error("Reply requires result or error");
      }
    } catch (error) { stop(error.message); }
  }
  if (Buffer.byteLength(buffer) > MAX_LINE_BYTES) stop("Input line is too large");
});
process.stdin.on("end", () => { if (!finished) stop("Parent input closed"); });
process.stdin.on("error", () => stop("Parent input failed"));
process.stdout.on("error", () => { agent?.abort(); process.exit(1); });
process.on("SIGTERM", () => stop("Operation cancelled"));
process.on("SIGINT", () => stop("Operation cancelled"));
