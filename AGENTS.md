# Agent Rules for nomnom

## How to run Python scripts

Always use `uv`, never system `python3` or bare `python`:

```bash
uv run python scripts/query.py --status
uv run python scripts/query.py --near "Cagliari, Italy" --radius-km 5 --limit 15
uv run python scripts/sync.py --source michelin
uv run python scripts/sync_blog.py
uv run python scripts/search.py ...
```

The codebase targets Python 3.10+ syntax. The user's default `python3` is 3.8 from a conda environment and **will break on `|` union type syntax**.

`uv run` from the repo root uses the correct interpreter and virtualenv defined in `pyproject.toml`.
