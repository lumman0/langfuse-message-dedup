# Langfuse Message Dedup

[简体中文](README.zh-CN.md) · [Verification report](docs/verification.md) · [Contributing](CONTRIBUTING.md)

An experimental, opt-in Python extension that moves repeated long message content into **Langfuse media attachments**, keeps references in observations, and restores the original JSON values when needed.

**Unofficial community project.** It does not modify Langfuse itself and is not affiliated with the Langfuse team. The package is not yet published on PyPI.

## Why

A conversation may produce a new trace every turn while including the same history in each model input. This library reuses Langfuse's existing content-addressed media storage for selected large values. Edited history creates new content, so earlier observations retain their original snapshots.

It is useful for experimenting with long, repeated histories. It is **not a transparent replacement for Langfuse storage**: the native UI displays attachments, and full-text search and built-in evaluations do not automatically recover the original chat text.

## Install from source

Requires Python 3.10+, a Langfuse project, and enabled S3-compatible media storage. The verified setup is self-hosted Langfuse 4.47.0 with MinIO and Python SDK 4.17.0.

```sh
git clone https://github.com/lumman0/langfuse-message-dedup.git
cd langfuse-message-dedup
python -m pip install ".[example]"
```

Set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_SECRET_KEY` for a test project, then run:

```sh
python examples/basic.py
```

## Use

Inside an existing Langfuse generation, transform a **telemetry copy**, then record it:

```python
from langfuse_message_dedup import (
    LangfuseMediaStore, MediaContext, MessageDeduplicator,
)

with LangfuseMediaStore(
    base_url=base_url,
    public_key=public_key,
    secret_key=secret_key,
) as store:
    codec = MessageDeduplicator(store, min_bytes=4096)
    result = codec.encode_messages(
        messages,
        context=MediaContext(
            trace_id=generation.trace_id,
            observation_id=generation.id,
            field="input",
        ),
    )
    generation.update(input=result.payload)
    # Send the original messages to the model, never result.payload.
    assert codec.restore_messages(result.payload) == messages
    # Track len(result.failures): failed offloads stay inline.
```

The same restore helper accepts input fetched from Langfuse. With the v2 observations API, explicitly request `fields=core,basic,io` and JSON-decode its input/output strings first.

For structured payloads, select paths explicitly:

```python
paths = [("history", i, "content") for i in range(len(payload["history"]))]
encoded = codec.encode_fields(payload, paths=paths, context=context)
restored = codec.restore_fields(encoded.payload, paths=paths)
```

Use `paths=[()]` to select a root string or JSON value. Packed JSON strings are not automatically parsed or split. Roles, tool calls, ordering, and unselected fields are preserved.

## Failure and compatibility boundaries

- Uploads are synchronous. A reference is returned only after upload confirmation; failures leave that value inline and appear in `result.failures`. Use your own telemetry worker to avoid blocking a model response.
- Every trace/observation/field association is registered, including reuse of an existing object.
- Restoration checks version, length, and SHA256. Missing or corrupt content raises `RestoreError` instead of silently producing incomplete history.
- Apply redaction **before** encoding. A later SDK masking callback cannot redact text already uploaded as an attachment.
- Only JSON values are supported. Non-finite numbers, tuples, non-string object keys, and the reserved `$lf_message_dedup` marker are rejected. Values whose JSON round trip changes their identity stay inline.
- The default 4 KiB threshold is experimental; values below 512 bytes stay inline even with a lower configured threshold. The default object limit is 16 MiB.
- This is explicit SDK integration, not automatic instrumentation. Other spans may still record full copies until separately integrated.
- No background queue, garbage collector, retention/deletion adapter, or atomic transaction across media and telemetry is provided. A crash between upload and trace submission can leave orphan media.
- Native chat rendering, search, exports, and evaluations need compatibility work. Cloud, Azure/GCS, and other storage setups have not been validated.

## Verification

```sh
python -m pip install ".[dev,example]"
python -m pytest
python -m ruff check src tests examples scripts
python scripts/live_probe.py --report reports/local-private.json
```

The unit suite contains 24 tests. HTTP protocol tests use a simulated transport; `live_probe.py` separately exercises real media uploads, OTLP ingestion, persisted API readback, and lossless restoration using synthetic conversations without an LLM call.

Recorded local result: **six turns, 42 media associations, 13 unique uploads, and all input/output values restored**. Original JSON was 413,278 bytes; references plus downloaded unique objects were 137,818 bytes, or 66.65% fewer logical bytes in this example. See the [machine-readable report](reports/live-final.json).

**This is not a total disk-space or cost result.** It excludes ClickHouse compression, Postgres indexes/WAL, object requests, backups, and cleanup. Short or frequently changing content may not benefit. The measured first turn took about 4.18 seconds to encode; subsequent turns took 77–134 ms on the local setup. These are not production latency guarantees.

The initial implementation, review, and test execution were AI-assisted. The reported local tests were actually executed by the coding agent; the verification record does not claim manual maintainer reproduction.

## Next work

1. Measure total storage and request costs against compressed inline payloads.
2. Verify concurrent uploads, project isolation, and shared-content retention/deletion.
3. Add optional background processing and restoration adapters for exports/evaluations.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and integration checks.

## License

[MIT](LICENSE).
