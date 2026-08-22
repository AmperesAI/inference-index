#!/usr/bin/env python3
"""Agent Verify — certify that an agent behaves the same after a model swap.

The problem: agent platforms run the same agent on different models
(cost, availability, routing) and the agent's *behavior* silently changes —
different tools called, malformed arguments, broken JSON, softer answers.
Nobody measures this before shipping the swap.

Agent Verify runs one task suite across a baseline model and N candidate
models on ANY OpenAI-compatible endpoint (OpenAI, Nebius Token Factory,
Databricks serving endpoints, Together, Groq, vLLM, ...) and scores each
candidate on behavioral-equivalence dimensions:

  1. tool_fidelity   — calls the same tool the baseline called
  2. arg_validity    — tool arguments parse and contain the required keys
  3. schema_validity — JSON-output tasks return parseable, schema-shaped JSON
  4. non_refusal     — no refusal/empty output on benign tasks
  5. equivalence     — panel of judge models scores candidate vs baseline 1–5
                       (reported as median + spread, per the Inference Index
                       10-judge methodology: never trust a single judge)

Verdicts: CERTIFIED (safe to swap) / CAUTION / FAIL — thresholds below.

Usage:
  python3 pipeline/agent_verify.py \
    --tasks pipeline/agent_tasks.jsonl \
    --model "gpt-5|https://api.openai.com/v1|gpt-5|OPENAI_API_KEY" \
    --model "haiku|https://api.anthropic.com/v1|claude-haiku-4-5|ANTHROPIC_API_KEY" \
    --model "llama-8b|https://api.tokenfactory.nebius.com/v1|meta-llama/Meta-Llama-3.1-8B-Instruct-fast|NEBIUS_API_KEY" \
    --judge "gemini-2.5-pro|https://generativelanguage.googleapis.com/v1beta/openai|gemini-2.5-pro|GEMINI_API_KEY" \
    --out docs/agent-verify-report.json

The FIRST --model is the baseline; the rest are swap candidates.
--judge may be repeated to grow the judge panel. Zero third-party deps.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

CERTIFY_TOOL_FIDELITY = 0.95
CERTIFY_EQUIV_MEDIAN = 4.0
CAUTION_TOOL_FIDELITY = 0.80


def parse_model(spec: str) -> dict:
    label, base_url, model_id, key_env = spec.split("|")
    key = os.environ.get(key_env, "")
    if not key:
        sys.exit(f"error: env var {key_env} is empty (needed for {label})")
    return {"label": label, "base_url": base_url.rstrip("/"), "model": model_id, "key": key}


def chat(endpoint: dict, messages: list, tools: list | None = None, response_json: bool = False) -> dict:
    body: dict = {"model": endpoint["model"], "messages": messages}
    if tools:
        body["tools"] = tools
    if response_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        endpoint["base_url"] + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {endpoint['key']}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    msg = out["choices"][0]["message"]
    return {
        "content": msg.get("content") or "",
        "tool_calls": [
            {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments", "{}")}
            for tc in (msg.get("tool_calls") or [])
        ],
        "latency_ms": round((time.time() - t0) * 1000),
        "finish_reason": out["choices"][0].get("finish_reason"),
    }


REFUSAL_MARKERS = ("i cannot", "i can't", "i'm unable", "i am unable", "as an ai")


def run_task(endpoint: dict, task: dict) -> dict:
    messages = []
    if task.get("system"):
        messages.append({"role": "system", "content": task["system"]})
    messages.append({"role": "user", "content": task["user"]})
    try:
        r = chat(endpoint, messages, task.get("tools"), task.get("expect", {}).get("json_output", False))
    except Exception as e:  # endpoint/network error is a result, not a crash
        return {"error": str(e)[:200]}
    return r


def score_candidate(task: dict, base: dict, cand: dict) -> dict:
    expect = task.get("expect", {})
    s: dict = {}
    if cand.get("error"):
        return {"error": cand["error"]}

    base_tools = [t["name"] for t in base.get("tool_calls", [])]
    cand_tools = [t["name"] for t in cand.get("tool_calls", [])]
    if task.get("tools"):
        # tool fidelity: candidate makes the same tool decision the baseline made
        s["tool_match"] = (cand_tools == base_tools) if not base.get("error") else None
        ok = True
        for tc in cand.get("tool_calls", []):
            try:
                args = json.loads(tc["arguments"])
                for req_key in expect.get("required_args", []):
                    if req_key not in args:
                        ok = False
            except json.JSONDecodeError:
                ok = False
        s["args_valid"] = ok if cand_tools else None

    if expect.get("json_output"):
        try:
            parsed = json.loads(cand["content"])
            s["schema_valid"] = all(k in parsed for k in expect.get("required_keys", []))
        except (json.JSONDecodeError, TypeError):
            s["schema_valid"] = False

    text = (cand.get("content") or "").lower()
    s["refused"] = any(m in text for m in REFUSAL_MARKERS) or (
        not cand.get("content") and not cand.get("tool_calls")
    )
    return s


JUDGE_PROMPT = """You are judging whether two AI agent responses to the same task are behaviorally equivalent.
Task given to both agents:
{task}

