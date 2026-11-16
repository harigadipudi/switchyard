# Developer Setup Guide: Vertex AI Enterprise Gateway

Connect your preferred coding harness—**Google Antigravity IDE & CLI (`agy`)**, **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **VS Code / Cursor / Cline / Copilot**, **Pi (`pi`)**, or **OpenCode**—to Google Cloud **Apigee X + Vertex AI Model Garden**.

Switch between **16+ frontier and open-weight models** natively via `/model <id>`, `--model <id>`, or your IDE's dropdown, with zero proxy containers or credential leaks.

---

## ⚡ Quick Navigation Menu

- [1-Minute Quickstart (Global Setup)](#-1-minute-quickstart-recommended)
- [How Platform Admins Retrieve the Gateway URL & Issue API Keys](#-how-platform-admins-retrieve-the-gateway-url--issue-api-keys)
- [Harness Setup Guides](#-harness-setup-guides)
  - [1. Google Antigravity IDE & CLI (`agy`)](#1-google-antigravity-ide--cli-agy)
  - [2. Claude Code CLI & VS Code Extension (`claude`)](#2-claude-code-cli--vs-code-extension-claude)
  - [3. OpenAI Codex CLI & Desktop (`codex`)](#3-openai-codex-cli--desktop-codex)
  - [4. VS Code / Cursor / Cline / Continue / Copilot](#4-vs-code--cursor--cline--copilot)
  - [5. Pi Coding Agent (`pi` / `oh-my-pi`)](#5-pi-coding-agent-pi--oh-my-pi)
  - [6. OpenCode & DeepSeek Harness (`dsh`)](#6-opencode--deepseek-harness)
- [Live Model Catalog & Aliases](#-live-model-catalog--short-aliases)
- [Switching Models On-the-Fly](#-switching-models-on-the-fly)
- [Developer FinOps: Track Your Spend & Quotas](#-developer-finops-track-your-spend--quotas)
- [Developer Troubleshooting FAQ](#-developer-troubleshooting-faq)
- [Architectural & Admin Reference](#-looking-for-architecture-or-admin-deployment)

---

## 🚀 1-Minute Quickstart (Recommended)

Get your `GATEWAY_URL` and `GATEWAY_API_KEY` from your platform administrator, then run:

```bash
export GATEWAY_URL="https://ai-gateway.yourcompany.com"
export GATEWAY_API_KEY="your-apigee-consumer-key"

./vertex-connect.sh setup auto
```

> [!TIP]
> **Are you the Platform Administrator setting this up?**  
> If you need to find the gateway endpoint or issue API keys for your developers, jump down to [How Platform Admins Retrieve the Gateway URL & Issue API Keys](#-how-platform-admins-retrieve-the-gateway-url--issue-api-keys) for Terraform outputs, `gcloud` CLI commands, and Google Cloud Console instructions.

### Why One-Time Global Setup?
- **Writes Persistent Configs**: Configures `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.gemini/antigravity-cli/settings.json`, and `~/.pi/agent/models.json` on disk once.
- **Survives Laptop Reboots & New Shells**: Launch your IDE or CLI from the macOS Dock, Windows Start Menu, or any terminal window without running `eval` or setup scripts every session.
- **Installs Status Bar HUD**: Automatically adds a real-time bottom status line showing active model, token count, USD cost, and % savings.
- **Installs Global Slash Commands**: Adds `/models`, `/finops`, and `/route` to your coding tools.

---

## 👨‍💼 How Platform Admins Retrieve the Gateway URL & Issue API Keys

If you are a cloud or platform engineer managing the deployment, here is exactly where `GATEWAY_URL` and `GATEWAY_API_KEY` come from:

### 1. Where to Get `GATEWAY_URL`
- **From Terraform (`infra/`)**:
  ```bash
  cd infra
  terraform output -raw gateway_url
  # Example output: https://switchyard.internal or https://ai-gw.yourcompany.com
  ```
- **From Google Cloud Console**:
  Navigate to **Apigee > Admin > Environments > Groups**. Look at the **Hostnames** column (e.g. `switchyard.internal` or your company's custom DNS hostname mapped to the Apigee Load Balancer).
- **From `gcloud` CLI**:
  ```bash
  gcloud apigee envgroups list --organization="YOUR_GCP_PROJECT_ID"
  ```

---

### 2. Where to Get or Issue `GATEWAY_API_KEY` (Apigee Consumer Key)

The `GATEWAY_API_KEY` is an **Apigee Consumer Key** tied to an **Apigee Developer App** that is assigned to either the `coding-agent-standard` or `coding-agent-frontier` API Product.

Choose the method that fits your workflow:

#### Method A: Using Terraform Outputs (Fastest if using `infra/`)
If you configured developers in `infra/variables.tf` (e.g., `alex-rivera-coding-agent-app`), Terraform provides a pre-built fetch command in its outputs:
```bash
cd infra
# 1. View the exact command:
terraform output -raw fetch_developer_api_key_command

# 2. Run the command to output the key directly:
eval "$(terraform output -raw fetch_developer_api_key_command)"
```

#### Method B: Via Google Cloud Console UI (Click-by-Click)
1. Open the [Google Cloud Console](https://console.cloud.google.com) and navigate to **Apigee**.
2. In the left navigation menu, go to **Publish > Apps** (or **Publish > Developers**).
3. Click the developer's app name (e.g., `alex-rivera-coding-agent-app` or `dev-coding-app`).
4. Under the **Credentials** section, find the credential bound to `coding-agent-standard` or `coding-agent-frontier`.
5. Click **Show** next to **Key** (Consumer Key) and copy the string.  
   👉 *This Consumer Key is the developer's `GATEWAY_API_KEY`.*

#### Method C: Via `gcloud` CLI (Scriptable / On-Demand Provisioning)
To register a new developer and generate their API key in terminal:
```bash
export GCP_PROJECT_ID="your-gcp-project-id"
export DEV_EMAIL="developer@yourcompany.com"
export APP_NAME="dev-coding-app"

# 1. Register developer in Apigee (if not already registered)
gcloud apigee developers create \
  --email="${DEV_EMAIL}" \
  --first-name="Dev" \
  --last-name="User" \
  --user-name="devuser" \
  --organization="${GCP_PROJECT_ID}"

# 2. Create the Developer App assigned to the standard coding product
gcloud apigee apps create "${APP_NAME}" \
  --developer="${DEV_EMAIL}" \
  --api-products="coding-agent-standard" \
  --organization="${GCP_PROJECT_ID}"

# 3. Retrieve the generated Consumer Key:
gcloud apigee apps describe "${APP_NAME}" \
  --developer="${DEV_EMAIL}" \
  --organization="${GCP_PROJECT_ID}" \
  --format="value(credentials[0].consumerKey)"
```

#### Method D: Passwordless Corporate Google OIDC (Recommended for Enterprise)
If your organization uses Google Cloud Identity or Google Workspace, developers do not need static API keys stored on their laptops at all. They can pass an active Google OIDC ID token:

```bash
# Developer authenticates with corporate SSO:
gcloud auth login

# Set GATEWAY_API_KEY to your short-lived (1-hour) Google ID token:
export GATEWAY_API_KEY="$(gcloud auth print-identity-token --audiences="${GATEWAY_URL}")"
```

**Why this is ideal for Enterprise Compliance**:
- **Zero Secrets on Disk**: Dotfiles (`~/.claude/settings.json`, `~/.codex/config.toml`) contain no persistent API keys.
- **Automatic Identity & Quota Binding**: Apigee's native `<VerifyJWT>` policy validates the signature against Google's public JWKS (`https://www.googleapis.com/oauth2/v3/certs`), extracts the developer's corporate `email` claim, and links directly to their assigned Apigee developer profile and daily `<LLMTokenQuota>`.
- **Instant Offboarding Revocation**: If an employee leaves the company or changes teams, revoking their Google Cloud / Workspace account immediately invalidates their token without rotating shared keys.
- **Reference Docs**: See [Authenticating with Google ID Tokens](https://cloud.google.com/docs/authentication/token-types#id) and [Apigee Verify JWT Policy](https://cloud.google.com/apigee/docs/api-platform/reference/policies/verify-jwt-policy).

---

## 💻 Harness Setup Guides

### 1. Google Antigravity IDE & CLI (`agy`)

The gateway natively implements the [Google Antigravity Enterprise Gateway Contract](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools/connect-enterprise-gateway) over `/v1beta/models/{model}:generateContent` and `:streamGenerateContent?alt=sse`.

#### Automated Setup
```bash
./vertex-connect.sh agy auto
```

#### Manual Laptop Setup
Place the following JSON in your system's Antigravity admin settings path:
- **macOS**: `/Library/Application Support/Antigravity/admin_settings.json` (or `~/.gemini/antigravity-cli/admin_settings.json`)
- **Linux**: `/etc/antigravity/admin_settings.json`
- **Windows**: `%PROGRAMDATA%\Antigravity\admin_settings.json`

```json
{
  "gateway": {
    "enabled": true,
    "url": "https://ai-gateway.yourcompany.com",
    "wireProtocol": "genai"
  },
  "models": {
    "defaultModelId": "auto",
    "list": [
      { "modelId": "auto", "displayName": "Auto (Unbiased 4-Tier Smart Router)" },
      { "modelId": "gemini-4-argon", "displayName": "Gemini 4 Argon (Frontier Coding)" },
      { "modelId": "gemini-3.8-flash", "displayName": "Gemini 3.8 Flash (Fast Workhorse)" },
      { "modelId": "claude-opus-5-5", "displayName": "Claude Opus 5.5 (Vertex AI)" },
      { "modelId": "claude-sonnet-5-5", "displayName": "Claude Sonnet 5.5 (Vertex AI)" },
      { "modelId": "deepseek-v4.1-flash", "displayName": "DeepSeek V4.1 Flash 1M (Vertex MaaS)" },
      { "modelId": "qwen3-coder", "displayName": "Qwen 3 Coder 480B (Vertex MaaS)" }
    ]
  }
}
```

*Alternative (Environment Variables in `~/.zshrc` or `~/.bashrc`):*
```bash
export AGY_LLM_GATEWAY_URL="https://ai-gateway.yourcompany.com"
export AGY_LLM_GATEWAY_API_KEY="your-apigee-consumer-key"
export AGY_LLM_GATEWAY_WIRE_PROTOCOL="genai"
export AGY_LLM_GATEWAY_MODELS="auto,gemini-4-argon,gemini-3.8-flash,claude-opus-5-5,claude-sonnet-5-5,qwen3-coder,deepseek-v4.1-flash"
```

#### How to Switch Models in Antigravity
- In **Antigravity IDE**: Click the model picker dropdown in the chat pane.
- In **`agy` CLI**: Run `agy --model auto` or switch inside the TUI with `/model auto`, `/model gemini-4-argon`, or `/model qwen3-coder`.

---

### 2. Claude Code CLI & VS Code Extension (`claude`)

Runs Claude Code over native Anthropic Messages protocol (`/v1/messages`), supporting multi-turn tool loops, streaming CRLF SSE, prompt caching, and subagents.

#### Automated Setup
```bash
./vertex-connect.sh claude auto
```

#### Manual Laptop Setup (`~/.claude/settings.json`)
Create `~/.claude/key-helper.sh`:
```bash
#!/usr/bin/env bash
printf '%s' "your-apigee-consumer-key"
```
Make it executable: `chmod 700 ~/.claude/key-helper.sh`.

Then configure `~/.claude/settings.json`:
```json
{
  "apiKeyHelper": "~/.claude/key-helper.sh",
  "model": "opus[1m]",
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  },
  "env": {
    "ANTHROPIC_BASE_URL": "https://ai-gateway.yourcompany.com",
    "ANTHROPIC_CUSTOM_HEADERS": "x-routing-mode: auto",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"
  }
}
```

> **Why this configuration?** By keeping `ANTHROPIC_CUSTOM_HEADERS` inside `settings.json` and using `apiKeyHelper`, Claude Code launches with **zero startup warnings**, retains its native 1M context UI, and switches models instantly without shell environment conflicts.

#### How to Switch Models in Claude Code
Inside any `claude` session, type:
- `/model auto` — Return to the 4-Tier Smart Router (default)
- `/model argon` — `gemini-4-argon`
- `/model flash` — `gemini-3.8-flash`
- `/model qwen` — `qwen3-coder`
- `/model deepseek` — `deepseek-v4.1-flash`
- `/model opus` — `claude-opus-5-5`
- `/model sonnet` — `claude-sonnet-5-5`
- `/models` — Display the live numbered catalog
- `/finops` — Check your live session tokens and spend

---

### 3. OpenAI Codex CLI & Desktop (`codex`)

Connects OpenAI Codex CLI (`npm i -g @openai/codex` or curl installer) to Vertex AI via the native OpenAI Responses API (`/v1/responses`).

#### Automated Setup
```bash
./vertex-connect.sh codex auto
```

#### Manual Laptop Setup (`~/.codex/config.toml`)
Add to `~/.codex/config.toml`:
```toml
default_provider = "vertex_gw"
model = "auto"

[model_providers.vertex_gw]
name = "Vertex AI Enterprise Gateway"
base_url = "https://ai-gateway.yourcompany.com/v1"
wire_api = "responses"
requires_openai_auth = false
experimental_bearer_token = "your-apigee-consumer-key"

[model_providers.vertex_gw.headers]
"x-api-key" = "your-apigee-consumer-key"
"X-Client-Name" = "codex-cli"
"X-Cost-Center" = "CC-Engineering"
```

#### How to Switch Models in Codex
- In CLI: `codex -m auto`, `codex -m gemini-4-argon`, `codex -m qwen3-coder`
- In interactive session: Type `/model` to pick from the live catalog.

---

### 4. VS Code / Cursor / Cline / Copilot

Connect any modern editor or extension using OpenAI-compatible or Anthropic endpoints.

#### A. GitHub Copilot Chat Custom Endpoint (VS Code)
1. Open **Copilot Chat** in VS Code and click the model picker dropdown at the bottom of the panel.
2. Select **Manage Models...** $\rightarrow$ **+ Add Models...** $\rightarrow$ **Custom Endpoint (OpenAI Compatible)**.
3. Enter:
   - **Provider Name**: `Vertex AI Enterprise Gateway`
   - **Endpoint Base URL**: `https://ai-gateway.yourcompany.com/v1` *(include `/v1`)*
   - **API Key**: `your-apigee-consumer-key`
   - **Model IDs**: `auto`, `gemini-4-argon`, `gemini-3.8-flash`, `claude-opus-5-5`, `claude-sonnet-5-5`, `deepseek-v4.1-flash`, `qwen3-coder`

#### B. Claude Code VS Code Extension
Add to your VS Code `settings.json`:
```json
{
  "claude-code.environmentVariables": [
    { "name": "ANTHROPIC_BASE_URL", "value": "https://ai-gateway.yourcompany.com" },
    { "name": "ANTHROPIC_CUSTOM_HEADERS", "value": "x-routing-mode: auto" },
    { "name": "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "value": "1" }
  ]
}
```

#### C. Cline / Roo Code / Continue
- **API Provider**: `OpenAI Compatible`
- **Base URL**: `https://ai-gateway.yourcompany.com/v1`
- **API Key**: `your-apigee-consumer-key`
- **Model ID**: `auto` (or `gemini-4-argon`, `claude-sonnet-5-5`, `qwen3-coder`)

---

### 5. Pi Coding Agent (`pi` / `oh-my-pi`)

Connect Pi (`@mariozechner/pi-coding-agent`, `oh-my-pi`) over `POST /v1/chat/completions`.

#### Automated Setup
```bash
./vertex-connect.sh pi auto
```

#### Manual Laptop Setup (`~/.pi/agent/models.json`)
Save to `~/.pi/agent/models.json`:
```json
{
  "providers": {
    "vertex-gateway": {
      "baseUrl": "https://ai-gateway.yourcompany.com/v1",
      "apiKey": "OPENAI_API_KEY",
      "api": "openai-completions",
      "models": [
        { "id": "auto", "name": "Smart Router (Auto 4-Tier Judge)", "reasoning": true, "contextWindow": 1000000 },
        { "id": "gemini-4-argon", "name": "Gemini 4 Argon (Frontier)", "reasoning": true, "contextWindow": 1000000 },
        { "id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash (Fast)", "reasoning": false, "contextWindow": 1000000 },
        { "id": "claude-opus-5-5", "name": "Claude Opus 5.5", "reasoning": true, "contextWindow": 1000000 },
        { "id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "reasoning": true, "contextWindow": 1000000 },
        { "id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash 1M", "reasoning": true, "contextWindow": 1000000 },
        { "id": "qwen3-coder", "name": "Qwen 3 Coder 480B", "reasoning": false, "contextWindow": 131072 }
      ]
    }
  }
}
```
In your `~/.zshrc` or `~/.bashrc`:
```bash
export OPENAI_BASE_URL="https://ai-gateway.yourcompany.com/v1"
export OPENAI_API_KEY="your-apigee-consumer-key"
export PI_MODEL="auto"
```

---

### 6. OpenCode & DeepSeek Harness

Export standard OpenAI/DeepSeek environment variables:
```bash
export OPENAI_BASE_URL="https://ai-gateway.yourcompany.com/v1"
export OPENAI_API_KEY="your-apigee-consumer-key"
export OPENAI_MODEL="auto"

export DEEPSEEK_API_BASE="https://ai-gateway.yourcompany.com/v1"
export DEEPSEEK_API_KEY="your-apigee-consumer-key"
export DEEPSEEK_MODEL="auto"
```

---

## 📋 Live Model Catalog & Short Aliases

The gateway normalizes model identifiers across all harnesses. You can request models by their full ID or short alias:

| Tier | Short Alias | Full Vertex AI Model ID | Context | Recommended Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **Auto** | `auto`, `smart` | `auto` | 1M | **Default**: Unbiased 4-Tier Smart Router |
| **Tier 1 (Fast)** | `flash` | `gemini-3.8-flash` | 1M | Subagents, file grep, test runs, syntax lookups |
| **Tier 1 (Fast)** | `lite` | `gemini-3.5-flash-lite` | 1M | Ultra-low latency single-turn queries |
| **Tier 2 (Daily)** | `sonnet` | `claude-sonnet-5-5` | 1M | Daily refactoring, feature implementation, code review |
| **Tier 2 (Daily)** | `qwen` | `qwen3-coder` | 131K | Open-weight code generation, Python/Go/TS tasks |
| **Tier 2 (Daily)** | `deepseek` | `deepseek-v4.1-flash` | 1M | Fast open-weight reasoning & large codebases |
| **Tier 3 (Frontier)**| `argon` | `gemini-4-argon` | 1M | Deep architectural reasoning, distributed systems |
| **Tier 3 (Frontier)**| `opus` | `claude-opus-5-5` | 1M | Frontier multi-file reasoning, complex bug hunting |
| **Tier 3 (Frontier)**| `fable` | `claude-fable-5-1` | 1M | Autonomous multi-step agent execution |
| **Tier 3 (Frontier)**| `r1` | `deepseek-r1` | 128K | Formal logic, math proofs, algorithm design |
| **Tier 4 (Open)** | `glm` | `glm-5.2` | 1M | Multilingual coding, long-document reasoning |
| **Tier 4 (Open)** | `kimi` | `kimi-k2` | 256K | Deep long-context reasoning |
| **Tier 4 (Open)** | `llama` | `llama-4-maverick` | 1M | General-purpose open-weight reasoning |
| **Tier 4 (Open)** | `gpt` | `gpt-oss-120b` | 128K | Open-weight instruction following |
| **Tier 4 (Open)** | `grok` | `grok-4.7` | 128K | High-throughput partner model |

---

## 🔀 Switching Models On-the-Fly

You never need to edit configuration files to test another model:

```bash
# 1. Interactive terminal picker (choose 1-16)
./vertex-connect.sh pick

# 2. Switch default for a specific harness
./vertex-connect.sh claude gemini-4-argon
./vertex-connect.sh codex qwen3-coder
./vertex-connect.sh agy auto

# 3. Verify routing and test response
./vertex-connect.sh verify auto
```

---

## 💰 Developer FinOps: Track Your Spend & Quotas

Each turn through the gateway is attributed to your developer key, cost center, and harness.

### Inspect Your Personal Spend
```bash
# View session token breakdown, cache hits, cost, and savings
./vertex-connect.sh finops

# Inspect spend split between interactive turns and background subagents
./vertex-connect.sh finops --subagents

# Stream a live self-refreshing dashboard in your terminal
./vertex-connect.sh finops --live 5
```

### What Developers See
```
⚡ Vertex │ Model: auto → gemini-3.8-flash │ Turn: 1.2k in / 340 out
Session Spend : $0.0032 (Saved 84% vs. Frontier Opus baseline)
Daily Quota   : 142,500 / 500,000 tokens used (71% remaining)
Cache Hit Rate: 68.4% (disjoint prompt caching)
```

### Spend Guardrails
- **At 80% of daily quota**: You receive an `X-Gateway-Quota-Warning` header in responses.
- **At 100% of daily quota**: Requests are either gracefully downgraded to `gemini-3.8-flash` ($0 internal credit pool) or return HTTP 429 if hard budget caps are enforced by your team policy.

### 🏢 Platform Administrators: Central Cloud Monitoring Dashboard
Platform and FinOps leads can view organization-wide spend, team waterwalls, and the Avoidable Frontier Overkill gauge in Google Cloud Console:
- **Console Navigation**: **Google Cloud Console > Monitoring > Dashboards > "Coding Agent Enterprise FinOps & Multi-Harness Governance"**
- **Direct Link**: `https://console.cloud.google.com/monitoring/dashboards/builder/projects/<PROJECT_ID>/dashboards/<DASHBOARD_ID>?project=<PROJECT_ID>`
- **Retrieve Dashboard ID via Terraform**: `cd infra && terraform output -raw finops_cloud_monitoring_dashboard_id`
- For full dashboard schema and metrics definitions, see the **[Observability & FinOps Section in README.md](README.md#-enterprise-finops-observability--metrics)**.

---

## 🛠️ Developer Troubleshooting FAQ

### 1. HTTP 401 Unauthorized
- **Cause**: Missing or incorrect `GATEWAY_API_KEY`.
- **Fix**: Check `echo $GATEWAY_API_KEY` or inspect `~/.claude/.gateway-api-key`. Ensure the key was created under an active Apigee Developer App.

### 2. HTTP 403 Forbidden ("Model Not Permitted by API Product")
- **Cause**: Your API key belongs to `coding-agent-standard`, which permits Fast, Daily, and Open-Weight models (`gemini-3.8-flash`, `claude-sonnet-5-5`, `qwen3-coder`), but you directly requested a pinned Frontier model (`claude-opus-5-5`).
- **Fix**: Switch to `/model auto` (the Smart Router automatically routes complex turns to frontier capabilities when permitted) or ask your platform admin to upgrade your key to `coding-agent-frontier`.

### 3. HTTP 429 Too Many Requests ("LLM Token Quota Exceeded")
- **Cause**: You reached your 24-hour token budget (e.g., 500K tokens for Standard, 2M tokens for Frontier).
- **Fix**: Run `./vertex-connect.sh finops` to view your quota reset window, or ask your administrator for a temporary quota bump.

### 4. SSL / Corporate Proxy Connection Error
- **Cause**: Corporate VPN or intercepting proxy.
- **Fix**: Run with bypass if on corporate network:
  ```bash
  export NO_PROXY="*.internal,localhost,127.0.0.1"
  ```

---

## 📖 Looking for Architecture or Admin Deployment?

- For gateway architecture, security flow, 4-tier router mechanics, and Terraform/Apigee deployment, see the root **[README.md](README.md)**.
