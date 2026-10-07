"""Falsification tests for the cascade 2xx-with-error guard.

A 2xx from an upstream is NOT proof of success. OpenRouter's free tier commits HTTP 200
and then reports the real upstream failure in the body (non-stream) or in the FIRST SSE
event (stream) — e.g. "Upstream error from Nvidia: Service temporarily overloaded". Before
this guard, cascade counted that as a success, cached a poison body, and returned a
healthy-looking 200 that the client (Hermes aux compression) read as a provider error and
re-routed on — which is how a compression call ended up on the main model and hung.

These tests extract the helpers straight from cascade.py by AST (importing the module
would start Flask and load Bitwarden keys), then drive them against hostile inputs.
"""
import ast
import json
from pathlib import Path

import pytest


def _load_helpers(*names):
    src = (Path(__file__).resolve().parents[1] / "cascade.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {"json": json}
    # always pull in the small helpers the extracted ones call
    wanted = set(names) | {"_extract_error_text", "_replace_surrogates"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<cascade>", "exec"), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f"helpers not found in cascade.py: {missing}"
    return ns


class FakeResp:
    """One-shot, like requests' streaming response."""

    def __init__(self, chunks):
        self._it = iter(chunks)

    def iter_content(self, chunk_size=None):
        return self._it


def test_stream_error_event_is_detected_and_bytes_preserved():
    ns = _load_helpers("_peek_stream_error", "_extract_error_text")
    resp = FakeResp([b'data: {"error":{"message":"Upstream error from Nvidia: Service temporarily overloaded"}}\n\n'])
    err, consumed = ns["_peek_stream_error"](resp)
    assert "overloaded" in err
    assert consumed.startswith(b"data:")  # nothing lost — caller replays it


def test_normal_stream_head_passes_through_and_is_re_emitted():
    ns = _load_helpers("_peek_stream_error", "_streaming_generator")
    resp = FakeResp([b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'])
    err, consumed = ns["_peek_stream_error"](resp)
    assert err == ""
    out = b"".join(ns["_streaming_generator"](resp, consumed))
    assert b"hi" in out  # the drained head must reach the client


def test_keepalive_comment_then_error_is_detected():
    ns = _load_helpers("_peek_stream_error")
    resp = FakeResp([b": ping\n", b'data: {"error":{"message":"boom"}}\n\n'])
    err, _ = ns["_peek_stream_error"](resp)
    assert err == "boom"


def test_error_split_across_chunks_is_detected():
    ns = _load_helpers("_peek_stream_error")
    resp = FakeResp([b'data: {"error":{"mes', b'sage":"split"}}\n'])
    err, _ = ns["_peek_stream_error"](resp)
    assert err == "split"


def test_non_stream_error_body_extraction():
    ns = _load_helpers("_extract_error_text")
    assert ns["_extract_error_text"]({"error": {"message": "x"}}) == "x"
    assert ns["_extract_error_text"]({"error": "plain"}) == "plain"
    assert ns["_extract_error_text"]({"choices": [{"message": {"content": "ok"}}]}) == ""
    assert ns["_extract_error_text"](None) == ""


def test_done_and_non_json_head_are_content_not_errors():
    ns = _load_helpers("_peek_stream_error")
    err, _ = ns["_peek_stream_error"](FakeResp([b"data: [DONE]\n"]))
    assert err == ""
    err, _ = ns["_peek_stream_error"](FakeResp([b"data: not-json\n"]))
    assert err == ""