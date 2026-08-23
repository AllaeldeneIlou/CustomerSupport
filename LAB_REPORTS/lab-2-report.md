# Lab 2 Report — Add Memory to Your Agent

## 1. What we built

We added cross-session memory to the customer support agent so it can remember facts about a user (name, preferences, purchase history) even when a brand-new session is started days later. Two extraction strategies run asynchronously after each conversation: one pulls discrete facts (SEMANTIC), the other compresses the full conversation into a summary (SUMMARIZATION). On the next invocation, relevant memories are retrieved and injected into the LLM's context before it responds.

## 2. Under the hood (de-branded)

| AWS Name | Generic Mechanism |
|----------|-------------------|
| **AgentCore Memory** | An async extraction pipeline + a vector/document store. After each conversation turn, an LLM pass extracts structured facts or summaries from raw events, embeds them, and writes them to a namespaced store indexed by actor ID. Retrieval is semantic top-k nearest-neighbour search over those embeddings. |
| **SEMANTIC strategy** | An LLM-as-extractor pass: takes raw conversation events, identifies discrete facts ("user's name is Sarah", "prefers email"), and stores each as an embedded record in a namespace scoped to the actor. Retrieval queries the store with the current user message to find relevant facts via cosine similarity. |
| **SUMMARIZATION strategy** | A compressive summarization pass: the full conversation is condensed into a shorter text summary, stored per-actor-per-session. On retrieval, recent summaries are fetched to give the LLM continuity without replaying entire transcripts. |
| **Namespace templates** (`/users/{actorId}/facts`) | A path-based partitioning scheme for the memory store — like a key prefix in Redis or a partition key in DynamoDB. Variable substitution (`{actorId}`, `{sessionId}`) ensures tenant isolation at the storage layer without application-level filtering. |
| **`MEMORY_SHAREDMEMORY_ID` env var** | Service discovery via environment injection — the deployment system resolves the memory resource ARN/ID and injects it as an env var into the runtime container, so the app code doesn't hardcode infrastructure identifiers. Same pattern as `DATABASE_URL` in 12-factor apps. |
| **`eventExpiryDuration: 30`** | TTL on raw conversation events in short-term storage (30 days). The extracted facts persist independently — this just controls how long the raw source material is retained for potential re-extraction or auditing. |
| **`RetrievalConfig(top_k=3, relevance_score=0.3)`** | Standard ANN search parameters: return at most 3 nearest neighbours, with a minimum cosine similarity threshold of 0.3 to filter irrelevant matches. |
| **`requestHeaderAllowlist`** | A runtime-level HTTP header passthrough config — by default, custom headers are stripped before reaching your app code. The allowlist tells the reverse proxy/gateway in front of the microVM to forward specific headers to the application. |
| **Session manager (Strands integration)** | A middleware hook that intercepts the agent's conversation loop: on each turn, it (1) retrieves relevant memories and prepends them to context, and (2) after the turn, emits conversation events to the memory pipeline for async extraction. |

## 3. Request lifecycle (end-to-end trace)

1. **Client invokes** → `agentcore invoke` sends HTTP POST to the AgentCore Runtime endpoint with payload `{"prompt": "..."}`, headers include `session-id` (UUID) and `X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id: Sarah`.
2. **Runtime proxy** → The reverse proxy checks the header allowlist, passes the custom user-id header through to the microVM.
3. **Application entry** → `invoke()` extracts `session_id` from context and `user_id` from the forwarded header.
4. **Agent factory** → `get_or_create_agent()` instantiates the Strands Agent with a `session_manager` configured for this user/session pair.
5. **Memory retrieval (pre-turn)** → The session manager queries the memory store: embeds the incoming prompt, performs ANN search against `/users/Sarah/facts` (top-k=3, threshold=0.3) and `/summaries/Sarah/{session_id}` (top-k=3). Retrieved memories are injected into the system context.
6. **LLM call** → The agent sends system prompt + retrieved memories + user message + tool definitions to Bedrock (Claude). The model generates a response (possibly calling tools like `get_product_info`).
7. **Streaming response** → Tokens stream back through the runtime proxy to the client via SSE/chunked HTTP.
8. **Memory ingestion (post-turn, async)** → The session manager emits the conversation turn (user message + assistant response) as events to the memory pipeline. An async process runs the SEMANTIC extractor (LLM identifies facts) and SUMMARIZATION extractor (LLM compresses conversation), then embeds and stores the results in their respective namespaces.
9. **Next session** → When a new session arrives for the same `actorId`, step 5 retrieves the previously extracted facts — proving recall is from the memory store, not session state.

## 4. Design decisions & trade-offs

