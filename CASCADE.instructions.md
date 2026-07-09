# Cascade Instructions

Applied when editing files under `cascade/`.

## Project Identity

Local LLM proxy with smart provider cascade, failover, and credential pooling.
Maintained by Chris. May go public in the future.

## Conventions

- Python 3.11+
- Use `requirements.txt` for dependencies
- Tests with `pytest`
- Ruff for linting
- No duplicate credential logic — secrets live in .env (not committed)

## Development

```bash
cd cascade
python3 -m venv .venv
pip install -r requirements.txt
pytest
```

## Source of Truth

This directory is canonically in the `hermes` monorepo but was imported from its own git repo (`cascade`). Full history preserved via git subtree. If this project is ever extracted to its own public repo, use `git subtree split`.