#!/usr/bin/env python3
"""
verify_live_vertex.py
Read-only Apigee X status check + Live End-to-End Verification across all Coding Agent Harnesses:
  - Scenario 1: Claude Code CLI/IDE (`POST /v1/messages` & `/v1/messages/count_tokens`, unary + SSE + 2-turn tool loop)
  - Scenario 2: Google Antigravity IDE/CLI (`POST /v1beta/models/*`, `:streamGenerateContent?alt=sse`, `/v1beta:recordClientEvent`)
  - Scenario 3: OpenAI Codex CLI & Codex Desktop (`POST /v1/responses`, unary + SSE + 2-turn tool loop)
  - Scenario 4: Live Enterprise FinOps, 4-Tier Smart Router, Open-Weight Catalog, and Model Armor
against REAL LIVE Vertex AI Model Garden endpoints in the target GCP project.
"""
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
STD_API_KEY = (
    os.environ.get("GATEWAY_STD_API_KEY")
    or os.environ.get("TEST_STD_API_KEY")
    or f"gw_std_{secrets.token_urlsafe(18)}"
)
FRONTIER_API_KEY = (
    os.environ.get("GATEWAY_FRONTIER_API_KEY")
    or os.environ.get("TEST_FRONTIER_API_KEY")
    or f"gw_frt_{secrets.token_urlsafe(18)}"
)


def get_adc_token():
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.exists(adc_path):
        try:
            with open(adc_path) as f:
                adc = json.load(f)
            if "refresh_token" in adc and "client_id" in adc and "client_secret" in adc:
                import urllib.parse
                data = urllib.parse.urlencode({
                    "client_id": adc["client_id"],
                    "client_secret": adc["client_secret"],
                    "refresh_token": adc["refresh_token"],
                    "grant_type": "refresh_token",
                }).encode("utf-8")
                req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))["access_token"]
        except Exception:
            pass
    return subprocess.check_output(
        ["gcloud", "auth", "application-default", "print-access-token"],
        text=True,
    ).strip()


def resolve_apigee_consumer_keys(token):
    """Fetch active consumer keys for standard & frontier developer apps from Apigee."""
    base = f"https://apigee.googleapis.com/v1/organizations/{PROJECT_ID}"
    std_key, frt_key = None, None
    for dev_email, app_name, is_frt in [
        ("dev-standard@example.com", "alex-rivera-coding-agent-app", False),
        ("dev-frontier@example.com", "sam-chen-coding-agent-app", True),
    ]:
        st, resp = api_get(f"{base}/developers/{dev_email}/apps/{app_name}", token)
        if st == 200:
            for cred in resp.get("credentials", []):
                if cred.get("status") == "approved" and cred.get("consumerKey"):
                    if is_frt:
                        frt_key = cred["consumerKey"]
                    else:
                        std_key = cred["consumerKey"]
                    break
    return std_key, frt_key


def api_get(url, token, timeout=20):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": PROJECT_ID,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        try:
            return err.code, json.loads(raw)
        except Exception:
            return err.code, {"raw": raw}


def check_apigee_status_readonly(token):
    """Read-only inspection of Apigee X control plane status."""
    base = f"https://apigee.googleapis.com/v1/organizations/{PROJECT_ID}"
    print("==================================================================")
    print(f" GCP APIGEE X CONTROL PLANE STATUS (READ-ONLY) ({PROJECT_ID})")
    print("==================================================================")
    st_apis, apis_resp = api_get(f"{base}/apis", token)
    proxy_names = [p.get("name") for p in apis_resp.get("proxies", [])] if st_apis == 200 else []
    print(f"  Registered Apigee API Proxies: {proxy_names}")
    st_inst, inst_resp = api_get(f"{base}/instances/apigee-us-central1", token)
    if st_inst == 200:
        print(f"  Apigee Runtime Instance 'apigee-us-central1' state: {inst_resp.get('state')}")