RESPONSE A (baseline model):
{base}

RESPONSE B (candidate model):
{cand}

Score B against A on a 1-5 scale: 5 = fully equivalent behavior and quality; 4 = minor differences, same outcome; 3 = noticeable degradation but usable; 2 = wrong or materially worse; 1 = failure/refusal/breakage.
Reply with ONLY a JSON object: {{"score": <1-5>, "reason": "<one sentence>"}}"""


def judge_pair(judges: list, task: dict, base: dict, cand: dict) -> list:
    scores = []
    base_repr = json.dumps({"content": base.get("content"), "tool_calls": base.get("tool_calls")})
    cand_repr = json.dumps({"content": cand.get("content"), "tool_calls": cand.get("tool_calls")})
    for j in judges:
        try:
            r = chat(j, [{"role": "user", "content": JUDGE_PROMPT.format(
                task=task["user"], base=base_repr, cand=cand_repr)}], response_json=True)
            scores.append({"judge": j["label"], "score": json.loads(r["content"])["score"]})
        except Exception as e:
            scores.append({"judge": j["label"], "error": str(e)[:120]})
    return scores


def pct(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(100 * sum(1 for v in vals if v) / len(vals), 1) if vals else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--model", action="append", required=True,
                    help='label|base_url|model_id|KEY_ENV — first is baseline')
    ap.add_argument("--judge", action="append", default=[],
                    help="same format; repeat to grow the judge panel")
    ap.add_argument("--out", default="docs/agent-verify-report.json")
    args = ap.parse_args()

    tasks = [json.loads(l) for l in Path(args.tasks).read_text().splitlines() if l.strip()]
    endpoints = [parse_model(m) for m in args.model]
    judges = [parse_model(j) for j in args.judge]
    baseline, candidates = endpoints[0], endpoints[1:]
    if not candidates:
        sys.exit("error: need at least one candidate --model after the baseline")

    print(f"baseline={baseline['label']}  candidates={[c['label'] for c in candidates]}  "
          f"judges={[j['label'] for j in judges]}  tasks={len(tasks)}")

    base_runs = {t["id"]: run_task(baseline, t) for t in tasks}
    report_models = []
    for cand in candidates:
        rows, equiv_medians = [], []
        for t in tasks:
            cr = run_task(cand, t)
            scores = score_candidate(t, base_runs[t["id"]], cr)
            jscores = judge_pair(judges, t, base_runs[t["id"]], cr) if judges and not cr.get("error") else []
            ok_scores = [s["score"] for s in jscores if "score" in s]
            if ok_scores:
                equiv_medians.append(statistics.median(ok_scores))
            rows.append({"task": t["id"], **scores, "latency_ms": cr.get("latency_ms"),
                         "judge_scores": jscores})
            print(f"  [{cand['label']}] {t['id']}: {scores}")
        tool_fid = pct([r.get("tool_match") for r in rows])
        agg = {
            "model": cand["label"], "model_id": cand["model"], "endpoint": cand["base_url"],
            "tool_fidelity_pct": tool_fid,
            "arg_validity_pct": pct([r.get("args_valid") for r in rows]),
            "schema_validity_pct": pct([r.get("schema_valid") for r in rows]),
            "refusal_pct": pct([r.get("refused") for r in rows]),
            "equivalence_median": round(statistics.median(equiv_medians), 1) if equiv_medians else None,
            "equivalence_spread": f"{min(equiv_medians)}–{max(equiv_medians)}" if equiv_medians else None,
            "mean_latency_ms": round(statistics.mean([r["latency_ms"] for r in rows if r.get("latency_ms")])) if any(r.get("latency_ms") for r in rows) else None,
            "tasks": rows,
        }
        tf, eq = agg["tool_fidelity_pct"], agg["equivalence_median"]
        if (tf is None or tf >= 100 * CERTIFY_TOOL_FIDELITY) and (eq is None or eq >= CERTIFY_EQUIV_MEDIAN) and (agg["refusal_pct"] or 0) == 0:
            agg["verdict"] = "CERTIFIED"
        elif tf is not None and tf < 100 * CAUTION_TOOL_FIDELITY:
            agg["verdict"] = "FAIL"
        else:
            agg["verdict"] = "CAUTION"
        report_models.append(agg)

    out = {
        "sample": False,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "baseline": {"model": baseline["label"], "model_id": baseline["model"], "endpoint": baseline["base_url"]},
        "judge_panel": [j["label"] for j in judges],
        "task_count": len(tasks),
        "candidates": report_models,
        "methodology": "Behavioral equivalence vs baseline: deterministic checks (tool fidelity, arg validity, schema validity, refusals) + judge-panel equivalence (median + spread, never a single judge).",
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    for m in report_models:
        print(f"{m['model']}: {m['verdict']}  tool={m['tool_fidelity_pct']}%  equiv={m['equivalence_median']}")
    print(f"wrote {args.out}")
    # CI gate: a FAILing swap candidate fails the build
    if any(m["verdict"] == "FAIL" for m in report_models):
        sys.exit(1)


if __name__ == "__main__":
    main()
