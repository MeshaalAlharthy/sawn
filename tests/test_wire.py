"""The real model path over HTTP: agentkit's OpenAI client talking to a local OpenAI-compatible stand-in server.

Everything a free OpenRouter model goes through is exercised here, offline: the Chat Completions wire format, tool
schemas, tool-call arguments as JSON strings, temperature, OpenRouter's fallback list, usage accounting, the
response cache and the served-model record.
"""

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

import agentkit as K
import harness as H
from offline import SCRIPTS
from sawn import Sawn

PROMPT = "السلام عليكم، أبي تحذفون حسابي وكل بياناتي. إيميلي fahad.harbi@mail.example، آخر 4 من الهوية 9356، جوالي 0503317725."


@pytest.fixture(scope="module")
def fake_openai():
    app, convos, calls = FastAPI(), {}, []

    @app.post("/v1/chat/completions")
    async def completions(req: Request):
        body = await req.json()
        calls.append(body)
        msgs = body["messages"]
        user = next(m["content"] for m in msgs if m["role"] == "user")
        if user not in convos:
            convos[user] = SCRIPTS["deletion-fahad"](user)
            step = next(convos[user])
        else:
            tail = []
            for m in reversed(msgs):
                if m["role"] != "tool":
                    break
                tail.insert(0, json.loads(m["content"]))
            step = convos[user].send(tail)
        if isinstance(step, str):
            message, finish = {"role": "assistant", "content": step}, "stop"
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{len(calls)}_{i}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for i, (n, a) in enumerate(step)]}
            finish = "tool_calls"
        return {"id": f"cmpl-{len(calls)}", "object": "chat.completion", "created": 0, "model": "fake/served-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}}

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/v1", calls
    server.should_exit = True


def test_real_client_path(fake_openai, mcp_tools, tmp_path, monkeypatch):
    base, calls = fake_openai
    monkeypatch.setitem(K.PROVIDERS["openrouter"], "base_url", base)
    monkeypatch.setattr(K, "CACHE", tmp_path / "cache")
    db = Sawn(tmp_path / "db")
    o = H.handle(PROMPT, sawn=db, tools=mcp_tools, approve=H.auto_approver(), sender="fahad.harbi@mail.example")
    assert o.stopped == "done" and o.delivery["status"] == "sent", o.events
    assert o.served == {"fake/served-model": 3} and o.tokens == 3 * 1050
    first = calls[0]
    assert first["temperature"] == 0
    assert first["models"][0] == K.MODEL and len(first["models"]) > 1  # OpenRouter fallback routing
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"verify_requester", "delete_data", "search_policies", "read_policy"} <= names
    assert "send_email" not in names and "deliver_reply" not in names
    assert first["messages"][0]["role"] == "system" and "Sawn" in first["messages"][0]["content"]
    assert "<request" in first["messages"][1]["content"]

    n = len(calls)  # the same request again: every model call replays from the cache
    db.reset()
    o2 = H.handle(PROMPT, sawn=db, tools=mcp_tools, approve=H.auto_approver(), sender="fahad.harbi@mail.example")
    assert len(calls) == n and o2.cached == 3 and o2.delivery["status"] == "sent"
