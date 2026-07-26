# Track C — attaching virtual references with the production publisher

Attach a **kerchunk or Icechunk virtual-reference asset** to an existing STAC
Item using the **production publisher tool** (`esgadd`, from
[`ESGF/esg-publisher`](https://github.com/ESGF/esg-publisher)) — with **no
production auth**. This nails the submission mechanism the other tracks depend on.

**Part 1** runs esgadd against a local server that mirrors the live ESGF
transaction endpoints *including their JSON-Schema validation*, and is where the
findings are. **Part 2** is the earlier flow against the stock
[ESGF-Playground](https://github.com/ESGF/ESGF-Playground) image, kept because
its `build` step writes a real Icechunk store to OSN.

## Files

| File | What |
|---|---|
| [`agg_demo.py`](./agg_demo.py) | **Start here.** Runs `esgadd --agg kerchunk` **and** `--agg icechunk` against a *validating* local catalog and prints exactly what lands. |
| [`validating_stac_server.py`](./validating_stac_server.py) | Local STAC server mirroring the live ESGF transaction endpoints, **including JSON-Schema validation** (which the Playground image does not do). |
| [`refresh_schemas.py`](./refresh_schemas.py) | Re-pins the offline cache of the real published JSON Schemas. |
| [`fixtures/`](./fixtures/) | A real CMIP6 Item mirrored from CEDA East production, so the demo and tests run offline. |
| [`esgadd_playground.py`](./esgadd_playground.py) | Older flow against the stock ESGF-Playground: `seed → build → submit → verify`. `build` writes a real Icechunk store to OSN. |
| [`prepopulate.py`](./prepopulate.py) | Factored-out **seed**: query a source catalog + mirror suitable Items into the Playground. |
| [`esg-playground.yaml`](./esg-playground.yaml) | The no-auth esgadd config (points the EGI client at the Playground). |

---

# Part 1 — `esgadd --agg` against a *validating* catalog

## TL;DR of the result

Against a local server that applies the **real published JSON Schemas** the way
the production Transaction API does:

| | `--agg kerchunk` | `--agg icechunk` |
|---|---|---|
| PATCH accepted by a faithful mirror of production? | **yes, 202** | **yes, 202** (as a 1st agg) |
| Resulting Item still schema-valid? | **NO** — `'protocol' is a required property` | **NO** — same |
| Expressible in the schema at all once fixed? | yes (`protocol: kerchunk`) | **no** — `icechunk` is not in the `protocol` enum |
| 2nd aggregation (`alternate/{site}` nesting) | — | **BROKEN**, RFC-6902 cannot apply |

So `esgadd --agg` **turns a valid Item into an invalid one, and production does
not notice.** Details and exact errors below.

## Why not the ESGF-Playground image

`ghcr.io/djspstfc/stac-fastapi-es:1.0` cannot exercise this path:

* it returns **HTTP 405 on item PATCH** (base transactions class — POST/PUT only),
  so esgadd's request never reaches a handler; and
* it does **no schema validation at all**, so a green run against it says nothing
  about whether the real federation would accept the submission.

A validating proxy in front does not help — the origin still 405s, so the proxy
would have to rewrite PATCH into PUT, changing the semantics under test.
ESGF-Playground is also unmaintained (last commit 2024-08-16). Hence a
purpose-built stub that mirrors [`ESGF/stac-transaction-api`](https://github.com/ESGF/stac-transaction-api)
(`3b472fe`) instead: same validation entry points, same status codes, same
RFC 9457 error envelope, same schemas.

## Run it

```bash
uv sync
uv run python playground/agg_demo.py                       # uses the in-process replica
uv run python playground/agg_demo.py --esgadd /path/to/esgadd   # drives the real binary
```

Without `--esgadd` the demo uses `cmip7_virtualization.esgadd_ops`, a replica of
esgadd's request construction — verified byte-identical against the real binary.
No network required either way; the catalog records the `--agg-url` href but
never dereferences it.

Standalone server:

```bash
uv run python playground/validating_stac_server.py                # :9020, faithful
uv run python playground/validating_stac_server.py --mode strict
```

**`faithful`** reproduces production exactly. **`strict`** additionally applies
the patch and validates the *resulting* Item — the check production does not run.

## What actually happened (verified 2026-07-26, real `esgadd` from `main` @ `59bb778`)

### `--agg kerchunk`, first aggregation → **202 Accepted**

Request, verbatim, `PATCH /collections/CMIP6/items/{id}`,
`Content-Type: application/json-patch+json`:

```json
[{"op": "add", "path": "/assets/reference_file",
  "value": {"href": "https://nyu1.osn.mghpcc.org/.../kerchunk.json",
            "type": "application/kerchunk", "role": ["data", "virtual"],
            "description": "TEST", "alternate:name": "esgf-playground.local",
            "created": "2026-07-26T19:32:44.028714Z",
            "updated": "2026-07-26T19:32:44.028714Z"}}]
```

Response `202 Item queued for publication`. esgadd logs `INFO Queued for update`.
`--agg icechunk` is identical except `"type": "application/icechunk"`.

But the Item the catalog now holds **no longer validates**:

```
Item `CMIP6.ScenarioMIP.MOHC.UKESM1-0-LL.ssp585.r1i1p1f2.AERday.zg500.gn.v20190726`
failed validation against
`https://esgf.github.io/stac-transaction-api/cmip6/v2.0.0/schema.json`:
'protocol' is a required property
```

The Item was valid before the patch — the demo asserts that first — so the patch
is the sole cause.

### `--agg icechunk`, second aggregation → **422, patch inapplicable**

esgadd sees the `reference_file` it just created and switches path:

```json
[{"op": "add", "path": "/assets/reference_file/alternate/esgf-playground.local",
  "value": {"type": "application/icechunk", ...}}]
```

```
RFC-6902 patch could not be applied: member 'alternate' not found in
{'href': '...kerchunk.json', 'type': 'application/kerchunk', ...}
```

RFC-6902 `add` requires the parent object to exist. esgadd never emits an op to
create `alternate: {}`, and its own first-aggregation asset has no `alternate`
key — so **the two-aggregation case cannot work at all**. Pre-creating
`alternate` makes it apply (there is a test for that), which identifies the
missing operation precisely.

### `strict` mode → **400 on both**, Item stays valid

```
[strict mode] the patched Item is not schema-valid: ... 'protocol' is a required property
```

## Why production accepts an invalid asset

`stac-transaction-api/src/utils.py` `validate_patch` (L251-299) validates a
**synthetic partial Item** built from the operations — `{"assets": {...}}` — not
the stored Item and not the patched result. Three behaviours then compound:

1. `if error.validator in ["oneOf"]: continue` (L284) discards the root `oneOf`
   error. Every ESGF project schema nests its whole Item definition, assets
   included, inside a top-level `oneOf` — so **all** asset errors arrive as
   `.context` of that one discarded error.
2. `required` errors are diverted into `required_keys` (L287) rather than raised.
3. The rescue at L293, `required_keys & null_keys`, compares
   `json.dumps(error.validator_value)` (e.g. `'["protocol"]'`) against bare key
   names (`'protocol'`). The intersection is always empty — dead code.

Net: **PATCH validation is inert for asset content.** `validate_post` (L302),
used on item creation, raises on every error — hence the asymmetry.

## The `protocol` enum has no icechunk

Both `cmip6/v2.0.0` and `cmip7/v1.2.12` define, for **every** asset:

```json
"require_asset_fields": {"allOf": [{"required": ["created"]}, {"required": ["protocol"]}, ...]},
"asset_fields": {"properties": {"protocol": {"enum": [
    "http","https","globus","gridftp","kerchunk","netcdfsubset","opendap","wms","wps","s3"]}}}
```

So `protocol: "kerchunk"` validates; `protocol: "icechunk"` fails
(`'icechunk' is not one of [...]`), and so would `"zarr"`. Fixing esgadd to emit
`protocol` is necessary but **not sufficient for icechunk** — the published
schemas have no vocabulary for it. That is a schema change, not a publisher change.

Scoping note: the schemas constrain `protocol`, never `type`. `banana/split`
validates fine, as does an asset with no `type` at all. So
`application/icechunk` vs `application/vnd.zarr+icechunk` is a *convention*
argument; `protocol` is the hard gate.

## Upstream bugs found (all verified, all filable)

**`ESGF/esg-publisher`** (`main` @ `59bb778`, `src/python/esgcet/`):

1. `stac_converter.py` L42-61 `add_aggregate` emits **no `protocol`** → the
   resulting Item fails the project schema. *(the headline)*
2. `"role"` singular instead of STAC's `"roles"`. Readers filtering on `roles`
   cannot see the asset.
3. `"description": "TEST"` hardcoded.
4. Second aggregation targets `/assets/reference_file/alternate/{site}` without
   creating `alternate` first → **invalid RFC-6902**.
5. `alternate` nesting is semantically wrong anyway: [alternate-assets](https://github.com/stac-extensions/alternate-assets)
   is for the *identical file* by another route (same checksum/size). A kerchunk
   sidecar and an Icechunk store are different objects.
6. Only ever writes the key `reference_file` — cannot add
   `reference_icechunk_osn` alongside `reference_icechunk_s3`.
7. `--agg` value is unvalidated: `--agg banana` yields `application/banana`.
8. **`esgadd` always exits 1, even on success.** `esgstacaddrep.py` `run()` has
   no `return` after the patch, so it returns `None`; `main()` does
   `rc = run(); if not rc: exit(1)`. Observed: `INFO Queued for update` (202)
   followed by exit 1. Any automation wrapping esgadd sees every run as failed.
9. `add_replica` L83 calls `asset("created")` on a dict → `TypeError`.
10. Packaging, still broken on `main`: version read via `importlib.metadata` at
    build time (`PackageNotFoundError: esgcet`); typo'd dep `wcrp-cc-plugi`
    (should be `cc-plugin-wcrp`); undeclared runtime dep `esgvoc`.

**`ESGF/stac-transaction-api`** (`3b472fe`):

11. `validate_patch` PATCH validation is inert (the three compounding issues above).
12. **`client.py` L215 calls `validate_patch(item_id=…, item=…, extensions=…)`
    but `utils.py` L251 declares `(event_id, request_id, item_id, item,
    extensions)`** → `TypeError`, uncaught → **HTTP 500 on every PATCH**.
    Tracked upstream as issue #41.
13. `settings/__init__.py` pins CMIP7 default `cmip7/v1.2.1/schema.json`, which
    **404s** — never published.
14. Extension regex `v[0-9]\.[0-9]\.[0-9]` cannot match two-digit patch versions,
    so the two newest published CMIP7 schemas (v1.2.10, v1.2.12) are rejected as
    *unexpected* extensions.
15. `validate_extensions` L141 `if strict & len(missing_extensions) > 0:` parses
    as `(strict & len(missing)) > 0` — bitwise. With an **even** number of
    missing extensions this is 0 and strict mode silently passes.
16. `validate_patch` raises `STACValidationException()` with **no detail**, while
    `validate_post` builds a full message. A rejected PATCH tells the publisher
    nothing about what was wrong.

## Offline by construction

`refresh_schemas.py` caches the real published schemas (following transitive
`$ref`s — `alternate-assets` refs `schemas.stacspec.org`) into
`src/cmip7_virtualization/schema_cache/`. Validation never reaches the network;
`get_extension_validator` raises on an uncached URI rather than fetching. Verified
by running the suite behind a dead proxy.

```bash
uv run pytest tests/test_stac_validation.py -q     # 36 passed, offline
uv run python playground/refresh_schemas.py --check  # report drift vs upstream
```

Note that production does **not** cache: `utils.py` L193 does a live `httpx.get`
of every extension URL on every request, plus jsonschema's own remote `$ref`
fetch — so a `schemas.stacspec.org` outage would break ESGF publication.

---

# Part 2 — the original ESGF-Playground flow

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

> All three are **still present on `main` @ `59bb778`** (re-verified 2026-07-26).
> The working install recipe, applied to `main`, is:
> ```bash
> uv venv ~/.venvs/esg-publisher --python 3.12
> git clone --depth 1 https://github.com/ESGF/esg-publisher /tmp/esg-publisher
> cp -R /tmp/esg-publisher/src/python /tmp/esgadd-build
> printf '__version__ = "5.4.5"\nproject = "esgcet"\n' > /tmp/esgadd-build/esgcet/__init__.py
> sed -i '' '/wcrp-cc-plugi/d' /tmp/esgadd-build/setup.py
> VIRTUAL_ENV=~/.venvs/esg-publisher uv pip install /tmp/esgadd-build esgvoc
> ```

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

> Superseded by [Upstream bugs found](#upstream-bugs-found-all-verified-all-filable)
> in Part 1, which adds the schema-validation findings and re-verifies against
> `main`. Kept here for the original per-quirk reasoning.

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

---

# Part 3 — could the aggregation be added at *publish* time instead?

`esgadd` bolts the reference on afterwards. The better place is a normal
`esgpublish` run, which **POSTs a complete Item** — sidestepping the PATCH path,
its 405s/500s, and its inert validation entirely. Here is what the publisher
actually does today (`ESGF/esg-publisher` `main` @ `59bb778`,
paths relative to `src/python/esgcet/`).

## The publish path, end to end

`esgpublish` → `pub_internal:main` (`setup.py` L41) → `PubRunner.run()`
(`pub_internal.py` L31) → for CMIP6/CMIP7, `GenericPublisher.workflow()`
(`generic_netcdf.py` L198):

```
generic_netcdf.py:202  map_json_data = self.mapfile()
generic_netcdf.py:205  self.compliance_check(map_json_data)
generic_netcdf.py:209  self.kerchunk_generate()          <-- the only reference hook
generic_netcdf.py:213  self.extract_method(map_json_data)
generic_netcdf.py:218  out_json_data = self.mk_dataset(map_json_data)   -> mk_dataset.py:467 get_records()
generic_netcdf.py:227  rc = self.index_pub(out_json_data)               -> generic_pub.py:114
```

`index_pub` (`generic_pub.py` L124) builds the Item with
`ESGSTACConverter.convert2stac(dataset_records)` and POSTs it via
`EGITransactionClient.publish` (`stac_client.py` L239).

## Where assets are built — and the gap

`convert2stac` (`stac_converter.py` L117-335) is the **only** function that
assembles publish-time assets. It starts `assets = {}` (L125) and can emit
exactly two kinds:

* a single `globus` asset, `type: text/html` (L134-157);
* one asset per NetCDF file keyed by filename, `type: application/netcdf`,
  with `roles: ["data"]`, `alternate:name`, `file:size`, `file:checksum` (L160-191).

**There is no hook for a `reference_file` asset at publish time.** Every
occurrence of `reference_file` in the codebase is in PATCH-building helpers
(`add_aggregate` L42, `remove_aggregate` L25, `add_replica` L69) or in
`update_stac.py` L36 — and `update_stac.update_assets()` is never called from the
publish workflow (`generic_pub.py` L86 `update()` only calls `up.run()`).

The one publish-time kerchunk hook that does exist,
`GenericPublisher.kerchunk_generate()` (`generic_netcdf.py` L90-140), is a pure
side-effect writer: it returns `None` on every path, its output filename is
discarded at the call site (`generic_netcdf.py` L209, bare
`self.kerchunk_generate()`), and its failures are swallowed
(`except Exception … publog.info`). So **`esgpublish` can today write a kerchunk
reference next to the data and then completely forget about it.** It is
configured only via an undocumented YAML `kerchunk:` block (`args.py` L238); there
is no CLI flag. `esgpublish`'s full flag list (`args.py` L28-49) contains no
`--agg`, no `--kerchunk`, no `--reference`.

## Icechunk specifically: not possible today

`icechunk` appears in exactly three places repo-wide, all of them `esgadd` help
text or docs (`esgstacaddrep.py` L73, `docs/esgadd.rst` L6 and L39). There is no
icechunk import, dependency, or writer. `KerchunkGenerator`
(`kerchunk/kerchunk_generator.py` L18-208) has two backends, `kerchunk` and
`virtualizarr`, and **both** end at `serialize(refs, …)` (L185, L208) writing
kerchunk JSON/parquet — the virtualizarr path even round-trips back via
`refs = meta.vz.to_kerchunk()` (L183). So even `esgadd --agg icechunk` is a bare
metadata assertion: the publisher never creates a store, it only records a URL
you supply.

## The minimal change set

1. **`generic_netcdf.py` L90** — make `kerchunk_generate()` return the *published*
   URL of what it wrote (`-> str | None`), mapping local→public via the existing
   `data_roots` + `data_node` config. Stop swallowing the exception.
2. **`generic_netcdf.py` L209** — capture it onto the Dataset record. `get_records()`
   appends the Dataset dict last (`mk_dataset.py` L508) and `convert2stac` finds it
   by `doc.get("type") == "Dataset"`, so:
   ```python
   agg = self.kerchunk_generate()
   ...
   if agg:
       out_json_data[-1]["reference_file"] = {"url": agg, "type": "kerchunk"}
   ```
   Carrying a **dict** (url + aggregation type), not a bare string, is what lets
   the same plumbing serve icechunk later.
3. **`stac_converter.py`, inside `convert2stac` after L191** — emit the asset,
   mirroring `add_aggregate`'s shape but **fixed**:
   ```python
   ref = dataset_doc.get("reference_file")
   if ref:
       assets["reference_file"] = {
           "href": ref["url"],
           "type": f"application/{ref['type']}",
           "roles": ["data", "virtual"],     # plural, unlike add_aggregate
           "protocol": ref["type"],          # REQUIRED by the project schema
           "description": "Virtual reference aggregation",
           "alternate:name": dataset_doc.get("data_node"),
           "created": now, "updated": now,
       }
   ```
   `now` is already in scope (L118) and `alternate-assets` is already declared
   (L285).
4. **For icechunk, two further things are needed that no plumbing supplies:**
   * a **schema change** — `icechunk` must be added to the `protocol` enum in the
     project schemas on the `gh-pages` branch of `ESGF/stac-transaction-api`.
     Without it, no icechunk asset can ever validate. This is the blocking item.
   * a **producer** — either a config key that merely *records* a pre-built store
     URL (minimal; needs only steps 2-3), or a real `icechunk_backend()` on
     `KerchunkGenerator` alongside the existing two, which would add an `icechunk`
     dependency and object-store credential config (the class currently uses only
     `LocalStore`, L161).

## PR #303 "Kerchunk improvement" — open, not merged

Base `ESGF:esgf-ng-v5.4a`, author `minxu74`, approved by `sashakames` 2026-06-10,
but `mergeable: false` (conflicts). It implements exactly the shape above:
`kerchunk_generate()` gains `return f"{kerchunk_uri}/{output_file.name}"`,
`workflow()` stashes it as `out_json_data[-1]["reference_file"]`, and
`convert2stac` gains `item["assets"]["reference_file"] = dataset_doc["reference_file"]`.

Strong evidence the maintainers accept this design — but as written it is
**kerchunk-only and malformed**: the value assigned is a bare **string**, not an
asset object, so it produces `"assets": {"reference_file": "https://…json"}`,
which fails STAC validation outright and breaks every consumer that treats it as
a dict (`add_replica` L72, `remove_aggregate` L33, `add_aggregate` L54). It also
assigns *after* the `if not assets: return None` guard, and adds a stray
`from h5py._hl import dataset`.

**Upgrading #303's string to the asset dict in step 3, adding `protocol`, and
adding an aggregation-type field is a small, well-targeted contribution** that
would make publish-time attachment correct for kerchunk — and reachable for
icechunk once the `protocol` enum admits it.
