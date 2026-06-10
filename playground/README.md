# Track C — `esgadd` → ESGF-Playground (end-to-end)

Prove we can attach an **Icechunk virtual-reference asset** to an existing STAC
Item using the **production publisher tool** (`esgadd`, from
[`ESGF/esg-publisher`](https://github.com/ESGF/esg-publisher)), talking to the
local [ESGF-Playground](https://github.com/ESGF/ESGF-Playground) — with **no
production auth**. This nails the submission mechanism the other tracks depend on.

## Files

| File | What |
|---|---|
| [`esgadd_playground.py`](./esgadd_playground.py) | Orchestrates `seed → build → submit → verify`. |
| [`prepopulate.py`](./prepopulate.py) | Factored-out **seed**: query a source catalog + mirror suitable Items into the Playground. |
| [`esg-playground.yaml`](./esg-playground.yaml) | The no-auth esgadd config (points the EGI client at the Playground). |

## End-to-end flow

1. **seed** — query the source discovery catalog for a few Items whose NetCDF is
   on a *reachable* node, and mirror them into the Playground.
2. **build** — virtualize each Item's NetCDF into an **Icechunk store on OSN**
   (`s3://leap-pangeo-pipeline/cmip7-virtualization/<id>/`). An AWS-S3 hosting
   target is included, commented out, in `build()`.
3. **submit** — `esgadd --agg icechunk --agg-url <public OSN URL>` PATCHes the
   reference asset onto the Item.
4. **verify** — read the Item back, then open the OSN store directly.

### Source catalog (2026-06-09): CEDA East prod is EMPTY → use ESGF-West

The CEDA East **production** catalog (`api.stac.esgf.ceda.ac.uk`) currently
returns **0 items** in every collection, so `prepopulate.py` sources Items from
the live **ESGF-West discovery** API (`discovery.integration.esgf-west.org`;
`api.stac.esgf-west.org` has no DNS yet). Two caveats with the current West
data-challenge content, both handled in `prepopulate.py`:

* It serves **no kerchunk `reference_file` assets**, so the demo adds the *first*
  virtual reference (not a second one alongside kerchunk).
* Many Items have **dummy data hrefs** (`esgf-test.test.gov`, globus-only
  `app.globus.org`); we keep only Items with NetCDF on `REACHABLE_HOSTS`
  (`esgf-node.ornl.gov`, `esgf.nci.org.au`, `noresg.nird.sigma2.no`).

## Prerequisites

1. **Playground running** (separate checkout, `~/Code/ESGF-Playground`):
   ```bash
   cd ~/Code/ESGF-Playground && docker compose up -d
   # clean wipe:
   docker compose down -v && rm -rf esdata-east esdata-west esdata-secondary && docker compose up -d
   ```
   Wait ~30–60 s for `stac-fastapi-es` (East node, `http://localhost:9010`).

2. **`esgadd` installed in a SEPARATE environment.** esg-publisher pulls in
   `globus-sdk`, `esgvoc`, etc., which conflict with the virtualizarr stack —
   do **not** add it to this project's `uv` env. A plain `pip install` of
   `esgf-ng-v5.4a` **fails** on three upstream packaging bugs (see
   [esg-publisher packaging bugs](#esg-publisher-packaging-bugs-file-upstream)
   below); the working recipe is:
   ```bash
   python -m venv ~/.venvs/esg-publisher
   git clone --depth 1 --branch esgf-ng-v5.4a \
     https://github.com/ESGF/esg-publisher.git /tmp/esg-publisher-src
   cd /tmp/esg-publisher-src/src/python
   # bug 1: __init__ reads version via importlib.metadata at build time -> hardcode
   printf '__version__ = "5.4.0a"\nproject = "esgcet"\n' > esgcet/__init__.py
   # bug 2: setup.py lists a non-existent package "wcrp-cc-plugi" (typo) -> drop it
   sed -i '' '/wcrp-cc-plugi/d' setup.py
   ~/.venvs/esg-publisher/bin/pip install .
   # bug 3: esgvoc is imported at runtime but not declared as a dependency
   ~/.venvs/esg-publisher/bin/pip install esgvoc
   # then pass --esgadd ~/.venvs/esg-publisher/bin/esgadd  (or put it on PATH)
   ```

3. This project installed (`uv sync`) — the `build`/`verify` steps import
   `cmip7_virtualization`.

4. **OSN write keys in the environment** (the `build`/`verify` steps host the
   Icechunk store on OSN):
   ```bash
   export AWS_ACCESS_KEY_ID=$(op read "op://Work/.../Read-Write/Access_Key")
   export AWS_SECRET_ACCESS_KEY=$(op read "op://Work/.../Read-Write/Secret_Access_Key")
   ```

## Run

```bash
# Everything in order (mirror 2 West Items, build on OSN, submit+verify each):
uv run python playground/esgadd_playground.py all --n 2 \
    --esgadd ~/.venvs/esg-publisher/bin/esgadd

# Or step by step, targeting one Item:
uv run python playground/esgadd_playground.py seed --n 2
uv run python playground/esgadd_playground.py build  --item-id <ITEM_ID>
uv run python playground/esgadd_playground.py submit --item-id <ITEM_ID> --esgadd <PATH> --dry-run
uv run python playground/esgadd_playground.py verify --item-id <ITEM_ID>
```

> Note: `submit` against the local Playground currently fails with **HTTP 405**
> (the image doesn't support PATCH — see the blocker under
> [How it actually works](#how-it-actually-works-verified-against-esgf-ng-v54a-source)).
> `seed`, `build`, and `submit --dry-run` work end-to-end today.

`--dry-run` on `submit` prints the exact `esgadd` invocation without running it,
e.g.:

```
esgadd --stac-api http://localhost:9010 --dataset-id <ITEM_ID> \
       --agg icechunk \
       --agg-url https://nyu1.osn.mghpcc.org/leap-pangeo-pipeline/cmip7-virtualization/<ITEM_ID>/ \
       --config playground/esg-playground.yaml --verbose
```

`--agg-url` defaults to the dataset's public OSN store URL (the Playground records
the href but does not dereference it).

## How it actually works (verified against `esgf-ng-v5.4a` source)

`esgadd` (= `esgstacaddrep.py`):

1. **Fetches** the Item: `GET {--stac-api}/collections/{collection}/items/{id}`.
2. Builds an **RFC-6902 JSON Patch** via `ESGSTACItem.add_aggregate("icechunk", url, site)`.
3. **PATCHes** `{--stac-api}/collections/{collection}/items/{id}` with
   `Content-Type: application/json-patch+json`.

**No-auth path:** `getTransactionClient` returns `EGITransactionClient` whenever
`stac_config.stac_client.redirect_uri` lacks `"globus"`. Because we pass
`--stac-api`, that client takes its anonymous branch (`self.auth = None`,
`verify=False`). Both the GET and the PATCH go to the single `--stac-api` URL.

**Why `:9010` and not the `:9050` transaction API.** The Playground's bundled
`esgf-transaction-api` (`transaction_api_east`, `:9050`) is **create-only** — it
implements `POST /{collection_id}/items` and pushes to Kafka; its
`PUT`/`DELETE`/PATCH routes are commented out
(`esgf-transaction-api/esgf_transaction_api/main.py`). `esgadd` needs a JSON-Patch
`PATCH /collections/{c}/items/{id}`, so we point it at the **stac-fastapi-es**
East node (`:9010`).

> ⚠️ **BLOCKER (verified 2026-06-09): this Playground image does not support
> PATCH.** The `ghcr.io/djspstfc/stac-fastapi-es:1.0` image used by ESGF-Playground
> exposes only the **base** transactions class
> (`…/ogcapi-features/extensions/transaction`): `POST` (create) and `PUT` (full
> replace) on items work, but `PATCH` returns **HTTP 405** (item endpoint `OPTIONS`
> → `allow: GET` only; the `…/transaction#patch` conformance class is absent). So
> esgadd runs correctly and emits the right JSON-Patch request, but the Playground
> rejects it — the reference cannot land here. (Production East reportedly does
> support PATCH; this is a gap in the local Playground build, not in esgadd or in
> this repo.) Workarounds: a stac-fastapi-es build with the PATCH addon, or
> emulate the patch with a `PUT` (full-item replace) — which bypasses the
> production tool and so isn't done here. **→ file against ESGF-Playground.**

The asset `add_aggregate` builds:

```json
{ "op": "add", "path": "/assets/reference_file",
  "value": {
    "href": "<--agg-url>",
    "type": "application/icechunk",
    "role": ["data", "virtual"],
    "description": "TEST",
    "alternate:name": "<data_node>",
    "created": "...", "updated": "..."
  } }
```

If the Item **already** has a `reference_file` asset, esgadd instead targets
`/assets/reference_file/alternate/{site}` (icechunk alongside kerchunk). The
`seed` step strips the mirrored kerchunk `reference_file` so the demo lands at the
clean top-level path (see gap #4 below).

## esg-publisher packaging bugs (file upstream → `ESGF/esg-publisher`)

A clean `pip install` of `esgf-ng-v5.4a` (`src/python`) fails three times in a
row (all verified 2026-06-09):

1. **Version read at build time.** `esgcet/__init__.py` does
   `__version__ = str(version("esgcet"))` via `importlib.metadata`, and `setup.py`
   does `import esgcet; VERSION = esgcet.__version__`. During the build the package
   isn't installed yet → `PackageNotFoundError: esgcet`, so the wheel can't even be
   configured. (The source comment literally says *"or just hardcode temporarily"*.)
   → use a build backend that reads the version from git/file, or hardcode.
2. **Typo'd dependency.** `setup.py` `additional_requirements` lists
   **`"wcrp-cc-plugi"`** — no such PyPI project (`No matching distribution found`).
   Almost certainly meant `cc-plugin-wcrp` (the WCRP compliance-checker plugin).
   → fix the name (or make it optional — it's QA/QC, not needed for `esgadd`).
3. **Undeclared runtime dependency.** `esgcet/stac_converter.py` does
   `from esgvoc.apps.jsg import json_schema_generator`, but `esgvoc` is **not** in
   `install_requires` → `ModuleNotFoundError: esgvoc` the first time `esgadd` runs.
   → add `esgvoc` to `install_requires`.

The install recipe in [Prerequisites](#prerequisites) applies all three
workarounds.

## `esgadd` quirks / bugs found (file upstream → `ESGF/esg-publisher`)

These are in `src/python/esgcet/stac_converter.py :: ESGSTACItem.add_aggregate`
on `esgf-ng-v5.4a`:

1. **Media type is `application/icechunk`**, not the convention we agreed toward
   (`application/vnd.zarr+icechunk`, the type `xpystac` keys on). Hardcoded as
   `f"application/{aggtype}"`. → propose making the icechunk media type
   configurable / spec-aligned.
2. **`"role"` (singular)** is written instead of the STAC-standard **`"roles"`**.
   STAC readers (incl. our `catalog.files_from_stac_item`, which filters on
   `roles`) won't see it. Likely a typo bug.
3. **`"description": "TEST"`** is hardcoded.
4. **Nested-alternate `add` can fail RFC-6902.** When `reference_file` exists, the
   single op targets `/assets/reference_file/alternate/{site}`; if the existing
   asset has no `alternate` object, an RFC-6902 `add` to a missing parent path is
   invalid. esgadd does not first create `alternate: {}`. (We sidestep this in the
   demo by stripping `reference_file` on seed.)
5. **No `--agg-url` validation / store reachability check** — the href is recorded
   verbatim.
6. **`alternate` nesting is semantically wrong for distinct virtual stores.** When
   `reference_file` exists, esgadd adds the icechunk ref under
   `/assets/reference_file/alternate/<site>`. But the
   [alternate-assets extension](https://github.com/stac-extensions/alternate-assets)
   is only for *identical files* (same checksum/size). An Icechunk store and a
   kerchunk reference are different objects, so they should be **separate
   top-level assets** (see `references.py` + the multi-reference notebooks), not
   alternates. (alternate-assets *is* the right tool for discovering replicated
   *source data* across nodes — a future use.)
7. **Cannot add an arbitrarily-keyed separate asset.** esgadd only ever writes
   `reference_file` (or its `alternate`), so it can't add e.g.
   `reference_icechunk_s3` alongside `reference_icechunk_osn`. The multi-reference
   seeding notebook uses a direct STAC `PUT` instead. → propose an esgadd option to
   set the asset key / add distinct reference assets.

## Verify

`verify` reads the Item back, asserts the icechunk asset is present (top-level or
nested `alternate`), then opens the Icechunk store **directly**
(`Repository.open(..., authorize_virtual_chunk_access={prefix: None})`) because
`xpystac` `engine="stac"` cannot yet read anonymous-HTTP virtual chunks
(S3-only; see `ESGF-INTEL.md`). Reading straight from the catalog awaits the
xpystac HTTP PR.
