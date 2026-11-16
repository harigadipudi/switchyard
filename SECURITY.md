# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of Switchyard and the enterprise systems it connects seriously.

If you discover a security vulnerability, please do not open a public GitHub issue. Instead, report it privately to the project maintainers.

### How to Report

Please submit vulnerability details with:
1. Description of the issue and potential impact.
2. Steps to reproduce or proof-of-concept.
3. Affected components, policies, or scripts.
4. Any proposed remediations.

We will acknowledge receipt within 48 hours and provide updates as the issue is investigated and remediated.

## Security Practices

Switchyard enforces enterprise zero-trust security:
- **Authentication**: Apigee API Keys or verified Google OIDC JWT tokens.
- **Least Privilege**: Southbound calls to Vertex AI are signed by Apigee using a dedicated service account (`apigee-vertex-router-sa`) with scoped `roles/aiplatform.user` permissions.
- **Guardrails**: Prompt and response sanitization via Google Cloud Model Armor.
- **Quota & Protection**: Distributed token quota enforcement (`<LLMTokenQuota>`) and SpikeArrest rate limiting.
- **Credential Hygiene**: Synthetic and ephemeral credentials in automated test suites; no hardcoded API keys or bearer tokens in code or commit history.
