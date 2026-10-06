# Evaluation

The project separates deterministic software testing from behavioural agent evaluation.

## Automated tests

The suite covers:

- API request and response contracts;
- tool schemas and registry filtering;
- conversation-state continuity;
- provider-compatible function calling;
- permissions and approval expiry;
- duplicate-write prevention;
- QloApps request parsing and normalization;
- booking creation, updates and verification;
- group allocation and composite operations;
- invoice and customer-request workflows;
- trace redaction.

Tests requiring a live QloApps instance and local credentials are marked as optional integration tests. The remaining suite runs without external credentials.

Current verified local result: **177 passed, 8 optional live-integration tests skipped**.

## Agent-level metrics

A representative evaluation set should measure:

| Metric | Question answered |
|---|---|
| Task success rate | Did the complete user objective succeed? |
| Tool-selection accuracy | Was the correct next capability selected? |
| Argument accuracy | Did the structured call contain the correct values? |
| Invalid-call rate | Did the model violate the function schema? |
| Unnecessary clarification rate | Did the agent ask for information already available? |
| Write verification rate | Was every reported write confirmed by a second read? |
| Duplicate-write rate | Did any logical action execute more than once? |
| Mean steps per task | How efficiently did the agent complete the workflow? |
| Latency and token cost | What operational cost did each task require? |

## Model comparison

The architecture supports the same scenario set against different model providers. A valid comparison keeps the tools, prompts, state and decoding configuration fixed, then compares end-to-end outcomes rather than relying only on generic model benchmarks.

This is the intended method for comparing the current DeepSeek configuration with local or fine-tuned alternatives.

