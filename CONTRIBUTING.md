# Contributing

This is an experimental community project, independent of Langfuse.
Open an issue before substantial architecture changes so we can agree on scope.

## Local checks

```sh
python -m pip install ".[dev]" build
python -m pytest
python -m ruff check src tests examples scripts
python -m ruff format --check src tests examples scripts
python -m build
```

Add a regression test for a bug and show it fails before the fix. Preserve
selected JSON values exactly and keep unselected fields unchanged. Never mutate
the model request; transformations are for telemetry copies only.

## Integration verification

Install `.[example]`, set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` for a **test project** with S3-compatible media storage,
then run `python scripts/live_probe.py --report reports/local-private.json`.
The script writes synthetic observations; it does not call an LLM or delete data.
Do not submit credentials, signed storage URLs, or private application content.

Distinguish unit tests, simulated HTTP tests, and actual server tests in PRs.
Storage claims must include the measurement scope: JSON bytes are not physical
ClickHouse storage or total cost. Document display/search/evaluation limitations.
Describe AI assistance honestly when used, along with the checks actually run.

## Useful next steps

- Measure compressed ClickHouse bytes plus media associations and request costs.
- Verify retention, deletion, cross-project permissions, and concurrent uploads.
- Explore an optional background integration with bounded queues and timeouts.
- Design explicit restoration adapters for exports and custom evaluations.

Run live tests locally; CI only runs offline unit/protocol tests and packaging.
