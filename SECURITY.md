# Security Policy

This is a static site plus a stdlib-only CLI — no servers, no stored
credentials, no user data. The interesting surface is **data integrity**:
poisoned price data or a tampered report would mislead routing decisions.

**Report vulnerabilities privately** to vnarasingamoorthy@gmail.com with
"inference-index security" in the subject. Please do not open public issues
for security reports. We aim to respond within 72 hours.

In scope: the GitHub Actions workflows (refresh/CI), the n8n workflow's
dispatch path, data-injection routes into `docs/data.json`, and
`pipeline/agent_verify.py` / `pipeline/mock_fleet.py`.
