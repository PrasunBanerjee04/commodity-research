# datalake

[Functions and examples](../docs/datalake.md) · [Status](IMPLEMENTATION_STATUS.md)

```sh
.venv/bin/python -m pip install -e './setup/datalake[dev]'
.venv/bin/python infra/examples/datalake/quickstart.py
.venv/bin/python -m pytest -c setup/datalake/pyproject.toml infra/tests/datalake
.venv/bin/python -m mypy --config-file setup/datalake/pyproject.toml infra/datalake
.venv/bin/python -m ruff check --config setup/datalake/pyproject.toml infra/datalake infra/tests/datalake infra/examples/datalake
```

Source: `infra/datalake/`. Reads existing files in `data/lake/`; no database or registration.
Core dependency: Polars. Optional inputs: `datalake[arrow]` or `datalake[pandas]`.
