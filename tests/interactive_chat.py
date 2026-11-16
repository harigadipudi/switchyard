#!/usr/bin/env python3
"""Interactive REPL for the Coding Agent Model Router (/v1/messages wire protocol)."""

import json
import os
import readline  # Enables arrow-key history and line editing in input()
import sys
import urllib.error
import urllib.request

SHORT_ALIASES = {
    "1": "auto",
    "auto": "auto",
    "smart": "auto",
    "smart-router": "auto",
    "2": "tiered",
    "tiered": "tiered",
    "3": "gemini-4-argon",
    "argon": "gemini-4-argon",
    "gemini": "gemini-4-argon",
    "4": "gemini-3.8-flash",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "5": "gemini-3.1-pro-preview",
    "gemini-pro": "gemini-3.1-pro-preview",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "6": "claude-opus-5-5",
    "opus": "claude-opus-5-5",
    "7": "claude-sonnet-5-5",
    "claude": "claude-sonnet-5-5",
    "sonnet": "claude-sonnet-5-5",
    "8": "claude-haiku-4-5",
    "haiku": "claude-haiku-4-5",
    "9": "qwen3-coder",
    "qwen": "qwen3-coder",
    "10": "deepseek-v4.1-flash",
    "deepseek": "deepseek-v4.1-flash",
    "11": "deepseek-r1",
    "r1": "deepseek-r1",
    "12": "glm-5.2",
    "glm": "glm-5.2",
    "13": "kimi-k2",
    "kimi": "kimi-k2",
    "14": "llama-4-maverick",
    "llama": "llama-4-maverick",
    "15": "gpt-oss-120b",
    "gpt": "gpt-oss-120b",
    "16": "grok-4.7",
    "grok": "grok-4.7",
}


def resolve_model(raw: str) -> str:
    key = raw.strip().lower()
    return SHORT_ALIASES.get(key, raw.strip())



def main():
    if len(sys.argv) < 4:
        print("Usage: interactive_chat.py <gateway_url> <api_key> <initial_model> [state_file]")
        sys.exit(1)

    gw = sys.argv[1].rstrip("/")
    key = sys.argv[2]
    current_model = sys.argv[3]
    state_file = sys.argv[4] if len(sys.argv) > 4 else None

    history = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    while True:
        try:
            prompt = input(f"\033[1;32m[{current_model}]>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting chat.")
            break

        if not prompt:
            continue
        if prompt in ("/quit", "/exit", "exit", "quit"):
            print("Exiting chat.")
            break

        if prompt in ("/models", "/model", "models"):
            try:
                req = urllib.request.Request(
                    f"{gw}/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with opener.open(req, timeout=10) as r:
                    models = json.loads(r.read().decode("utf-8")).get("data", [])
                print("\nAvailable Gateway Models (type '/model <id>' to switch):")
                marker = "->" if current_model == "auto" else "  "
                print(f" {marker} {'auto':<26} {'smart-router':<16} (4-Tier Complexity & Task Classifier)")
                for m in models:
                    marker = "->" if m["id"] == current_model else "  "
                    p = m.get("pricing_per_1m_usd", {})
                    print(
                        f" {marker} {m['id']:<26} {m['tier']:<16} "
                        f"(${p.get('input', 0):.2f}/${p.get('output', 0):.2f} per 1M tokens)"
                    )
                print()
            except Exception as exc:
                print(f"Error fetching models: {exc}")
            continue

        if (
            prompt.startswith("/model ")
            or prompt.startswith("/models ")
            or prompt.startswith("model ")
            or prompt.startswith("/switch ")
            or prompt.startswith("switch ")
        ):
            new_model = resolve_model(prompt.split(None, 1)[1])
            current_model = new_model
            if state_file:
                try:
                    with open(state_file, "w", encoding="utf-8") as f:
                        f.write(current_model + "\n")
                except OSError:
                    pass
            print(f"\033[1;36m✓ Switched active model to: {current_model}\033[0m")
            continue

        if prompt in ("/finops", "finops"):
            try:
                req = urllib.request.Request(
                    f"{gw}/v1/finops/report",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with opener.open(req, timeout=10) as r:
                    s = json.loads(r.read().decode("utf-8")).get("summary", {})
                print(
                    f"\n\033[1;35m[FinOps Ledger]\033[0m Requests: {s.get('total_requests', 0)} | "
                    f"Tokens: {s.get('total_tokens', 0):,} | "
                    f"Actual Spend: ${s.get('actual_list_cost_usd', 0):.6f} | "
                    f"Saved vs Frontier Opus: ${s.get('routing_savings_usd', 0):.6f} ({s.get('routing_savings_pct', '0%')})\n"
                )
            except Exception as exc:
                print(f"Error fetching FinOps report: {exc}")
            continue

        if prompt in ("/clear", "/reset"):
            history.clear()
            print("Conversation history cleared.")
            continue

        if prompt.startswith("/"):
            print(
                "\033[1;33mUnknown command.\033[0m Use: "
                "\033[1;32m/model <id|1..16>\033[0m | \033[1;32m/models\033[0m | "
                "\033[1;32m/finops\033[0m | \033[1;32m/clear\033[0m | \033[1;32m/quit\033[0m"
            )
            continue

        history.append({"role": "user", "content": prompt})
        body = json.dumps(
            {
                "model": current_model,
                "max_tokens": 1024,
                "messages": history,
            }
        ).encode("utf-8")
        req_headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "claude-cli/1.0.0",
            "Content-Type": "application/json",
        }
        if current_model == "auto":
            req_headers["x-routing-mode"] = "auto"
        else:
            req_headers["x-model-override"] = current_model
        req = urllib.request.Request(
            f"{gw}/v1/messages",
            data=body,
            headers=req_headers,
            method="POST",
        )
        try:
            with opener.open(req, timeout=90) as r:
                hdrs = {k.lower(): v for k, v in r.info().items()}
                resp = json.loads(r.read().decode("utf-8"))
                reply = (resp.get("content") or [{}])[0].get("text", "").strip()
                history.append({"role": "assistant", "content": reply})
                routed = hdrs.get("x-vertex-target-model", resp.get("model", current_model))
                tier = hdrs.get("x-vertex-target-tier", "-")
                reason = hdrs.get("x-smart-router-decision", "pinned")
                cost = hdrs.get("x-estimated-cost-usd", "0")
                savings = hdrs.get("x-routing-savings-pct", "0%")
                print(f"\n{reply}\n")
                print(
                    f"\033[2m  ↳ [Router: requested={current_model} -> routed={routed} "
                    f"({tier}, {reason}) | Cost: ${cost} | Saved vs Opus: {savings}]\033[0m\n"
                )
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"\n\033[1;31m[HTTP {exc.code} Error]\033[0m {err_body}\n")
            history.pop()
        except Exception as exc:
            print(f"\n\033[1;31m[Error]\033[0m {exc}\n")
            history.pop()


if __name__ == "__main__":
    main()
