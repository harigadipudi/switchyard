#!/usr/bin/env python3
"""Call the Smart Router's LLM judge directly and compare its labels with a labelled set.

Sends the payload `SC-ModelJudge` sends (features/ai-model-preprocess.yaml) straight to
Vertex AI, so the judge model and its instruction can be evaluated without a gateway:

    v1  the stage 05 instruction (four labels, no definitions)
    v2  the stage 06 instruction (the same labels with definitions)

Latency here is client -> Vertex AI from wherever this runs; use
tests/bench_router_classifiers.py for the latency a deployed gateway sees.

Client-side API calls only, authenticated with Application Default Credentials. Prompt
text is not written to the results file. Standard library only.

    GCP_PROJECT_ID=your-project python3 tests/bench_llm_judge.py \
        --out /tmp/judge.json gemini-3.5-flash-lite@v1 gemini-3.5-flash-lite@v2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

TIERS = ("tier1-fast", "tier4-open", "tier2-daily", "tier3-frontier")  # cheapest first

INSTRUCTIONS = {
    "v1": "Classify the complexity of this software engineering request into exactly one token: "
          "SIMPLE, MEDIUM, COMPLEX, or REASONING.",
    "v2": "Classify this developer request for model routing. Answer with exactly one token: "
          "SIMPLE, MEDIUM, COMPLEX, or REASONING.\n"
          "SIMPLE = a quick factual answer, definition, short explanation, greeting, status check or "
          "one-line command.\n"
          "MEDIUM = write one small self-contained piece of code such as a function, test, script, query "
          "or config file, or work a math exercise.\n"
          "COMPLEX = change, fix, review or extend existing code across files, or find the cause of a "
          "failure in an existing codebase.\n"
          "REASONING = design a system, compare design options and trade-offs, or reason about "
          "concurrency, scale, security or failure modes.",
}


def access_token() -> str:
    env_token = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    return subprocess.check_output(
        ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
        text=True, stderr=subprocess.DEVNULL).strip()


def map_label(text: str) -> str:
    """Same precedence as src/ai-functions/applySmartJudgeSelection.js."""
    clean = (text or "").strip().lower()
    if "tier1-fast" in clean or "simple" in clean:
        return "tier1-fast"
    if "tier3-frontier" in clean or "reasoning" in clean:
        return "tier3-frontier"
    if "tier4-open" in clean or "medium" in clean:
        return "tier4-open"
    return "tier2-daily"


def judge(project: str, model: str, variant: str, prompt: str, token: str) -> Dict[str, Any]:
    url = ("https://aiplatform.googleapis.com/v1/projects/%s/locations/global/publishers/google/models/%s"
           ":generateContent" % (project, model))
    body = {
        "contents": [{"role": "user", "parts": [{"text": INSTRUCTIONS[variant] + "\nRequest: " + prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 64,
                             "thinkingConfig": {"thinkingLevel": "MINIMAL"}},
    }
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json", "X-Goog-User-Project": project})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = response.status
    except urllib.error.HTTPError as error:
        return {"status": error.code, "ms": (time.perf_counter() - started) * 1000.0, "text": "",
                "error": error.read().decode("utf-8", "ignore")[:300]}
    elapsed = (time.perf_counter() - started) * 1000.0
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = ""
    usage = payload.get("usageMetadata") or {}
    return {"status": status, "ms": elapsed, "text": text,
            "prompt_tokens": usage.get("promptTokenCount"), "output_tokens": usage.get("candidatesTokenCount"),
            "thought_tokens": usage.get("thoughtsTokenCount")}


def percentile(values: List[float], fraction: float) -> float:
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - (position - lower)) + ordered[upper] * (position - lower)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("specs", nargs="*", default=["gemini-3.5-flash-lite@v1", "gemini-3.5-flash-lite@v2"],
                        help="MODEL@v1 or MODEL@v2")
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""))
    parser.add_argument("--eval", default="tests/data/routing_eval.jsonl")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("--project (or GCP_PROJECT_ID) is required.")

    with open(args.eval, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    token = access_token()
    order = {tier: index for index, tier in enumerate(TIERS)}
    report: Dict[str, Any] = {}
    for spec in args.specs:
        model, _, variant = spec.partition("@")
        variant = variant or "v1"
        if variant not in INSTRUCTIONS:
            raise SystemExit("unknown instruction %r in %r (use v1 or v2)" % (variant, spec))
        # One untimed call so connection setup is not counted in the first sample, and so a
        # model the project cannot call is reported once instead of a hundred times.
        warm = judge(args.project, model, variant, "hello", token)
        if warm["status"] != 200:
            print("%s: HTTP %s %s" % (spec, warm["status"], warm.get("error", "")))
            report[spec] = {"error": "HTTP %s" % warm["status"], "detail": warm.get("error", "")}
            continue
        results = []
        for row in rows:
            answer = judge(args.project, model, variant, row["prompt"], token)
            results.append({
                "id": row["id"], "gold": row["tier"], "status": answer["status"],
                "raw": (answer["text"] or "").strip()[:40],
                "tier": map_label(answer["text"]) if answer["status"] == 200 else None,
                "ms": round(answer["ms"], 1), "prompt_tokens": answer.get("prompt_tokens"),
                "output_tokens": answer.get("output_tokens"), "thought_tokens": answer.get("thought_tokens")})
        ok = [r for r in results if r["status"] == 200]
        latencies = [r["ms"] for r in ok]
        confusion = {gold: {pred: 0 for pred in TIERS} for gold in TIERS}
        for r in ok:
            confusion[r["gold"]][r["tier"]] += 1
        summary = {
            "n": len(results), "ok": len(ok),
            "same_tier_rate": round(sum(1 for r in ok if r["tier"] == r["gold"]) / len(ok), 3) if ok else None,
            "under_routed": sum(1 for r in ok if order[r["tier"]] < order[r["gold"]]),
            "over_routed": sum(1 for r in ok if order[r["tier"]] > order[r["gold"]]),
            "latency_ms": {
                "min": round(min(latencies), 1), "p50": round(percentile(latencies, 0.5), 1),
                "p90": round(percentile(latencies, 0.9), 1), "p95": round(percentile(latencies, 0.95), 1),
                "max": round(max(latencies), 1), "mean": round(statistics.fmean(latencies), 1),
            } if latencies else {},
            "raw_labels": sorted({r["raw"] for r in ok})[:12],
            "confusion": confusion,
        }
        report[spec] = {"summary": summary, "results": results}
        print(spec, json.dumps(summary))
    if args.out:
        with open(os.path.expanduser(args.out), "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
