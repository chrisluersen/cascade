# Cascade Instructions

Applied when editing files under `cascade/`.

## Project Identity

Local LLM proxy with smart provider cascade, failover, and credential pooling.
Maintained by Chris. Public project since 2026-07-13.

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

This directory is the canonical source for cascade. Imported from its own git repo with full history preserved via git subtree. The public repo at `github.com/chrisluersen/cascade` is synced from here via `git subtree push`.