# Plan — Track C: esgadd existing OSN references into the Playground

> Goal (from the user): find the **existing** OSN virtual stores with the AWS CLI,
> build a **factored-out** script that prepopulates the Playground with the
> matching STAC items, then **esgadd** each OSN Icechunk reference onto its item.

## ⚠️ The CEDA East *production* catalog is EMPTY — but the ESGF-West catalog is LIVE

**CEDA East prod (`api.stac.esgf.ceda.ac.uk`) — empty (2026-06-09):**

```
https://api.stac.esgf.ceda.ac.uk/collections/{C}/items?limit=1  ->  numberMatched=0
  CMIP6=0  CMIP6Plus=0  CMIP7=0  CORDEX-CMIP6=0  obs4REF=0
```
Collection metadata still resolves (`GET /collections/CMIP6` → 200), but zero
items. Earlier this same session the seed code pulled live items from it, so it
drained recently (reindex / rebuild, presumed temporary).

**ESGF-West discovery (`discovery.integration.esgf-west.org`) — LIVE & populated.**
Found via Slack (#core-architecture, #esgf-gui, #arco) + `ESGF-INTEL.md`:
- Canonical `api.stac.esgf-west.org` has **no DNS yet**; the *integration /
  data-challenge* host is the one that serves items.
- `GET discovery.integration.esgf-west.org/collections/CMIP6/items?limit=2` →
  real items (AerChemMIP / CFMIP / E3SM data-challenge datasets).
- Publisher config (Alok's 2026-06-05 log) confirms the split:
  discovery `https://discovery.integration.esgf-west.org`,
  transaction `https://transaction.integration.esgf-west.org` (Globus-auth).
- Sasha + Lee (2026-06-08): East⇄West STAC metadata is **replicated** ("eventual
  consistency, ready for use now").
- Rhys (2026-05-15): CEDA-sourced subset *was* findable via
  `filter alternate:name = ceda.ac.uk`; **now returns 0** (data challenge moved on).

**Decision (user): "don't care about the actual experiments."** So we point the
source-catalog endpoint at the live West discovery API and proceed — done in
`playground/esgadd_playground.py` (`SOURCE_STAC`, old CEDA line commented out).

**Note:** our 4 existing OSN VolMIP stores are **not** in the current West
catalog (the data challenge rotated datasets). So "matching datasets" still has
to be reconstructed from the OSN store names (next section) — West gives us the
live **collection metadata** + a working source for *new* demo items, but not
our specific VolMIP Items.

**→ STILL FOR USER/ESGF:** (1) is the empty CEDA prod catalog intended/temporary?
(2) source-URL recovery for reconstructed Items (deferred to ESGF — see Open Qs).

## What still works (verified live this session)

- **The 4 existing OSN Icechunk stores** (single source of truth for "what we
  have"). `aws --endpoint-url https://nyu1.osn.mghpcc.org s3 ls
  s3://leap-pangeo-pipeline/cmip7-virtualization/`:
  ```
  CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.ta.gn.v20230810/
  CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.uas.gn.v20230810/
  CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.wap.gn.v20230810/
  CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.zg.gn.v20230810/
  ```
  Each prefix is a valid Icechunk repo (`config.yaml`, `snapshots/`,
  `manifests/`, `refs/`, `chunks/`, `transactions/`). The prefix **is** the
  ESGF dataset_id. Built by `notebooks/build_static_examples.ipynb` (which used
  `osn_storage` + `http_vccs_from_registry`, anonymous-HTTP CEDA source).
- **The CEDA DAP file archive** (`dap.ceda.ac.uk`) — the actual `.nc` source
  files. The DRS maps to the archive path, e.g.
  `CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.ta.gn.v20230810`
  → `https://dap.ceda.ac.uk/badc/cmip6/data/CMIP6/VolMIP/NERC/UKESM1-0-LL/volc-pinatubo-full/r9i1p1f2/day/ta/gn/v20230810/`
  (HTTP 200; dir lists `ta_day_UKESM1-0-LL_volc-pinatubo-full_r9i1p1f2_gn_19910601-19940630.nc`).
- **OSN public-read URL** for the store href (no signing):
  `https://nyu1.osn.mghpcc.org/leap-pangeo-pipeline/cmip7-virtualization/{dataset_id}/`
- **The Playground** is up (`docker compose` in `~/Code/ESGF-Playground`);
  `stac-fastapi-es` East answers on `http://localhost:9010`.

## esgadd mechanics (already verified, see `playground/README.md`)

- `esgadd --stac-api http://localhost:9010 --dataset-id {id} --agg icechunk
  --agg-url {href} --config playground/esg-playground.yaml` → GET the item, build
  an RFC-6902 JSON-Patch, **PATCH** `/collections/{C}/items/{id}` anonymously.
- esgadd only ever writes the `reference_file` asset; if it already exists it
  nests under `/assets/reference_file/alternate/{site}` (needs the item to NOT
  already have `reference_file`, or it can break RFC-6902 — so seed strips it).
- esgadd lives in a **separate venv** (`~/.venvs/esg-publisher/bin/esgadd`);
  heavy deps conflict with our stack. Not yet installed here.
- Known esgadd quirks (file upstream): `application/icechunk` media type, `role`
  vs `roles`, hardcoded `description:"TEST"`, can't set a custom asset key.

## Design — IMPLEMENTED: live-catalog flow (query → mirror → build → esgadd)

The earlier OSN-driven plan (reconstruct Items from OSN store names) was
superseded once we found the West catalog is live: it's more demonstrative to
mirror **real** Items from a live catalog, build fresh Icechunk stores, and
esgadd. Two factored modules under `playground/`:

1. **`prepopulate.py`** — the factored-out *seed*:
   - `fetch_source_items(source_stac, collection, n)` — query West discovery, keep
     only Items whose NetCDF is on a reachable node (`REACHABLE_HOSTS` =
     ornl / nci / nird; skips dummy `esgf-test.test.gov` + globus-only hrefs).
   - `ensure_collection(...)` — mirror the collection metadata (rewrite id to
     `CMIP6`, strip fields stac-fastapi-es rejects).
   - `put_item(...)` — POST-create, PUT-on-409 (transactions extension).
   - `mirror_items(...)` / `prepopulate(...)` — seed the Playground; strips any
     `reference_file` so esgadd would land cleanly (West has none today).
2. **`esgadd_playground.py`** — orchestrates `seed → build → submit → verify`:
   - `build` virtualizes the Item's NetCDF into an Icechunk store **on OSN**
     (`s3://leap-pangeo-pipeline/cmip7-virtualization/<id>/`); an **AWS-S3** target
     is included but **commented out** in `build()`.
   - `submit` runs `esgadd --agg icechunk --agg-url <public OSN URL>`.
   - `verify` reads the Item back, then opens the OSN store directly
     (`authorize_virtual_chunk_access={host: None}`; xpystac can't read anon-HTTP).

## Task list

- [ ] **USER:** ask ESGF whether the empty CEDA East prod catalog is intended/
      temporary (West discovery is live and used in the meantime).
- [x] Resolve the disk-full blocker; `uv sync --group dev`; `uv run pytest` (11 pass).
- [x] Discover existing OSN references via AWS CLI (4 VolMIP UKESM stores).
- [x] Find a queryable catalog: **ESGF-West discovery is live** (`discovery.integration.esgf-west.org`); CEDA East prod empty.
- [x] Point the source endpoint at West (old CEDA line commented out).
- [x] Write `playground/prepopulate.py` (factored-out, query→mirror).
- [x] Refactor `playground/esgadd_playground.py` (seed→build-on-OSN→submit→verify).
- [x] Verify end-to-end vs live Playground+OSN: seed, build, submit --dry-run, verify ✅.
- [x] Install esgadd in a separate venv (needed 3 packaging-bug workarounds — documented).
- [x] Run real `esgadd` PATCH — **BLOCKED: Playground image returns HTTP 405 (no PATCH).**
- [x] Document upstream issues in `playground/README.md`.
- [ ] Update project `CLAUDE.md` with the live-catalog flow (next).
- [ ] Resolve PATCH-405 (stac-fastapi-es image with PATCH addon, or accept dry-run proof).

## Upstream issues found (file these)

**ESGF/esg-publisher (`esgf-ng-v5.4a`) — packaging, blocks `pip install`:**
1. `esgcet/__init__.py` reads its version via `importlib.metadata.version("esgcet")`
   at build time (`setup.py` does `import esgcet`) → `PackageNotFoundError` before
   install. Hardcode / use a file-or-git version backend.
2. `setup.py` requires **`wcrp-cc-plugi`** — non-existent (typo for `cc-plugin-wcrp`).
3. `esgvoc` imported at runtime (`stac_converter.py`) but not in `install_requires`.

**ESGF-Playground — `ghcr.io/djspstfc/stac-fastapi-es:1.0`:**
4. Item endpoint **does not support PATCH** (405; `OPTIONS` → `allow: GET`; only the
   base transactions conformance class, not `…/transaction#patch`). `POST`/`PUT`
   work. esgadd is PATCH-only → its reference can't land on the local Playground.

**ESGF/esg-publisher — `esgadd` semantics (already in README, 7 items):**
`application/icechunk` media type, `role` vs `roles`, hardcoded `description:"TEST"`,
nested-`alternate` RFC-6902 gap, no `--agg-url` validation, alternate-nesting is
wrong for distinct stores, can't add an arbitrarily-keyed asset.

## Open questions (for ESGF / user)

1. **Empty CEDA East prod catalog** — temporary? ETA for items returning?
2. **PATCH on the Playground** — which stac-fastapi-es image/flag enables the
   JSON-Patch addon so esgadd can land its reference locally?
3. **No kerchunk refs in West** — the demo adds the first virtual reference, not a
   second alongside kerchunk. Fine for now, or wait for kerchunk-bearing Items?
