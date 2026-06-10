# Security Policy

## Reporting A Vulnerability

Please report vulnerabilities privately by email to `security@visualagent.dev`.

Do not open a public issue for suspected vulnerabilities, secrets exposure, SSRF, unsafe workflow execution, or authentication bypasses.

## Response Targets

- Initial acknowledgement: within 3 business days.
- Triage update: within 7 business days.
- Fix or mitigation plan: based on severity and reproducibility.

## Scope

In scope:

- Unsafe workflow execution or permission profile bypass.
- Secret leakage in reports, telemetry, MCP payloads, or logs.
- SSRF or unsafe URL handling.
- Unsafe file writes outside the workspace.
- Marketplace publishing or public workflow authorization issues.

Out of scope:

- Vulnerabilities requiring local administrator compromise first.
- Issues in third-party websites under test.
- Reports without enough reproduction detail to investigate.

## Safe Handling

Visual Agent reports and fixtures must not include secrets, cookies, tokens, private customer data, or production credentials.
