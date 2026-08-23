# Lab 4 Report: Securing and Observing in Production

## 1. What We Built

We added end-user authentication to both the agent Runtime and its tool Gateway using JWT tokens issued by Amazon Cognito, establishing a single identity model that flows from the client through every component. We also explored the automatic OpenTelemetry-based observability that captures every request's full lifecycle without any instrumentation code. The result: only authenticated users can reach the agent, every component knows *who* is asking, and every request is traced end-to-end.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Runtime (Custom JWT authorizer)** | An API gateway/reverse-proxy that intercepts incoming HTTP requests, fetches the JWKS (JSON Web Key Set) from the OIDC discovery URL, verifies the JWT signature using RSA/EC public keys, checks `iss` (issuer), `exp` (expiry), and `aud`/`client_id` (audience) claims, then either passes the request through or returns 401. Entirely stateless — no session store, no database lookup. |
| **Amazon Cognito** | An OIDC-compliant identity provider. It stores user credentials (hashed passwords), issues signed JWTs (access + ID + refresh tokens) via standard OAuth2 flows (USER_PASSWORD_AUTH = Resource Owner Password Grant; client_credentials = machine-to-machine). The `.well-known/openid-configuration` endpoint publishes the JWKS URI, issuer, supported flows. Any OIDC provider (Auth0, Okta, Keycloak, Firebase Auth) works identically. |
| **AgentCore Gateway (Custom JWT)** | A tool-routing reverse-proxy (speaking MCP over HTTP) that independently validates the same JWT before forwarding tool calls to backends. The Runtime propagates the raw `Authorization` header, so the Gateway re-validates the same token — defense in depth, each hop checks independently. |
| **Token propagation (Runtime → Gateway)** | Standard HTTP header forwarding. The `requestHeaderAllowlist` config tells the Runtime to pass `Authorization` through to agent code, which then includes it in outbound MCP requests to the Gateway. No token exchange or re-signing — same bearer token, re-validated at each hop. |
| **Session persistence** | Conversation state held in RAM inside an isolated microVM/container. One session = one process with its full conversation history in memory. Destroyed on idle timeout (15min) or max lifetime (8hr). No external storage involved. |
| **AgentCore Memory** | An async fact-extraction pipeline: when a session ends, an LLM processes the transcript to extract structured facts (SEMANTIC strategy) and summaries (SUMMARIZATION strategy). These are embedded into vectors and stored in a vector database indexed by `(actorId, namespace)`. Retrieval is semantic top-k approximate nearest-neighbor (ANN) search over the embeddings, triggered at the start of each new session for the same actor. |
| **Observability (automatic)** | The Runtime wraps your agent process with an OpenTelemetry SDK auto-instrumentation layer. Every LLM call, tool invocation, memory read/write, and HTTP request becomes a span in a trace. Spans carry `gen_ai.*` semantic conventions (prompt, completion, model, token counts). Exported to CloudWatch via an OTel collector sidecar, but the wire format is standard OTLP — could target Jaeger, Tempo, Datadog, or any OTel-compatible backend. |
| **`extract_user_id()` pattern** | Decode-without-verify: since the Runtime already validated the signature, agent code can safely `jwt.decode(token, verify_signature=False)` to read claims. This avoids the agent needing the JWKS — separation of concerns between auth validation (infrastructure) and identity extraction (application). |

## 3. Request Lifecycle (One Request, End-to-End)

1. **Client** sends HTTP POST to Runtime URL with `Authorization: Bearer <JWT>` and `session-id` header.
2. **Runtime auth layer** fetches JWKS from Cognito's discovery URL (cached), verifies JWT signature + expiry + audience. Rejects with 401 if invalid.
3. **Runtime dispatches** request to the agent's microVM for that session-id (creates one if new session).
4. **Agent code** (`invoke()`) extracts `Authorization` header, decodes JWT to get `username` claim → this becomes the `user_id` for memory.
5. **Memory retrieval**: session manager queries the vector store with `actorId=username`, retrieves top-k relevant facts, injects them as user context into the system prompt.
6. **LLM call**: Strands Agent sends the enriched prompt to Bedrock (Claude). Model decides which tool to call.
7. **Tool execution** (if Gateway tool): Agent's MCP client sends request to Gateway URL with `Authorization: Bearer <same JWT>`.
8. **Gateway auth layer** independently validates the JWT (same JWKS, same checks). Passes if valid.
9. **Gateway routes** the MCP tool call to the Lambda backend, invokes it, returns the result.
10. **LLM processes** tool result, generates final response.
11. **Response streams** back to client as server-sent events.
12. **OTel spans** for each step (2-10) are exported to CloudWatch asynchronously.
13. **Memory write** (async, after session ends): conversation events are queued; extraction pipeline processes them into durable facts.

## 4. Design Decisions and Trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **JWT (stateless) over session cookies** | No server-side session store needed; tokens are self-contained and verifiable by any component independently. Scales horizontally with zero shared state. | Session cookies + Redis/DB store — simpler for web-only apps but requires shared state and sticky sessions or a centralized store. |
| **Same JWT for Runtime + Gateway** | Single identity model; the user's identity provably flows end-to-end. Simpler than token exchange. | Separate service accounts per hop (Runtime uses its own IAM to call Gateway) — simpler setup but loses end-user identity at the tool layer. |
| **MicroVM isolation per session** | Strong tenant isolation; one user's session can't observe another's memory or crash their process. | Shared process with logical session isolation — cheaper but weaker isolation guarantees, risk of cross-tenant data leaks. |
| **Decode-without-verify in app code** | The infrastructure already validated; re-validating in Python adds latency and JWKS management complexity for zero security gain. | Full verification in app code — needed if the app receives tokens from an untrusted path. |
| **Gateway auth can't be updated in-place** | Likely an immutable-infrastructure design choice (the auth config is baked into the gateway resource). Forces explicit destroy/recreate. | Mutable config update — more convenient but risks inconsistent states during transitions. |

