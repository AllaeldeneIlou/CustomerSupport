# Lab 9 Report: Optimizing Agent Quality

## 1. What We Built

A closed-loop optimization system that analyzes real agent traces to generate concrete improvements (system prompt rules, sharper tool descriptions), packages them as immutable config bundles, and validates them against the baseline via a controlled A/B test on live traffic — all without redeploying agent code.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **Recommendation (system-prompt)** | An LLM-as-optimizer that reads OTel traces + eval scores, clusters sessions by outcome (pass/fail), diffs behavior patterns between clusters, and generates additive prompt rules targeting the failure modes. Input: spans + reward signal. Output: rewritten prompt text. Analogous to DSPy's prompt optimization or manual prompt-iteration — but automated with trace-grounded evidence. |
| **Recommendation (tool-description)** | Same pattern but focused on tool-selection confusion. Analyzes which tool was called, what parameters were passed, and where calls failed or returned empty. Generates more specific parameter docs, failure-recovery instructions, and parallel-call guidance. No evaluator needed — confusion is inferred from the traces themselves. |
| **Config Bundle** | A versioned, immutable key-value snapshot (like a Git commit for configuration). The runtime reads the active bundle at invocation time via an SDK call. Equivalent to: a row in a config table with a version UUID, or a tagged config artifact in a feature-flag system (LaunchDarkly, Split.io). |
| **A/B Test** | A traffic splitter at the reverse-proxy layer (Gateway) that assigns each new session ID to a variant (sticky assignment via deterministic hash or random draw). Both variants hit the same runtime binary — only the injected config differs. The online eval scores both groups independently, then a statistical test (two-sample t-test or proportion test) determines significance. Standard controlled experiment infrastructure. |
| **`BeforeModelCallEvent` hook** | A lifecycle callback in the agent framework that fires before each LLM API call. Used here to swap the system prompt based on the active config bundle — runtime polymorphism without code changes. Same concept as middleware/interceptors in web frameworks. |
| **http-runtime Gateway target** | The Gateway calls a runtime endpoint over HTTP with SigV4 (AWS request signing). The target is just a reverse-proxy route entry: path prefix → backend URL + auth method. Equivalent to an nginx `location` block with an auth module. |
| **Policy Engine LOG_ONLY mode** | The Cedar authorization layer evaluates policies but doesn't enforce them — decisions are logged for auditing. Standard "dry-run" / "shadow mode" pattern in policy engines (OPA, Cedar, Istio AuthorizationPolicy). |

## 3. Request Lifecycle (A/B Test Path)

1. **Client** sends POST to Gateway URL with Cognito JWT in `Authorization` header and a unique session ID.
2. **Gateway** validates the JWT (signature + issuer + audience via JWKS).
3. **Gateway A/B splitter** checks if this session ID has a prior variant assignment; if not, assigns based on weights (80/20). Records assignment.
4. **Gateway** injects the assigned config bundle (control or treatment) into the request context and forwards to `CustomerSupportAB` runtime via SigV4.
5. **Runtime** receives the request. The `BeforeModelCallEvent` hook fires, reads the injected config bundle via `BedrockAgentCoreContext.get_config_bundle()`, and sets the system prompt.
6. **Agent** calls Bedrock (Claude) with the variant-specific system prompt + user message + tool definitions.
7. **LLM** reasons, optionally calls tools (`get_product_info`, `get_return_policy`), agent executes them locally, returns tool results, LLM produces final response.
8. **Response** flows back through Gateway to client.
9. **Async:** OTel spans are exported to CloudWatch. The `ABQualityMonitor` online eval picks up the trace, runs `GoalSuccessRate` (LLM-as-judge), and records the score tagged with the variant.
10. **Later:** The A/B test service aggregates scores per variant and computes statistical significance.

