# Contributing Guide

## Setting Up the Environment

```bash
uv sync
```

## Linting and Formatting

Pre-commit hooks run automatically on commit using [Ruff](https://docs.astral.sh/ruff/). To run manually:

```bash
uv run ruff check --fix
uv run ruff format
```

Install pre-commit hooks once:

```bash
uv run pre-commit install
```

## Building the Docs Locally

Install docs dependencies:

```bash
uv sync --group docs
```

Build static HTML:

```bash
uv run sphinx-build -b html docs _build/html
```

Or use autobuild for live reloading:

```bash
uv run sphinx-autobuild docs _build/html
```

Output is written to `./_build/html/`.

## Adding New Pages

1. Create a new `.md` file under `docs/`.
2. Add it to the `toctree` in `docs/index.md`.

## Notebooks

Notebooks are rendered **statically** from stored cell outputs — they are **not re-executed** at build time. This is intentional: the example notebooks require a live Dask cluster and access to ESGF/S3 data sources that are not available in CI.

When adding or updating a notebook:
1. Run it locally to completion so that output cells are populated.
2. Commit the notebook with outputs included.
3. Add the notebook path to the toctree in `docs/index.md`.

## ReadTheDocs Deployment

The docs are automatically built and deployed on ReadTheDocs on every push to `main`. Configuration is in `.readthedocs.yaml`.