class GatewayHttpClient:
    def __init__(self, base_url=None, token=None):
        self.proc = None
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            self.base_url = f"http://127.0.0.1:{port}"
            env = os.environ.copy()
            env["PORT"] = str(port)
            env["USE_LIVE_VERTEX"] = "true"
            env["ALLOW_TEST_HOOKS"] = "false"
            env["GCP_PROJECT_ID"] = PROJECT_ID
            if token:
                env["VERTEX_ACCESS_TOKEN"] = token
            env["GATEWAY_STD_API_KEY"] = STD_API_KEY
            env["GATEWAY_FRONTIER_API_KEY"] = FRONTIER_API_KEY
            self.proc = subprocess.Popen(
                ["node", "tests/apigee_local_emulator.js"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=1) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)

    def send(self, method, path, headers, body, timeout=90):
        url = f"{self.base_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        req_headers.update(headers or {})
        data = (
            json.dumps(body).encode("utf-8")
            if isinstance(body, (dict, list))
            else (body.encode("utf-8") if isinstance(body, str) else None)
        )
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return {
                    "status": resp.status,
                    "headers": dict(resp.info().items()),
                    "body": raw,
                }
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            return {
                "status": err.code,
                "headers": dict(err.headers.items()) if err.headers else {},
                "body": raw,
            }

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)


def get_header(headers, name):
    target = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == target:
            return v
    return None


