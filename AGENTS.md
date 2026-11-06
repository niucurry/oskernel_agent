# Agent notes

## Tests

- Unit tests: `python -m pytest tests/test_recall_probe.py tests/test_read_canary.py tests/test_read_coverage.py tests/test_read_audit.py tests/test_llm_batch.py -q`
- Real OpenCode + MCP tests (localhost scripted model, no API cost) are opt-in:
  `OSKERNEL_TEST_OPENCODE=1 python -m pytest tests/test_read_audit_opencode.py tests/test_recall_probe_opencode.py -q`.
  They need a Python with `mcp` installed (the system `python3` currently lacks it; the
  experiment venv `/home/niu/agent-recall-probe-20261029/venv` has `mcp[cli]==1.30.0`).
- No `ruff` is installed on this machine.

## OpenCode 1.14.48 gotchas

- `opencode run --session <id> --fork` is broken (posts an empty body, prints "Session not found").
  `cli/recall_probe.py` forks by copying the session's data/state directories instead.
- Every OpenCode process leaves a ~5MB native library in `TMPDIR`, and every isolated data home
  installs ~60MB of `node_modules` into its config dir. The root disk is small (30GB, often
  nearly full), so experiment runners set a per-run `TMPDIR` and delete `node_modules` afterwards.
- With `permission: {"*": "deny"}` an agent's requests carry no `tools` at all.

## Post-report recall experiment

`python -m oskernel_agent.cli.recall_probe --help` (`prepare`/`evaluate`/`analyze` are offline;
`run`/`replay` call the real API). The local recorder (`engines/request_recorder.py`) injects
the API key, so OpenCode configs only hold a placeholder, and it logs bodies, never headers.
`replay --model X --upstream https://api.siliconflow.cn/v1` re-asks a recorded context to another
model (SiliconFlow needs the `/v1` suffix; larger models hit per-model TPM limits with ~54k-token
requests, so use few workers). Results go to `replay-<model>.jsonl`; `analyze` reports per-model
validity checks under `validity`.
