## Introduction

This interview project designs an automated AI Agent system and delivers a lightweight PoC and documentation to support the creation of Quarterly Business Reviews from project email communication. The primary goal is to detect and surface attention-worthy issues, especially problems, risks, and hidden bottlenecks across long, multi-thread email chains.

## 1. Data Ingestion & Initial Processing

### What we ingest
The input folder contains:
- **Colleagues.txt**: a file of the workers containing the details of **name / role / email**, used for mapping names to roles and email addresses.
- **email1.txt, email2.txt**, …: email thread exports where multiple messages appear in one file, separated by repeated header blocks (From/To/Cc/Date/Subject) followed by the body of the message. 

---

### Scalable ingestion approach
For production scale, I would use an object-storage + event-driven pipeline:
1. **Landing zone**: store incoming files in S3/GCS/Azure Blob under `.../<tenant>/<project>/<date>/...` and compute a content hash (if the hash already exists, the process stops).
2. **Trigger + queue**: storage events enqueue `{object_uri, hash, tenant_id/project_id}` into SQS/PubSub.
3. **Stateless workers**: horizontally scaled parser workers consume the queue and process files by type:
   - **Colleagues.txt** → roster parser
   - **email*.txt** → thread parser (split into messages, parse headers/body)
4. **Storage layers**:
   - **raw archive** (for audit/debug)
   - **normalized store** (e.g. Postgres, Warehouse ) for analysis.

---

### Initial processing 

**A) Roster parsing (`Colleagues.txt`)**
- Parse into `{email → (name, role)}` lookup for enrichment.

**B) Email parsing (`email*.txt`)**
- **Message segmentation**: split by repeated header anchors (e.g., \nFrom:).
- **Header normalization**: extract `from/to/cc/date/subject`, convert date to **UTC** (if needed), normalize subject for grouping.
- **Body cleaning**: remove/mark quoted replies/signatures, normalize whitespace.
- **Enrichment**: join participants with roster to attach roles (when present).
- **Noise tagging**: optionally tag clearly off-topic content.

**Finalized Data stored per message:**
`thread_id, source_file, timestamp_utc, subject_norm, from_email, to[], cc[], body_clean, from_role(optional), hash`

---

### Diagram (high-level)
```mermaid
flowchart LR
  A[Object Storage raw files] -->|event| B[Ingestion trigger]
  B --> C[Queue]
  C --> D[Parser and normalizer workers]
  D --> E[(Raw archive)]
  D --> F[(Normalized store)]
  F --> G[Analysis engine]
  G --> H[QBR outputs and flags]
  I[(Roster Colleagues)] --> D
```

## 2. The Analytical Engine (Multi-Step AI Logic)

### Step 1 — Issue drafting (AI: `ANALYZE_MODEL`, default `gpt-4o-mini`)

- **Input:** full thread (`thread_text`)
- **Output (structured):** a **deduplicated** list of issues (Flag A or Flag B), each with:
  - `severity_or_priority` (`low` / `medium` / `high`)
  - `evidence_quotes` (1–3 **verbatim** quotes)
  - short rationale (“why A/B and why level”)
- **Why this model:** this is a **high-volume extraction** task, so we use a cost-efficient model with deterministic settings (`temperature=0`).
- **Why structured outputs:** we use Pydantic schemas via `with_structured_output(...)` so the model must return predictable JSON (reduces parsing errors and “creative” answers).

---

### Step 2 — Resolution adjudication (AI: `RESOLVE_MODEL`, default `gpt-4o-mini`)

For **each drafted issue**:

- **Input:** full thread + the issue JSON + optional “resolution snippets”
- **Output (structured):** `status ∈ {resolved, unresolved, unknown}` plus:
  - `resolution_quotes` (1–3 **verbatim** quotes proving resolution)
  - short rationale (“why resolved/unresolved/unknown”)
- **Why a second model call?**  
  Deciding “resolved later” is a **separate reasoning task** that benefits from being isolated and forced to produce **explicit proof** (quotes). This reduces false “still open” cases (e.g., when the fix is confirmed only at the end).

---

### Step 2b — Guardrails (deterministic checks, anti-hallucination)

We enforce rules **outside** the LLM:

- **Quote existence check:** both evidence and resolution quotes must be exact substrings of `thread_text`.
- **Chronology check:** resolution quotes must appear in a later `[MSG k]` than the problem evidence.
- If the model claims `resolved` but fails these checks → downgrade to `unknown`.

---

### Step 3 — Executive summary (AI: `SUMMARY_MODEL`, default `gpt-5-mini`)

- **Input:** only `unresolved` / `unknown` items + an evidence map `{E# → quote}`
- **Output:** a short Director-friendly markdown summary grouped by Flag A vs Flag B, referencing evidence IDs like `[E3]`.
- **Why a stronger summary model?**  
  Summaries are what leadership reads; we prefer higher output quality here while keeping extraction/adjudication cost-efficient.

---

### Model routing and cost control

All model choices are configurable by environment variables:

- `OPENAI_ANALYZE_MODEL` (default: `gpt-4o-mini`)
- `OPENAI_RESOLVE_MODEL` (default: `gpt-4o-mini`)
- `OPENAI_SUMMARY_MODEL` (default: `gpt-5-mini`)

