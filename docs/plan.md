# Optional message deduplication prototype

Approved scope: independent Python package using Langfuse's existing media API,
with lossless restoration helpers. No Langfuse source changes.
The initial prototype was local; publishing this independent GitHub repository
was subsequently authorized. PyPI publication remains out of scope.

## Architecture and constraints

- Python >=3.10; httpx for HTTP; pytest for development.
- `codec.py`: copy JSON data, replace explicitly selected large values with a
  versioned wrapper containing native media token, size and SHA256; restore and
  validate. Standard messages select each `content`; structured payloads use
  explicit tuple paths. Never modify model request objects.
- `media.py`: synchronous public media POST / PUT / PATCH, GET / object GET.
  Authenticate only API calls. Register each new trace/observation association
  even on a content hit. Return references only after successful upload/patch.
- Default 4096 byte threshold is an experimental policy, not a measured optimum.
- On upload failure keep that value inline and return a safe error description.
  Restore errors are explicit; never silently return incomplete history.
- No background queue: callers run preparation off the model critical path.
- Only JSON values; reserved reference marker rejected at encoding. Preserve
  role, ordering, tools and exact string values; no automatic JSON-string parsing.
- Native UI shows attachments; search and built-in eval equivalence not promised.
- Blob retention, orphan cleanup and shared-reference deletion stay with the host;
  the prototype performs no deletion. No production readiness claim.

## Execution (inline)

- [x] Task 1 — tests/test_codec.py: tests for lossless repeated histories,
  edited content, nonmutation, small values, tools, explicit paths, partial
  upload failure, corruption, marker ambiguity. Run failing tests, then implement
  codec.py and public exports. Verify with `python -m pytest tests/test_codec.py`.
- [x] Task 2 — tests/test_media.py: real protocol expectations via httpx test
  transport for upload reuse and association, credentials, PUT/PATCH failures,
  malformed responses, download bounds. Implement media.py. Run both test files.
- [x] Task 3 — scripts/live_probe.py: real local Langfuse + MinIO. Upload repeated
  messages across traces, persist input/output, read them back and restore with a
  fresh codec; test editing, new-client reuse, unavailable upload, missing blob.
  Record actual object bytes and reference sizes, durations and API counts;
  distinguish payload metrics from total physical storage costs.
- [x] Task 4 — README.md, examples/basic.py, pyproject.toml: installable package,
  clear API and limitations, reproducible commands. Build/install smoke test,
  lint, unit tests, inspect live report. Record any blocked live checks honestly.

## Acceptance

Every successful restore equals the original JSON value, repeated content uses
the same media ID within one project, changed content uses a different one,
failures cannot emit an unconfirmed reference. Native server round trip is a
separate gate from simulated HTTP tests. Public upload/PR is out of scope.
