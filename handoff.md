# Session handoff — CMIP7 virtualization

> Written before a machine restart (disk was full — see Blocker). Everything below
> is committed to the working tree, not yet to git. Plans live at
> `~/.claude/plans/go-deep-and-surface-mellow-beaver.md` (3-track exec plan) and
> `~/.claude/plans/crispy-fluttering-newell.md` (this multi-reference feature).
> Living intel: `~/Code/earthstack/cmip7-virtualization/ESGF-INTEL.md`.

## ⚠️ Blocker: disk full
`/System/Volumes/Data` hit 100% (≈322 MiB free). A `uv sync` failed mid-download
(`No space left on device`) and left `.venv` **without the editable install**, so
`uv run pytest` currently errors `ModuleNotFoundError: cmip7_virtualization`.
**After restart / freeing space:** `uv sync --group dev` then `uv run pytest`
(11 tests, all passed before the disk filled). `uv cache prune` may help.

---

## What this session delivered

### Track C — `esgadd` → ESGF-Playground (a previous step, complete)
New dir `playground/`:
- `esgadd_playground.py` — `seed → build → submit → verify` orchestrator.
- `esg-playground.yaml` — no-auth esgadd config.
- `README.md` — commands + the verified esgadd flow + **7 upstream findings**.

Verified by reading `ESGF/esg-publisher@esgf-ng-v5.4a` source (not inferred):
- esgadd fetches the item then **PATCHes** `/collections/{c}/items/{id}` with
  `application/json-patch+json`.
- **No-auth path:** `--stac-api <url>` makes `EGITransactionClient` use
  `auth=None`. Point it at the stac-fastapi-es East node **`:9010`** (the bundled
  transaction API `:9050` is **create-only**, no PATCH).
- esgadd quirks (file upstream): media type `application/icechunk` (not
  `application/vnd.zarr+icechunk`); `role` (singular) vs `roles`;
  `description:"TEST"`; nested-`alternate` add can break RFC-6902; **alternate
  nesting is semantically wrong for distinct virtual stores**; **cannot add an
  arbitrarily-keyed separate asset**.
- esgadd is NOT installed here. Install in a SEPARATE env (heavy deps):
  `pip install "git+https://github.com/ESGF/esg-publisher.git@esgf-ng-v5.4a#subdirectory=src/python"`
  → gives the `esgadd` console script. Pass `--esgadd <path>`.

### Multi-reference S3 + notebooks (this session, code complete, NOT yet run end-to-end)
Driven by: new AWS S3 access → host Icechunk on **S3 as well as OSN**, seed all
examples into the Playground with **multiple references per dataset**, and a
notebook demonstrating discovery/filter/selection across **every** access pattern.

**Package changes (`src/cmip7_virtualization/`):**
- `references.py` (NEW) — `build_reference_asset`, `reference_asset_key`,
  `select_reference(assets, prefer_engine=("icechunk","kerchunk"),
  prefer_storage=("s3","osn","http"))`, `is_reference_asset`. **Design decision:
  each (engine×storage×source) is a SEPARATE top-level asset, not an `alternate`**
  — alternate-assets requires *identical files* (verified from the extension spec).
- `storage.py` — added `aws_s3_storage` (real AWS S3, creds via AWS default chain
  so `AWS_PROFILE`/SSO works), `vccs_from_registry` (HTTP **and** S3 virtual-chunk
  containers), `authorize_prefixes_from_registry` (`None` for HTTP,
  `s3_anonymous_credentials()` for S3). Kept `osn_storage`, `http_vccs_from_registry`.
- `virtualize.py` — `virtualize_from_urls(urls, s3_region="us-east-2")` now handles
  anonymous `s3://` sources (`skip_signature=True`).
- `__init__.py` — exports the new helpers.

**Tests:** `tests/test_references.py` (11 tests, **passed** before disk filled).
`pyproject.toml` — added `dev` group (pytest), `[tool.pytest.ini_options]`, and
deps `pystac>=1.10`, `xpystac`.

**Notebooks (`notebooks/`, generated, syntax-validated, NOT executed):**
- `build_and_seed_playground.ipynb` — builds Icechunk on OSN+AWS-S3 for N CEDA
  items + 1 esgf-world-S3-sourced item; seeds the Playground with separate
  reference assets via direct STAC `PUT`.
- `reference-discovery-and-selection.ipynb` — discovery→filter→select→read across
  7 patterns (STAC search, pystac `get_assets`+`select_reference`, direct Icechunk
  open, **xpystac `engine="stac"`**, kerchunk `reference_file`, intake-ESGF,
  alternate-vs-separate discussion), recording WORKS/FAILS into a summary matrix.
  Failures are kept on purpose (user wants the negative results as feedback).

### Verified APIs (not inferred)
- `ic.s3_storage(..., from_env=True)` uses the AWS default chain; `anonymous=True`
  for public buckets.
- `ic.s3_store(region=, anonymous=)` for S3 virtual-chunk containers.
- `ic.Repository.open(..., authorize_virtual_chunk_access={prefix: None | creds})`;
  use `ic.s3_anonymous_credentials()` for anonymous S3 source chunks.
- **esgf-world is `us-east-2`**, public-read per-object via obstore
  `from_url("s3://esgf-world", skip_signature=True, region="us-east-2")` (bucket
  root listing is denied; prefix listing works). Confirmed live: 17 `.nc` objects
  for the CanESM5 example.
- xpystac `_icechunk.py` is **S3-only** (no HTTP virtual-chunk path) — so the
  xpystac pattern is expected to FAIL for OSN/HTTP and only possibly work for the
  S3-store + esgf-world-S3-source case.

---

## To resume / run end-to-end
1. Restart, free space, `uv sync --group dev && uv run pytest`.
2. **Fill in** `notebooks/build_and_seed_playground.ipynb` config cell:
   `AWS_PROFILE`, `S3_BUCKET`, `S3_REGION` (the new bucket — *you still owe me the
   name/region/profile*). OSN keys load via 1Password `op read` as before.
3. Bring up Playground: `cd ~/Code/ESGF-Playground && docker compose up -d` (wait
   ~30–60 s for stac-fastapi-es on `:9010`).
4. Run `build_and_seed_playground.ipynb`, then
   `reference-discovery-and-selection.ipynb` top-to-bottom; read the summary matrix.
5. Optional Track C live run: install esgadd in a separate venv (above), then
   `uv run python playground/esgadd_playground.py all --n 3 --esgadd <path>`.

## Open follow-ups
- **Run the notebooks** and capture the real WORKS/FAILS matrix (esp. xpystac).
- **intake-ESGF** pattern is a stub (`TODO`) — check STAC/icechunk support.
- **Replica discovery via alternate-assets** (your note): explore once replicas are
  in the catalog — read the `alternate` list of *source* NetCDF to find a node-B
  copy, then build a *new* virtual store over it.
- File the esgadd findings upstream (`ESGF/esg-publisher`).
- Docs updated: project `CLAUDE.md`, `playground/README.md`. Nothing committed to
  git yet — review the diff before committing.