## 4. Design Decisions & Trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| Separate A/B runtime (IAM auth) | Production runtime is JWT-locked; Gateway uses SigV4 internally. Avoids weakening prod security. | Add JWT-passthrough to Gateway (not available in preview), or use a single runtime with dual auth. |
| Config bundles vs. code deploy per variant | Faster iteration (seconds vs. minutes), immutable versioning, no cold-start penalty for new deploys. | Deploy two separate runtimes (wasteful), or use env vars (not immutable/versioned). |
| 80/20 split | Conservative — limits blast radius of untested treatment while still collecting data. | 50/50 for faster significance, but more risk if treatment is bad. |
| LLM-as-judge for scoring | No human labeling needed, scales to all traffic, consistent criteria. | Human evaluation (gold standard but doesn't scale), rule-based heuristics (brittle). |
| Additive prompt optimization | Preserves original intent, only patches gaps. Less likely to regress working cases. | Full prompt rewrite (higher ceiling but higher risk of regression). |
| Session-sticky assignment | User gets consistent experience within a conversation. | Request-level randomization (more samples, but incoherent UX). |

## 5. Portable Concepts vs. AWS Lock-in

| Portable (any stack) | AWS-specific convenience |
|---------------------|--------------------------|
| LLM-as-optimizer analyzing traces → prompt improvements (DSPy, TextGrad, manual) | Managed recommendation service with integrated trace access |
| Config versioning + runtime injection (feature flags, config servers) | Config Bundle API with built-in versioning and runtime SDK integration |
| Traffic splitting at reverse proxy (Istio, Envoy, nginx, LaunchDarkly) | Gateway-level A/B with session-sticky assignment and auto-scoring |
| Statistical significance testing (scipy, statsmodels, any A/B platform) | Integrated `evaluatorMetrics` with p-value and confidence intervals |
| OTel traces as optimization input | CloudWatch Transaction Search as the trace query layer |
| LLM-as-judge evaluation (any model, any framework) | Managed online eval with `Builtin.GoalSuccessRate` |
| Lifecycle hooks / middleware for dynamic config | `BeforeModelCallEvent` in Strands SDK |
| Immutable deployments + canary patterns | AgentCore deploy + promote workflow |

## 6. Build-it-yourself Sketch

```
1. Trace store:       OTel Collector → Jaeger/Tempo + SQL index
2. Optimizer:         Python script: query traces by eval score, cluster failures,
                      feed to Claude/GPT with "generate prompt rules" meta-prompt
3. Config store:      Postgres table (bundle_id, version_uuid, config_json, created_at)
4. A/B splitter:      Envoy/nginx with consistent-hash routing on session-id header
5. Runtime:           FastAPI app reading config from Postgres at request time
6. Eval pipeline:     Async worker consuming from trace queue, calling LLM-as-judge,
                      writing scores to a metrics table partitioned by variant
7. Stats:             Cron job running scipy.stats.ttest_ind on variant scores,
                      alerting when p < 0.05
8. Dashboard:         Grafana panel showing variant means + CI over time
```

## 7. Glossary

| Term | What it actually is |
|------|-------------------|
| **Recommendation** | An LLM-generated improvement to a prompt or tool description, derived from analyzing real traces against an eval signal |
| **Config Bundle** | A versioned, immutable JSON snapshot of agent configuration (prompts, parameters) that can be swapped at runtime without redeploying code |
| **A/B Test** | A controlled experiment: traffic splitter + variant assignment + statistical comparison of outcomes |
| **Control** | The baseline variant (current prompt) |
| **Treatment** | The experimental variant (optimized prompt) |
| **Config-bundle mode** | A/B testing by swapping config (vs. routing to different code) — same binary, different parameters |
| **http-runtime target** | A Gateway route that forwards to a runtime endpoint using SigV4 auth |
| **BeforeModelCallEvent** | A lifecycle hook that fires before each LLM inference call — used to inject dynamic configuration |
| **LOG_ONLY mode** | Policy engine dry-run: evaluate and log decisions without blocking requests |
| **Statistical significance** | p-value < 0.05 means <5% chance the observed difference is due to random noise |
| **Lookback window** | How many days of traces the recommendation engine analyzes |
| **Session-sticky assignment** | Once a session gets variant C or T1, all messages in that session stay on that variant |

## 8. Operational Gotchas & Lessons

| Gotcha | Principle |
|--------|-----------|
| **409 on recommendation names** — names are unique per account, not per project. Must archive/use different names on re-runs. | Idempotency keys should be scoped appropriately; when they're not, you need cleanup commands. |
| **JWT vs SigV4 mismatch** — Gateway uses IAM/SigV4 to call targets, but production runtime only accepts JWT. Solution: separate A/B runtime with IAM auth. | Auth boundaries between internal services and external clients are different concerns; don't collapse them. |
| **`view recommendation` returns most recent, not by type** — must pass the specific ID to get the system-prompt recommendation when tool-description exists too. | CLI "convenience" defaults can bite you in multi-resource scenarios; always use explicit IDs. |
| **Cedar ENFORCE blocks new targets** — no permit policy = implicit deny. Must switch to LOG_ONLY or add a permit before routing traffic to new targets. | Default-deny authorization requires explicit allows for every new path; plan for this when adding targets. |
| **Eval results take 10–15 minutes** — async scoring pipeline has inherent latency. Not a bug, it's the trade-off of non-blocking evaluation. | Async scoring doesn't affect user latency but means you can't get instant experiment results. |
| **Config bundle key errors** — the `{{runtime:X}}` placeholder resolves at deploy time; get it wrong and the bundle deploys but the runtime can't read its config. | Template resolution happens at a different phase than validation; test the resolved output. |
| **Policy engine field name changed across CLI versions** — `policyEngine` vs `policyEngineConfiguration`. Always check the CLI's own validation messages. | Schema evolution in preview services means you can't rely on yesterday's field names. |

## 9. So What (Production Lens)

This is the pattern for continuous improvement without guesswork: real traces → automated analysis → concrete hypothesis → controlled validation → statistical promotion. In production, you'd run this as a recurring loop — each winner becomes the new baseline, and fresh traces from the winner feed the next round of recommendations. The key insight is that prompt engineering becomes a data-driven optimization problem rather than artisanal craft.
