# AI_DEV_TEST
This is a test case AI Agent for an interview at Attracto

## What this does
- Parses `email*.txt` thread exports into messages
- Uses OpenAI models with Structured Outputs to:
  1) extract action items and risks with evidence quotes (`gpt-4o-mini`)
  2) verify/ground each extracted item (`gpt-4o-mini`)
  3) prioritize and generate an executive Portfolio Health report (`gpt-5-mini`)

Set credentials (recommended via environment variable, do not hardcode):
```bash
export OPENAI_API_KEY="YOUR_KEY"
```

## Run
```bash
python .\email_processing_agent.py --input_dir .\AI_Developer --out_json report.json --out_md report.md --redact
```

### Model configuration (cost control)
Override defaults via env vars:
- `OPENAI_EXTRACT_MODEL` (default: `gpt-4o-mini`)
- `OPENAI_VERIFY_MODEL`  (default: `gpt-4o-mini`)
- `OPENAI_SUMMARY_MODEL` (default: `gpt-5-mini`)

## Notes on security
- Use `--redact` to pseudonymize email addresses before sending text to the model.
- The report keeps short evidence quotes for traceability.