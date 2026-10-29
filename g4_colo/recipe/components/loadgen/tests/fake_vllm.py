"""Fake streaming /v1/completions server for local loadgen tests.

python fake_vllm.py PORT [TTFT_MS] [TOKENS]
"""
import asyncio
import json
import sys

from aiohttp import web

TTFT = float(sys.argv[2]) / 1000 if len(sys.argv) > 2 else 0.02
TOKENS = int(sys.argv[3]) if len(sys.argv) > 3 else 16


async def completions(req: web.Request) -> web.StreamResponse:
    body = await req.json()
    assert body["stream"] and body["max_tokens"] == 16 and body["temperature"] == 0
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(req)
    await asyncio.sleep(TTFT)
    for i in range(TOKENS):
        chunk = {"id": "x", "object": "text_completion", "choices": [{"index": 0, "text": f" t{i}", "finish_reason": None}]}
        await resp.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
    usage = {"id": "x", "choices": [], "usage": {"prompt_tokens": 550, "completion_tokens": TOKENS}}
    await resp.write(b"data: " + json.dumps(usage).encode() + b"\n\n")
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


app = web.Application()
app.add_routes([web.post("/v1/completions", completions)])
web.run_app(app, port=int(sys.argv[1]), access_log=None, print=None)
