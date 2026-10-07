import copy
import hashlib
import json

import pytest

from langfuse_message_dedup import MediaContext, MessageDeduplicator, RestoreError


class MemoryStore:
    def __init__(self):
        self.blobs = {}
        self.calls = []
        self.fail = False

    def put(self, data, context):
        if self.fail:
            raise OSError("secret details should not enter reports")
        key = hashlib.sha256(data).hexdigest()
        self.blobs[key] = data
        self.calls.append((key, context))
        return f"@@@langfuseMedia:type=application/json|id={key}|source=bytes@@@"

    def get(self, token):
        key = token.split("|id=")[1].split("|")[0]
        return self.blobs[key]


CTX = MediaContext(trace_id="1" * 32, observation_id="2" * 16, field="input")


def test_round_trip_history_changes_reuse_and_input_is_untouched():
    store = MemoryStore()
    codec = MessageDeduplicator(store, min_bytes=20)
    a = {"role": "user", "content": "A1你好\n" * 100}
    b = {"role": "assistant", "content": "B1🤖" * 100}
    first = [a]
    second = [a, b, {"role": "user", "content": "A2"}]
    original = copy.deepcopy(second)
    one = codec.encode_messages(first, context=CTX)
    two = codec.encode_messages(second, context=CTX)
    assert second == original
    assert two.payload[0] == one.payload[0]
    assert len(store.blobs) == 2
    assert codec.restore_messages(two.payload) == second
    assert codec.restore_messages(json.loads(json.dumps(two.payload))) == second
    second[0]["content"] += "edited"
    changed = codec.encode_messages(second, context=CTX)
    assert changed.payload[0] != one.payload[0]
    assert len(store.blobs) == 3
    assert codec.restore_messages(one.payload) == first[:-1] + [original[0]]


def test_tools_null_and_multimodal_json_round_trip():
    store = MemoryStore()
    codec = MessageDeduplicator(store, min_bytes=1)
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "function": {"arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "t1",
            "content": [{"type": "text", "text": "结果" * 1000}],
        },
        {"role": "user", "content": ""},
        {"role": "assistant", "tool_calls": []},
    ]
    result = codec.encode_messages(messages, context=CTX)
    assert codec.restore_messages(result.payload) == messages
    assert result.payload[0]["tool_calls"] == messages[0]["tool_calls"]
    # Small JSON values must stay inline even with an aggressively low threshold.
    assert result.payload[0]["content"] is None
    assert result.payload[2]["content"] == ""


def test_explicit_paths_leave_other_fields_and_packed_strings_alone():
    codec = MessageDeduplicator(MemoryStore(), min_bytes=20)
    data = {
        "state": 12,
        "history": [{"content": "history" * 1000}],
        "packed": '{"history": "leave me"}',
    }
    paths = [("history", 0, "content")]
    result = codec.encode_fields(data, paths=paths, context=CTX)
    assert result.payload["packed"] == data["packed"]
    assert result.payload["state"] == 12
    assert codec.restore_fields(result.payload, paths=paths) == data


def test_failed_upload_keeps_original_and_does_not_expose_exception_text():
    store = MemoryStore()
    store.fail = True
    codec = MessageDeduplicator(store)
    messages = [{"role": "user", "content": "large" * 2000}]
    result = codec.encode_messages(messages, context=CTX)
    assert result.payload == messages
    assert result.offloaded == 0
    assert len(result.failures) == 1
    assert "secret" not in str(result.failures)
    store.fail = False
    assert codec.encode_messages(messages, context=CTX).offloaded == 1


def test_corrupted_or_missing_content_fails_explicitly():
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    result = codec.encode_messages(
        [{"role": "user", "content": "a" * 10000}], context=CTX
    )
    key = next(iter(store.blobs))
    store.blobs[key] = b'"wrong"'
    with pytest.raises(RestoreError):
        codec.restore_messages(result.payload)
    store.blobs.clear()
    with pytest.raises(RestoreError):
        codec.restore_messages(result.payload)


def test_each_context_registered_even_if_content_repeats():
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    data = [{"content": "x" * 10000, "role": "user"}]
    codec.encode_messages(data, context=CTX)
    other = MediaContext(trace_id="3" * 32, field="output")
    codec.encode_messages(data, context=other)
    assert [call[1] for call in store.calls] == [CTX, other]
    assert len(store.blobs) == 1


@pytest.mark.parametrize(
    "data", [float("nan"), {1: "key"}, ("tuple",), {"$lf_message_dedup": {}}]
)
def test_rejects_lossy_or_reserved_input_before_upload(data):
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    with pytest.raises((ValueError, TypeError)):
        codec.encode_fields(data, paths=[()], context=CTX)
    assert not store.calls


def test_invalid_paths_are_validated_before_any_upload():
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    with pytest.raises((ValueError, KeyError)):
        codec.encode_fields(
            {"a": "x" * 10000}, paths=[("a",), ("missing",)], context=CTX
        )
    assert not store.calls


def test_reject_overlapping_paths():
    codec = MessageDeduplicator(MemoryStore())
    with pytest.raises(ValueError):
        codec.encode_fields({"a": {"b": "x"}}, paths=[("a",), ("a", "b")], context=CTX)


def test_small_content_does_not_make_requests():
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    result = codec.encode_messages([{"role": "user", "content": "你好"}], context=CTX)
    assert result.offloaded == 0
    assert not store.calls


def test_shared_python_containers_do_not_change_unselected_fields():
    shared = {"content": "x" * 5000}
    payload = {"selected": shared, "unselected": shared}
    codec = MessageDeduplicator(MemoryStore())
    paths = [("selected", "content")]
    encoded = codec.encode_fields(payload, paths=paths, context=CTX)
    assert encoded.payload["unselected"] == payload["unselected"]
    transported = json.loads(json.dumps(encoded.payload))
    assert codec.restore_fields(transported, paths=paths) == payload


@pytest.mark.parametrize("size", [1, 5000])
def test_json_escaped_surrogates_round_trip(size):
    message = {"role": "user", "content": json.loads('"\\ud800"') * size}
    codec = MessageDeduplicator(MemoryStore())
    encoded = codec.encode_messages([message], context=CTX)
    assert codec.restore_messages(encoded.payload) == [message]


def test_noncanonical_surrogate_pairs_cannot_be_offloaded_lossily():
    messages = [{"role": "user", "content": "\ud83d\ude00" * 2000}]
    store = MemoryStore()
    codec = MessageDeduplicator(store)
    encoded = codec.encode_messages(messages, context=CTX)
    assert encoded.payload == messages
    assert encoded.offloaded == 0
    assert len(encoded.failures) == 1
    assert not store.calls
