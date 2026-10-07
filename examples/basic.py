"""Use synthetic messages; requires Langfuse SDK and a project with media enabled."""

import os

from langfuse import Langfuse

from langfuse_message_dedup import LangfuseMediaStore, MediaContext, MessageDeduplicator


def main():
    base_url = os.environ["LANGFUSE_BASE_URL"]
    public_key = os.environ["LANGFUSE_PUBLIC_KEY"]
    secret_key = os.environ["LANGFUSE_SECRET_KEY"]
    client = Langfuse(base_url=base_url, public_key=public_key, secret_key=secret_key)
    try:
        with LangfuseMediaStore(
            base_url=base_url, public_key=public_key, secret_key=secret_key
        ) as store:
            codec = MessageDeduplicator(store)
            messages = [{"role": "user", "content": "Synthetic long history. " * 300}]
            with client.start_as_current_observation(
                name="message-dedup-demo", as_type="generation"
            ) as generation:
                result = codec.encode_messages(
                    messages,
                    context=MediaContext(
                        trace_id=generation.trace_id, observation_id=generation.id
                    ),
                )
                generation.update(
                    input=result.payload,
                    metadata={
                        "dedup_version": 1,
                        "offloaded": result.offloaded,
                        "fallbacks": len(result.failures),
                    },
                )
                # The model call, if any, receives messages, never result.payload.
                assert codec.restore_messages(result.payload) == messages
                print(
                    f"Offloaded {result.offloaded} values; {len(result.failures)} fallbacks"
                )
        client.flush()
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
