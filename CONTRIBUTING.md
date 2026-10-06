# Contributing

## Development setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

## Contribution principles

- Keep model decisions separate from deterministic safety checks.
- Add Pydantic schemas for every new tool argument.
- Require exact approval for state-changing operations.
- Verify every write through a second read.
- Add tests for success, ambiguity, permission failure and external-service errors.
- Never add real customer data or credentials.
- Document new capabilities in generic hotel-management language.

