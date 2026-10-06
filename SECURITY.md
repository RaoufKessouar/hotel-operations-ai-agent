# Security policy

## Reporting a vulnerability

Please report security issues privately to the repository owner rather than opening a public issue. Include the affected component, reproduction steps and potential impact.

## Secrets and test data

- Never commit `.env` files, API keys, passwords or local backups.
- Use only synthetic guest and booking data in the demonstration environment.
- Rotate any credential that may have been exposed.
- Keep the API and local management application bound to loopback addresses during development.

## Supported scope

The repository demonstrates controlled hotel operations in a local environment. Payments, destructive deletion and unrestricted administrative access are intentionally not exposed as model-callable tools.

