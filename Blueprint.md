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
  A[Object Storage: raw files] -->|event| B[Ingestion Trigger]
  B --> C[Queue]
  C --> D[Parser/Normalizer Workers]
  D --> E[(Raw Archive)]
  D --> F[(Normalized Store)]
  F --> G[Analysis Engine (Flags + QBR Outputs)]
  H[(Roster: Colleagues)] --> D

