# Lab 5 Report: Evaluating Agent Quality

## 1. What We Built

We added continuous quality monitoring to a deployed agent without touching any agent code. A separate evaluation pipeline subscribes to the agent's trace stream and runs three LLM judges (goal success, factual correctness, tool selection accuracy) against every interaction. Results flow to CloudWatch for trend tracking and are queryable on-demand for point-in-time snapshots. Our agent scored 83% goal success and 84% correctness across 12 sessions.

## 2. Under the Hood (De-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Online Evaluation** | An event-driven pipeline: a sampling filter sits on the trace stream; selected traces are fanned out to N judge prompts (one per evaluator dimension); each judge is an LLM call with a structured scoring rubric baked into its system prompt; scores are written to a time-series metrics store. |
| **Built-in Evaluators (GoalSuccessRate, Correctness, ToolSelectionAccuracy)** | Pre-authored LLM-as-judge prompt templates. Each template receives the full conversation trace (user messages, assistant messages, tool calls, tool responses) as context, then asks a specific question ("Did the user's goal get resolved? Score 0 or 1 and explain."). The judge model is separate from the agent model — it's a second LLM call purely for assessment. |
| **On-demand Evaluation** | Batch mode: query the trace store for sessions in a time window, run the same judge prompts over all of them, aggregate scores. Same mechanism as online eval but triggered manually instead of event-driven. |
| **Sampling Rate** | A probabilistic filter (Bernoulli sampling) on the trace ingestion path. At 100% every trace is evaluated; at 10% roughly 1-in-10 are randomly selected. Controls cost since each evaluation is itself an LLM inference. |
| **Eval Results (CloudWatch / JSON)** | Time-series metrics (evaluator name → score → timestamp) stored in a metrics backend. Enables dashboarding, alerting (score drops below threshold → page), and trend analysis over deployments. |

**How LLM-as-judge works mechanically:**
1. Trace is serialized into a structured document (messages + tool calls + tool results)
2. A prompt template wraps it: "Given this conversation, did the assistant achieve the user's goal? Answer Yes/No and explain."
3. The judge LLM responds with a structured output (score + explanation)
4. The score (0 or 1, or continuous 0-1) is extracted and stored as a metric
5. The explanation is stored alongside for debugging

This is fundamentally the same pattern as using an LLM for classification — the input is a conversation trace, the output is a quality label.

## 3. Request Lifecycle (End-to-End with Evaluation)

1. **Client** sends authenticated request (JWT in Authorization header) to runtime endpoint
2. **API Gateway** validates JWT signature against JWKS from Cognito's OIDC discovery URL; extracts claims (sub, client_id)
3. **Runtime** (microVM) receives request, loads agent code, extracts user_id from JWT claims
4. **Memory retrieval** — agent queries semantic memory store (embeddings + ANN search) for relevant prior context by actor_id
5. **LLM inference** — agent sends system prompt + user message + memory context + tool definitions to Bedrock model
6. **Tool execution** — if model requests tools, agent calls local functions (get_product_info, get_return_policy) or routes through Gateway (check_warranty → Lambda) with auth header forwarding
7. **LLM continuation** — tool results fed back to model for final response generation
8. **Response streams** back to client; memory extraction pipeline asynchronously processes the turn
9. **Trace emission** — full conversation (all messages, tool calls, tool results, latencies) is exported as OpenTelemetry spans to the observability backend
10. **Evaluation sampling** — the online eval config's sampling filter (100%) selects this trace
11. **Judge fan-out** — trace is sent to 3 separate judge LLM calls in parallel (GoalSuccessRate, Correctness, ToolSelectionAccuracy)
12. **Score storage** — each judge returns a 0-1 score + explanation; scores are written to CloudWatch metrics; explanations stored in eval results

Steps 1-8 happen synchronously (user waits). Steps 9-12 happen asynchronously (user never sees them).

