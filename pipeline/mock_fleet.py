#!/usr/bin/env python3
"""Mock fleet — an OpenAI-compatible server for end-to-end testing Agent Verify.

Serves POST /v1/chat/completions with four scripted model personalities plus a
judge, so `agent_verify.py` can be exercised for real — network, auth header,
tool-call parsing, scoring, judging, report writing, CI exit code — with zero
API keys and zero cost.

Personalities:
  mock-baseline  well-behaved reference model
  mock-good      matches baseline behavior            -> expect CERTIFIED
  mock-flaky     drops an arg, calls a phantom tool   -> expect CAUTION
  mock-bad       wrong tools, bad JSON, one refusal   -> expect FAIL
  mock-judge     scores equivalence by comparing the two responses

Run a server:      python3 pipeline/mock_fleet.py --serve [--port 8091]
Full e2e selftest: python3 pipeline/mock_fleet.py --selftest
  (starts the server, runs agent_verify.py against it, asserts the three
   verdicts and the non-zero CI exit code, prints PASS/FAIL)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ── scripted behaviors ────────────────────────────────────────────────────────

def pick_tool(user: str, tools: list) -> tuple | None:
    """The 'correct' tool decision for each suite task, keyed on the prompt."""
    u = user.lower()
    names = {t["function"]["name"] for t in (tools or [])}
    if "weather" in u and "get_weather" in names:
        return "get_weather", {"city": "San Francisco"}
    if "customers" in u and "run_sql" in names:
        return "run_sql", {"query": "SELECT customer_id, SUM(order_value) v FROM orders GROUP BY 1 ORDER BY v DESC LIMIT 10"}
    if "cost" in u and "calculator" in names:
        return "calculator", {"expression": "4.20*317"}
    if "design doc" in u and "search_documents" in names:
        return "search_documents", {"query": "Q3 vector-search migration design doc"}
    if "book" in u and "book_room" in names:
        return "book_room", {"room": "Aurora", "start": "14:00", "end": "15:00", "attendees": 6, "video": True}
    return None  # correct answer for the distractor / tool-free tasks


def respond(model: str, messages: list, tools: list, want_json: bool) -> dict:
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    u = user.lower()

    if model == "mock-judge":
        # score = how similar RESPONSE A and B are inside the judge prompt
        a = re.search(r"RESPONSE A \(baseline model\):\n(.*?)\n\nRESPONSE B", user, re.S)
        b = re.search(r"RESPONSE B \(candidate model\):\n(.*?)\n\nScore B", user, re.S)
        score = 3
        if a and b:
            if a.group(1).strip() == b.group(1).strip():
                score = 5
            else:
                try:
                    aj, bj = json.loads(a.group(1)), json.loads(b.group(1))
                    at = [t["name"] for t in (aj.get("tool_calls") or [])]
                    bt = [t["name"] for t in (bj.get("tool_calls") or [])]
                    if not bj.get("content") and not bt:
                        score = 1
                    elif at != bt:
                        score = 2
                    else:
                        score = 4
                except Exception:
                    score = 3
        return {"content": json.dumps({"score": score, "reason": "mock judge"}), "tool_calls": []}

    correct = pick_tool(user, tools)

    if model in ("mock-baseline", "mock-good"):
        if correct:
            return {"content": "", "tool_calls": [{"name": correct[0], "arguments": json.dumps(correct[1])}]}
        if want_json and "entities" in u:
            return {"content": json.dumps({"person": "Priya Sharma", "email": "priya@nebius.com",
                                           "amount_usd": 48000, "company": "Databricks", "date": "March 3rd"}), "tool_calls": []}
        if want_json:
            return {"content": json.dumps({"category": "outage", "severity": "critical",
                                           "summary": "Inference latency doubled post-deploy; customer timeouts."}), "tool_calls": []}
        if "token" in u:
            return {"content": "A token is a model-level text unit, often a word fragment, while a word is a full linguistic unit.", "tool_calls": []}
        if "cost per hour" in u:
            return {"content": "120*60=7200 calls/hr -> 6.48M in / 2.16M out -> 0.972 + 1.296 = $2.27 per hour (final: $2.27).", "tool_calls": []}
        return {"content": "- Cut cost per request\n- Survive provider outages\n- Match tasks to models", "tool_calls": []}

    if model == "mock-flaky":
        if correct and correct[0] == "book_room":
            return {"content": "", "tool_calls": [{"name": "book_room",
                    "arguments": json.dumps({"room": "Aurora", "start": "14:00"})}]}  # missing 'end'
        if not correct and tools:  # phantom call on the distractor
            return {"content": "", "tool_calls": [{"name": "get_weather", "arguments": json.dumps({"city": "SF"})}]}
        base = respond("mock-baseline", messages, tools, want_json)
        if base["content"]:
            base = dict(base, content=base["content"].split(".")[0] + ".")  # terser, same behavior
        return base

    if model == "mock-bad":
        if correct and correct[0] in ("run_sql",):
            return {"content": "I cannot execute database queries.", "tool_calls": []}  # refusal
        if correct and correct[0] in ("search_documents", "book_room"):
            return {"content": "", "tool_calls": [{"name": "calculator", "arguments": json.dumps({"expression": "1+1"})}]}  # wrong tool
        if not correct and tools:
            return {"content": "", "tool_calls": [{"name": "get_weather", "arguments": "{city: SF}"}]}  # phantom + bad JSON args
        if want_json and "entities" in u:
            return {"content": "Sure! Here are the entities: Priya, priya@nebius.com, $48,000.", "tool_calls": []}  # not JSON
        base = respond("mock-baseline", messages, tools, want_json)
        return dict(base, content=(base["content"] or "")[:40])

    if correct:
        return {"content": "", "tool_calls": [{"name": correct[0], "arguments": json.dumps(correct[1])}]}
    return {"content": "ok", "tool_calls": []}


# ── server ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_response(404); self.end_headers(); return
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        want_json = body.get("response_format", {}).get("type") == "json_object"
        r = respond(body["model"], body["messages"], body.get("tools"), want_json)
        msg = {"role": "assistant", "content": r["content"] or None}
        if r["tool_calls"]:
            msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": t["name"], "arguments": t["arguments"]}}
                                 for i, t in enumerate(r["tool_calls"])]
        out = {"id": "mock", "object": "chat.completion", "model": body["model"],
               "choices": [{"index": 0, "message": msg,
                            "finish_reason": "tool_calls" if r["tool_calls"] else "stop"}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── e2e selftest ──────────────────────────────────────────────────────────────

def selftest(port: int) -> int:
    srv = serve(port)
    base = f"http://127.0.0.1:{port}/v1"
    # server sanity ping
    req = urllib.request.Request(base + "/chat/completions",
        data=json.dumps({"model": "mock-baseline", "messages": [{"role": "user", "content": "weather in SF"},],
                         "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {
                             "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer mock"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read())["choices"][0]["message"]["tool_calls"], "sanity ping failed"

    out = ROOT / "pipeline" / "_e2e_report.json"
    env = dict(os.environ, MOCK_KEY="mock")
    spec = lambda label, model: f"{label}|{base}|{model}|MOCK_KEY"
    proc = subprocess.run([sys.executable, str(ROOT / "pipeline" / "agent_verify.py"),
        "--tasks", str(ROOT / "pipeline" / "agent_tasks.jsonl"),
        "--model", spec("baseline", "mock-baseline"),
        "--model", spec("good", "mock-good"),
        "--model", spec("flaky", "mock-flaky"),
        "--model", spec("bad", "mock-bad"),
        "--judge", spec("judge-1", "mock-judge"),
        "--out", str(out)], env=env, capture_output=True, text=True, timeout=300)
    srv.shutdown()
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    report = json.loads(out.read_text())
    verdicts = {c["model"]: c["verdict"] for c in report["candidates"]}
    checks = [
        ("CLI exit code is 1 (FAIL present -> CI gate fires)", proc.returncode == 1),
        ("good  -> CERTIFIED", verdicts.get("good") == "CERTIFIED"),
        ("flaky -> CAUTION", verdicts.get("flaky") == "CAUTION"),
        ("bad   -> FAIL", verdicts.get("bad") == "FAIL"),
        ("report is not marked sample", report.get("sample") is False),
        ("every candidate has a judged equivalence median",
         all(c["equivalence_median"] is not None for c in report["candidates"])),
    ]
    ok = True
    for name, passed in checks:
        print(("PASS  " if passed else "FAIL  ") + name)
        ok &= passed
    out.unlink(missing_ok=True)
    print("E2E " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--port", type=int, default=8091)
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest(args.port))
    if args.serve:
        print(f"mock fleet on http://127.0.0.1:{args.port}/v1  (Ctrl-C to stop)")
        serve(args.port)
        while True:
            time.sleep(3600)
    ap.print_help()
