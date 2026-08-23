# Lab 7 Report: Governing Agent Actions with Policies

## 1. What we built

We added a deterministic authorization layer to the agent's Gateway using Cedar policies. A new refund tool (Lambda) was exposed through the Gateway, then locked down with three rules: refunds must be under $100, the reason must contain "defective", and warranty checks are explicitly permitted. Zero lines of agent code changed — governance is enforced at the infrastructure boundary before requests reach the backend.

## 2. Under the hood (de-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Policy Engine** | A stateless policy evaluation service — receives a structured authorization request (principal, action, resource, context) and returns allow/deny by evaluating a set of declarative rules. Conceptually identical to OPA (Open Policy Agent) evaluating Rego, or Cedar's open-source engine evaluating Cedar policies. No LLM involved at evaluation time — pure deterministic logic. |
| **Cedar Policy** | A declarative authorization rule in the Cedar language (open-source, created by AWS's Automated Reasoning Group). Each policy is a `permit` or `forbid` statement with optional conditions (`when`/`unless`). The language is designed for formal verification — you can prove properties like "no policy ever permits action X" statically. Evaluation semantics: collect all applicable policies; if ANY `forbid` matches, deny (forbid wins). If no forbid but at least one `permit` matches, allow. Otherwise, default deny. |
| **ENFORCE mode** | The policy evaluation result is authoritative — denied requests return an error to the caller (the agent). In LOG_ONLY mode, the evaluation still runs but the request proceeds regardless; decisions are logged for auditing. This is the standard "shadow mode → enforce mode" rollout pattern for authorization systems. |
| **Default Deny** | The closed-world assumption: if no policy explicitly permits an action, it's denied. This is the standard in RBAC/ABAC systems (AWS IAM, Kubernetes RBAC, OPA). The alternative (default allow) requires explicit deny for every disallowed action — far more dangerous. |
| **Gateway + Policy integration** | The Gateway acts as a reverse-proxy/tool-router. With a policy engine attached, it performs an authorization check *before* forwarding the tool call to the backend Lambda. The sequence: (1) receive tool call from agent, (2) extract context (tool name, input parameters, caller identity from JWT), (3) evaluate Cedar policies, (4) if permitted → forward to Lambda; if denied → return error to agent. |
| **`--generate` (NL to Cedar)** | An LLM-assisted authoring step — translates English to Cedar syntax. The LLM is used at policy *creation* time only, never at evaluation time. The generated Cedar is validated against the tool schema before acceptance. Blocked in our environment by an SCP (organization-level permission boundary). |

### How Cedar evaluation actually works

```
Input to policy engine:
{
  principal: <caller identity from JWT>,
  action: "ProcessRefund___process_refund",
  resource: "<gateway-arn>",
  context: {
    input: { order_id: "ORD-12345", amount: 50, reason: "item was defective" }
  }
}

Evaluation:
1. Collect all policies whose (principal, action, resource) pattern matches
2. For each matching forbid: evaluate condition → if ANY forbid matches, DENY
3. For each matching permit: evaluate condition → if at least one permit matches, ALLOW
4. No matches → DEFAULT DENY
```

The `context.input` is populated from the tool call's input parameters (mapped via the JSON Schema in the tool schema file). This is why `"type": "integer"` matters — it maps to Cedar's `Long` type, enabling `< 100` comparisons directly.

## 3. Request lifecycle (refund attempt, $50, reason "defective")

1. **User** types "refund $50 for ORD-12345, item was defective" in chat UI
2. **Browser** sends REST request to AgentCore Runtime with JWT `Authorization` header
3. **Runtime** (agent code) receives prompt, calls LLM (Bedrock)
4. **LLM** decides to call `process_refund` tool with `{order_id: "ORD-12345", amount: 50, reason: "item was defective"}`
5. **Agent code** forwards tool call to Gateway MCP client (passes `Authorization` header)
6. **Gateway** receives tool call, extracts caller identity from JWT
7. **Gateway** constructs authorization request: principal=caller, action=`ProcessRefund___process_refund`, resource=gateway-arn, context.input={amount:50, reason:"item was defective"}
8. **Policy Engine** evaluates:
   - `refund_reason_policy` (forbid...unless reason like "\*defective\*") → reason contains "defective" → forbid does NOT apply
   - `refund_limit_policy` (permit...when amount < 100) → 50 < 100 → PERMIT matches
   - Result: **ALLOW**
9. **Gateway** forwards request to `workshop-process-refund` Lambda
10. **Lambda** processes refund, returns success
11. **Gateway** returns tool result to agent
12. **Agent/LLM** formulates response: "Your refund of $50 has been processed"
13. **Runtime** streams response back to browser

For a $500 refund: step 8 would find `refund_limit_policy` permit condition fails (500 >= 100), no permit matches → DEFAULT DENY. Gateway returns error at step 9. Agent receives "authorization denied" and tells user to contact support.

## 4. Design decisions & trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **Policy at Gateway, not in agent code** | Tamper-proof — prompt injection can't bypass infrastructure-level rules. No agent code changes needed for new rules. | Embedding rules in system prompt ("never refund > $100") — fragile, bypassable, no audit trail. |
| **Cedar (declarative) over imperative code** | Formally verifiable, auditable, hot-swappable without redeploy. Separation of concerns: security team writes policies, dev team writes agent logic. | Custom middleware in Lambda or agent code — flexible but no formal guarantees, harder to audit. |
| **Default deny** | Fail-safe: forgetting a policy blocks access rather than granting it. Forces explicit enumeration of allowed actions. | Default allow — faster to get started but catastrophic on oversight. |
| **forbid overrides permit** | Enables "hard stops" — a compliance team can add a forbid that no amount of permit rules can override. Clear precedence prevents conflicting-rule confusion. | Priority-based evaluation (like firewall rules) — more flexible but harder to reason about at scale. |
| **ENFORCE vs LOG_ONLY as a mode** | Allows shadow-mode testing of policies before enforcing. Standard blue/green pattern for authorization changes. | Only offering enforce — risky for complex policy sets, no safe testing path. |
| **Tool input in context** | Enables fine-grained attribute-based access control (ABAC) — policies can inspect actual request parameters. | Binary permit/deny per tool — no conditional logic, much less useful. |

## 5. Portable concepts vs AWS lock-in

| Portable (transfers to any stack) | AWS-specific convenience |
|-----------------------------------|--------------------------|
| Cedar language itself (open-source, runs anywhere) | Managed Policy Engine service (evaluation hosting, CloudWatch logging) |
| ABAC pattern: evaluate policies against request context | `agentcore add policy` CLI with NL generation |
| Default-deny + forbid-overrides-permit semantics | Integration with Gateway (auto-injects context from tool schema) |
| Policy-as-code in version control | CDK deployment of policies as CloudFormation resources |
| Separation of authZ from business logic | Automatic action naming convention (`Target___tool`) |
| JWT claims as principal attributes | Cognito + Gateway JWT integration |
| Shadow mode → enforce mode rollout pattern | `ENFORCE` / `LOG_ONLY` mode toggle on attachment |

## 6. Build-it-yourself sketch

```
- FastAPI or Express reverse-proxy (replaces Gateway)
- cedar-policy Rust crate or cedarpy Python bindings (open-source Cedar engine)
- Policy files in git, loaded at proxy startup
- Extract JWT claims + tool input → construct Cedar authorization request
- Evaluate locally (sub-millisecond) → allow/deny before forwarding to backend
- Audit log: structured JSON to stdout → any log aggregator (ELK, Datadog, CloudWatch)
- Hot-reload: watch policy files, re-parse on change (no redeploy needed)
- Optional: OPA/Rego instead of Cedar if team already knows it
```

## 7. Glossary

| Term | What it actually is |
|------|---------------------|
| **Policy Engine** | A stateless rule evaluator that takes (principal, action, resource, context) and returns allow/deny |
| **Cedar** | An open-source declarative authorization language with formal verification properties |
| **ENFORCE mode** | Policy decisions are binding — denied = blocked |
| **LOG_ONLY mode** | Shadow mode — decisions logged but not enforced |
| **Default Deny** | Closed-world assumption: no matching permit → deny |
| **forbid...unless** | "Block this action UNLESS condition is true" — takes precedence over any permit |
| **Action naming** | `TargetName___tool_name` (triple underscore) — identifies which tool on which gateway target |
| **context.input** | The tool call's input parameters, available for conditional evaluation in policies |
| **ABAC** | Attribute-Based Access Control — decisions based on attributes of the request, not just identity |
| **SCP** | Service Control Policy — organization-level permission boundary that overrides account IAM |

## 8. Operational gotchas & lessons

| Gotcha | Principle |
|--------|-----------|
| **Missing semicolon in Cedar** → deploy fails with cryptic "unexpected end of input" | Cedar is a strict grammar — linting/validation before deploy is essential. Always terminate statements with `;` |
| **Default deny breaks existing tools** when policy engine is attached | Adding authZ retroactively requires enumerating ALL existing allowed actions, not just new ones. Plan the permit list before flipping to ENFORCE. |
| **`--generate` hit an SCP deny** and silently targeted us-west-2 | Organization-level controls override everything. Always check region defaults. LLM-generated policies are a convenience, not a requirement — you can always write Cedar directly. |
| **forbid overrides permit** means policy order doesn't matter but composition does | Adding a new `forbid` can break previously-working flows. Test with LOG_ONLY first. Think of forbid as a circuit breaker. |
| **Integer vs number in JSON Schema** determines Cedar type (`Long` vs `Decimal`) | Schema design decisions propagate into policy syntax. Get the schema right first; policies are built on top of it. |

## 9. So what (production lens)

Policy-at-the-boundary is how you ship an LLM agent that handles money, PII, or destructive actions without relying on prompt engineering for safety. In production, this is your compliance layer — auditable, formally verifiable, and immune to prompt injection — sitting between the unpredictable LLM and the consequential backends.
