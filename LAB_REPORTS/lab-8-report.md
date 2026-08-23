# Lab 8 Report: Zero-Code Agents with AgentCore Harness

## 1. What We Built

A fully functional "Order Research Agent" defined entirely as a JSON configuration file — no Python, no dependencies, no build step. It connects to the same secured Gateway (with Cedar policies) as the custom-coded Runtime agent from Labs 1–7, proving that governance applies uniformly regardless of how an agent is built. We also added a human-in-the-loop approval flow using an inline function declaration that pauses the agent mid-execution and returns control to the caller.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Harness** | A managed agentic loop runner. You provide a config (model + system prompt + tool list) and the platform runs the standard reason→act→observe loop inside a microVM. Equivalent to hosting a Strands/LangGraph/CrewAI agent where the framework is the platform itself — you supply only the declarative inputs. |
| **Harness `harness.json`** | An agent manifest — a JSON schema declaring behavior. Analogous to a Kubernetes Pod spec or a Docker Compose service definition: what to run (model), how to behave (system prompt), what capabilities to expose (tools). No imperative code. |
| **Code Interpreter tool** | A sandboxed Python/shell environment inside the microVM. The agent can execute arbitrary code in isolation. Same concept as OpenAI's Code Interpreter or any Jupyter kernel attached to an agent — deterministic computation alongside LLM reasoning. |
| **Inline Function** | A tool declaration that triggers a pause/resume protocol. When the agent calls it, the runtime returns control to the caller with `stopReason: "tool_use"`. The caller provides the result asynchronously, and the agent resumes. This is a **coroutine-style suspension** at the API level — the same pattern as a webhook callback, a human-approval queue, or an async/await yield point. The session state (conversation history) is held server-side; the inline function turn is intentionally NOT persisted until the caller completes it. |
| **OAuth Credential Provider (Token Vault)** | A managed secrets store + token-fetching service. Stores client_id/secret, calls the OAuth token endpoint on behalf of the agent, caches tokens, and injects them into outbound requests. Equivalent to a sidecar that runs `client_credentials` grant flows — like HashiCorp Vault's JWT/OIDC secrets engine or AWS Secrets Manager + a Lambda rotation function. |
| **`client_credentials` grant** | Standard OAuth2 machine-to-machine flow (RFC 6749 §4.4). No user involved — the service authenticates with its own identity. The token carries scopes (permissions) rather than user claims. |
| **Session persistence (microVM)** | Each session ID maps to a stateful Linux VM. Files created in one invocation are available in the next, so the agent can iteratively build documents across turns. Analogous to a per-user container with a persistent volume mount, keyed by session ID. |
| **Model override per invocation** | Runtime model routing without redeployment. The manifest sets a default; each API call can override it. This is a router/gateway pattern — send cheap tasks to smaller models, expensive reasoning to larger ones. |
| **Cedar policy enforcement (unchanged from Lab 7)** | Attribute-based access control (ABAC) evaluated at the Gateway. The policy engine doesn't know or care which agent is calling — it evaluates the tool call's parameters against declarative rules. Governance is decoupled from the caller's implementation. |

## 3. Request Lifecycle (HITL Refund Flow)

1. **Caller** sends `invoke_harness(harnessArn, sessionId, messages=[{role:"user", text:"..."}])`
2. **Harness runtime** loads `harness.json`, hydrates the system prompt, fetches available tools (code-interpreter, gateway via MCP, approve_exception inline)
3. **LLM call** (Claude Sonnet via Bedrock) reasons: "I should try process_refund first"
4. **Gateway tool call**: harness's Token Vault fetches an OAuth access token from Cognito (`client_credentials` + client_id/secret → access_token with scope `agentcore/invoke`)
5. **Gateway** validates the JWT (signature via JWKS, issuer, audience/client check) → passes
6. **Cedar policy engine** evaluates `process_refund(amount=200, reason="damaged product")` → `refund_limit_policy` denies (amount >= 100)
7. **Gateway returns denial** to harness → LLM observes the failure
8. **LLM reasons**: "Policy blocked it; I should escalate" → calls `approve_exception(order_id, amount, reason)`
9. **Harness detects inline function** → streams response with `stopReason: "tool_use"` → **pauses**
10. **Caller** (our Python script) receives the tool call, prompts human, gets "yes"
11. **Caller resumes**: sends `[assistant: {toolUse: ...}, user: {toolResult: {approved: true, approver: "manager-jane"}}]`
12. **LLM resumes** with approval context → produces final summary → streams back as `stopReason: "end_turn"`

## 4. Design Decisions & Trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **Declarative config vs code** | Reduces surface area for bugs; most agents don't need custom orchestration. Faster to deploy, easier to audit. | Custom code (Strands SDK) when you need complex control flow, multi-step planning, custom retry logic, or state machines. |
| **Machine-to-machine OAuth vs shared token** | The harness has no inbound user context — it IS the service. Service identity is the correct auth pattern here. | Token passthrough (Runtime agent pattern) when you need user-level attribution and per-user policy evaluation. |
| **Inline function vs webhook** | Simpler caller-side code; the harness manages session suspension natively. No need for a callback URL or queue infrastructure. | Webhooks/queues when the approval takes hours/days (session timeout), or when the approval system can't call back synchronously. |
| **Policy at Gateway vs in-agent** | Agents can't bypass policies. New agents inherit governance automatically. Single enforcement point. | In-agent policy checks when you need nuanced, context-dependent decisions that can't be expressed as ABAC rules (but then you lose the guarantee). |
| **Per-session microVM vs shared process** | Strong isolation — one session can't leak data to another. Full Linux environment enables code execution. | Shared process (cheaper, faster cold start) when you don't need code execution or session isolation. |
| **Model override per-call vs per-deploy** | Enables cost optimization without downtime. Route by task complexity. | Fixed model when you need deterministic behavior guarantees or cost predictability. |

