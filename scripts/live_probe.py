"""Real Langfuse media + OTLP + observation readback. Synthetic content only.

No mocks. Reads LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY. Writes a safe report;
does not print credentials, signed URLs, or recorded content. Does not delete data.
"""

import argparse
import copy
import hashlib
import json
import os
import random
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
from langfuse import Langfuse

from langfuse_message_dedup import (
    LangfuseMediaStore,
    MediaContext,
    MessageDeduplicator,
    RestoreError,
)
from langfuse_message_dedup.codec import MARKER, serialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="reports/live.json")
    args = parser.parse_args()
    base = os.environ["LANGFUSE_BASE_URL"]
    pk, sk = os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]
    run_id = uuid.uuid4().hex[:12]
    counts = Counter()

    def record_api(request):
        counts[f"api_{request.method}"] += 1

    def record_blob(request):
        counts[f"blob_{request.method}"] += 1

    def make_store():
        store = LangfuseMediaStore(base_url=base, public_key=pk, secret_key=sk)
        store.api.event_hooks["request"].append(record_api)
        store.blobs.event_hooks["request"].append(record_blob)
        return store

    client = Langfuse(base_url=base, public_key=pk, secret_key=sk)
    expected = []
    refs = {}
    raw_bytes = encoded_bytes = 0
    history = []
    rng = random.Random(1939)
    started = time.perf_counter()
    write_seconds = []
    try:
        with make_store() as store:
            codec = MessageDeduplicator(store)
            for turn in range(6):
                # Include changing realistic-ish text and random identifiers, not
                # just a long run of identical characters that compresses trivially.
                text = "\n".join(
                    f"{run_id} turn={turn} scene={i} 用户走进房间，检查道具并询问下一步。 detail={rng.getrandbits(64):016x}"
                    for i in range(90)
                )
                history.append({"role": "user", "content": text})
                reply = {
                    "role": "assistant",
                    "content": text.replace("用户", "角色") + "\n回复结束。",
                }
                if turn == 4:
                    history[0] = {
                        **history[0],
                        "content": history[0]["content"] + "\n历史纠错：钥匙是蓝色。",
                    }
                with client.start_as_current_observation(
                    name=f"dedup-probe-{run_id}-{turn}", as_type="generation"
                ) as span:
                    tick = time.perf_counter()
                    encoded = codec.encode_messages(
                        history,
                        context=MediaContext(span.trace_id, observation_id=span.id),
                    )
                    output = codec.encode_messages(
                        [reply],
                        context=MediaContext(
                            span.trace_id, field="output", observation_id=span.id
                        ),
                    )
                    write_seconds.append(time.perf_counter() - tick)
                    assert not encoded.failures and not output.failures, (
                        "Media offload failed"
                    )
                    assert codec.restore_messages(encoded.payload) == history
                    span.update(
                        input=encoded.payload,
                        output=output.payload,
                        metadata={"dedup_probe": run_id, "turn": turn},
                    )
                    expected.append(
                        (
                            span.trace_id,
                            span.id,
                            copy.deepcopy(history),
                            [reply],
                            encoded.payload,
                        )
                    )
                    for original, transformed in (
                        (history, encoded.payload),
                        ([reply], output.payload),
                    ):
                        raw_bytes += len(serialize(original))
                        encoded_bytes += len(serialize(transformed))
                        for message in transformed:
                            ref = message["content"][MARKER]
                            refs[ref["media"]] = ref
                history.append(reply)
            puts_after_upload = counts["blob_PUT"]
        client.flush()
        # New store/client state: deduplication must survive process cache loss.
        with make_store() as fresh:
            codec = MessageDeduplicator(fresh)
            reused = codec.encode_messages(
                expected[0][2],
                context=MediaContext(expected[0][0], observation_id=expected[0][1]),
            )
            assert reused.payload == expected[0][4]
            assert counts["blob_PUT"] == puts_after_upload
            assert expected[0][4][0]["content"] != expected[4][4][0]["content"]
            fetched = {}
            deadline = time.monotonic() + 90
            with httpx.Client(auth=(pk, sk), timeout=10) as api:
                while len(fetched) < len(expected) and time.monotonic() < deadline:
                    for trace_id, observation_id, original, reply, _ in expected:
                        if observation_id in fetched:
                            continue
                        response = api.get(
                            f"{base}/api/public/v2/observations",
                            params={
                                "traceId": trace_id,
                                "limit": 100,
                                "fields": "core,basic,io",
                            },
                        )
                        response.raise_for_status()
                        for row in response.json()["data"]:
                            if row["id"] != observation_id or row.get("input") is None:
                                continue
                            recorded_input = row["input"]
                            recorded_output = row["output"]
                            if isinstance(recorded_input, str):
                                recorded_input = json.loads(recorded_input)
                            if isinstance(recorded_output, str):
                                recorded_output = json.loads(recorded_output)
                            assert codec.restore_messages(recorded_input) == original
                            assert codec.restore_messages(recorded_output) == reply
                            fetched[observation_id] = True
                    if len(fetched) < len(expected):
                        time.sleep(2)
            assert len(fetched) == len(expected), "Persisted observations missing"
            blob_bytes = sum(len(fresh.get(token)) for token in refs)
            for token, ref in refs.items():
                assert hashlib.sha256(fresh.get(token)).hexdigest() == ref["sha256"]
            # A genuinely unavailable endpoint must keep the original inline.
            with LangfuseMediaStore(
                base_url="http://127.0.0.1:1", public_key=pk, secret_key=sk, timeout=0.5
            ) as unavailable:
                fallback = MessageDeduplicator(unavailable).encode_messages(
                    expected[0][2], context=MediaContext(expected[0][0])
                )
                assert (
                    fallback.payload == expected[0][2] and len(fallback.failures) == 1
                )
            missing = copy.deepcopy(expected[0][4])
            missing[0]["content"][MARKER]["media"] = (
                "@@@langfuseMedia:type=application/json|id=nonexistent-dedup-probe|source=bytes@@@"
            )
            try:
                codec.restore_messages(missing)
            except RestoreError:
                pass
            else:
                raise AssertionError("Missing object was silently accepted")
        report = {
            "status": "passed",
            "run_id": run_id,
            "real_server": base,
            "observations_restored": len(fetched),
            "unique_blobs": len(refs),
            "blob_puts": puts_after_upload,
            "original_json_bytes": raw_bytes,
            "reference_json_bytes": encoded_bytes,
            "downloaded_unique_blob_bytes": blob_bytes,
            "logical_byte_saving_fraction": 1
            - (encoded_bytes + blob_bytes) / raw_bytes,
            "write_seconds_per_turn": write_seconds,
            "http_requests": dict(counts),
            "duration_seconds": time.perf_counter() - started,
            "checks": [
                "persisted OTLP input/output round trip",
                "edited history independent",
                "new client reuses content",
                "upload unavailable falls back inline",
                "missing media raises",
                "downloaded content SHA256 verified",
            ],
            "trace_ids": [item[0] for item in expected],
            "limits": "Logical JSON plus actual downloaded blob bytes only; excludes ClickHouse compression, Postgres associations/indexes/WAL, storage allocation, request billing and orphan cleanup. Not a total disk/cost saving claim.",
        }
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
