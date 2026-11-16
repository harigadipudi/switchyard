#!/usr/bin/env python3
"""Show what the semantic cache does to latency, and who shares its entries.

For each of a list of short, unrelated questions the script sends, through a deployed gateway:

    1. the request                              -> answered by the model, then stored
    2. the identical request again              -> expected to be served from the cache
    3. the identical request with a second key  -> shows whether another developer is served
       (only with --second-key-file)               the first developer's entry

A response served from the cache carries the header `Cached-content: true`. Requests are
non-streaming, single-message and tool-free, which is what the gateway treats as cacheable.
Entries live for the cache TTL (one hour by default), so a second run inside that window
finds its first requests already cached; those are reported separately.

The semantic cache is off unless the proxy was built with SemanticCacheEnabled=true. If no
response is served from the cache, the script says so.

Data-plane requests only. API keys come from files or $GATEWAY_API_KEY and are never
printed; answers are not written to the results file. Standard library only.

    python3 tests/bench_semantic_cache.py --gateway-url http://lab.example.nip.io \
        --api-key-file ~/dev1.key --second-key-file ~/dev2.key --out /tmp/cache.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Unrelated questions: their cache keys are far apart, so one does not match another's entry.
QUESTIONS = [
    "In one sentence, what does the HTTP status code 409 mean?",
    "In one sentence, what is a B-tree index used for in a database?",
    "In one sentence, what does the git rebase command do?",
    "In one sentence, what is the difference between a process and a thread?",
    "In one sentence, what is a Kubernetes liveness probe?",
    "In one sentence, what does idempotent mean for an API call?",
    "In one sentence, what is a TLS certificate chain?",
    "In one sentence, what does the Python keyword yield do?",
    "In one sentence, what is eventual consistency?",
    "In one sentence, what is a memory leak?",
    "In one sentence, what is the purpose of a Dockerfile ENTRYPOINT?",
    "In one sentence, what is a JSON Web Token?",
    "In one sentence, what does CORS protect against?",
    "In one sentence, what is a bloom filter?",
    "In one sentence, what is the CAP theorem about?",
    "In one sentence, what does a load balancer health check do?",
    "In one sentence, what is tail-call optimization?",
    "In one sentence, what is a foreign key constraint?",
    "In one sentence, what does the Unix command chmod change?",
    "In one sentence, what is a circuit breaker in a microservice?",
    "In one sentence, what is garbage collection in a language runtime?",
    "In one sentence, what is a WebSocket?",
    "In one sentence, what is semantic versioning?",
    "In one sentence, what does DNS TTL control?",
]


def percentile(values: List[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - (position - lower)) + ordered[upper] * (position - lower)


def summarize(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values)),
        "p50": round(percentile(values, 0.50)),
        "p90": round(percentile(values, 0.90)),
        "p95": round(percentile(values, 0.95)),
        "max": round(max(values)),
        "mean": round(statistics.fmean(values)),
    }


def read_key(path: str) -> str:
    with open(os.path.expanduser(path), encoding="ascii") as handle:
        return handle.read().strip()


def send(opener: Any, url: str, api_key: str, model: str, prompt: str, max_tokens: int,
         timeout: float) -> Dict[str, Any]:
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-code/cache-bench",
    })
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read()
            status, headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        error.read()
        status, headers = error.code, dict(error.headers)
    except (urllib.error.URLError, OSError) as error:
        return {"status": 0, "error": type(error).__name__, "cached": False,
                "total_ms": round((time.perf_counter() - started) * 1000.0, 1)}
    cached = any(key.lower() == "cached-content" and str(value).lower() == "true" for key, value in headers.items())
    return {"status": status, "cached": cached, "total_ms": round((time.perf_counter() - started) * 1000.0, 1)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway-url", default=os.environ.get("GATEWAY_URL", ""), help="e.g. http://lab.host")
    parser.add_argument("--api-key-file", default="")
    parser.add_argument("--second-key-file", default="", help="key of a different developer, to show sharing")
    parser.add_argument("--model", default="gemini-3.5-flash-lite")
    parser.add_argument("--count", type=int, default=20, help="number of questions (max %d)" % len(QUESTIONS))
    parser.add_argument("--offset", type=int, default=0, help="skip the first N questions")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--settle-ms", type=int, default=1500,
                        help="wait after the first request so its entry is stored before the repeat")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if not args.gateway_url:
        raise SystemExit("--gateway-url (or GATEWAY_URL) is required.")
    api_key = read_key(args.api_key_file) if args.api_key_file else os.environ.get("GATEWAY_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set GATEWAY_API_KEY or pass --api-key-file.")
    second_key = read_key(args.second_key_file) if args.second_key_file else ""

    questions = QUESTIONS[args.offset: args.offset + args.count]
    url = args.gateway_url.rstrip("/") + "/v1/messages"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    rows: List[Dict[str, Any]] = []
    for index, question in enumerate(questions):
        first = send(opener, url, api_key, args.model, question, args.max_tokens, args.timeout)
        time.sleep(args.settle_ms / 1000.0)
        repeat = send(opener, url, api_key, args.model, question, args.max_tokens, args.timeout)
        row: Dict[str, Any] = {"question": args.offset + index, "first": first, "repeat": repeat}
        if second_key:
            row["other_developer"] = send(opener, url, second_key, args.model, question, args.max_tokens, args.timeout)
        rows.append(row)
        if (index + 1) % 5 == 0:
            print("  %d/%d questions" % (index + 1, len(questions)), file=sys.stderr, flush=True)

    def ok(entry: Dict[str, Any]) -> bool:
        return entry.get("status") == 200

    first_model = [r["first"]["total_ms"] for r in rows if ok(r["first"]) and not r["first"]["cached"]]
    first_warm = [r["first"]["total_ms"] for r in rows if ok(r["first"]) and r["first"]["cached"]]
    repeat_hit = [r["repeat"]["total_ms"] for r in rows if ok(r["repeat"]) and r["repeat"]["cached"]]
    repeat_miss = [r["repeat"]["total_ms"] for r in rows if ok(r["repeat"]) and not r["repeat"]["cached"]]
    summary: Dict[str, Any] = {
        "questions": len(rows),
        "model": args.model,
        "first_request_answered_by_model_ms": summarize(first_model),
        "first_request_already_cached_ms": summarize(first_warm),
        "repeat_served_from_cache_ms": summarize(repeat_hit),
        "repeat_not_served_from_cache_ms": summarize(repeat_miss),
        "http_errors": sum(1 for r in rows for k in ("first", "repeat", "other_developer")
                           if k in r and not ok(r[k])),
    }
    if second_key:
        other = [r["other_developer"] for r in rows if ok(r["other_developer"])]
        summary["other_developer_served_from_cache"] = sum(1 for entry in other if entry["cached"])
        summary["other_developer_requests"] = len(other)
        summary["other_developer_served_from_cache_ms"] = summarize([e["total_ms"] for e in other if e["cached"]])
    if first_model and repeat_hit:
        summary["median_speedup"] = round(percentile(first_model, 0.5) / max(percentile(repeat_hit, 0.5), 1.0), 1)
    if not repeat_hit and not first_warm:
        summary["note"] = "No response was served from the cache: the semantic cache is switched off here."

    print(json.dumps(summary, indent=2))
    if args.out:
        with open(os.path.expanduser(args.out), "w", encoding="utf-8") as handle:
            json.dump({"gateway": args.gateway_url, "summary": summary, "rows": rows}, handle, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
