# Lab 1 Report: Customer Support Agent Prototype

## 1. What We Built

A customer support agent that answers product questions, looks up return policies, and searches the web — first tested on a local dev server, then deployed to a managed cloud endpoint on AWS. The agent uses Claude Sonnet 4.6 as its reasoning engine, two Python tool functions as its domain capabilities, and an MCP-connected web search service for general queries.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Runtime** | A serverless container platform (similar to AWS Lambda or Cloud Run) that receives HTTP POST requests, boots your Python process on cold start, routes the request to your `@app.entrypoint` function, and streams the response back via SSE. Isolation is per-invocation; scaling is managed. Think: "Function-as-a-Service but for long-running agent conversations." |
| **Strands Agent SDK** | A thin orchestration loop: (1) send user message + tool schemas to the LLM, (2) if the LLM returns a tool_use block, execute the matching Python function, (3) feed the result back, (4) repeat until the LLM produces a final text response. This is the ReAct pattern (Reason → Act → Observe → Repeat). |
| **`@tool` decorator** | Reads the function's type hints and docstring, generates a JSON Schema tool definition, and registers it with the agent. At runtime, the LLM sees this schema and can "call" the function by emitting a structured JSON block with the function name and arguments. |
| **MCP Client (Exa AI)** | Model Context Protocol — a JSON-RPC-over-HTTP standard for tool servers. The client discovers available tools from the server's `/tools/list` endpoint, adds them to the agent's tool list, and proxies tool calls to the server at invocation time. It's a plugin architecture for AI tools. |
| **Bedrock (model provider)** | A managed API gateway to foundation models. Your code sends a `converse` or `invoke_model` API call with IAM auth; Bedrock routes it to the model, handles token metering, and streams the response. No API keys — auth is via the instance's IAM role. |
| **Cross-region inference (`global.` prefix)** | A load-balancing layer that routes model requests to whichever AWS region has available GPU capacity. Functionally equivalent to a multi-region model proxy with health-check-based routing. |
| **CDK / CloudFormation** | Infrastructure-as-Code. Your `agentcore.json` config is compiled into a CloudFormation template (via CDK TypeScript), which declaratively provisions IAM roles, the runtime resource, and networking. Same concept as Terraform or Pulumi. |
| **`agentcore dev`** | A local uvicorn HTTP server with hot-reload (watchfiles/StatReload). Exposes `/invocations` on localhost:8081 so you can curl or use a TUI to test. Equivalent to `uvicorn main:app --reload` with some wrapper logic. |

## 3. Request Lifecycle

One invocation, end-to-end:

1. **Client** sends `POST /invocations` with `{"prompt": "What's the return policy for my Smart Watch?"}` to the AgentCore Runtime endpoint (or localhost in dev).
2. **Runtime** deserializes the payload, calls `invoke(payload, context)`.
3. **Agent orchestration loop** (Strands SDK) formats the prompt + system prompt + tool schemas into a Bedrock `Converse` API call.
4. **Bedrock** routes to Claude Sonnet 4.6 (cross-region). The LLM decides to call `get_product_info("Smart Watch")`.
5. **Tool execution**: the SDK invokes the Python function locally (in-process), gets back the product string including `category: electronics`.
6. **Loop continues**: the tool result is appended to the conversation and sent back to the LLM. The LLM now calls `get_return_policy("electronics")`.
7. **Second tool execution** returns the policy string.
8. **Final response**: the LLM synthesizes both results into a natural language answer.
9. **Streaming**: each text chunk is yielded as an SSE `data:` frame back through the HTTP response to the client.

