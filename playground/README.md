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
| [`esg-playground.yaml`](./esg-playground.yaml) | The no-auth esgadd config (points the EGI client at the Playground). |

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
   do **not** add it to this project's `uv` env.
   ```bash
   python -m venv ~/.venvs/esg-publisher
   ~/.venvs/esg-publisher/bin/pip install \
     "git+https://github.com/ESGF/esg-publisher.git@esgf-ng-v5.4a#subdirectory=src/python"
   # then pass --esgadd ~/.venvs/esg-publisher/bin/esgadd  (or put it on PATH)
   ```

3. This project installed (`uv sync`) — the `build`/`verify` steps import
   `cmip7_virtualization`.

## Run

```bash
# Everything in order (seed 3 CEDA items, build+submit+verify the first):
uv run python playground/esgadd_playground.py all --n 3 \
    --esgadd ~/.venvs/esg-publisher/bin/esgadd

# Or step by step, targeting one Item:
uv run python playground/esgadd_playground.py seed --n 3
uv run python playground/esgadd_playground.py build  --item-id <ITEM_ID>
uv run python playground/esgadd_playground.py submit --item-id <ITEM_ID> --esgadd <PATH> --dry-run
uv run python playground/esgadd_playground.py verify --item-id <ITEM_ID>
```

`--dry-run` on `submit` prints the exact `esgadd` invocation without running it,
e.g.:

```
esgadd --stac-api http://localhost:9010 --dataset-id <ITEM_ID> \
       --agg icechunk --agg-url file:///.../refs/icechunk/<ITEM_ID> \
       --config playground/esg-playground.yaml --verbose
```

For a production-style host, build the store on OSN
(`s3://leap-pangeo-pipeline/...` via `https://nyu1.osn.mghpcc.org`) and pass that
public URL with `--agg-url`; the Playground records the href but does not
dereference it.

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
East node (`:9010`), which has the STAC **transactions extension** enabled. This
mirrors production (East transaction API supports PATCH) closely enough for the
submission proof.

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