## 5. Portable Concepts vs AWS Lock-in

| Portable (works anywhere) | AWS-specific (re-implement elsewhere) |
|---|---|
| OIDC / JWT / JWKS validation | Cognito as the specific IdP (swap for Auth0/Keycloak/Okta) |
| OAuth2 flows (password grant, client_credentials) | Cognito User Pool API (`admin-create-user`, `initiate-auth`) |
| OpenTelemetry spans + OTLP export | CloudWatch as the trace/log backend (swap for Jaeger/Datadog) |
| MCP protocol for tool routing | AgentCore Gateway as the MCP proxy (build your own or use an MCP server framework) |
| Bearer token forwarding pattern | `requestHeaderAllowlist` config syntax |
| Vector embeddings + ANN retrieval for memory | AgentCore Memory's managed extraction pipeline |
| Session-per-microVM isolation model | AgentCore Runtime's specific microVM orchestration |
| PyJWT decode for claim extraction | N/A — fully portable |
| IaC (CDK) for deployment | AgentCore CLI / CDK constructs (swap for Terraform + your own infra) |

## 6. Build-it-yourself Sketch

```
FastAPI app + python-jose (JWT validation middleware against any OIDC provider's JWKS)
+ Firecracker/gVisor or Docker containers per session (isolation)
+ pgvector or Qdrant for memory storage (embeddings + ANN search)
+ Background worker (Celery/SQS) running an LLM to extract facts from ended sessions
+ A lightweight MCP server (FastMCP) as the tool proxy, with its own JWT middleware
+ OpenTelemetry Python SDK + OTel Collector → Jaeger/Tempo/Datadog
+ Terraform or Pulumi for deployment
+ Any OIDC provider (Keycloak self-hosted, Auth0, Firebase Auth)
```

## 7. Glossary

| Term | What it actually is |
|------|---------------------|
| **Custom JWT Authorizer** | A middleware that validates a signed JWT against a JWKS endpoint before passing the request to your code |
| **Discovery URL** | The OIDC `.well-known/openid-configuration` endpoint — a JSON doc listing the issuer, JWKS URI, supported grant types |
| **JWKS** | JSON Web Key Set — the public keys used to verify JWT signatures, rotated periodically by the IdP |
| **Allowed Clients** | Audience check — only tokens minted for these specific OAuth client IDs are accepted |
| **Bearer Token** | A signed JWT passed in the `Authorization: Bearer <token>` header — the standard way to authenticate API requests |
| **Session Persistence** | Conversation state in RAM, scoped to a session-id, destroyed when the microVM shuts down |
| **AgentCore Memory** | Async LLM-powered fact extraction + vector storage + semantic retrieval, indexed per user |
| **Token Propagation** | Forwarding the same auth header from one service to the next — enables end-to-end identity without re-authentication |
| **OpenTelemetry Auto-instrumentation** | A library that wraps your code to emit spans/traces automatically without manual `tracer.start_span()` calls |
| **requestHeaderAllowlist** | Config that tells the Runtime which incoming HTTP headers to pass through to your agent code (by default, custom headers are stripped) |

## 8. Operational Gotchas and Lessons

| Gotcha | Principle |
|--------|-----------|
| **SSM parameters didn't exist** — the "prerequisites" stack wasn't deployed. We had to create Cognito resources manually. | Never assume infrastructure exists just because a runbook says so. Verify before depending on it. Have a fallback creation path. |
| **`us-west-2` default region** — CreateUserPool tried the wrong region due to AWS CLI default config. | Always pass `--region` explicitly or set `AWS_DEFAULT_REGION` in every terminal session. Region mismatches are silent and devastating. |
| **Gateway auth is immutable** — can't update authorizer config in-place, must destroy and recreate. | Understand which resources are mutable vs immutable in your infrastructure. Plan for blue-green when configs can't be patched. |
| **Token expiry (60 min)** — long-running lab sessions will hit 401s. | Build token refresh into your client. In a real app, use refresh tokens; in testing, script the re-auth. |
| **New gateway = new env var name** — `AGENTCORE_GATEWAY_MY_GATEWAY_URL` became `AGENTCORE_GATEWAY_MY_GATEWAY_SECURE_URL`. | Naming conventions in injected env vars are derived from resource names. Changing a resource name is a breaking change for your code. |
| **Per-request MCP client** — gateway client must be created per-request (not at startup) because each request carries a different user's token. | When auth is per-user, anything that caches auth context (connection pools, clients) must be scoped to the request lifecycle. |
| **Cognito `username` is a UUID, not the email** — memory actor identity changed from "Sarah"/"Alex" to `44c8b4d8-...`. | The identity claim you extract determines how memory is partitioned. Switching identity systems means existing memory is orphaned under the old keys. |

## 9. So What (Production Lens)

This is the minimum viable security posture for a user-facing agent: stateless JWT auth that identifies individual users end-to-end without exposing cloud credentials, combined with zero-config observability that gives you the trace of every request from prompt to response. In production, you'd add token refresh flows, rate limiting per user identity, and alert rules on the OTel metrics — but the foundation (identity propagation + full tracing) is what makes those possible.
