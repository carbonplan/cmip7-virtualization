# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Dependencies
```bash
uv sync                   # install all project dependencies
uv sync --group docs      # also install mystmd for docs
```

### Run scripts
```bash
uv run python virtual-zarr-script.py
uv run python reference_generation/netcdf4_s3_icechunk.py
```

### Linting
```bash
uv run ruff check --fix
uv run ruff format
```

### Tests
```bash
uv run pytest                    # everything, including live network tests
uv run pytest -m "not live"      # offline / CI-without-network
```
Tests marked `live` (`tests/test_live_icechunk.py`) read a real remote store over
the network. They run by default on purpose: a store somebody else hosts breaking
is exactly what we want to hear about. Marker registered in `pyproject.toml`.

## Worktree workflow

Work on a feature in a git worktree under `.worktrees/`, one directory per branch,
so the main checkout keeps its uncommitted state and you can run two envs side by
side (each worktree gets its own `.venv`).

```bash
git worktree add .worktrees/<branch-name> -b <branch-name> main   # create
cd .worktrees/<branch-name> && uv sync                            # own venv
...                                                               # work, commit
git push -u origin <branch-name>
cd ../.. && git worktree remove .worktrees/<branch-name>          # after merge
git worktree list                                                 # what exists
```

`.worktrees/` is gitignored. Branch from `main` unless the work genuinely builds on
another branch — a worktree cut from a feature branch drags all of its commits into
the eventual PR. Note `uv sync` in a worktree resolves against that worktree's
`uv.lock`, so a dependency bump stays scoped to the branch.

### Docs (requires `uv sync --group docs` first)
```bash
myst start          # local preview server at http://localhost:3000
myst build --html   # build static HTML to ./_build/html/
```

### Jupyter kernel
```bash
uv run python -m ipykernel install --user --name=esgf-virtual-zarr
jupyter kernelspec uninstall esgf-virtual-zarr
```

## Architecture

This is a research project with no installable Python package — it contains scripts and notebooks demonstrating how to create virtual Zarr reference stores for CMIP6 data.

**Two reference-generation patterns:**

1. **HTTP + kerchunk JSON** (`virtual-zarr-script.py`): Opens NetCDF files from ESGF Thredds HTTP URLs as VirtualiZarr virtual datasets, concatenates along time, writes kerchunk JSON. Simple and serial.

2. **S3 + dask + icechunk** (`reference_generation/netcdf4_s3_icechunk.py`): Opens NetCDF from `s3://esgf-world/`, parallelises with dask, writes to kerchunk parquet then converts to an [IcechunkStore](https://github.com/earth-mover/icechunk) on S3 for Zarr V3 access.

**Reading a store hosted over plain HTTPS:** `storage.open_http_repository(url,
virtual_prefixes=[...])` opens a read-only Icechunk repo served as static files by
an ordinary web server (CEDA publishes one from a JASMIN group workspace this way).
Demo: `notebooks/reference-generation/read-jasmin-icechunk-over-https.ipynb`.
`virtual_prefixes` are the hosts the *chunk manifests* point at — omit them and the
metadata opens fine while every array read fails.

**Key libraries:**
- `virtualizarr` — installed from git HEAD (not PyPI); entry points: `open_virtual_dataset`, `open_virtual_mfdataset`, `HDFParser`, `ObjectStoreRegistry`
- `icechunk` — Zarr V3 virtual reference store. **Requires >= 2.0**: `http_storage()`
  (HTTP as the *repository* backend) only exists in 2.x. Do not confuse it with
  `http_store()`, which is the *virtual-chunk* source and exists in both. Icechunk 2
  also replaced the bare `None` no-auth sentinel with `credentials.HttpAccess`.
- `kerchunk` — legacy format support (JSON/parquet)

**Docs:** MyST-MD site with source in `docs/` and `notebooks/`. Config in `myst.yml`. Deployed to GitHub Pages via `.github/workflows/deploy-docs.yml` on push to `main`. Notebooks are rendered statically (not re-executed in CI).

**Output directories:**
- `refs/` — intermediate and final reference files (parquet, icechunk repo)
- `./_build/html/` — built docs (gitignored)
