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
- [x] Update project `CLAUDE.md` with the live-catalog flow + blockers.
- [x] Add `post-full` workaround (build full Item incl. ref, POST whole) — lands the ref despite 405.
- [x] Keep `submit` as a live 405 demonstration.
- [ ] Resolve PATCH-405 for real (authenticated transaction API — see Track 1).

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

---

# Forward plan — three tracks (2026-06-10)

## Track 1 — Where to test the *real* (prod-like) PATCH, + access

**Catalog endpoints (read-only probes 2026-06-09/10):**

| Endpoint | Status | Notes |
|---|---|---|
| `api.stac.esgf.ceda.ac.uk` (CEDA East **prod**) | **empty** (0 items) | discovery; nothing published yet |
| `api.stac.esgf-test.ceda.ac.uk` (CEDA East **test**) | **19,105 items**, **has kerchunk `reference_file`** (`application/vnd.zarr+kerchunk`) | best **read** staging; good multi-ref source |
| `discovery.east.esgf.io` / `transaction.east.esgf.io` | 200 ("ESGF EAST STAC API") | East aliases; conformance shows **no** transaction/patch class publicly |
| `discovery.integration.esgf-west.org` (West) | live, ~21 items, **no** kerchunk refs | what `prepopulate.py` uses now |
| `transaction.integration.esgf-west.org` (West write) | **401** | Globus-auth; the real write path |

**Conclusion:** PATCH/write is **auth-gated everywhere** — no unauthenticated
staging accepts writes, and no public discovery endpoint advertises the
`…/transaction#patch` class. So the only way to test the *real* submit is to get
**onboarded** and point `esgadd --stac-api` at an authenticated transaction API.

**Access path (the work on our end):**
- Register as an **"asset contributor"** — process not yet documented; **asked
  Sasha 2026-06-09, awaiting reply** (chase in `#core-architecture`).
- Auth: **Globus group** (West) / **EGI Check-In** (East). West transaction uses
  Globus scope `7467bc71-1417-43f0-a7a9-a26c45757c36/transaction` (from a publish
  log in `#general`, 2026-06-05).
- Once authed: run `esgadd` against `transaction.integration.esgf-west.org`
  (Globus) or the East transaction API (EGI) — `esg-playground.yaml` becomes a
  real auth config instead of the no-auth one.
- **Until then:** CEDA **test** for read-side validation; local Playground to
  simulate writes (but its image lacks PATCH — see the 405 blocker).

**Open asks:** (a) asset-contributor process + which catalog to onboard with
(Sasha); (b) confirm whether a writable test transaction API exists that we can
get into faster than prod.

## Track 2 — Get our Icechunk logic to run inside CEDA `padocc`

High-value: CEDA-published data would get Icechunk refs **automatically** (covers
CEDA data; the decoupled `esgadd` path covers everything else). Two concrete
integration points exist in **`cedadev/padocc@cmip7_beta`**:

1. **`padocc/phases/aggregate.py`** — the VirtualiZarr combine already does
   `open_virtual_dataset → combine_nested → virtualize.to_kerchunk`, and contains
   a **commented-out icechunk block** (Daniel's `# TESTING`: `to_icechunk` +
   `VirtualChunkContainer`, local-fs only). → *Smallest change:* generalize this
   to optionally emit Icechunk to a **configurable storage** (local for CI,
   OSN/S3 for prod), reusing our verified `vccs_from_registry()` /
   `osn_storage()` / `aws_s3_storage()`.
2. **`padocc/phases/compute.py :: IcechunkDS(ComputeOperation)`** — a **stub**
   whose `_run` only runs CFA (`super()._run`) and returns `True`; it builds
   nothing. `ZarrDS` (same file) is the model: `_run → create_store`. → *Proper
   home:* implement `IcechunkDS.create_store` mirroring `ZarrDS`, delegating the
   actual virtualize+write to `cmip7_virtualization`.

CLI is already wired: `-C/--cloud_format icechunk` (or `mode='icechunk'`); the
scan phase can auto-switch to icechunk when chunk count > 3M (today: kerchunk-parquet).

**Plan:**
1. Branch `cedadev/padocc` (or our fork); reproduce a kerchunk run on one CMIP6
   dataset to learn the storage/chunking/CFA abstractions.
2. Implement `IcechunkDS.create_store` using our package; make the storage target
   pluggable (local | OSN | S3) via padocc config.
3. Decide store layout + the asset it should emit (align with Track 3 media types
   / `reference_icechunk_<storage>` key).
4. Tests on a small fixture; wire into the `scan`→`compute`→`aggregate` flow.
5. PR to `cedadev/padocc`, coordinate with **Daniel (`#arco`)**; keep our logic as
   the dependency so it "runs for us".

**Risks:** padocc's storage filehandlers, chunk-scheme/CFA interplay, and how
aggregations are registered as STAC assets post-publication.

## Track 3 — STAC details + fully-programmatic open across engines × storage × access

**Decisions to lock (in `references.py`):**
- **Kerchunk media type:** we currently emit `application/vnd+zarr+kerchunk`;
  CEDA-test uses **`application/vnd.zarr+kerchunk`**. → **align to the dotted
  form** (and keep accepting both on read).
- **Asset model:** keep "each (engine×storage×source) = separate top-level asset"
  (`reference_icechunk_osn`, `reference_icechunk_s3`, `reference_file`=kerchunk).
- **storage_options:** standardize on `xarray:storage_options`
  (`region`/`anonymous`/`endpoint_url`) so a generic reader can pass them through.

**Access matrix to implement + document (WORKS/FAILS, failures kept on purpose):**

| engine | source storage | reader | expected |
|---|---|---|---|
| icechunk | S3 (esgf-world) | xpystac `engine="stac"` | works (xpystac `_icechunk` is S3-only) |
| icechunk | HTTP (CEDA dap) | xpystac `engine="stac"` | **FAILS** — no `http_store` path (document) |
| icechunk | OSN / S3 / HTTP | direct `ic.Repository.open(..., authorize_virtual_chunk_access={prefix: None or creds})` | works (our canonical reader) |
| kerchunk | HTTP json | `xr.open_dataset(href, engine="kerchunk"/"reference_file")` | works |
| kerchunk | S3/parquet | kerchunk + fsspec | tbd — document |

**Deliverable:** a single `open_reference(asset, *, prefer=...)` that dispatches on
`type` + `xarray:storage_options` to the right reader, plus a generated WORKS/FAILS
matrix (the `reference-discovery-and-selection.ipynb` notebook already prototypes
this — formalize it into the package + a results table; document the access
options: anonymous vs keyed, region, endpoint_url, per-prefix authorization).

**Canonical reader for now:** direct Icechunk open (xpystac HTTP support pending —
ESGF-INTEL.md). Revisit when the xpystac HTTP PR lands.
