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

**Catalog submission demo (`playground/`):** Track C — attach a kerchunk/Icechunk
reference asset to a STAC Item using the production tool `esgadd`, no auth.

*Part 1 — validating mirror (the useful one).* `validating_stac_server.py` is a
FastAPI stub mirroring [`ESGF/stac-transaction-api`](https://github.com/ESGF/stac-transaction-api)
(`3b472fe`) — same validation entry points, 202/RFC-9457 responses, and the **real
published JSON Schemas** (offline cache in `src/cmip7_virtualization/schema_cache/`,
refreshed by `playground/refresh_schemas.py`). The stock Playground image can't do
this job: it 405s on PATCH *and* validates nothing. Two modes: `faithful`
(reproduces production exactly) and `strict` (also validates the *patched* Item —
the check production omits). `agg_demo.py` drives `esgadd --agg kerchunk` then
`--agg icechunk` against it; without `--esgadd` it falls back to
`esgadd_ops.add_aggregate_ops`, a replica verified byte-identical to the binary.

Package modules: `stac_validation.py` (ported `validate_patch`/`validate_post`/
`operation_to_partial_item`/`validate_extensions`, each upstream bug marked and
reproduced) and `esgadd_ops.py` (esgadd's patch construction). Both offline-testable;
`tests/test_stac_validation.py` is 36 offline tests, several of which deliberately
**pin upstream bugs** — if one fails, upstream fixed something.

⚠️ **Headline finding (2026-07-26):** `esgadd --agg` emits an asset with **no
`protocol`**, which every ESGF project schema requires of every asset → the
patched Item **fails POST validation** (`'protocol' is a required property`).
Production accepts it anyway because `validate_patch` skips `oneOf` errors and all
asset validation lives inside the schema's top-level `oneOf` — PATCH validation is
inert. Worse for icechunk: `protocol`'s enum has `kerchunk` but **no `icechunk`**,
so no icechunk asset can validate until the *schema* changes. The **two-aggregation
case is broken**: the 2nd `--agg` targets `/assets/reference_file/alternate/{site}`
without creating `alternate` first → invalid RFC-6902. Also: `esgadd` **always exits
1**, even on a 202. 16 filable upstream bugs + the publish-time analysis (PR #303,
`convert2stac`, the minimal change set) are in `playground/README.md`.

*Part 2 — original Playground flow.* `prepopulate.py` (seed from a live catalog)
and `esgadd_playground.py` (`seed → build → submit → verify`). **build** virtualizes
the Item's NetCDF into an Icechunk store **on OSN** (AWS-S3 target commented out).

**Source catalog (re-probed 2026-07-25):** CEDA East prod
(`api.stac.esgf.ceda.ac.uk`) is **now populated** — 391,729 items (CMIP6 390,739,
CORDEX-CMIP6 990, CMIP7 0) — so the original premise for sourcing from West is
gone; `prepopulate.py` still points at **ESGF-West discovery**
(`discovery.integration.esgf-west.org`, now ~8,587 items) and filters to
`REACHABLE_HOSTS` (ornl/nci/nird) because West has many dummy hrefs. East test
(`api.stac.esgf-test.ceda.ac.uk`, 29,525 items) is reachable again but its
kerchunk `reference_file` assets have **disappeared**. No catalog currently
serves reference assets. West **production** (`discovery.production.esgf-west.org`)
is genuinely empty (0 items, not a client bug) and
`integration-testing.api.stac.esgf-west.org` is dead (NXDOMAIN) — removed from
`STAC_BASES`. See `internal/todos/todos.md` for the live table.

⚠️ **The `playground/` + `esgadd` route is a local experiment, not the publication
path.** Real submission is `esgpublish` with a `stac_config:` block pointing at a
**Transaction API** (West = Globus auth, East = EGI Check-in device flow); both
federations are documented step-by-step in
[`ESGF/esgf-ng-onboarding`](https://github.com/ESGF/esgf-ng-onboarding). East
*does* implement PATCH — the Playground's HTTP 405 says nothing about the real
catalogs. See `internal/todos/todos.md` (Track 1) for endpoints, working
`esg.yaml` stanzas, and the live probe table.

Two blockers local to the playground demo: (1) esgadd needs 3 packaging-bug
workarounds to install (version-at-build-time; typo'd dep `wcrp-cc-plugi`;
undeclared `esgvoc`) — **still broken on `main` @ `59bb778`**; install it in a
SEPARATE env (conflicting deps), recipe in `playground/README.md`. (2) The
Playground image (`djspstfc/stac-fastapi-es:1.0`) **rejects PATCH (HTTP 405)**,
POST/PUT only — which is why `validating_stac_server.py` exists. Note the real
Transaction API at HEAD also fails every PATCH: `client.py` L215 calls
`validate_patch` with 3 of its 5 required args → `TypeError` → HTTP 500
(upstream issue #41).

**Key libraries:**
- `virtualizarr` — installed from git HEAD (not PyPI); entry points: `open_virtual_dataset`, `open_virtual_mfdataset`, `HDFParser`, `ObjectStoreRegistry`
- `icechunk` — Zarr V3 virtual reference store. **Requires >= 2.0**: `http_storage()`
  (HTTP as the *repository* backend) only exists in 2.x. Do not confuse it with
  `http_store()`, which is the *virtual-chunk* source and exists in both. Icechunk 2
  also replaced the bare `None` no-auth sentinel with `credentials.HttpAccess`.
- `kerchunk` — legacy format support (JSON/parquet)

**Docs:** MyST-MD site with source in `docs/` and `notebooks/`. Config in `myst.yml`. Deployed to GitHub Pages via `.github/workflows/deploy-docs.yml` on push to `main`. Notebooks are rendered statically (not re-executed in CI).

**padocc icechunk integration (Track 2) — lives OUTSIDE this repo.** Fork
`jbusecke/padocc`, branch `icechunk-compute`, at `~/Code/padocc` (venv `kvenv/`,
`./kvenv/bin/python -m pytest padocc/tests/ --ignore=padocc/tests/test_project.py`).
Implemented + green, **not pushed**. Local-filesystem only; helpers **vendored** into
`padocc/phases/icechunk_store.py` (padocc is py≥3.11 + PyPI virtualizarr, so it cannot
depend on this package). Blocker before PR: regenerate `poetry.lock`. Full status,
the 3 pre-existing padocc bugs it fixes, and the PR argument: `internal/todos/todos.md`
(Track 2) and `~/.claude/plans/make-a-plan-how-streamed-tome.md`.

**Output directories:**
- `refs/` — intermediate and final reference files (parquet, icechunk repo)
- `./_build/html/` — built docs (gitignored)
