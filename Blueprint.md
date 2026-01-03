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

## 2. The Analytical Engine (Multi-Step AI Logic)

This component turns raw email threads into a QBR-ready “what needs attention” view. It is designed to be **thread-aware** (reads the full conversation), **deduplicated** (no repeated issues), and **grounded** (every claim must be supported by word-for-word quotes).

### 2.1 Attention Flags (Director-grade signals)

**Attention Flag A — Unresolved Action Items**
- **What it is:** Explicit asks / questions / tasks / decisions that remain **open** by the end of the thread.
- **Why it matters:** Open asks often translate into delivery risk (blocked decisions, unclear ownership, stalled progress).

**Attention Flag B — Emerging Risks / Blockers**
- **What it is:** Delivery threats (incidents, blockers, scope/timeline risk, resourcing gaps) that are **unresolved or uncertain** by thread end.
- **Why it matters:** Directors need early awareness of risks that can impact customers, timeline, or cost.

> Note: The final report intentionally surfaces only items with `status in {unresolved, unknown}`.
> Resolved items are kept in `all_issues` for auditability, but do not distract the QBR “attention” view.

### 2.2 Multi-step detection logic (high-level)

We use a **multi-step AI pipeline** because “detect issue” and “decide resolved later” are different reasoning tasks. Splitting them improves reliability and reduces hallucinations.

```mermaid
flowchart LR
  A[Parse email*.txt into messages] --> B[Build full thread_text with [MSG 1..N]]
  B --> C[Step 1: Draft issues (AI)\n- deduplicate\n- classify A/B\n- evidence quotes + rationale]
  C --> D[Step 2: Resolution adjudication (AI)\n- resolved/unresolved/unknown\n- resolution proof quotes]
  D --> E[Deterministic guardrails\n- quotes must exist\n- resolution must be later]
  E --> F[Attention Flags output\n- keep only unresolved/unknown]
  F --> G[Step 3: Executive summary (AI)\n- short, actionable\n- references evidence IDs]
  G --> H[Artifacts: report.json + report.md]
