# Lab 6 Report: Building the Customer Interface

## 1. What we built

A Flask web server that authenticates against an OIDC-compliant identity provider (Cognito) on each page load, then serves a single-page chat UI. The browser becomes the direct REST client to the agent runtime — the server is only a token broker, not a message relay. The user types a message, the browser POSTs it with a Bearer JWT to the runtime's invocation endpoint, and the response streams back as Server-Sent Events.

## 2. Under the hood (de-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **Cognito USER_PASSWORD_AUTH** | OAuth2 Resource Owner Password Credentials (ROPC) grant — client sends username+password directly to the token endpoint, gets back a signed JWT (access token). Stateless: the IdP issues the token, never sees subsequent API calls. |
| **AgentCore Runtime invocation endpoint** | A managed HTTP endpoint (REST) fronting an isolated compute unit (container/microVM) that hosts your agent process. Accepts `POST /runtimes/{arn}/invocations`, validates the Bearer token's signature against the IdP's JWKS, then routes the request to your agent code. Response is SSE (chunked `data:` lines). |
| **X-Amzn-Bedrock-AgentCore-Runtime-Session-Id** | A server-side session key — a UUID that the runtime uses to group turns into a conversation. Internally maps to a context window or message buffer scoped to that ID. New UUID = fresh context. |
| **SSM Parameter Store** | A key-value config store. Used here to decouple the client ID from the code — the Flask server reads `/app/.../web_client_id` at runtime instead of hardcoding. Generic equivalent: any secrets/config manager (Vault, .env, Consul KV). |
| **deployed-state.json** | A local IaC state file (like Terraform's `.tfstate`) that records the ARN of the deployed runtime. The Flask server reads it to know where to point the browser. |

## 3. Request lifecycle

1. **Browser requests `/`** → Flask's `index()` handler fires.
2. **Flask → Cognito token endpoint** — sends `InitiateAuth(USER_PASSWORD_AUTH)` with client_id + username + password. Cognito validates credentials, returns a signed JWT (RS256, 60-min TTL).
3. **Flask → SSM** — fetches `web_client_id` param (and reads `deployed-state.json` for the runtime ARN).
4. **Flask renders HTML** — injects `TOKEN`, `RUNTIME_ARN`, `ENDPOINT` as JS variables into the Jinja2 template. Sends to browser with no-cache headers.
5. **User types message** → JS builds a `fetch()` POST to `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{url-encoded-arn}/invocations?qualifier=DEFAULT` with headers: `Authorization: Bearer <JWT>`, `Content-Type: application/json`, `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <uuid>`.
6. **AgentCore API gateway** validates the JWT: fetches JWKS from the Cognito OIDC discovery URL, verifies RS256 signature, checks `exp`, `iss`, `client_id` claims.
7. **AgentCore routes to runtime** — the agent process receives the prompt, executes tool calls (local tools, Gateway→Lambda for warranty), reads/writes long-term memory, calls the LLM.
8. **Streaming response** — runtime emits SSE `data:` lines back through the API gateway to the browser. JS accumulates chunks and renders the final message.

## 4. Design decisions & trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **Token injected server-side, browser calls API directly** | Eliminates a message proxy, reduces latency, simplifies server to stateless token broker. | Backend-for-frontend (BFF) proxy — server relays all messages. Adds latency but hides the token from JS (more secure against XSS). |
| **ROPC grant (username+password)** | Simplest for a workshop — no redirect flow needed. | Authorization Code + PKCE — standard for browser apps in production. Requires a redirect to the IdP login page. |
| **Session ID as a random UUID per browser tab** | Gives conversational continuity without server-side session stores. Client controls session lifecycle (New Session = new UUID). | Server-managed sessions with cookies — more control but adds state. |
| **No WebSocket** | SSE over a single HTTP response is simpler for request-response agents. Works through CDN/proxy without upgrade negotiation. | WebSocket — better for bidirectional streaming (typing indicators, server-push notifications), but needs sticky routing. |
| **No-cache headers everywhere** | Workshop's CloudFront proxy was caching stale pages. Aggressive cache-busting ensures iteration speed. | ETags / short TTL — better for production where you want CDN benefits but controlled invalidation. |

## 5. Portable concepts vs AWS lock-in

| Portable (works anywhere) | AWS-specific (re-implement elsewhere) |
|---------------------------|---------------------------------------|
| OIDC/JWT authentication (any IdP: Auth0, Keycloak, Okta) | Cognito as the specific IdP + its `initiate_auth` API shape |
| Bearer token in Authorization header | AgentCore's specific `/runtimes/{arn}/invocations` endpoint URL structure |
| SSE streaming (standard HTTP) | The `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header convention |
| Flask / any WSGI framework as token broker | SSM Parameter Store for config (swap for Vault, env vars, etc.) |
| Session ID as client-generated UUID | `deployed-state.json` format (IaC state is always tool-specific) |
| REST + JSON for agent invocation | AgentCore's managed runtime hosting + auto-scaling |
| Single-page app calling an API | — |

## 6. Build-it-yourself sketch

```
FastAPI server (token broker):
  - Authenticates against any OIDC IdP (Keycloak, Auth0) via requests/httpx
  - Serves Jinja2 template with token + agent URL injected

Agent backend:
  - FastAPI/Starlette app with StreamingResponse (SSE)
  - JWT validation via python-jose + JWKS fetched from IdP
  - Session context stored in Redis (keyed by session UUID)
  - Strands/LangGraph agent behind the endpoint
  - Deployed on Fly.io / Railway / ECS / k8s

Frontend:
  - Same vanilla HTML/JS (or React) calling the agent endpoint
  - fetch() + ReadableStream for SSE parsing
```

## 7. Glossary

| Term | What it actually is |
|------|---------------------|
| **USER_PASSWORD_AUTH** | OAuth2 ROPC grant — direct credential exchange for tokens, no browser redirect |
| **Access Token** | A signed JWT (RS256) with claims (sub, exp, iss, client_id) — proof of identity, stateless |
| **JWKS** | JSON Web Key Set — the public keys the API uses to verify JWT signatures without calling the IdP |
| **SSE (Server-Sent Events)** | HTTP chunked response where each chunk is prefixed with `data: ` — one-way streaming from server to client |
| **Session ID** | A client-generated UUID sent as a header; the runtime uses it to group turns into a single conversation context |
| **Token broker** | A server-side component whose only job is acquiring a token on behalf of the client and handing it off |
| **deployed-state.json** | Local IaC state file recording what's deployed and its ARN — read-only reference for the frontend |

## 8. Operational gotchas & lessons

| Gotcha | Principle |
|--------|-----------|
| **SSM parameters pointed to wrong Cognito pool** | Config indirection (SSM/Vault) is only as good as the data in it. Always verify the chain: param → pool → user exists. |
| **`boto3.session.Session().region_name` returns None** | Environment variables don't cross process boundaries automatically. Always set `AWS_REGION` explicitly in subprocess launches. |
| **Cognito auth flows must be explicitly enabled** | Least-privilege by default — even the IdP won't allow an auth flow you haven't allowlisted on the client. |
| **Token TTL is 60 minutes** | JWTs are stateless = no server-side revocation. Short TTL is the safety valve. Page refresh re-acquires a fresh token via Flask. |
| **CloudFront caches pages aggressively** | CDN proxies cache by default. No-cache headers + cache-busting query params (`?v=2`) are needed during development behind a CDN. |

## 9. So what (production lens)

This lab demonstrates the minimum viable user-facing layer: a stateless token broker + direct browser→API calls. In production you'd swap ROPC for Authorization Code + PKCE (so users log in via redirect, not hardcoded credentials), add a refresh-token rotation loop in JS, and potentially move to a BFF proxy to keep tokens out of the browser entirely — but the streaming REST + SSE pattern for agent interaction remains the same.
