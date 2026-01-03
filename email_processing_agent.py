"""
Run (PowerShell):
  $env:OPENAI_API_KEY="sk-..."
  python .\email_processing_agent.py --input_dir .\AI_Developer --out_json report.json --out_md report.md --redact
"""

import os, re, glob, json, hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Literal, Tuple
from pathlib import Path

# LangChain + OpenAI integration 
try:
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate
except Exception as e:
    raise SystemExit(
        "Missing dependency: langchain / langchain-openai. Install with:\n"
        "  pip install langchain langchain-openai\n"
        f"Import error: {e}"
    )

try:
    from pydantic import BaseModel, Field
except Exception as e:
    raise SystemExit(
        "Missing dependency: pydantic. Install with: pip install pydantic\n"
        f"Import error: {e}"
    )

# Models (cost control via env vars)
ANALYZE_MODEL   = os.getenv("OPENAI_ANALYZE_MODEL", "gpt-4o-mini")
RESOLVE_MODEL   = os.getenv("OPENAI_RESOLVE_MODEL", "gpt-4o-mini")
SUMMARY_MODEL   = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5-mini")

# Prompts
THREAD_SYSTEM = (
    "You are a Director-level QBR analyst for project delivery email threads.\n"
    "Return ONLY issues supported by verbatim quotes from the provided thread.\n"
    "IMPORTANT: NO duplicates — merge repeated mentions of the same incident into ONE issue.\n"
    "Do not guess."
)

THREAD_USER = """Analyze the full email thread and return a deduplicated list of issues.

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
"""

RESOLVE_SYSTEM = (
    "You are a strict resolution adjudicator.\n"
    "Your job is to decide if the issue is RESOLVED later in the thread.\n"
    "Use contextual proof (e.g., 'fix is out', 'tested', 'working again').\n"
    "Do not guess: if there is no clear proof, set status='unknown' or 'unresolved'."
)

RESOLVE_USER = """Decide whether the issue is resolved by the END of the thread.

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
"""

SUMMARY_SYSTEM = (
    "You write concise executive summaries for Directors.\n"
    "Use only the provided unresolved/unknown items.\n"
    "Do not invent facts."
)

SUMMARY_USER = """Create a Portfolio Health summary.

Rules:
- Group by Attention Flag A and B.
- Include only items with status='unresolved' and 'unknown' (unknown -> needs clarification).
- Each bullet MUST reference evidence IDs like [E1], [E2] (can be multiple).
- Keep it short and actionable.

PAYLOAD_JSON:
{payload_json}
"""

# Structured output schemas
FlagType = Literal["A_unresolved_action_item", "B_emerging_risk_blocker"]
LevelType = Literal["low", "medium", "high"]
StatusType = Literal["resolved", "unresolved", "unknown"]

class IssueDraft(BaseModel):
    flag: FlagType
    title: str
    severity_or_priority: LevelType
    rationale_flag_level: str
    evidence_quotes: List[str] = Field(..., min_length=1)

class ThreadIssuesDraft(BaseModel):
    issues: List[IssueDraft] = Field(default_factory=list)

class ResolutionDecision(BaseModel):
    status: StatusType
    rationale_status: str
    resolution_quotes: List[str] = Field(default_factory=list)

class SummaryResult(BaseModel):
    summary_md: str

# LCEL chain builder with compatibility fallback
def structured_chain(prompt: ChatPromptTemplate, model: str, schema):
    llm = ChatOpenAI(model=model, temperature=0)
    try:
        return prompt | llm.with_structured_output(schema, method="json_schema")
    except TypeError:
        return prompt | llm.with_structured_output(schema)

draft_chain = structured_chain(
    ChatPromptTemplate.from_messages([("system", THREAD_SYSTEM), ("human", THREAD_USER)]),
    ANALYZE_MODEL,
    ThreadIssuesDraft,
)

resolve_chain = structured_chain(
    ChatPromptTemplate.from_messages([("system", RESOLVE_SYSTEM), ("human", RESOLVE_USER)]),
    RESOLVE_MODEL,
    ResolutionDecision,
)

summary_chain = structured_chain(
    ChatPromptTemplate.from_messages([("system", SUMMARY_SYSTEM), ("human", SUMMARY_USER)]),
    SUMMARY_MODEL,
    SummaryResult,
)

# Email parsing
EMAIL_RE = re.compile(r'[\w\.\-]+@[\w\.\-]+\.\w+')
RFC2822_FMT = "%a, %d %b %Y %H:%M:%S %z"
ALT_DATE_FMTS = [
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
]

@dataclass
class EmailMessage:
    thread_id: str
    source_file: str
    from_email: str
    to_emails: List[str]
    cc_emails: List[str]
    date: datetime
    subject: str
    body: str