| Decision | Why | Alternative |
|----------|-----|-------------|
| **Async extraction (not inline)** | Keeps response latency low — extraction happens after the response streams. Trade-off: ~1-2 min delay before facts are retrievable. | Inline extraction: slower responses but immediate availability. |
| **LLM-as-extractor (not rule-based)** | Flexible — handles arbitrary natural language without predefined schemas. Trade-off: costs LLM tokens per conversation, non-deterministic extraction. | Regex/NER pipelines: cheaper, deterministic, but brittle and narrow. |
| **Namespace-per-actor isolation** | Simple tenant separation at the storage layer. No cross-user leakage by design. Trade-off: no cross-user insights (e.g., "most common complaint"). | Shared namespace with actor-ID filter: flexible but risks leakage bugs. |
| **Global singleton agent (`_agent = None`)** | Reuses the agent across invocations in the same microVM for efficiency. Trade-off: the session_manager is set once at creation — if a different user hits the same warm VM, it uses the first user's memory config. | Per-request agent creation: correct isolation but higher latency. This is a known limitation of the lab's simplified pattern. |
| **Custom header for user-id (not JWT claim)** | Simpler for the lab — no auth infrastructure needed yet. Trade-off: no cryptographic proof of identity; anyone can set the header. | JWT-based identity (Lab 4): user-id extracted from verified token claims. |
| **Embedding-based retrieval (not keyword)** | Handles semantic similarity ("contact preference" matches "prefers email") without exact keyword match. Trade-off: requires embedding model, less predictable than exact match. | Full-text search / BM25: faster, predictable, but misses paraphrases. |

## 5. Portable concepts vs AWS lock-in

| Portable (any stack/cloud) | AWS-specific convenience |
|---|---|
| Embeddings + ANN retrieval (pgvector, Pinecone, Qdrant, Weaviate) | AgentCore Memory as managed service (extraction pipeline + store + retrieval in one API) |
| LLM-as-extractor pattern (works with any LLM) | Bedrock model access + built-in strategy definitions |
| Namespace/tenant isolation via key prefixing | Automatic `{actorId}` variable substitution in namespace templates |
| Session manager middleware pattern (pre/post hooks) | `AgentCoreMemorySessionManager` Strands SDK integration |
| Environment-based service discovery (12-factor) | CDK auto-injection of `MEMORY_*` env vars |
| HTTP header passthrough configuration | `requestHeaderAllowlist` in agentcore.json |
| TTL-based expiry on raw events | `eventExpiryDuration` config field |

## 6. Build-it-yourself sketch

```
FastAPI app (replaces Runtime)
+ pgvector on Postgres (replaces Memory store — stores embedded facts/summaries)
+ Background worker (Celery/SQS) that runs post-conversation:
    - Calls an LLM to extract facts (SEMANTIC equivalent)
    - Calls an LLM to summarize (SUMMARIZATION equivalent)
    - Embeds results via an embedding model (e.g., text-embedding-3-small)
    - Upserts into pgvector with actor_id partition
+ Pre-turn middleware: embeds the user query, runs ANN search (top-k=3, threshold=0.3)
+ Injects retrieved records into the LLM system prompt
+ Any LLM API (OpenAI, Anthropic, local) for both chat and extraction
```

## 7. Glossary

| Term | What it actually is |
|------|-------------------|
| **AgentCore Memory** | Managed extraction pipeline + vector store with namespace-scoped retrieval |
| **Short-term memory** | Raw conversation event log with a TTL (source material for extraction) |
| **Long-term memory** | Extracted/compressed records (facts or summaries) that persist indefinitely |
| **SEMANTIC strategy** | LLM pass that identifies and stores discrete facts from conversation |
| **SUMMARIZATION strategy** | LLM pass that compresses full conversations into retrievable summaries |
| **Namespace template** | Partition key pattern with variable substitution for tenant isolation |
| **RetrievalConfig** | ANN query parameters: how many results (top_k) and minimum similarity threshold |
| **Session manager** | Middleware that hooks into the agent loop for memory read (pre-turn) and write (post-turn) |
| **eventExpiryDuration** | TTL in days for raw conversation events in short-term storage |
| **requestHeaderAllowlist** | Proxy config that permits specific HTTP headers to reach the application |

## 8. Operational gotchas & lessons

| Gotcha | Principle |
|--------|-----------|
| **2-minute extraction delay** — facts aren't immediately available after the conversation ends. If you test recall too quickly, it appears broken. | Async pipelines have propagation delay. Design tests with explicit waits or polling. |
| **Singleton agent bug** — the `_agent` global means the first user's memory config persists for all subsequent users on the same warm VM. | Singletons and per-request state don't mix. In production, either create agents per-request or invalidate on user change. |
| **Header allowlist is opt-in** — forgetting to add the custom header to the allowlist means `context.request_headers` silently returns None, not an error. | Fail-open defaults are dangerous. Always validate required headers exist and raise explicitly. |
| **`relevance_score=0.3` is arbitrary** — too low and you get irrelevant noise; too high and you miss valid matches. No way to tune without observing real queries. | Embedding similarity thresholds need empirical tuning per domain. Start permissive, tighten with data. |
| **No memory deletion API exposed** — if wrong facts get extracted ("user likes X" when they don't), there's no straightforward correction mechanism in this setup. | Any memory system needs a forget/correct path. GDPR, user corrections, and extraction errors all require it. |
| **Cost scales with conversation length** — every turn triggers extraction LLM calls. Long conversations = proportionally more extraction cost, even if no new facts emerge. | Extraction should be smart about deduplication or only run on "interesting" turns. |

## 9. So what (production lens)

Cross-session memory transforms an agent from a stateless Q&A bot into something that builds a relationship with users over time — essential for support, sales, and any repeated-interaction use case. The real production challenge isn't adding memory (that's plumbing) — it's governing what gets remembered, ensuring extraction quality, handling corrections/deletions, and tuning retrieval so the agent surfaces the right context without hallucinating from stale or irrelevant memories.
