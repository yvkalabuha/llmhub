"""llmhub with the official openai-python SDK: chat, stream, errors.

    OPENAI_BASE_URL=http://10.8.0.1:8095/v1 OPENAI_API_KEY=lh_... python openai_chat.py

- timeout above the 480 s synchronous window (600 s here);
- the SDK retries 503 itself (max_retries) and joins the same task (identical body), but it does
  not honour Retry-After above 60 s: long jobs belong to /v1/tasks (see raw_tasks.py);
- llmhub details live in `e.body` (code, reason, retryable, retry_after_s, task_id).
"""

from __future__ import annotations

import os
import sys

import openai

MODEL = os.environ.get("LLMHUB_MODEL", "auto")


def main() -> int:
    client = openai.OpenAI(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=600,
        max_retries=2,
    )
    # 1. plain chat; llmhub headers via with_raw_response
    raw = client.chat.completions.with_raw_response.create(
        model=MODEL, messages=[{"role": "user", "content": "Say hello"}]
    )
    answer = raw.parse()
    print("answer:", answer.choices[0].message.content)
    print(
        "backend:", raw.headers.get("x-llmhub-backend"), "task:", raw.headers.get("x-llmhub-task")
    )
    if raw.headers.get("x-llmhub-truncated") == "1":
        print("warning: answer truncated (max_tokens)")

    # 2. stream: v1 sends keep-alive comments first, the text arrives at the end
    text = ""
    stream = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "Count to three"}], stream=True
    )
    for chunk in stream:
        for choice in chunk.choices:
            text += choice.delta.content or ""
    print("streamed:", text)

    # 3. llmhub options: priority/policy via headers (or the body field "llmhub")
    client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "urgent question"}],
        extra_headers={"X-LLMHub-Priority": "normal", "X-LLMHub-Policy": "BALANCED"},
    )

    # 4. errors
    try:
        client.chat.completions.create(
            model="no-such-model", messages=[{"role": "user", "content": "x"}]
        )
    except openai.APIStatusError as e:
        body = e.body if isinstance(e.body, dict) else {}
        print("error:", e.status_code, body.get("code"), "retryable:", body.get("retryable"))
        if e.status_code == 503 and body.get("task_id"):
            print("still running as task", body["task_id"], "- poll /v1/tasks/{id}")
        if body.get("code") != "model_not_available":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