This allows cost/performance tuning without code changes.

```mermaid
flowchart LR
  A["Parse email*.txt into messages"] --> B["Build full thread_text with [MSG 1..N]"]
  B --> C["Step 1: Draft issues (AI)- deduplicate- classify A/B- evidence quotes + rationale"]
  C --> D["Step 2: Resolution adjudication (AI)- resolved/unresolved/unknown- resolution proof quotes"]
  D --> E["Deterministic guardrails- quotes must exist- resolution must be later"]
  E --> F["Attention Flags output- keep only unresolved/unknown"]
  F --> G["Step 3: Executive summary (AI)- short, actionable- references evidence IDs"]
  G --> H["Artifacts: report.json + report.md"]
  ```
### Engineered prompts used in the code (full text + intent)

This PoC uses **three prompt pairs** (System + User) aligned to the three AI steps:
- **Draft** (extract + deduplicate + classify issues)
- **Resolve** (decide if each issue was resolved later in the thread, with proof)
- **Summarize** (Director-ready output, grounded in evidence IDs)

Each step is separated to keep the model’s job narrow and measurable, and to enforce *grounding* via verbatim quotes.

---

#### 1) Issue drafting prompts (`THREAD_SYSTEM` + `THREAD_USER`)

**Goal:** From the full email thread, produce a **deduplicated** list of issues, labeled as Flag A or Flag B, each backed by **verbatim evidence quotes**.

```text
THREAD_SYSTEM:
You are a Director-level QBR analyst for project delivery email threads.
Return ONLY issues supported by verbatim quotes from the provided thread.
IMPORTANT: NO duplicates — merge repeated mentions of the same incident into ONE issue.
Do not guess.

THREAD_USER:
Analyze the full email thread and return a deduplicated list of issues.

Definitions:
- Attention Flag A_unresolved_action_item: explicit asks/questions/tasks/decisions needed.
- Attention Flag B_emerging_risk_blocker: blockers/incidents/risks (prod issues, outages, scope/timeline risks, etc.).

Rules (strict):
1) NO duplicates: merge repeated mentions of the same incident/task into one issue.
2) severity_or_priority: low|medium|high (use high only for explicit cues like URGENT, panic, prod/live impact, "all hands").
3) evidence_quotes: 1-3 short verbatim quotes that demonstrate the PROBLEM / ASK.
4) rationale_flag_level: 1-2 sentences explaining why it’s A or B and why the level.

THREAD (verbatim):
{thread_text}
```

**Design notes (why it works):**
- “NO duplicates” forces thread-level merging, not message-by-message extraction.
- Evidence quotes are required, so output stays grounded in the source.

---

#### 2) Resolution adjudication prompts (`RESOLVE_SYSTEM` + `RESOLVE_USER`)

**Goal:** For each drafted issue, decide if it was resolved later by the end of the thread, even if the word “resolved” is not used. If resolved, require **verbatim resolution proof quotes**.

```text
RESOLVE_SYSTEM:
You are a strict resolution adjudicator.
Your job is to decide if the issue is RESOLVED later in the thread.
Use contextual proof (e.g., 'fix is out', 'tested', 'working again').
Do not guess: if there is no clear proof, set status='unknown' or 'unresolved'.

RESOLVE_USER:
Decide whether the issue is resolved by the END of the thread.

Inputs:
- THREAD (verbatim)
- ISSUE (title + flag + level + problem evidence quotes)
- OPTIONAL: candidate_resolution_snippets (machine-selected snippets that may indicate resolution)

Rules (strict):
1) status must be one of: resolved|unresolved|unknown.
2) If status=resolved, you MUST provide 1-3 resolution_quotes copied verbatim from the thread that show:
   - a fix was applied/deployed OR completion happened AND
   - confirmation/verification (e.g., tested, working again) when available.
3) resolution_quotes should come from later messages than the problem evidence (chronologically).
4) rationale_status: 1-2 sentences explaining why you chose the status.

THREAD:
{thread_text}

ISSUE_JSON:
{issue_json}

CANDIDATE_RESOLUTION_SNIPPETS (may be empty):
{candidate_snippets}
```

**Design notes (why it works):**
- Requires explicit proof (`resolution_quotes`), which reduces “guessing”.
- The code further enforces chronology outside the LLM (resolution proof must appear in a later `[MSG k]`).

---

#### 3) Executive summary prompts (`SUMMARY_SYSTEM` + `SUMMARY_USER`)

**Goal:** Generate a short Director-friendly summary using only unresolved/unknown items, referencing evidence IDs for auditability.

```text
SUMMARY_SYSTEM:
You write concise executive summaries for Directors.
Use only the provided unresolved/unknown items.
Do not invent facts.

SUMMARY_USER:
Create a Portfolio Health summary.

Rules:
- Group by Attention Flag A and B.
- Include only items with status='unresolved' and 'unknown' (unknown -> needs clarification).
- Each bullet MUST reference evidence IDs like [E1], [E2] (can be multiple).
- Keep it short and actionable.

PAYLOAD_JSON:
{payload_json}
```

**Design notes (why it works):**
- Prevents scope creep by only summarizing items already selected by the engine.
- Evidence IDs make the summary traceable back to exact quotes in the report.

