# Safety and reliability

The project treats language-model output as an untrusted proposal. Safety does not depend on the model following instructions perfectly.

## Closed tool surface

The model can only select functions registered by the application. Arbitrary URLs, SQL, shell commands and unrestricted QloApps endpoints are not exposed.

## Typed validation

Every tool defines a Pydantic argument model. Invalid dates, missing identifiers, unsupported values and inconsistent payloads are rejected before the adapter is called.

## Permission model

Capabilities are filtered by role. Read and write credentials are separate, and write tools are omitted when the restricted write key is unavailable.

## Exact approvals

State-changing operations produce a human-readable summary and an approval token bound to:

- the actor;
- the request;
- the tool;
- the exact arguments;
- a call fingerprint;
- a five-minute expiry.

An approval is single-use. Any parameter change creates a new action requiring a new approval.

## Idempotency

Completed write fingerprints are tracked so a repeated model decision, browser retry or double click cannot silently execute the same logical operation twice.

## Post-write verification

The response from a write endpoint is not sufficient proof of success. The adapter reads the affected booking, service or invoice again and checks the persisted state against the approved action.

## Financial grounding

Room prices, service prices, quantities and tax rules come from QloApps. The model can explain these values but cannot invent or override them. A mismatch between the confirmed quote and the stored result produces a controlled failure and, where supported, a verified rollback.

## Observability

JSONL traces record:

- request and turn identifiers;
- selected tool;
- redacted arguments;
- tool status and error code;
- verification status;
- completed plan steps;
- final outcome.

API keys, common personal data and hidden model reasoning are excluded from traces.

## Deliberate boundaries

Payments, destructive deletion and unrestricted administrative configuration are not part of the model-accessible tool set. The design favours explicit, auditable capabilities over broad access.

