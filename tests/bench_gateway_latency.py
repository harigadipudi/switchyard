#!/usr/bin/env python3
"""Measure what the gateway and the Smart Router add to a request, end to end.

Sends the same kind of short question four ways and times each one from the client:

    direct       client -> Vertex AI, no gateway (optional baseline, needs ADC)
    explicit     client -> gateway, a named model, so the Smart Router is not involved
    auto-rule    client -> gateway, `model: auto`, a prompt a keyword rule decides
    auto-judge   client -> gateway, `model: auto`, a prompt only the classifier can decide

All four end at the same model (--model, the tier1-fast target of `auto`), so the differences
are the gateway and the router, not the model. By default, persistent HTTP/1.1 keep-alive
connections are warmed once and reused across rounds (matching active CLI/IDE sessions) so
TLS/TCP setup jitter on `direct` HTTPS vs gateway HTTP does not skew the proxy delta; pass
`--no-keepalive` to open a fresh connection on every call.

Requests stream; the script reports time to first byte and total time, plus the gateway's
attribution headers:

    X-Router-Classifier      which step decided: rules | gemini | custom | length-fallback
    X-Router-Classifier-Ms   time the classifier call added inside the gateway
    X-Vertex-Target-Model    model the request was routed to

Scenarios are interleaved round by round, so drift over the run affects all of them equally.

Data-plane requests only: nothing here touches the Apigee control plane. The API key comes
from --api-key-file or $GATEWAY_API_KEY and is never printed. Standard library only.

    GATEWAY_API_KEY=... python3 tests/bench_gateway_latency.py \
        --gateway-url http://lab.example.nip.io --direct --project my-project --out /tmp/latency.json
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import ssl
import statistics
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# (a, b, words) - "what is a * b?" matches the gateway's trivial-arithmetic rule (tier1-fast);
# the worded form matches no rule, so only the classifier can decide it.
PAIRS = [
    (17, 23, "seventeen times twenty-three"),
    (12, 19, "twelve times nineteen"),
    (31, 14, "thirty-one times fourteen"),
    (26, 11, "twenty-six times eleven"),
    (15, 15, "fifteen times fifteen"),
    (22, 13, "twenty-two times thirteen"),
    (18, 21, "eighteen times twenty-one"),
    (27, 16, "twenty-seven times sixteen"),
    (33, 12, "thirty-three times twelve"),
    (24, 25, "twenty-four times twenty-five"),
]
SCENARIOS = ("direct", "explicit", "auto-rule", "auto-judge")
HEADERS_OF_INTEREST = (
    "X-Router-Classifier",
    "X-Router-Classifier-Ms",
    "X-Vertex-Target-Tier",
    "X-Vertex-Target-Model",
)


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


def header(headers: Dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


class EndpointClient:
    """Thin HTTP/1.1 client supporting either persistent keep-alive or fresh connections."""

    def __init__(self, base_url: str, timeout: float, keepalive: bool) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        self.scheme = parsed.scheme or "http"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.timeout = timeout
        self.keepalive = keepalive
        self._ssl_ctx = ssl.create_default_context() if self.scheme == "https" else None
        self._conn: Optional[http.client.HTTPConnection] = None

    def _connect(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=self._ssl_ctx
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def post_stream(self, path_and_query: str, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
        req_headers = dict(headers)
        req_headers["Connection"] = "keep-alive" if self.keepalive else "close"
        req_headers["Content-Length"] = str(len(body))
        full_path = self.base_path + path_and_query

        for attempt in range(2):
            if not self.keepalive or self._conn is None:
                self.close()
                self._conn = self._connect()
            conn = self._conn
            started = time.perf_counter()
            try:
                conn.request("POST", full_path, body=body, headers=req_headers)
                response = conn.getresponse()
                status = response.status
                resp_headers = dict(response.getheaders())
                if status == 200:
                    first = response.read(1)
                    first_byte_ms = (time.perf_counter() - started) * 1000.0
                    rest = response.read()
                    total_ms = (time.perf_counter() - started) * 1000.0
                    if not self.keepalive or header(resp_headers, "connection").lower() == "close":
                        self.close()
                    return {
                        "status": status,
                        "ttfb_ms": round(first_byte_ms, 1),
                        "total_ms": round(total_ms, 1),
                        "bytes": len(first) + len(rest),
                        "headers": resp_headers,
                    }
                response.read()
                total_ms = (time.perf_counter() - started) * 1000.0
                if not self.keepalive or header(resp_headers, "connection").lower() == "close":
                    self.close()
                return {"status": status, "total_ms": round(total_ms, 1), "headers": resp_headers}
            except (http.client.HTTPException, OSError) as error:
                self.close()
                if self.keepalive and attempt == 0:
                    # Server closed an idle keep-alive socket between rounds; reconnect cleanly.
                    continue
                return {
                    "status": 0,
                    "error": type(error).__name__,
                    "total_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "headers": {},
                }
        return {"status": 0, "error": "RetryExhausted", "total_ms": 0.0, "headers": {}}


def gateway_payload(api_key: str, model: str, prompt: str, max_tokens: int) -> Tuple[str, bytes, Dict[str, str]]:
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-code/latency-bench",
        "X-Bypass-Cache": "true",
    }
    return "/v1/messages", body, headers


def direct_payload(project: str, model: str, prompt: str, token: str, max_tokens: int) -> Tuple[str, bytes, Dict[str, str]]:
    path = (
        "/v1/projects/%s/locations/global/publishers/google/models/%s:streamGenerateContent?alt=sse"
        % (project, model)
    )
    # Match buildVertexGeminiPayload in src/ai-functions/transformPayload.js (maxOutputTokens >= 1024, temperature = 1.0)
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 1.0},
    }).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "X-Goog-User-Project": project,
    }
    return path, body, headers


def access_token() -> str:
    env_token = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    return subprocess.check_output(
        ["gcloud", "auth", "application-default", "print-access-token", "--quiet"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def report(name: str, rows: List[Dict[str, Any]], expected_model: str) -> Dict[str, Any]:
    ok = [r for r in rows if r["status"] == 200 and "ttfb_ms" in r]
    on_model = [r for r in ok if name == "direct" or r.get("X-Vertex-Target-Model") == expected_model]
    decided: Dict[str, int] = {}
    models: Dict[str, int] = {}
    for r in ok:
        if name != "direct":
            by = r.get("X-Router-Classifier", "(not routed)")
            decided[by] = decided.get(by, 0) + 1
            mdl = r.get("X-Vertex-Target-Model", "?")
            models[mdl] = models.get(mdl, 0) + 1
    statuses: Dict[str, int] = {}
    for r in rows:
        statuses[str(r["status"])] = statuses.get(str(r["status"]), 0) + 1
    classifier_ms = [float(r["X-Router-Classifier-Ms"]) for r in on_model if r.get("X-Router-Classifier-Ms")]
    return {
        "scenario": name,
        "requests": len(rows),
        "http_status": statuses,
        "on_expected_model": len(on_model),
        "decided_by": decided,
        "routed_model": models,
        "time_to_first_byte_ms": summarize([r["ttfb_ms"] for r in on_model]),
        "total_ms": summarize([r["total_ms"] for r in on_model]),
        "classifier_ms": summarize(classifier_ms),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway-url", default=os.environ.get("GATEWAY_URL", ""), help="e.g. http://lab.host")
    parser.add_argument("--api-key-file", default="")
    parser.add_argument(
        "--model",
        default="gemini-3.5-flash-lite",
        help="model for the explicit and direct scenarios; should be the tier1-fast target of auto",
    )
    parser.add_argument("--rounds", type=int, default=30, help="requests per scenario")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--direct", action="store_true", help="also call Vertex AI without the gateway (needs ADC)")
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", ""), help="project for --direct")
    parser.add_argument(
        "--direct-max-tokens",
        type=int,
        default=1024,
        help="output limit for the direct call; the gateway sends at least 1024 to Gemini models",
    )
    parser.add_argument("--no-keepalive", action="store_true", help="open a fresh TCP/TLS connection per request")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--pause-ms", type=int, default=0, help="wait between requests")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    if not args.gateway_url:
        raise SystemExit("--gateway-url (or GATEWAY_URL) is required.")
    api_key = os.environ.get("GATEWAY_API_KEY", "").strip()
    if args.api_key_file:
        with open(os.path.expanduser(args.api_key_file), encoding="ascii") as handle:
            api_key = handle.read().strip()
    if not api_key:
        raise SystemExit("Set GATEWAY_API_KEY or pass --api-key-file.")
    scenarios = [s for s in SCENARIOS if s != "direct" or args.direct]
    token = ""
    if args.direct:
        if not args.project:
            raise SystemExit("--direct needs --project (or GCP_PROJECT_ID).")
        token = access_token()

    keepalive = not args.no_keepalive
    gw_client = EndpointClient(args.gateway_url.rstrip("/"), args.timeout, keepalive=keepalive)
    direct_client = (
        EndpointClient("https://aiplatform.googleapis.com", args.timeout, keepalive=keepalive)
        if args.direct
        else None
    )

    try:
        # Warm sockets once when keep-alive is enabled so round 0 does not pay cold TCP/TLS setup.
        if keepalive:
            p_path, p_body, p_hdrs = gateway_payload(api_key, args.model, "what is 2 * 3?", args.max_tokens)
            gw_client.post_stream(p_path, p_body, p_hdrs)
            if direct_client is not None:
                d_path, d_body, d_hdrs = direct_payload(
                    args.project, args.model, "what is 2 * 3?", token, args.direct_max_tokens
                )
                direct_client.post_stream(d_path, d_body, d_hdrs)

        results: Dict[str, List[Dict[str, Any]]] = {name: [] for name in scenarios}
        for index in range(args.rounds):
            a, b, words = PAIRS[index % len(PAIRS)]
            digits_prompt = "what is %d * %d?" % (a, b)
            words_prompt = "What is %s?" % words
            for name in scenarios:
                if name == "direct":
                    assert direct_client is not None
                    path, body, hdrs = direct_payload(
                        args.project, args.model, digits_prompt, token, args.direct_max_tokens
                    )
                    record = direct_client.post_stream(path, body, hdrs)
                elif name == "explicit":
                    path, body, hdrs = gateway_payload(api_key, args.model, digits_prompt, args.max_tokens)
                    record = gw_client.post_stream(path, body, hdrs)
                elif name == "auto-rule":
                    path, body, hdrs = gateway_payload(api_key, "auto", digits_prompt, args.max_tokens)
                    record = gw_client.post_stream(path, body, hdrs)
                else:
                    path, body, hdrs = gateway_payload(api_key, "auto", words_prompt, args.max_tokens)
                    record = gw_client.post_stream(path, body, hdrs)
                headers = record.pop("headers")
                for wanted in HEADERS_OF_INTEREST:
                    value = header(headers, wanted)
                    if value:
                        record[wanted] = value
                record["round"] = index
                results[name].append(record)
                if args.pause_ms:
                    time.sleep(args.pause_ms / 1000.0)
            if (index + 1) % 5 == 0:
                print("  %d/%d rounds" % (index + 1, args.rounds), file=sys.stderr, flush=True)
    finally:
        gw_client.close()
        if direct_client is not None:
            direct_client.close()

    summary = [report(name, results[name], args.model) for name in scenarios]
    by_name = {s["scenario"]: s for s in summary}

    def p50(name: str, field: str) -> Optional[float]:
        entry = by_name.get(name, {}).get(field, {})
        return entry.get("p50") if entry.get("n") else None

    derived: Dict[str, Any] = {"connection_mode": "keep-alive" if keepalive else "fresh-per-request"}
    if p50("explicit", "time_to_first_byte_ms") is not None and p50("direct", "time_to_first_byte_ms") is not None:
        derived["gateway_adds_ttfb_p50_ms"] = (
            p50("explicit", "time_to_first_byte_ms") - p50("direct", "time_to_first_byte_ms")
        )
    if p50("auto-rule", "time_to_first_byte_ms") is not None and p50("explicit", "time_to_first_byte_ms") is not None:
        derived["rule_routing_adds_ttfb_p50_ms"] = (
            p50("auto-rule", "time_to_first_byte_ms") - p50("explicit", "time_to_first_byte_ms")
        )
    if p50("auto-judge", "time_to_first_byte_ms") is not None and p50("auto-rule", "time_to_first_byte_ms") is not None:
        derived["classifier_adds_ttfb_p50_ms"] = (
            p50("auto-judge", "time_to_first_byte_ms") - p50("auto-rule", "time_to_first_byte_ms")
        )

    print(json.dumps({"summary": summary, "derived_from_medians": derived}, indent=2))
    if args.out:
        with open(os.path.expanduser(args.out), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "gateway": args.gateway_url,
                    "model": args.model,
                    "rounds": args.rounds,
                    "connection_mode": "keep-alive" if keepalive else "fresh-per-request",
                    "summary": summary,
                    "derived_from_medians": derived,
                    "rows": results,
                },
                handle,
                indent=1,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
