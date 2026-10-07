"""Lossless opt-in transforms of JSON telemetry copies."""

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

MARKER = "$lf_message_dedup"
Path = tuple[str | int, ...]
TOKEN = re.compile(
    r"@@@langfuseMedia:type=application/json\|id=([A-Za-z0-9_-]+)\|source=bytes@@@"
)


def serialize(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8", errors="backslashreplace")


def _clone(value: Any) -> Any:
    # JSON has no shared-container identity. deepcopy would retain aliases and
    # changing one selected path could then also replace an unselected field.
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


def _validate(value: Any, *, allow_refs: bool = False) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate(item, allow_refs=allow_refs)
        return
    if type(value) is dict:
        if not allow_refs and MARKER in value:
            raise ValueError("Input contains the reserved deduplication marker")
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be strings")
            _validate(item, allow_refs=allow_refs)
        return
    raise TypeError("Only finite JSON values are supported")


def _get(payload: Any, path: Path) -> Any:
    for part in path:
        if (
            isinstance(payload, list)
            and type(part) is int
            and 0 <= part < len(payload)
            or isinstance(payload, dict)
            and type(part) is str
        ):
            payload = payload[part]
        else:
            raise ValueError("Path does not address a JSON value")
    return payload


def _set(payload: Any, path: Path, value: Any) -> Any:
    if not path:
        return value
    _get(payload, path[:-1])[path[-1]] = value
    return payload


def _paths(payload: Any, paths: Iterable[Path]) -> list[Path]:
    result = [tuple(path) for path in paths]
    for i, path in enumerate(result):
        _get(payload, path)
        for other in result[:i]:
            if path[: len(other)] == other or other[: len(path)] == path:
                raise ValueError("Paths must be unique and cannot overlap")
    return result


def _message_paths(messages: Any) -> list[Path]:
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise TypeError("Expected a list of message objects")
    return [
        (i, "content") for i, message in enumerate(messages) if "content" in message
    ]


@dataclass(frozen=True)
class MediaContext:
    trace_id: str
    field: str = "input"
    observation_id: str | None = None

    def __post_init__(self):
        if not self.trace_id or self.field not in ("input", "output", "metadata"):
            raise ValueError("A trace ID and input/output/metadata field are required")


class ContentStore(Protocol):
    def put(self, data: bytes, context: MediaContext) -> str: ...

    def get(self, token: str) -> bytes: ...


@dataclass(frozen=True)
class EncodeFailure:
    path: Path
    error_type: str


@dataclass(frozen=True)
class EncodeResult:
    payload: Any
    offloaded: int
    failures: tuple[EncodeFailure, ...]


class RestoreError(ValueError):
    pass


class MessageDeduplicator:
    def __init__(self, store: ContentStore, *, min_bytes: int = 4096):
        if type(min_bytes) is not int or min_bytes < 1:
            raise ValueError("min_bytes must be a positive integer")
        self.store = store
        self.min_bytes = min_bytes

    def encode_messages(
        self, messages: list[dict], *, context: MediaContext
    ) -> EncodeResult:
        return self.encode_fields(
            messages, paths=_message_paths(messages), context=context
        )

    def restore_messages(self, messages: list[dict]) -> list[dict]:
        return self.restore_fields(messages, paths=_message_paths(messages))

    def encode_fields(
        self, payload: Any, *, paths: Iterable[Path], context: MediaContext
    ) -> EncodeResult:
        _validate(payload)
        selected = _paths(payload, paths)
        prepared = [(path, serialize(_get(payload, path))) for path in selected]
        result = _clone(payload)
        failures = []
        count = 0
        for path, data in prepared:
            # References cost ~250 bytes; do not offload tiny values even if the
            # configured threshold is smaller. Final reference size is checked too.
            if len(data) < max(512, self.min_bytes):
                continue
            try:
                # Some Python strings contain literal adjacent UTF-16 surrogates.
                # JSON decoding combines those into a scalar, changing identity.
                # Keep any non-round-trippable value inline instead of storing a
                # reference that would later restore a different value.
                if json.loads(data) != _get(payload, path):
                    raise ValueError("JSON encoding would change the selected value")
                token = self.store.put(data, context)
                if not isinstance(token, str) or not TOKEN.fullmatch(token):
                    raise ValueError("Store returned an invalid media token")
                ref = {
                    MARKER: {
                        "version": 1,
                        "media": token,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                }
                if len(serialize(ref)) >= len(data):
                    continue
                result = _set(result, path, ref)
                count += 1
            except Exception as exc:  # noqa: BLE001 - store failures must preserve inline content
                # Do not include HTTP exception text, signed URLs, keys or content.
                failures.append(EncodeFailure(path, type(exc).__name__))
        return EncodeResult(result, count, tuple(failures))

    def restore_fields(self, payload: Any, *, paths: Iterable[Path]) -> Any:
        _validate(payload, allow_refs=True)
        selected = _paths(payload, paths)
        result = _clone(payload)
        # Cache only within this restore operation, never across authorization or
        # retention boundaries. Verify each reference's length and digest below.
        downloaded: dict[str, bytes] = {}
        for path in selected:
            value = _get(payload, path)
            if not isinstance(value, dict) or MARKER not in value:
                continue
            try:
                ref = value[MARKER]
                if (
                    set(value) != {MARKER}
                    or not isinstance(ref, dict)
                    or set(ref) != {"version", "media", "sha256", "bytes"}
                ):
                    raise ValueError("Invalid reference shape")
                if type(ref["version"]) is not int or ref["version"] != 1:
                    raise ValueError("Unsupported reference version")
                if type(ref["bytes"]) is not int or ref["bytes"] < 0:
                    raise ValueError("Invalid content length")
                token = ref["media"]
                if not isinstance(token, str) or not TOKEN.fullmatch(token):
                    raise ValueError("Invalid media token")
                if token not in downloaded:
                    downloaded[token] = self.store.get(token)
                data = downloaded[token]
                if (
                    len(data) != ref["bytes"]
                    or hashlib.sha256(data).hexdigest() != ref["sha256"]
                ):
                    raise ValueError("Content integrity mismatch")
                restored = json.loads(data)
                _validate(restored)
                result = _set(result, path, restored)
            except Exception as exc:  # noqa: BLE001 - normalize store errors without leaking signed URLs
                raise RestoreError(
                    f"Cannot restore selected content ({type(exc).__name__})"
                ) from None
        return result