No auth is checked on the runtime endpoint itself yet (that's Lab 3+). The IAM role attached to the runtime authorizes it to call Bedrock.

## 4. Design Decisions & Trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **Tools as in-process Python functions** | Zero latency, simple debugging, no network hop. | External microservices (more scalable but adds latency and deployment complexity). |
| **Singleton agent (global `_agent`)** | Avoids re-initializing the model client on every request. | Per-request agent (stateless, but wastes cold-start time re-connecting). |
| **SSE streaming** | Low time-to-first-token for the user; partial responses appear immediately. | Wait for full response (simpler client code but worse UX for long answers). |
| **MCP for web search** | Standard protocol; swap Exa for any MCP-compatible search server without changing agent code. | Direct API integration (tighter coupling, custom code per provider). |
| **IAM auth to Bedrock (no API keys)** | Keys can't leak, rotation is automatic, least-privilege via policies. | API key in env var (simpler locally but a secret management headache in prod). |
| **Serverless runtime** | Zero ops, auto-scale, pay-per-invocation. | Self-managed containers on ECS/EKS (more control over cold starts, GPU affinity, cost at scale). |

## 5. Portable Concepts vs AWS Lock-in

| Portable (works anywhere) | AWS-specific convenience |
|---------------------------|--------------------------|
| ReAct agent loop (reason + tool use) | AgentCore Runtime (managed serverless hosting) |
| Tool-use JSON Schema contract | Bedrock model routing & cross-region inference |
| MCP protocol for tool servers | IAM-based auth to model APIs |
| SSE streaming over HTTP | CDK/CloudFormation IaC specifics |
| System prompt engineering | `agentcore` CLI scaffolding |
| Python function tools with type hints | `BedrockAgentCoreApp` wrapper class |

## 6. Build-it-yourself Sketch

```
FastAPI app (uvicorn, SSE streaming via StreamingResponse)
+ anthropic SDK (direct Claude API calls, or any LLM provider)
+ a ReAct loop (while tool_use in response: execute tool, re-prompt)
+ @tool decorator that reads type hints → JSON Schema (or use langchain/instructor)
+ MCP client library (mcp-python-sdk) pointing at any MCP server
+ Docker / Cloud Run / Lambda Web Adapter for deployment
+ Terraform or Pulumi for IaC
+ IAM or service account for model API auth
```

Total: ~200 lines of orchestration code + your tool functions. AgentCore saves you the boilerplate and ops, not the concepts.

## 7. Glossary

| Term | What it actually is |
|------|-------------------|
| **AgentCore Runtime** | Serverless container that runs your agent code behind an HTTP endpoint |
| **Strands Agent** | A ReAct orchestration loop: LLM → tool call → result → LLM → ... → final answer |
| **`@tool`** | Decorator that converts a Python function into an LLM-callable tool via JSON Schema |
| **MCP (Model Context Protocol)** | JSON-RPC standard for discovering and calling tools on external servers |
| **Bedrock** | Managed LLM API gateway (no keys, IAM auth, usage metering) |
| **Cross-region inference** | Multi-region load balancer for model requests (`global.` prefix) |
| **CDK** | TypeScript-based IaC that compiles to CloudFormation templates |
| **`agentcore.json`** | Project manifest declaring runtimes, memories, credentials, gateways |
| **`aws-targets.json`** | Deployment target config (account + region) |
| **SSE (Server-Sent Events)** | HTTP streaming protocol; each chunk is a `data:` line |
| **SCP (Service Control Policy)** | Org-level guardrail that restricts what actions/regions are allowed |

## 8. Operational Gotchas & Lessons

| Gotcha | Principle |
|--------|-----------|
| **Deploy target defaulted to us-west-2 but SCP blocked it** | Always verify your deploy region matches account/org constraints. IaC won't save you from policy denials. |
| **`agentcore dev` requires a TTY; `agentcore dev -l` for scripted use** | Interactive vs non-interactive modes are a common pattern in CLIs. Know both paths. |
| **`grep -c` returns exit code 1 on zero matches** | Exit codes aren't just 0/non-zero — tools have semantics. `grep` exit 1 = "no match" (not "error"). |
| **Node version warning (v20 vs v22)** | SDK deprecation warnings are informational today, breaking tomorrow. Track them. |
| **First Bedrock call may hit account verification (self-paced)** | New accounts have a one-time model access gate. Not a code bug — an onboarding gate. |
| **Cold starts on serverless** | First invocation after idle is slower (process boot + model client init). The singleton agent pattern mitigates repeated init within a warm instance. |

## 9. So What (Production Lens)

This is the foundation layer: a deployed, invocable agent with domain tools. In production you'd add authentication (who can call it), memory (conversation continuity), observability (traces/metrics), and evaluation (does it answer correctly) — which is exactly what Labs 2-5 layer on. The agent pattern itself (LLM + tools + streaming) is the same whether you use AgentCore or roll your own — the managed runtime just removes the Docker/scaling/IAM boilerplate.