## 5. Portable Concepts vs AWS Lock-in

| Portable (works anywhere) | AWS-specific (re-implement elsewhere) |
|---------------------------|---------------------------------------|
| OAuth2 `client_credentials` flow (RFC 6749) | Token Vault managed credential provider |
| JWT validation against OIDC discovery URL | AgentCore Gateway as the hosting layer |
| ABAC/Cedar-style policy evaluation | Cedar policy engine integration |
| Agent manifest pattern (model + prompt + tools) | `harness.json` schema and `agentcore` CLI |
| MCP protocol for tool communication | AgentCore Gateway's MCP-native routing |
| Coroutine/pause-resume pattern for HITL | `invoke_harness` API's specific message format |
| Sandboxed code execution (Jupyter/Docker) | AgentCore Code Interpreter |
| Session-keyed stateful containers | AgentCore's managed microVM lifecycle |
| Model routing per request | `--model-id` override mechanism |

## 6. Build-it-Yourself Sketch

```
1. Agent manifest:     YAML/JSON config → loaded by a lightweight orchestrator (e.g., LiteLLM proxy + a 50-line Python loop)
2. Managed runtime:    Docker container per session, orchestrated by K8s Jobs or Fly Machines (session ID → container affinity)
3. OAuth credentials:  HashiCorp Vault (JWT secrets engine) or a sidecar that fetches client_credentials tokens
4. Gateway/tools:      FastAPI + MCP server (streamable-http) routing tool calls to backend functions
5. Policy engine:      Open Policy Agent (OPA) with Rego policies, or Cedar (open-source) evaluated at the gateway middleware
6. Code interpreter:   Jupyter kernel or gVisor-sandboxed Python subprocess inside the container
7. HITL pause/resume:  Return a 202 Accepted with a continuation token; caller POSTs the result back; agent loop resumes from stored state
8. Model routing:      LiteLLM or a custom proxy that accepts model_id per request and routes to OpenAI/Anthropic/Bedrock/etc.
```

## 7. Glossary

| Term | What it actually is |
|------|-------------------|
| **Harness** | Managed agentic loop that runs from a declarative config (no user code) |
| **harness.json** | Agent manifest — model, system prompt, tool list as JSON |
| **Inline function** | A declared tool that triggers coroutine-style pause/resume — control returns to caller |
| **Token Vault** | Managed secrets store + OAuth token-fetching service |
| **client_credentials** | OAuth2 machine-to-machine grant — service authenticates as itself, no user involved |
| **Credential Provider** | A Token Vault entry that knows how to fetch tokens for a specific OAuth server |
| **outboundAuth** | Config block specifying how the harness authenticates to an external service (the Gateway) |
| **--exec** | Run a shell command in the agent's microVM (deterministic execution, result returned via the model) |
| **stopReason: "tool_use"** | The streaming signal that the agent has paused on an inline function, awaiting caller input |
| **Session** | A microVM instance keyed by session ID; filesystem and state persist across invocations |
| **Model override** | Per-invocation model routing without redeployment |

## 8. Operational Gotchas & Lessons

| Gotcha | Principle |
|--------|-----------|
| **403 from Gateway despite valid OAuth token** | The token issuer (Cognito pool) must match the Gateway's JWT authorizer config. Verify the full trust chain: issuer → JWKS → audience → allowed clients. A token from the wrong pool is cryptographically valid but semantically rejected. |
| **Two Cognito pools in environment** | Always verify which pool a client belongs to. The Gateway's discovery URL is the source of truth — match your credential provider to it. |
| **Region not set → boto3/CLI fails** | Environment variables are terminal-scoped. Always set `AWS_DEFAULT_REGION` explicitly or pass `--region`. In containers/microVMs, inject it as an env var at launch. |
| **`--exec` returns results through the model** | Shell access isn't zero-token raw passthrough in the current CLI. For truly deterministic output, use the API directly. |
| **Inline function turns not persisted until completed** | If the caller never returns a result, the session remains clean. This prevents orphaned tool calls from corrupting history. But you must echo back the assistant's toolUse message when resuming. |
| **Client secret in CLI flags** | Exposed to shell history and process table. In production, use interactive mode, env vars, or a secrets manager. |
| **Two agents = must disambiguate** | After adding the harness, `agentcore invoke` requires `--runtime CustomerSupport` or `--harness OrderResearchAgent`. |

## 9. So What (Production Lens)

The harness pattern is the right default for 80% of agents that don't need custom orchestration — customer service bots, research assistants, data analysis agents. You define behavior declaratively, connect to governed tools via the Gateway, and get session persistence + code execution for free. When you need custom control flow, you graduate to a Runtime agent — but you keep the same Gateway, same policies, same memory infrastructure. The two patterns coexist and complement each other.
