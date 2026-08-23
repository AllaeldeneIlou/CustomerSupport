# Lab 3 Report: Scaling Tools with Gateway

## 1. What We Built

We exposed an existing AWS Lambda function (`workshop-warranty-check`) as an MCP-compatible tool that our agent can discover and call at runtime, without modifying the Lambda code. The agent now has a federated tool surface: local Python functions, an external MCP server (Exa AI), and a Gateway-proxied Lambda — all accessed uniformly through the MCP protocol.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|---|---|
| **AgentCore Gateway** | A managed reverse-proxy / tool registry that speaks MCP (Model Context Protocol) on the agent-facing side and invokes backends (Lambda, HTTP APIs) on the target side. It maintains a tool catalog (name + JSON Schema + description) and routes tool-call requests to the correct backend, handling serialization and IAM auth to the target. Conceptually: an MCP server whose tools are dynamically backed by remote functions instead of local code. |
| **Gateway Target (Lambda)** | A registration record that maps a tool name to a Lambda ARN + invocation config. When the Gateway receives an MCP `tools/call` for `check_warranty`, it invokes the Lambda synchronously with the tool parameters as the event payload, then wraps the Lambda response as an MCP tool result. |
| **Tool Schema (warranty_schema.json)** | An MCP tool definition: name, natural-language description (consumed by the LLM to decide when to call it), and a JSON Schema for input validation. This is the metadata layer that makes an opaque function intelligible to an LLM — essentially prompt engineering for tools. |
| **`AGENTCORE_GATEWAY_MY_GATEWAY_URL` env var** | Service discovery via environment injection. The deployment system writes the Gateway's HTTPS endpoint into the runtime container's environment so the agent code can find it at boot without hardcoding URLs or querying a registry. |
| **MCP Client (streamable HTTP)** | An HTTP-based client implementing the MCP protocol (tool listing via `tools/list`, invocation via `tools/call`). The agent connects to the Gateway URL the same way it connects to Exa AI — protocol uniformity means the agent doesn't know or care what's behind the endpoint. |

## 3. Request Lifecycle

Tracing: `"Check the warranty for PROD-003"`

1. **CLI to Runtime:** `agentcore invoke` sends an HTTP POST to the AgentCore Runtime endpoint with the prompt, session-id, and user-id header.
2. **Runtime to Agent boot:** The runtime container starts (or reuses) the Python process. `get_or_create_agent()` initializes the Strands Agent with the model, memory session manager, local tools, and two MCP clients (Exa + Gateway).
3. **MCP tool discovery:** On first use, each MCP client calls `tools/list` on its endpoint. The Gateway returns `[{name: "check_warranty", description: "...", inputSchema: {...}}]`. The agent now has ~8 tools in its tool list.
4. **LLM planning:** The prompt + system prompt + tool descriptions go to Bedrock (Claude). The LLM reads the `check_warranty` description and decides this tool fits the query. It emits a tool-call: `check_warranty(product_id="PROD-003")`.
5. **MCP tool call to Gateway:** The Strands framework routes the tool call to the Gateway MCP client, which sends an MCP `tools/call` request over HTTPS to the Gateway URL.
6. **Gateway to Lambda:** The Gateway looks up target "WarrantyCheck", assumes its IAM role (which has `lambda:InvokeFunction` permission), and does a synchronous `Invoke` on the Lambda ARN with `{"product_id": "PROD-003"}` as the event payload.
7. **Lambda execution:** The Lambda handler does a dict lookup, returns `{"statusCode": 200, "body": "{\"product\": \"Laptop Stand\", \"warranty_months\": 6, \"status\": \"expired\", \"expires\": \"2026-01-01\"}"}`.
8. **Response unwind:** Gateway wraps the Lambda response as an MCP tool result, MCP client returns it to Strands, Strands feeds it back to the LLM as the tool result, LLM synthesizes a natural-language answer.
9. **Streaming:** The agent yields text chunks via `stream_async`, Runtime streams them back to the CLI over HTTP.

## 4. Design Decisions & Trade-offs

| Decision | Why | Alternative |
|---|---|---|
| **Gateway as managed MCP proxy** | Avoids modifying existing Lambda code; provides a uniform MCP interface regardless of backend type; centralizes tool metadata | Embed tool logic directly in agent code (tight coupling), or run your own MCP server wrapper around each Lambda (operational overhead) |
| **Tool schema separate from Lambda** | Separation of concerns: Lambda team owns logic, agent team owns how it's described to LLMs. Schema can be tuned for LLM comprehension without redeploying the Lambda | Embed descriptions in Lambda metadata/tags (limited length, wrong abstraction), or auto-generate from code (loses nuance the LLM needs) |
| **Env var for service discovery** | Simple, no external registry needed, works in containers | Service mesh / DNS-based discovery (more complex but more dynamic), config file (less flexible) |
| **Single Gateway, multiple targets** | One MCP endpoint = one connection from the agent. Adding a new tool is just registering another target, not changing agent code | One MCP server per tool (connection overhead), or a monolithic tool server (single point of failure, harder to manage permissions per tool) |
| **No auth on Gateway (yet)** | Simplifies initial setup; auth added in Lab 4 | Always-on auth (better security posture but slower iteration during development) |

