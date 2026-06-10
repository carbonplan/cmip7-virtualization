# Plan — Track C: esgadd existing OSN references into the Playground

> Goal (from the user): find the **existing** OSN virtual stores with the AWS CLI,
> build a **factored-out** script that prepopulates the Playground with the
> matching STAC items, then **esgadd** each OSN Icechunk reference onto its item.

## ⚠️ Unexpected blocker — the official CEDA STAC catalog is EMPTY (2026-06-09)

The production CEDA STAC API responds, the collections still exist, but **every
collection has zero items**:

```
https://api.stac.esgf.ceda.ac.uk/collections/{C}/items?limit=1  ->  numberMatched=0
  CMIP6=0  CMIP6Plus=0  CMIP7=0  CORDEX-CMIP6=0  obs4REF=0
```

- The collection metadata still resolves (`GET /collections/CMIP6` → 200).
- Earlier **this same session** the seed code pulled live items from
  `…/collections/CMIP6/items?limit=20` and got real features — so the catalog
  was drained recently (reindex / rebuild on CEDA's side, presumed temporary).
- **Impact:** the original `seed()` path (mirror CEDA collection + pull live
  Items) no longer returns anything. We cannot source "matching datasets" from
  the CEDA STAC API right now.

**→ ACTION FOR USER: ask ESGF/CEDA whether the empty catalog is intended /
temporary, and when items return.** Until then we drive seeding from the OSN
store names (below), which is arguably more robust anyway.

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

## Design — factor the prepopulate step OUT

Currently `seed()` is embedded in `playground/esgadd_playground.py` and pulls from
CEDA. Refactor so prepopulation is reusable and **OSN-driven**:

1. **New module `playground/prepopulate.py`** (or `cmip7_virtualization` package
   module if it should be importable/tested):
   - `list_osn_stores(bucket, root_prefix) -> list[str]` — list OSN prefixes →
     dataset_ids (the AWS-CLI listing, in code via obstore/boto3 + OSN endpoint).
   - `dataset_id_to_source_urls(dataset_id) -> list[str]` — DRS → CEDA DAP
     archive dir → list `.nc` files → `https://dap.ceda.ac.uk/...` URLs.
     **(pending the URL-source decision the user deferred to ESGF — see Open
     questions.)**
   - `minimal_item(dataset_id, urls) -> dict` — STAC Item keyed by dataset_id
     with `data` assets (reuse the pattern in
     `notebooks/build_and_seed_playground.ipynb`).
   - `ensure_collection(stac_url, collection)` — create the CMIP6 collection
     (mirror minimal metadata; can't pull from CEDA now, so use a static stub).
   - `prepopulate(stac_url, dataset_ids)` — PUT each item (stripping
     `reference_file` so esgadd lands cleanly).
2. **Rewire `esgadd_playground.py`** to import from `prepopulate` instead of its
   inline `seed()`. Keep the `build/submit/verify` subcommands. Add a mode that
   takes the OSN store list as the dataset source (no CEDA dependency).
3. **esgadd loop**: for each OSN dataset_id, `--agg-url` =
   `https://nyu1.osn.mghpcc.org/leap-pangeo-pipeline/cmip7-virtualization/{id}/`
   (public OSN URL — production-style, not `file://`).
4. **verify**: read item back, assert the icechunk asset present; open the OSN
   store directly with `Repository.open(..., authorize_virtual_chunk_access=
   {"https://dap.ceda.ac.uk/": None})` (xpystac engine='stac' can't read
   anonymous-HTTP virtual chunks yet).

## Task list

- [ ] **USER:** confirm with ESGF whether the empty CEDA STAC catalog is
      intended/temporary, and the source-URL approach (deferred above).
- [x] Resolve the disk-full blocker; `uv sync --group dev`; `uv run pytest` (11 pass).
- [x] Discover existing OSN references via AWS CLI (4 VolMIP UKESM stores).
- [x] Confirm each OSN prefix is a real Icechunk repo (config.yaml etc.).
- [x] Confirm CEDA DAP source `.nc` files still reachable at the DRS path.
- [x] Confirm Playground East (`:9010`) up.
- [ ] Decide URL-source strategy (CEDA DAP archive vs read-from-store vs minimal)
      — **blocked on user/ESGF**.
- [ ] Write `playground/prepopulate.py` (factored-out, OSN-driven seeding).
- [ ] Refactor `playground/esgadd_playground.py` to use it; add OSN-source mode.
- [ ] Install esgadd in a separate venv (`~/.venvs/esg-publisher`).
- [ ] Run end-to-end: prepopulate → esgadd (OSN URL) → verify, for the 4 stores.
- [ ] Update `playground/README.md` + project `CLAUDE.md` with the OSN-driven flow.

## Open questions (for ESGF / user)

1. **Empty CEDA STAC catalog** — temporary? ETA for items returning? (blocks the
   live-CEDA seed path; we work around it via OSN names for now.)
2. **Source-URL recovery for seeded items** — user is checking with ESGF.
   Candidates: (a) CEDA DAP archive path from DRS [recommended, works today];
   (b) read exact source URLs from the Icechunk store manifest [most faithful];
   (c) minimal items, no data assets [simplest]. Decision deferred.
3. **Item identity** — is keying the Playground item purely by the OSN
   dataset_id acceptable as "the matching dataset", given CEDA can't supply the
   canonical item right now?
