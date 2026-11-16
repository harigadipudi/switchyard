# CLAUDE.md

@AGENTS.md

See [AGENTS.md](AGENTS.md) for the complete project architecture, Switchyard model routing requirements, Apigee X + Vertex AI Model Garden wire-protocol translation rules, and build/test commands.

## Quick Reference for Claude Code

- **Build Apigee X XML Bundles**: `./deploy.sh --build-only`
- **Run Offline E2E Tests**: `/usr/bin/python3 tests/verify_e2e.py`
- **Inspect CLI Helper**: `./vertex-connect.sh {status|models|finops|claude|codex|pi|agy} [model]`
- **Key Source Directories**:
  - `src/ai-functions/` — Apigee JavaScript callouts (must remain compatible with both Apigee Rhino JS and Node.js).
  - `features/` & `templates/` & `stages/` — Declarative Apigee X policy and proxy definitions.
  - `DEVELOPER_SETUP_GUIDE.md` & `README.md` — Developer setup guide and technical architecture documentation.