def parse_date(s: str) -> datetime:
    s = (s or "").strip()
    for fmt in [RFC2822_FMT] + ALT_DATE_FMTS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            continue
    return datetime.now(timezone.utc)

def extract_emails(s: str) -> List[str]:
    return [e.lower() for e in EMAIL_RE.findall(s or "")]

def split_messages(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if re.search(r"(?m)^Subject:\s", raw):
        parts = re.split(r"(?m)^(?=Subject:\s)", raw)
    else:
        parts = re.split(r"(?m)^(?=From:\s)", raw)
    return [p.strip() for p in parts if p.strip()]

def parse_email_thread(path: str) -> List[EmailMessage]:
    raw = Path(path).read_text(encoding="utf-8")
    blocks = split_messages(raw)
    msgs: List[EmailMessage] = []
    tid = Path(path).stem
    for b in blocks:
        lines = b.splitlines()
        hdr: Dict[str, str] = {}
        i = 0
        while i < len(lines) and re.match(r"^(From|To|Cc|Date|Subject):", lines[i]):
            k, v = lines[i].split(":", 1)
            hdr[k.lower().strip()] = v.strip()
            i += 1
        body = "\n".join(lines[i:]).strip()
        from_email = (extract_emails(hdr.get("from",""))[:1] or ["unknown"])[0]
        msgs.append(EmailMessage(
            thread_id=tid,
            source_file=Path(path).name,
            from_email=from_email,
            to_emails=extract_emails(hdr.get("to","")),
            cc_emails=extract_emails(hdr.get("cc","")),
            date=parse_date(hdr.get("date","")),
            subject=(hdr.get("subject","") or "").strip(),
            body=body,
        ))
    msgs.sort(key=lambda m: m.date)
    return msgs

def build_thread_text(thread: List[EmailMessage]) -> Tuple[str, List[str]]:
    chunks = []
    for idx, m in enumerate(thread, 1):
        chunks.append(
            f"[MSG {idx}]\n"
            f"Subject: {m.subject}\n"
            f"Date: {m.date.isoformat()}\n"
            f"From: {m.from_email}\n"
            f"To: {', '.join(m.to_emails)}\n"
            f"Cc: {', '.join(m.cc_emails)}\n"
            f"Body:\n{m.body}\n"
        )
    return "\n---\n".join(chunks).strip(), chunks

# Security: email redaction (pseudonymize)
def redact_emails_in_text(text: str) -> str:
    def repl(m: re.Match) -> str:
        email = m.group(0).lower()
        h = hashlib.sha256(email.encode("utf-8")).hexdigest()[:8]
        domain = email.split("@")[-1]
        return f"user_{h}@{domain}"
    return EMAIL_RE.sub(repl, text or "")

# Guardrails / helpers
def quotes_present(text: str, quotes: List[str]) -> bool:
    for q in quotes or []:
        qq = (q or "").strip()
        if not qq or qq not in text:
            return False
    return True

def locate_quote_msg_index(msg_chunks: List[str], quote: str) -> Optional[int]:
    q = (quote or "").strip()
    if not q:
        return None
    for idx, chunk in enumerate(msg_chunks, 1):
        if q in chunk:
            return idx
    return None

def max_problem_msg_index(msg_chunks: List[str], evidence_quotes: List[str]) -> int:
    idxs = [locate_quote_msg_index(msg_chunks, q) for q in (evidence_quotes or [])]
    idxs = [i for i in idxs if isinstance(i, int)]
    return max(idxs) if idxs else 0

def resolution_quotes_are_later(msg_chunks: List[str], evidence_quotes: List[str], resolution_quotes: List[str]) -> bool:
    prob_max = max_problem_msg_index(msg_chunks, evidence_quotes)
    if prob_max == 0:
        return True
    for rq in resolution_quotes or []:
        ridx = locate_quote_msg_index(msg_chunks, rq)
        if ridx is None or ridx <= prob_max:
            return False
    return True

# Candidate resolution snippet harvesting (helps the AI) 
RESOLUTION_PATTERNS = [
    r"\bfix\b", r"\bfixed\b", r"\bpushed\b", r"\bdeployed\b", r"\brolled back\b",
    r"\bworking again\b", r"\bworks again\b", r"\bworking now\b", r"\bworks now\b",
    r"\btested\b", r"\bverified\b", r"\blive shortly\b", r"\bshould be live\b",
    r"\brestored\b", r"\bback up\b", r"\bback online\b", r"\bapologize\b", r"\binform the client\b",
]
RESOLUTION_RE = re.compile("|".join(RESOLUTION_PATTERNS), re.IGNORECASE)

def harvest_resolution_snippets(msg_chunks: List[str], after_msg_index: int, limit: int = 6) -> List[str]:
    snippets: List[str] = []
    for i in range(max(after_msg_index, 1), len(msg_chunks) + 1):
        chunk = msg_chunks[i-1]
        body = chunk.split("Body:\n", 1)[-1]
        for line in body.splitlines():
            if RESOLUTION_RE.search(line):
                s = line.strip()
                if s and s not in snippets:
                    snippets.append(s)
                if len(snippets) >= limit:
                    return snippets
    return snippets

def issue_type(flag: FlagType) -> str:
    return "action_item" if flag == "A_unresolved_action_item" else "risk"

def issue_level_key(flag: FlagType) -> str:
    return "priority" if flag == "A_unresolved_action_item" else "severity"

# Report generation 
def build_report(input_dir: str, redact: bool) -> Dict[str, Any]:
    threads: Dict[str, List[EmailMessage]] = {}
    for p in sorted(glob.glob(os.path.join(input_dir, "email*.txt"))):
        msgs = parse_email_thread(p)
        if msgs:
            threads[msgs[0].thread_id] = msgs

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threads": [],
        "models": {"draft": ANALYZE_MODEL, "resolve": RESOLVE_MODEL, "summary": SUMMARY_MODEL},
    }

    for tid, thread in threads.items():
        thread_text, msg_chunks = build_thread_text(thread)
        if redact:
            thread_text = redact_emails_in_text(thread_text)
            msg_chunks = [redact_emails_in_text(c) for c in msg_chunks]

        # Step 1) Draft issues from full thread
        draft: ThreadIssuesDraft = draft_chain.invoke({"thread_text": thread_text})

        drafts: List[IssueDraft] = []
        for it in (draft.issues or []):
            if not it.title.strip():
                continue
            if not quotes_present(thread_text, it.evidence_quotes):
                continue
            drafts.append(it)

        finalized: List[Dict[str, Any]] = []
        evidence_bank: Dict[str, str] = {}
        ev_counter = 0

        def add_evidence(quotes: List[str]) -> List[str]:
            nonlocal ev_counter
            ids: List[str] = []
            for q in quotes or []:
                qq = (q or "").strip()
                if not qq:
                    continue
                ev_counter += 1
                ev_id = f"E{ev_counter}"
                evidence_bank[ev_id] = qq
                ids.append(ev_id)
            return ids

        for it in drafts:
            prob_idx = max_problem_msg_index(msg_chunks, it.evidence_quotes)
            candidates = harvest_resolution_snippets(msg_chunks, after_msg_index=prob_idx + 1, limit=6)

            issue_json = json.dumps(it.model_dump(), ensure_ascii=False)

            decision: ResolutionDecision = resolve_chain.invoke({
                "thread_text": thread_text,
                "issue_json": issue_json,
                "candidate_snippets": json.dumps(candidates, ensure_ascii=False),
            })

            status = decision.status
            res_quotes = decision.resolution_quotes or []

            # If AI claims resolved, enforce proof + ordering
            if status == "resolved":
                if (not res_quotes) or (not quotes_present(thread_text, res_quotes)) or (not resolution_quotes_are_later(msg_chunks, it.evidence_quotes, res_quotes)):
                    status = "unknown"
                    res_quotes = []

            # Second pass if we found candidates but status isn't resolved
            if status in ("unresolved", "unknown") and candidates:
                decision2: ResolutionDecision = resolve_chain.invoke({
                    "thread_text": thread_text,
                    "issue_json": issue_json,
                    "candidate_snippets": json.dumps(candidates, ensure_ascii=False),
                })
                if decision2.status == "resolved":
                    if decision2.resolution_quotes and quotes_present(thread_text, decision2.resolution_quotes) and resolution_quotes_are_later(msg_chunks, it.evidence_quotes, decision2.resolution_quotes):
                        status = "resolved"
                        res_quotes = decision2.resolution_quotes
                        decision = decision2

            # Map to message metadata for opened_at/subject convenience
            opened_at = thread[0].date.isoformat()
            subject = thread[0].subject
            if it.evidence_quotes:
                mi = locate_quote_msg_index(msg_chunks, it.evidence_quotes[0])
                if mi and 1 <= mi <= len(thread):
                    opened_at = thread[mi-1].date.isoformat()
                    subject = thread[mi-1].subject

            ev_ids = add_evidence(it.evidence_quotes[:3])
            res_ids = add_evidence(res_quotes[:3]) if res_quotes else []

            out: Dict[str, Any] = {
                "type": issue_type(it.flag),
                "subject": subject,
                "opened_at": opened_at,
                "title": it.title,
                issue_level_key(it.flag): it.severity_or_priority,
                "flag": it.flag,
                "status": status,
                "resolved_later": True if status == "resolved" else False,
                "rationale_flag_level": it.rationale_flag_level,
                "rationale_status": decision.rationale_status,
                "evidence_ids": ev_ids,
                "evidence_quotes": it.evidence_quotes[:3],
                "resolution_evidence_ids": res_ids,
                "resolution_quotes": res_quotes[:3],
            }
            finalized.append(out)

        # Attention flags = unresolved + unknown only
        A = [x for x in finalized if x["flag"] == "A_unresolved_action_item" and x["status"] in ("unresolved", "unknown")]
        B = [x for x in finalized if x["flag"] == "B_emerging_risk_blocker" and x["status"] in ("unresolved", "unknown")]

        rank = {"high": 2, "medium": 1, "low": 0}
        def sort_key(x):
            level = x.get("priority") or x.get("severity") or "low"
            return (-rank.get(level, 0), 0 if x["status"] == "unresolved" else 1)

        A.sort(key=sort_key)
        B.sort(key=sort_key)

        payload = {"thread_id": tid, "attention_flag_A": A, "attention_flag_B": B, "evidence": evidence_bank}
        summary: SummaryResult = summary_chain.invoke({"payload_json": json.dumps(payload, ensure_ascii=False)})

        report["threads"].append({
            "thread_id": tid,
            "source_files": sorted(set(m.source_file for m in thread)),
            "time_range": {"start": thread[0].date.isoformat(), "end": thread[-1].date.isoformat()},
            "attention_flags": {"A_unresolved_action_items": A, "B_emerging_risks_blockers": B},
            "all_issues": finalized,
            "evidence": evidence_bank,
            "executive_summary_md": summary.summary_md,
        })

    report["threads"].sort(key=lambda t: -(len(t["attention_flags"]["A_unresolved_action_items"]) + len(t["attention_flags"]["B_emerging_risks_blockers"])))
    return report

