# Feishu Pi bridge

This private Node application uses the real Pi `Agent` runtime for multi-round
tool orchestration. It does not use Pi's coding CLI, provider networking, resource
discovery, plugins, filesystem tools, or shell tools. Python remains the owner of
DeepSeek HTTP requests, credentials, repository reads, proposal confirmation,
auditing, session isolation, and conversation deadlines.

## Install and verify

Node.js 22.19 or newer is required. Install in this directory, without lifecycle
scripts or global packages:

```sh
npm ci --ignore-scripts --no-audit --no-fund
node --test bridge.test.mjs
```

The direct dependencies are pinned, and `package-lock.json` freezes transitive
dependencies and integrity hashes. The bridge receives a scrubbed environment
from Python; do not pass credentials, `NODE_OPTIONS`, user home, or plugin settings.
It is an orchestration component, not an operating-system sandbox.

## Protocol

Each process handles one user request over UTF-8 newline-delimited JSON on stdio.
Stdout contains protocol frames only. Each frame is limited to 2 MiB.

Python starts with:

```json
{"type":"start","messages":[{"role":"user","content":"Explain the scheduler"}],"tools":[],"max_tool_rounds":6,"max_tool_calls":24,"max_context_chars":96000}
```

Messages and tool declarations use OpenAI chat-completions shapes. Pi converts
the transcript internally and sends these reverse requests to Python:

```json
{"type":"request","id":"1","kind":"model","messages":[],"tools":[]}
{"type":"request","id":"2","kind":"tool","name":"search_repository","arguments":{"query":"scheduler"}}
```

Python returns exactly one matching reply:

```json
{"reply_to":"1","result":{"role":"assistant","content":"Answer"}}
{"reply_to":"2","result":{"path":"src/core/scheduler.py"}}
{"reply_to":"2","error":"Approved file unavailable"}
```

Pi exposes only the initial tool declarations, runs them sequentially, and
strictly validates parameters before requesting execution. It does not coerce
types. Unknown tools cannot reach Python. Python validates tools again and owns
the final business rules. Model errors end the request; tool errors become tool
results so the model can adjust its next request.

The last allowed model round sends an empty tool list for summarization. Python
is authoritative for actual model-call counts, including citation repair calls,
and compacts the supplied transcript against `max_context_chars` before HTTP.
The bridge applies only the 2 MiB transport cap to growing context.

Terminal frames are either:

```json
{"type":"done","answer":"Answer","model_rounds":2,"tool_calls":1}
{"type":"error","message":"Operation cancelled","model_rounds":1,"tool_calls":0}
```

The counters describe the Node loop; Python's persisted audit is authoritative.
EOF, SIGTERM, and SIGINT abort the active Pi operation and settle pending RPC.
Python owns the watchdog and must terminate the process if the transport stalls.

Official interfaces: [Pi Agent](https://github.com/earendil-works/pi/tree/main/packages/agent),
[Pi AI](https://github.com/earendil-works/pi/tree/main/packages/ai).

## Research profile

`node bridge.mjs --profile research` uses the same pinned Pi `Agent.continue()`
implementation through `shared-runtime.mjs`. The default Feishu `ndjson/1`
profile remains compatible. `--profile research --check` reports
`research-ndjson/1` without making a model call.

The research profile supports 48 MiB logical messages split into ordered,
SHA-256-checked base64 frames, each below the existing 2 MiB limit. The
`framing.mjs` encoder/decoder is shared by both directions. Missing, duplicate,
reordered, oversized, or corrupt frames terminate visibly.

The former fundamental-quant Python research adapter was retired on 2026-10-24.
The bridge remains shared infrastructure for the independent Feishu assistant;
it is not an entrypoint for the retired fundamental research workflow. Historical
protocol notes below describe the bridge capabilities, not an active Python
fundamental research integration.

Research tools can return `_pi_images: [{data: BASE64, mimeType: "image/png"}]`.
Pi retains those as actual image content and exports inline OpenAI image parts.
Python's provider transport is responsible for converting them to the provider's
accepted message roles, measuring the real wire request, and reserving input
and output usage. Research and independent review instantiate separate runtime
sessions. Private provider `reasoning_content` is retained only for continuity
of its original session.

The Python runner chooses a compatible bundled Node when available, falls back
to a compatible PATH Node, and scrubs the subprocess environment. Every model,
tool, context preparation, checkpoint, and stop remains Python-controlled.
The runtime does not authorize a paid campaign or reset any ledger.