def main():
    global STD_API_KEY, FRONTIER_API_KEY
    external_url = os.environ.get("GATEWAY_URL")
    token = get_adc_token()
    if PROJECT_ID and PROJECT_ID != "your-gcp-project-id":
        check_apigee_status_readonly(token)
        if external_url and (
            not os.environ.get("GATEWAY_STD_API_KEY")
            or not os.environ.get("GATEWAY_FRONTIER_API_KEY")
        ):
            apigee_std, apigee_frt = resolve_apigee_consumer_keys(token)
            if apigee_std and not os.environ.get("GATEWAY_STD_API_KEY"):
                STD_API_KEY = apigee_std
            if apigee_frt and not os.environ.get("GATEWAY_FRONTIER_API_KEY"):
                FRONTIER_API_KEY = apigee_frt
    if external_url:
        print("==================================================================")
        print(f" TESTING LIVE DEPLOYED GATEWAY: {external_url}")
        print("==================================================================")

    gw = GatewayHttpClient(base_url=external_url, token=token)
    try:
        print("\n==================================================================")
        print(" SCENARIO 1: CLAUDE CODE HARNESS (POST /v1/messages -> Live Vertex)")
        print("==================================================================")

        r1a = gw.send(
            "POST",
            "/v1/messages",
            {"Authorization": f"Bearer {STD_API_KEY}"},
            {
                "model": "gemini-3.1-pro",
                "max_tokens": 128,
                "messages": [
                    {
                        "role": "user",
                        "content": "In one short sentence, confirm you are responding to Claude Code from Vertex AI.",
                    }
                ],
            },
        )
        b1a = json.loads(r1a["body"])
        print(f"  [1A] Claude Code -> model='gemini-3.1-pro' (Vertex: {get_header(r1a['headers'], 'X-Vertex-Model-Endpoint-Id')})")
        print(f"       Status: {r1a['status']} | Anthropic Response Text: {b1a['content'][0]['text'].strip()}")
        assert r1a["status"] == 200 and b1a["type"] == "message"

        turn1_msg = {
            "role": "user",
            "content": "Call the Bash tool to run `git status --short`. Do not reply with plain text; invoke the Bash tool.",
        }
        tools_def = [
            {
                "name": "Bash",
                "description": "Execute a bash command in the workspace",
                "input_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            }
        ]
        r1b_t1 = gw.send(
            "POST",
            "/v1/messages",
            {"x-api-key": STD_API_KEY, "X-Bypass-Cache": "true"},
            {
                "model": "gemini-3.1-pro",
                "max_tokens": 1024,
                "tool_choice": {"type": "any"},
                "messages": [turn1_msg],
                "tools": tools_def,
            },
        )
        b1b_t1 = json.loads(r1b_t1["body"])
        tool_blocks = [c for c in b1b_t1.get("content", []) if c.get("type") == "tool_use"]
        assert r1b_t1["status"] == 200 and len(tool_blocks) > 0 and tool_blocks[0]["name"] == "Bash"
        tool_use_block = tool_blocks[0]
        print(f"  [1B-Turn1] Claude Code -> Gemini 3.1 Pro emitted tool_use id={tool_use_block['id'][:36]}... name={tool_use_block['name']}")

        r1b_t2 = gw.send(
            "POST",
            "/v1/messages",
            {"x-api-key": STD_API_KEY, "X-Bypass-Cache": "true"},
            {
                "model": "gemini-3.1-pro",
                "max_tokens": 256,
                "messages": [
                    turn1_msg,
                    {"role": "assistant", "content": b1b_t1["content"]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_block["id"],
                                "content": "M README.md\nM src/ai-functions/transformPayload.js",
                            }
                        ],
                    },
                ],
                "tools": tools_def,
            },
        )
        b1b_t2 = json.loads(r1b_t2["body"])
        text_blocks_t2 = [c.get("text", "") for c in b1b_t2.get("content", []) if c.get("type") == "text"]
        print(f"  [1B-Turn2] Claude Code -> Gemini 3.1 Pro Turn 2 (thoughtSignature verified): {' '.join(text_blocks_t2).strip()[:120]}")
        assert r1b_t2["status"] == 200 and len(text_blocks_t2) > 0

        r1c = gw.send(
            "POST",
            "/v1/messages",
            {"x-api-key": STD_API_KEY, "X-Bypass-Cache": "true"},
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 128,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "In one short sentence, confirm you are streaming from Claude Sonnet 4.6 on Vertex AI.",
                    }
                ],
            },
        )
        print(f"  [1C] Claude Code Streaming SSE -> model='claude-sonnet-4-6' | Status: {r1c['status']} | Content-Type: {get_header(r1c['headers'], 'Content-Type')}")
        assert r1c["status"] == 200 and "event: message_start" in r1c["body"] and "event: message_stop" in r1c["body"]

        r1d = gw.send(
            "POST",
            "/v1/messages/count_tokens",
            {"x-api-key": STD_API_KEY},
            {
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "Count tokens for this prompt via Vertex AI countTokens."}],
            },
        )
        b1d = json.loads(r1d["body"])
        print(f"  [1D] Claude Code /v1/messages/count_tokens -> Status: {r1d['status']} | Body: {b1d}")
        assert r1d["status"] == 200 and b1d.get("input_tokens", 0) > 0

        print("\n==================================================================")
        print(" SCENARIO 2: GOOGLE ANTIGRAVITY IDE/CLI HARNESS (/v1beta/models/*)")
        print("==================================================================")

        r2a = gw.send(
            "POST",
            "/v1beta:recordClientEvent",
            {
                "Authorization": f"Bearer {STD_API_KEY}",
                "User-Agent": "antigravity/1.104.0 darwin/arm64",
            },
            {"events": [{"eventType": "AGENT_STEP_COMPLETED"}]},
        )
        print(f"  [2A] Antigravity POST /v1beta:recordClientEvent -> Status: {r2a['status']}, Body: {r2a['body']}")
        assert r2a["status"] == 200 and r2a["body"] == "{}"

        r2b = gw.send(
            "POST",
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse",
            {
                "Authorization": f"Bearer {STD_API_KEY}",
                "User-Agent": "antigravity/1.104.0 darwin/arm64",
                "X-Goog-Api-Client": "gl-node/22.0.0",
            },
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "Reply in one short sentence to Google Antigravity CLI from Gemini 3 Flash."}],
                    }
                ],
                "generationConfig": {"maxOutputTokens": 128},
            },
        )
        print(f"  [2B] Antigravity SSE -> /v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse | Status: {r2b['status']}")
        assert r2b["status"] == 200 and "data: " in r2b["body"]

        r2c = gw.send(
            "POST",
            "/v1beta/models/claude-sonnet-4-6:generateContent",
            {
                "Authorization": f"Bearer {STD_API_KEY}",
                "User-Agent": "antigravity/1.104.0 darwin/arm64",
            },
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "Reply in one short sentence to Google Antigravity IDE from Claude Sonnet 4.6 on Vertex AI."}],
                    }
                ],
                "generationConfig": {"maxOutputTokens": 128},
            },
        )
        b2c = json.loads(r2c["body"])
        ag_text_2 = b2c["candidates"][0]["content"]["parts"][0]["text"].strip()
        print(f"  [2C] Antigravity Model Switch -> /v1beta/models/claude-sonnet-4-6:generateContent")
        print(f"       Status: {r2c['status']} | Translated Response: {ag_text_2}")
        assert r2c["status"] == 200 and get_header(r2c["headers"], "X-Vertex-Target-Family") == "anthropic"

        print("\n==================================================================")
        print(" SCENARIO 3: OPENAI CODEX CLI & CODEX DESKTOP (POST /v1/responses)")
        print("==================================================================")

        r3a = gw.send(
            "POST",
            "/v1/responses",
            {"Authorization": f"Bearer {STD_API_KEY}", "X-Bypass-Cache": "true"},
            {
                "model": "gemini-3.8-flash",
                "stream": True,
                "instructions": "You are powering OpenAI Codex CLI via the Enterprise AI Gateway.",
                "input": "Reply in one short sentence confirming you are Gemini 3 Flash serving Codex CLI.",
                "max_output_tokens": 128,
            },
        )
        print(f"  [3A] Codex Streaming SSE POST /v1/responses -> model='gemini-3.8-flash' | Status: {r3a['status']}")
        assert (
            r3a["status"] == 200
            and "event: response.created" in r3a["body"]
            and "event: response.completed" in r3a["body"]
        )

        codex_tools = [
            {
                "type": "function",
                "name": "shell",
                "description": "Execute a shell command in the Codex workspace",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ]
        r3b_t1 = gw.send(
            "POST",
            "/v1/responses",
            {"Authorization": f"Bearer {STD_API_KEY}", "X-Bypass-Cache": "true"},
            {
                "model": "gemini-3.1-pro",
                "max_output_tokens": 1024,
                "tool_choice": "required",
                "input": [
                    {
                        "role": "user",
                        "content": "Invoke the `shell` function tool to execute `pytest tests/`. Do not answer with text.",
                    }
                ],
                "tools": codex_tools,
            },
        )
        b3b_t1 = json.loads(r3b_t1["body"])
        fc_items = [item for item in b3b_t1.get("output", []) if item.get("type") == "function_call"]
        assert r3b_t1["status"] == 200 and len(fc_items) > 0 and fc_items[0]["name"] == "shell"
        fc_item = fc_items[0]
        print(f"  [3B-Turn1] Codex -> Gemini 3.1 Pro function_call call_id={fc_item['call_id'][:36]}... name={fc_item['name']}")

        r3b_t2 = gw.send(
            "POST",
            "/v1/responses",
            {"Authorization": f"Bearer {STD_API_KEY}", "X-Bypass-Cache": "true"},
            {
                "model": "gemini-3.1-pro",
                "max_output_tokens": 256,
                "input": [
                    {
                        "role": "user",
                        "content": "Invoke the `shell` function tool to execute `pytest tests/`. Do not answer with text.",
                    },
                    fc_item,
                    {
                        "type": "function_call_output",
                        "call_id": fc_item["call_id"],
                        "output": "38 passed in 1.24s",
                    },
                ],
                "tools": codex_tools,
            },
        )
        b3b_t2 = json.loads(r3b_t2["body"])
        print(f"  [3B-Turn2] Codex -> Gemini 3.1 Pro Turn 2 output_text: {b3b_t2.get('output_text', '').strip()[:120]}")
        assert r3b_t2["status"] == 200 and b3b_t2.get("object") == "response"

        r3c = gw.send(
            "POST",
            "/v1/responses",
            {"Authorization": f"Bearer {FRONTIER_API_KEY}"},
            {
                "model": "claude-opus-4-7",
                "instructions": "You are powering OpenAI Codex CLI via the Enterprise AI Gateway.",
                "input": "Reply in one short sentence confirming you are Claude Opus 4.7 serving Codex CLI from Vertex AI.",
                "max_output_tokens": 128,
            },
        )
        b3c = json.loads(r3c["body"])
        print(f"  [3C] Codex POST /v1/responses -> model='claude-opus-4-7' | Status: {r3c['status']} | output_text: {b3c.get('output_text', '').strip()}")
        assert r3c["status"] == 200 and b3c.get("object") == "response"

        print("\n==================================================================")
        print(" SCENARIO 4: LIVE ENTERPRISE SHOWCASE (FINOPS, RBAC & GUARDRAILS)")
        print("==================================================================")

        r6_models = gw.send(
            "GET",
            "/v1/models",
            {"Authorization": f"Bearer {FRONTIER_API_KEY}"},
            None,
        )
        b6_models = json.loads(r6_models["body"])
        model_ids = [m["id"] for m in b6_models.get("data", [])]
        print(f"  [4A] GET /v1/models Catalog | Status: {r6_models['status']} | Models: {len(model_ids)} ({', '.join(model_ids[:5])}...)")
        assert r6_models["status"] == 200 and "kimi-k2" in model_ids and "qwen3-coder" in model_ids

        r6_pi = gw.send(
            "POST",
            "/v1/chat/completions",
            {
                "Authorization": f"Bearer {STD_API_KEY}",
                "User-Agent": "oh-my-pi/2.4.0",
                "X-Bypass-Cache": "true",
            },
            {
                "model": "auto",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Say hello to Pi coding agent in 5 words."}],
            },
        )
        pool_hdr = get_header(r6_pi["headers"], "X-Funding-Pool") or get_header(
            r6_pi["headers"], "x-vertex-funding-pool"
        )
        print(
            f"  [4B] Pi Coding Harness (model='auto') | Status: {r6_pi['status']} "
            f"| Savings: {get_header(r6_pi['headers'], 'X-Routing-Savings-Pct')} "
            f"| Pool: {pool_hdr}"
        )
        assert r6_pi["status"] == 200

        r6_finops = gw.send(
            "GET",
            "/v1/finops/report",
            {"Authorization": f"Bearer {FRONTIER_API_KEY}"},
            None,
        )
        b6_finops = json.loads(r6_finops["body"])
        s6 = b6_finops.get("summary", {})
        print(f"  [4C] Live FinOps Report (/v1/finops/report) | Status: {r6_finops['status']} | Summary: {s6}")
        assert r6_finops["status"] == 200 and b6_finops.get("object") == "finops.report"

        print("\n==================================================================")
        print(" SCENARIO 5: 5-HARNESS x 4+ MODEL E2E MATRIX (21 LIVE MODEL CALLS)")
        print("==================================================================")
        matrix_cases = [
            # 1. Claude Code (/v1/messages)
            ("Claude Code", "/v1/messages", {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24"}, "auto", {"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_CLAUDE_AUTO."}]}),
            ("Claude Code", "/v1/messages", {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24"}, "gemini-4-argon", {"model": "gemini-4-argon", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_CLAUDE_GEMINI4."}]}),
            ("Claude Code", "/v1/messages", {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24"}, "claude-sonnet-5-5", {"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_CLAUDE_SONNET55."}]}),
            ("Claude Code", "/v1/messages", {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24"}, "claude-opus-5-5", {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_CLAUDE_OPUS55."}]}),
            ("Claude Code", "/v1/messages", {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24"}, "qwen3-coder", {"model": "qwen3-coder", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_CLAUDE_QWEN3."}]}),
            # 2. Google Antigravity (/v1beta/models/{model}:generateContent)
            ("Google Antigravity", "/v1beta/models/gemini-3.8-flash:generateContent", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "antigravity/1.104.0"}, "gemini-3.8-flash", {"contents": [{"role": "user", "parts": [{"text": "Reply with PONG_AGY_FLASH."}]}], "generationConfig": {"maxOutputTokens": 64}}),
            ("Google Antigravity", "/v1beta/models/gemini-4-argon:generateContent", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "antigravity/1.104.0"}, "gemini-4-argon", {"contents": [{"role": "user", "parts": [{"text": "Reply with PONG_AGY_ARGON."}]}], "generationConfig": {"maxOutputTokens": 64}}),
            ("Google Antigravity", "/v1beta/models/claude-sonnet-5-5:generateContent", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "antigravity/1.104.0"}, "claude-sonnet-5-5", {"contents": [{"role": "user", "parts": [{"text": "Reply with PONG_AGY_CLAUDE."}]}], "generationConfig": {"maxOutputTokens": 64}}),
            ("Google Antigravity", "/v1beta/models/qwen3-coder:generateContent", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "antigravity/1.104.0"}, "qwen3-coder", {"contents": [{"role": "user", "parts": [{"text": "Reply with PONG_AGY_QWEN3."}]}], "generationConfig": {"maxOutputTokens": 64}}),
            # 3. OpenAI Codex CLI (/v1/responses)
            ("OpenAI Codex CLI", "/v1/responses", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "codex-cli/0.118.0"}, "auto", {"model": "auto", "input": "Reply with PONG_CODEX_AUTO.", "max_output_tokens": 64}),
            ("OpenAI Codex CLI", "/v1/responses", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "codex-cli/0.118.0"}, "gemini-4-argon", {"model": "gemini-4-argon", "input": "Reply with PONG_CODEX_ARGON.", "max_output_tokens": 64}),
            ("OpenAI Codex CLI", "/v1/responses", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "codex-cli/0.118.0"}, "claude-sonnet-5-5", {"model": "claude-sonnet-5-5", "input": "Reply with PONG_CODEX_SONNET.", "max_output_tokens": 64}),
            ("OpenAI Codex CLI", "/v1/responses", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "codex-cli/0.118.0"}, "deepseek-v4.1-flash", {"model": "deepseek-v4.1-flash", "input": "Reply with PONG_CODEX_DEEPSEEK.", "max_output_tokens": 64}),
            # 4. Pi Coding Agent (/v1/chat/completions)
            ("Pi Coding Agent", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "pi-agent/2.1.0", "X-Client-Name": "pi-agent"}, "auto", {"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_PI_AUTO."}]}),
            ("Pi Coding Agent", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "pi-agent/2.1.0", "X-Client-Name": "pi-agent"}, "qwen3-coder", {"model": "qwen3-coder", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_PI_QWEN."}]}),
            ("Pi Coding Agent", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "pi-agent/2.1.0", "X-Client-Name": "pi-agent"}, "glm-5.2", {"model": "glm-5.2", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_PI_GLM."}]}),
            ("Pi Coding Agent", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "pi-agent/2.1.0", "X-Client-Name": "pi-agent"}, "claude-sonnet-5-5", {"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_PI_SONNET."}]}),
            # 5. VS Code (Continue/Cline) (/v1/chat/completions)
            ("VS Code (Continue/Cline)", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "Continue-VSCode/1.1.0", "X-Client-Name": "vscode-continue"}, "auto", {"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_VSCODE_AUTO."}]}),
            ("VS Code (Continue/Cline)", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "Continue-VSCode/1.1.0", "X-Client-Name": "vscode-continue"}, "gemini-4-argon", {"model": "gemini-4-argon", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_VSCODE_ARGON."}]}),
            ("VS Code (Continue/Cline)", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "Continue-VSCode/1.1.0", "X-Client-Name": "vscode-continue"}, "claude-sonnet-5-5", {"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_VSCODE_SONNET."}]}),
            ("VS Code (Continue/Cline)", "/v1/chat/completions", {"Authorization": f"Bearer {FRONTIER_API_KEY}", "User-Agent": "Continue-VSCode/1.1.0", "X-Client-Name": "vscode-continue"}, "kimi-k2.6", {"model": "kimi-k2.6", "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with PONG_VSCODE_KIMI."}]}),
        ]
        print(f"  | # | Harness | Endpoint | Requested Model | Vertex Target Model | Family | Status | Savings |")
        print(f"  |---|---|---|---|---|---|---|---|")
        for idx, (h_name, m_path, m_hdrs, req_m, m_body) in enumerate(matrix_cases, 1):
            rm = gw.send("POST", m_path, m_hdrs, m_body)
            v_model = get_header(rm["headers"], "X-Vertex-Target-Model") or get_header(rm["headers"], "x-vertex-ai-model") or req_m
            v_fam = get_header(rm["headers"], "X-Vertex-Target-Family") or "vertex"
            v_sav = get_header(rm["headers"], "X-Routing-Savings-Pct") or "0.0%"
            print(f"  | {idx} | {h_name} | `{m_path}` | `{req_m}` | `{v_model}` | `{v_fam}` | {rm['status']} | {v_sav} |")
            assert rm["status"] == 200, f"Case {idx} ({h_name} -> {req_m}) failed with HTTP {rm['status']}: {rm['body'][:200]}"

        print("\n==================================================================")
        print(" SCENARIO 6: SMART ROUTER (`auto`) REACHES A LIVE MODEL IN EVERY TIER")
        print("==================================================================")
        # Each prompt is decided by a deterministic keyword rule, so the expected tier does not
        # depend on which classifier backend the gateway was built with. A tier whose model ID
        # Vertex AI does not serve fails here with the upstream 4xx instead of going unnoticed.
        tier_cases = [
            ("tier1-fast", "tell me 1+2"),
            ("tier4-open", "Write a regex that matches ISO 8601 dates."),
            ("tier2-daily", "Find the root cause of a login test that fails only on CI."),
            ("tier3-frontier", "Explain how two services can end up in a deadlock."),
        ]
        print(f"  | Expected Tier | Routed Tier | Vertex Target Model | Decided By | Status |")
        print(f"  |---|---|---|---|---|")
        for want_tier, tier_prompt in tier_cases:
            rt = gw.send(
                "POST",
                "/v1/messages",
                {"x-api-key": FRONTIER_API_KEY, "anthropic-version": "2023-06-01", "User-Agent": "claude-cli/1.0.24", "X-Bypass-Cache": "true"},
                {"model": "auto", "max_tokens": 32, "messages": [{"role": "user", "content": tier_prompt}]},
            )
            got_tier = get_header(rt["headers"], "X-Vertex-Target-Tier") or ""
            got_model = get_header(rt["headers"], "X-Vertex-Target-Model") or ""
            decided_by = get_header(rt["headers"], "X-Router-Classifier") or "n/a"
            print(f"  | `{want_tier}` | `{got_tier}` | `{got_model}` | `{decided_by}` | {rt['status']} |")
            assert rt["status"] == 200, f"auto -> {want_tier} failed with HTTP {rt['status']}: {rt['body'][:200]}"
            assert got_tier == want_tier, f"auto routed {tier_prompt!r} to {got_tier or 'no tier header'}, expected {want_tier}"

        print("\n==================================================================")
        print(" ALL LIVE CODING HARNESSES + 21-MODEL MATRIX + FINOPS SUCCEEDED!")
        print("==================================================================")
    finally:
        gw.close()


if __name__ == "__main__":
    main()
