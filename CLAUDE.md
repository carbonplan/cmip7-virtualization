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
uv run pytest                          # everything, live network tests included
uv run pytest -m "not live"            # offline only
uv run pytest -m "live and not watch"  # live hard invariants only
uv run pytest -m watch                 # "did the catalogs change?" checks
```
`live` tests hit real ESGF catalogs and run by default — a catalog we do not
control moving under us is exactly what we want to hear about. `watch` tests
assert an *observed* server state still holds; a failure means the world changed
and a decision is due, not that our code broke. Markers in `pyproject.toml`.

### CI (`.github/workflows/`)
- `tests.yml` — every PR + push to `main`. Two jobs, `offline` and `live`, which together are the whole suite; split so the check name says whether it is our code or someone else's catalog.
- `catalog-monitor.yml` — daily 06:00 UTC. Runs the whole suite and, on failure, opens (or comments on) an issue labelled `catalog-drift`. ⚠️ `schedule` **and** `workflow_dispatch` both require the workflow to be on the **default branch** — dispatching from a feature branch 404s — so the monitor cannot run at all until this is on `main`. The same tests run on every PR via `tests.yml`; only the issue-filing step is unexercised until then.
- `deploy-docs.yml` — pre-existing; MyST → GitHub Pages on push to `main`.
- Pinned to `actions/checkout@v5` + `astral-sh/setup-uv@v9` (`enable-cache: true`). Lint changes with `uvx --from actionlint-py actionlint .github/workflows/*.yml`.

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

A research project — scripts and notebooks demonstrating how to create virtual Zarr reference stores for CMIP6/CMIP7 data — plus an installable package, `src/cmip7_virtualization/`.

**`catalog.py`** — the only module documented here; the rest arrive on their own branches.

- `STAC_BASES`: read/write endpoints for test and production on both the East (CEDA) and West federations.
- `collection_counts(base, verify=True)`: item count per collection. Absorbs two server quirks so callers need not know them — (a) `/collections` can 500 *wholesale* when a single collection document is unserialisable (West's `obs4ref`), so `_collections_meta` falls back to the root catalog's `child` links; (b) West advertises lowercase ids (`cmip6plus`) but stores canonical DRS case (`CMIP6Plus`) and `/search` is case-sensitive, so the collection title is OR'd in alongside `upper()`/`lower()`. ⚠️ `verify=True` reconciles against the unfiltered total and, on mismatch, **pages every item** — 391k on East prod. Pass `verify=False` on hot paths.
- `is_reference_asset(asset)`: substring-matches `kerchunk`/`icechunk` in the media type and the `reference`/`virtual` roles, because publishers disagree on spelling (`application/vnd.zarr+kerchunk` vs our `application/vnd+zarr+kerchunk`).
- `notebooks/catalog-discovery/catalog-check.ipynb` prints the whole matrix; `tests/test_live_catalogs.py` is the live monitor.

**Two reference-generation patterns:**

1. **HTTP + kerchunk JSON** (`virtual-zarr-script.py`): Opens NetCDF files from ESGF Thredds HTTP URLs as VirtualiZarr virtual datasets, concatenates along time, writes kerchunk JSON. Simple and serial.

2. **S3 + dask + icechunk** (`reference_generation/netcdf4_s3_icechunk.py`): Opens NetCDF from `s3://esgf-world/`, parallelises with dask, writes to kerchunk parquet then converts to an [IcechunkStore](https://github.com/earth-mover/icechunk) on S3 for Zarr V3 access.

**Key libraries:**
- `virtualizarr` — installed from git HEAD (not PyPI); entry points: `open_virtual_dataset`, `open_virtual_mfdataset`, `HDFParser`, `ObjectStoreRegistry`
- `icechunk` — Zarr V3 virtual reference store with `StorageConfig`, `StoreConfig`, `VirtualRefConfig`
- `kerchunk` — legacy format support (JSON/parquet)

**Docs:** MyST-MD site with source in `docs/` and `notebooks/`. Config in `myst.yml`. Deployed to GitHub Pages via `.github/workflows/deploy-docs.yml` on push to `main`. Notebooks are rendered statically (not re-executed in CI).

**Output directories:**
- `refs/` — intermediate and final reference files (parquet, icechunk repo)
- `./_build/html/` — built docs (gitignored)
