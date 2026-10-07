import base64
import hashlib
import json

import httpx
import pytest

from langfuse_message_dedup import MediaContext, MessageDeduplicator
from langfuse_message_dedup.media import LangfuseMediaStore


class MediaServer:
    """Protocol double, not evidence of a real Langfuse server round trip."""

    def __init__(self):
        self.blobs = {}
        self.ready = set()
        self.associations = []
        self.requests = []
        self.fail_method = None

    def handle(self, request):
        self.requests.append(request)
        if request.url.host == "objects.test":
            assert "authorization" not in request.headers
            key = request.url.path.strip("/")
            if request.method == self.fail_method:
                return httpx.Response(503)
            if request.method == "PUT":
                assert (
                    request.headers["x-amz-checksum-sha256"]
                    == base64.b64encode(
                        hashlib.sha256(request.content).digest()
                    ).decode()
                )
                self.blobs[key] = request.content
                return httpx.Response(200)
            return httpx.Response(200, content=self.blobs[key])
        assert (
            request.headers["authorization"]
            == "Basic " + base64.b64encode(b"pk:sk").decode()
        )
        if request.method == self.fail_method:
            return httpx.Response(503)
        if request.method == "POST":
            body = json.loads(request.content)
            self.associations.append(body)
            key = base64.b64decode(body["sha256Hash"]).hex()
            return httpx.Response(
                201,
                json={
                    "mediaId": key,
                    "uploadUrl": None
                    if key in self.ready
                    else f"https://objects.test/{key}",
                },
            )
        key = request.url.path.split("/")[-1]
        if request.method == "PATCH":
            assert json.loads(request.content)["uploadHttpStatus"] == 200
            self.ready.add(key)
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "mediaId": key,
                "contentType": "application/json",
                "contentLength": len(self.blobs[key]),
                "url": f"https://objects.test/{key}",
            },
        )


def store_for(server, **kwargs):
    transport = httpx.MockTransport(server.handle)
    return LangfuseMediaStore(
        base_url="https://langfuse.test",
        public_key="pk",
        secret_key="sk",
        api_transport=transport,
        blob_transport=transport,
        **kwargs,
    )


def test_upload_then_reuse_registers_both_traces_and_restores_after_restart():
    server = MediaServer()
    data = [{"role": "user", "content": "history" * 2000}]
    with store_for(server) as store:
        codec = MessageDeduplicator(store)
        first = codec.encode_messages(data, context=MediaContext("trace-a"))
        second = codec.encode_messages(data, context=MediaContext("trace-b"))
        assert first.offloaded == second.offloaded == 1
        assert first.payload == second.payload
        assert [r.method for r in server.requests].count("PUT") == 1
        assert [a["traceId"] for a in server.associations] == ["trace-a", "trace-b"]
    with store_for(server) as fresh:
        assert MessageDeduplicator(fresh).restore_messages(second.payload) == data


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
def test_each_upload_stage_failure_falls_back_to_inline(method):
    server = MediaServer()
    server.fail_method = method
    with store_for(server) as store:
        data = [{"role": "user", "content": "a" * 5000}]
        result = MessageDeduplicator(store).encode_messages(
            data, context=MediaContext("trace-a")
        )
        assert result.payload == data
        assert result.offloaded == 0
        assert len(result.failures) == 1


def test_download_limit_and_metadata_are_checked():
    server = MediaServer()
    with store_for(server) as store:
        token = store.put(b'"1234567890"', MediaContext("trace-a"))
    with store_for(server, max_blob_bytes=5) as small:
        with pytest.raises(ValueError):
            small.get(token)


def test_invalid_token_is_rejected_before_network():
    server = MediaServer()
    with store_for(server) as store:
        with pytest.raises(ValueError):
            store.get("../../credentials")
        assert not server.requests


def test_bounded_stream_even_if_server_understates_size():
    def handle(request):
        if request.url.host == "langfuse.test":
            return httpx.Response(
                200,
                json={
                    "mediaId": "test",
                    "contentType": "application/json",
                    "contentLength": 1,
                    "url": "https://objects.test/test",
                },
            )
        return httpx.Response(200, content=b"x" * 100)

    transport = httpx.MockTransport(handle)
    with LangfuseMediaStore(
        base_url="https://langfuse.test",
        public_key="pk",
        secret_key="sk",
        api_transport=transport,
        blob_transport=transport,
        max_blob_bytes=10,
    ) as store:
        with pytest.raises(ValueError):
            store.get("@@@langfuseMedia:type=application/json|id=test|source=bytes@@@")