## 4. Design Decisions & Trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **LLM-as-judge instead of deterministic tests** | Agent outputs are non-deterministic and natural language — you can't write exact assertions. A judge model can assess semantic quality. | Deterministic: regex/keyword matching (brittle, high false-positive), human review (expensive, doesn't scale), embedding similarity to gold answers (misses nuance). |
| **Multiple orthogonal evaluators** | Different failure modes are invisible to a single judge. GoalSuccessRate misses hallucinations; Correctness misses "correct but unhelpful." | Single composite score (loses diagnostic power — you can't tell what to fix). |
| **Sampling instead of 100% in production** | Each evaluation is an LLM inference (~same cost as one agent turn). At high traffic, evaluating everything doubles your LLM spend. | Evaluate everything (expensive but complete), evaluate only flagged sessions (selection bias — you miss unknown-unknown failures). |
| **Async evaluation (not inline)** | Adding judge calls to the request path would add seconds of latency to every user response. | Inline/synchronous (enables real-time blocking of bad responses, but doubles latency and cost on the critical path). |
| **Built-in evaluator prompts** | Standardized, tested prompts reduce setup time and enable cross-agent comparison. | Custom prompts only (more control, but requires prompt engineering expertise and testing the evaluator itself). |
| **Judge model separate from agent model** | Avoids self-grading bias (a model is poor at judging its own outputs). The judge can be a different, potentially stronger model. | Same model (cheaper but less reliable — systematic blind spots propagate to evaluation). |

## 5. Portable Concepts vs AWS Lock-in

| Portable (works anywhere) | AWS-specific convenience |
|---------------------------|--------------------------|
| LLM-as-judge pattern (any model can judge any other model's outputs) | AgentCore's built-in evaluator library and managed pipeline |
| Structured scoring rubrics as prompt templates | `agentcore add online-eval` CLI scaffolding |
| Trace-based evaluation (score recorded conversations) | Automatic trace subscription and fan-out infrastructure |
| Sampling strategies (Bernoulli, stratified) | `--sampling-rate` config wiring |
| Time-series metrics for quality trends | CloudWatch integration and GenAI Observability dashboard |
| On-demand batch evaluation over historical data | `agentcore run eval` with trace store query |
| OpenTelemetry trace format | Bedrock-specific trace attributes and namespaces |
| Separation of judge model from agent model | Managed judge model selection |

## 6. Build-It-Yourself Sketch

```
Agent traces → OpenTelemetry Collector → trace store (Jaeger/Tempo/Postgres)
                                              ↓
Evaluation service (cron or event-driven):
  - Query traces from store (last N hours, sampled)
  - For each trace, format into judge prompt template
  - Call judge LLM (OpenAI/Anthropic/local model) with rubric
  - Parse structured score + explanation from response
  - Write scores to Prometheus/InfluxDB/Postgres
                                              ↓
Grafana dashboard: time-series of scores per evaluator
Alert rule: score < 0.7 for 1h → PagerDuty/Slack notification
```

Tools: FastAPI eval service, any LLM API, any time-series DB, Grafana. ~200 lines of Python for the core loop.

## 7. Glossary

| Term | What It Actually Is |
|------|---------------------|
| **Online Evaluation** | Event-driven pipeline that scores live traces asynchronously using LLM judges |
| **On-demand Evaluation** | Batch scoring of historical traces triggered manually |
| **Built-in Evaluator** | A pre-written LLM prompt template that asks a specific quality question about a conversation |
| **GoalSuccessRate** | Judge prompt: "Did the user's stated goal get resolved in this conversation?" → binary 0/1 |
| **Correctness** | Judge prompt: "Is the information in the assistant's response factually accurate given the tool outputs?" → 0/0.5/1 |
| **ToolSelectionAccuracy** | Judge prompt: "Given the user's intent, did the assistant call the appropriate tools?" → binary 0/1 |
| **Sampling Rate** | Percentage of traces randomly selected for evaluation (cost control knob) |
| **LLM-as-Judge** | Using a language model to evaluate another language model's outputs by scoring against a rubric |
| **Evaluator Explanation** | Free-text rationale the judge model produces alongside its score — the "why" for debugging |

## 8. Operational Gotchas & Lessons

| Gotcha | Principle |
|--------|-----------|
| **Judge counts "ask for clarification" as goal failure** — our agent correctly asked for more info on an ambiguous query, but GoalSuccessRate scored it 0 because the outcome wasn't achieved. | Evaluators measure outcomes, not intent. If your agent design includes clarification flows, you need a custom evaluator that accounts for "appropriate clarification" as a valid outcome. |
| **Hallucination passes GoalSuccessRate but fails Correctness** — the agent invented account context, but the user's goal was still "met." | Single evaluators have blind spots. Always use orthogonal evaluators that cover different failure axes. |
| **100% sampling = 2x LLM cost** — every interaction spawns 3 additional judge calls. | In production, sample 10-20%. Use 100% only during development or after prompt changes to validate improvements quickly. |
| **Eval results take minutes, not seconds** — traces must be indexed before they can be evaluated; judge calls add latency. | Don't expect real-time feedback. Design your workflow around async results (dashboards, daily reports, alerts on drops). |
| **The judge model can be wrong** — it's an LLM with its own biases and limitations. | Spot-check judge explanations regularly. If the judge systematically misjudges a pattern (like clarification questions), fix the evaluator prompt or write a custom one. |
| **On-demand eval CLI may timeout or error on older CLI versions** | Known issue — fall back to `agentcore evals history` or CloudWatch dashboard. The mechanism is the same; only the trigger differs. |

## 9. So What (Production Lens)

Online evaluation is your continuous regression detector for AI quality — it catches the failures that unit tests and type systems physically cannot (hallucinations, goal misses, wrong tool choices). In production, you'd set alerts on score drops (e.g., GoalSuccessRate < 0.75 for 1h → investigate), use it as a gate for prompt/model changes (A/B compare scores before and after), and build custom evaluators for business-specific criteria (e.g., "Did the agent follow the escalation policy?" or "Did it stay within approved discount limits?").
