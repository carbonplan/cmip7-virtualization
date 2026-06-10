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
`live` tests hit real ESGF catalogs (`tests/test_live_catalogs.py`) and remote
Icechunk stores (`tests/test_live_icechunk.py`), and run by default — someone
else's catalog or store moving under us is exactly what we want to hear about.
`watch` tests assert an *observed* server state still holds; a failure means the
world changed and a decision is due, not that our code broke. Markers in
`pyproject.toml`.

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

## Architecture

This project has an installable Python package (`cmip7_virtualization`) plus scripts and notebooks demonstrating virtual Zarr reference store creation for CMIP6/CMIP7 data.

**Package:** `src/cmip7_virtualization/` — importable via `from cmip7_virtualization import virtualize_from_urls`.

- `virtualize.py`: `virtualize_from_urls(urls, s3_region=...)` → `(xr.Dataset, ObjectStoreRegistry)` using `HDFParser` + `open_virtual_mfdataset`. Handles anonymous HTTP (CEDA) **and** anonymous `s3://` sources (esgf-world is `us-east-2`, `skip_signature=True`).
- `storage.py`: `osn_storage` (Ceph, static keys) / `aws_s3_storage` (real AWS S3, creds via AWS default chain so `AWS_PROFILE`/SSO works) / `vccs_from_registry` (HTTP **and** S3 virtual-chunk containers) / `authorize_prefixes_from_registry` (read-side `authorize_virtual_chunk_access`).
- `catalog.py`: `STAC_BASES` (read/write endpoints for test and production on both the East (CEDA) and West federations); `collection_counts(base, verify=True)` for item count per collection; `is_reference_asset(asset)`. Two server quirks are absorbed here so callers need not know them — (a) `/collections` can 500 *wholesale* when a single collection document is unserialisable (West's `obs4ref`), so `_collections_meta` falls back to the root catalog's `child` links; (b) West advertises lowercase ids (`cmip6plus`) but stores canonical DRS case (`CMIP6Plus`) and `/search` is case-sensitive, so the collection title is OR'd in alongside `upper()`/`lower()`. ⚠️ `verify=True` reconciles against the unfiltered total and, on mismatch, **pages every item** — 391k on East prod; pass `verify=False` on hot paths. `is_reference_asset` substring-matches `kerchunk`/`icechunk` in the media type and the `reference`/`virtual` roles, because publishers disagree on spelling (`application/vnd.zarr+kerchunk` vs our `application/vnd+zarr+kerchunk`). `notebooks/catalog-discovery/catalog-check.ipynb` prints the whole matrix; `tests/test_catalog.py` is offline, `tests/test_live_catalogs.py` is the live monitor.
- `references.py`: multi-reference asset model + `select_reference(assets, prefer_engine, prefer_storage)` policy (icechunk>kerchunk, s3>osn). **Each (engine×storage×source) is a SEPARATE top-level asset, not an `alternate`** — alternate-assets is only for *identical files* (replicas). Tested in `tests/test_references.py` (`uv run pytest`).

**Multi-reference notebooks** (`notebooks/`): `build_and_seed_playground.ipynb` builds Icechunk on OSN+AWS-S3 (one example sourced from esgf-world S3) and seeds the Playground with separate reference assets; `reference-discovery-and-selection.ipynb` demonstrates discovery→filter→select→read across **every** access pattern (pystac `get_assets`+`select_reference`, direct Icechunk open, xpystac `engine="stac"`, kerchunk `reference_file`, intake-ESGF), recording WORKS/FAILS into a summary matrix. (Not run in CI; require Playground + AWS + OSN creds.)

**Two reference-generation patterns:**

1. **HTTP + kerchunk JSON** (`virtual-zarr-script.py`): Opens NetCDF files from ESGF Thredds HTTP URLs as VirtualiZarr virtual datasets, concatenates along time, writes kerchunk JSON. Simple and serial.

2. **S3 + dask + icechunk** (`reference_generation/netcdf4_s3_icechunk.py`): Opens NetCDF from `s3://esgf-world/`, parallelises with dask, writes to kerchunk parquet then converts to an [IcechunkStore](https://github.com/earth-mover/icechunk) on S3 for Zarr V3 access.

**Reading a store hosted over plain HTTPS:** `storage.open_http_repository(url,
virtual_prefixes=[...])` opens a read-only Icechunk repo served as static files by
an ordinary web server (CEDA publishes one from a JASMIN group workspace this way).
Demo: `notebooks/reference-generation/read-jasmin-icechunk-over-https.ipynb`.
`virtual_prefixes` are the hosts the *chunk manifests* point at — omit them and the
metadata opens fine while every array read fails.

**Catalog submission demo (`playground/`):** Track C end-to-end — attach an
Icechunk reference asset to a STAC Item using the production tool `esgadd`
against the local [ESGF-Playground](https://github.com/ESGF/ESGF-Playground),
no auth. `esgadd_playground.py` runs `seed → build → submit → verify`; `esgadd`
PATCHes the stac-fastapi-es East node (`:9010`) anonymously (the bundled
transaction API on `:9050` is create-only). Install `esgadd` in a SEPARATE env
(conflicting deps). See `playground/README.md` for commands + the esgadd quirks
to file upstream (`type: application/icechunk` vs `vnd.zarr+icechunk`, `role` vs
`roles`).

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