def render_md(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Portfolio Health Report (AI PoC — Thread-level + Resolution)\n")
    lines.append(f"Generated at: `{report['generated_at']}`")
    lines.append(f"Models: draft=`{report['models']['draft']}`, resolve=`{report['models']['resolve']}`, summary=`{report['models']['summary']}`\n")

    def fmt_ids(ids: List[str]) -> str:
        return "".join([f" [{i}]" for i in (ids or [])])

    for t in report["threads"]:
        lines.append(f"## Thread: `{t['thread_id']}`")
        lines.append("- Source files: " + ", ".join(f"`{sf}`" for sf in t["source_files"]))
        lines.append(f"- Time range: {t['time_range']['start']} → {t['time_range']['end']}\n")

        lines.append("### Executive Summary")
        lines.append((t["executive_summary_md"] or "").strip() or "_(empty)_")
        lines.append("")

        A = t["attention_flags"]["A_unresolved_action_items"]
        B = t["attention_flags"]["B_emerging_risks_blockers"]

        if A:
            lines.append("### Attention Flag A — Unresolved Action Items")
            for it in A:
                lvl = it.get("priority","low")
                lines.append(f"- **{lvl}** | **{it['status']}** | {it['title']}{fmt_ids(it.get('evidence_ids', []))}")
                lines.append(f"  - Why A/level: {it.get('rationale_flag_level','')}")
                lines.append(f"  - Why status: {it.get('rationale_status','')}")
            lines.append("")
        if B:
            lines.append("### Attention Flag B — Emerging Risks / Blockers")
            for it in B:
                lvl = it.get("severity","low")
                lines.append(f"- **{lvl}** | **{it['status']}** | {it['title']}{fmt_ids(it.get('evidence_ids', []))}")
                lines.append(f"  - Why B/level: {it.get('rationale_flag_level','')}")
                lines.append(f"  - Why status: {it.get('rationale_status','')}")
            lines.append("")
        if not A and not B:
            lines.append("_No unresolved/unknown attention flags detected in this thread._\n")

    return "\n".join(lines).strip() + "\n"

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", default=".", help="Folder containing email*.txt")
    ap.add_argument("--out_json", default="report.json")
    ap.add_argument("--out_md", default="report.md")
    ap.add_argument("--redact", action="store_true", help="Pseudonymize email addresses before sending to models")
    args = ap.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. In PowerShell:\n"
            "  $env:OPENAI_API_KEY='sk-...'\n"
            "Do NOT hardcode credentials in source files."
        )

    report = build_report(args.input_dir, redact=args.redact)
    Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.out_md).write_text(render_md(report), encoding="utf-8")
    print(f"Wrote {args.out_json} and {args.out_md}")

if __name__ == "__main__":
    main()