## 5. Portable Concepts vs AWS Lock-in

| Portable (transfers to any stack) | AWS-specific (re-implement elsewhere) |
|---|---|
| MCP protocol for tool discovery/invocation | AgentCore Gateway as managed service (you'd run your own MCP server) |
| JSON Schema for tool input validation | CDK deployment of Gateway + IAM wiring |
| Natural-language tool descriptions for LLM reasoning | Lambda as compute backend (replace with any FaaS or HTTP endpoint) |
| Env-var-based service discovery | `AGENTCORE_GATEWAY_*` naming convention |
| Reverse-proxy pattern for tool federation | Automatic IAM role creation for Lambda invocation |
| Separation of tool metadata from tool implementation | SSM Parameter Store for ARN storage |

## 6. Build-it-yourself Sketch

```
1. Write a small MCP server (Python/TS) that serves tools/list and tools/call
2. For each "target", register a handler that invokes the backend (HTTP call, Lambda SDK, gRPC)
3. Store tool schemas in a JSON/YAML file loaded at boot
4. Deploy the MCP server behind a reverse proxy (nginx/Envoy) with TLS
5. Pass the MCP server URL to your agent as an env var
6. In your agent framework, add an MCP client pointing at that URL
7. For auth: add a middleware that validates Bearer tokens before routing
8. For multiple backends: add routing logic (tool-name to backend mapping)
```

Equivalent stack: FastAPI MCP server + tool registry YAML + httpx for Lambda/API calls + any agent framework with MCP client support.

## 7. Glossary

| Term | What it actually is |
|---|---|
| **AgentCore Gateway** | Managed MCP-speaking reverse proxy that routes tool calls to registered backends |
| **Gateway Target** | A routing rule: tool name(s) to backend endpoint (Lambda ARN, API URL, etc.) |
| **Tool Schema** | MCP tool definition: name + NL description + JSON Schema for inputs |
| **MCP (Model Context Protocol)** | Open protocol for tools to advertise capabilities and accept invocations from LLM agents |
| **Streamable HTTP** | MCP transport variant using standard HTTP with streaming support (vs stdio) |
| **`tools/list`** | MCP method an agent calls to discover available tools and their schemas |
| **`tools/call`** | MCP method an agent calls to invoke a specific tool with parameters |
| **Lambda event mapping** | Gateway passes tool parameters directly as the Lambda event object (not wrapped in body) |
| **Env var injection** | Deployment system sets `AGENTCORE_GATEWAY_*_URL` in the runtime container's environment |

## 8. Operational Gotchas & Lessons

| Gotcha | Principle |
|---|---|
| **`inputSchema` must not have a nested `"json"` wrapper** — deploy fails with "Attribute type null is not yet supported" | Validate schemas against the exact format the platform expects before deploying; schema-of-schema mismatches are silent until deploy time |
| **Region mismatch** — Lambda was in us-east-1 but CLI defaulted to us-west-2 | Always verify region context in multi-region setups; "resource not found" is almost always a region or account problem |
| **Gateway URL only available after deploy** — local dev gets `None` | Design for graceful degradation: guard MCP client creation so local testing works without the full stack |
| **Tool description quality directly affects agent accuracy** — vague descriptions = wrong tool selection | Treat tool descriptions as prompt engineering; test with adversarial queries to verify the LLM picks the right tool |
| **First gateway deploy is slow (~2 min)** — subsequent updates are faster | Gateway creation involves provisioning the MCP endpoint + IAM role propagation; factor this into CI/CD pipeline estimates |
| **Lambda event shape matters** — Gateway sends params at top level, not inside `event["body"]` | Document the invocation contract; existing Lambdas designed for API Gateway (body-wrapped) need an adapter or a new handler |

## 9. So What (Production Lens)

Gateway solves the "tool sprawl" problem: in a real org, useful logic already exists as Lambda functions, APIs, and services owned by different teams. Rather than duplicating or wrapping each one into every agent's codebase, you register them once behind a Gateway and any agent with MCP client support can discover and call them — turning your organization's existing services into an agent-accessible tool catalog without modifying a single backend.
