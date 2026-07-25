# cmip7-virtualization

This repository serves as central point for public discussions, examples, and code related to creating virtual zarr stores for CMIP7 data.


## Quick Start

```bash
uv sync
```


## Catalog discovery and monitoring

The ESGF STAC catalogs we build against are operated by other people and change
without notice. `cmip7_virtualization.catalog` wraps them:

```python
from cmip7_virtualization.catalog import STAC_BASES, collection_counts

collection_counts("https://api.stac.esgf.ceda.ac.uk")
# {'CMIP6': 390739, 'CMIP6Plus': 0, 'CMIP7': 0, 'CORDEX-CMIP6': 990, 'obs4REF': 0}
```

`STAC_BASES` holds the read/write endpoints for test and production on both the
East (CEDA) and West federations. `notebooks/catalog-discovery/catalog-check.ipynb`
prints the full matrix in one go.

Two server-side quirks are handled inside `collection_counts`, so callers do not
have to know about them:

- `/collections` can return **HTTP 500 as a whole** when a *single* collection
  document fails to serialise — West integration's `obs4ref` does exactly this.
  Discovery falls back to the root catalog's `child` links, which name each
  collection separately.
- West advertises lowercase collection ids (`cmip6plus`) but stores items under
  the canonical DRS case (`CMIP6Plus`), and `/search` is **case-sensitive**. The
  collection title is searched alongside the id, with a full-item tally as a
  last-resort reconciliation.

### Running the tests

```bash
uv run pytest                          # everything, including live network tests
uv run pytest -m "not live"            # offline only — no network
uv run pytest -m "live and not watch"  # live hard invariants only
uv run pytest -m watch                 # "did the catalogs change?" checks
```

`tests/test_live_catalogs.py` hits the real catalogs. It is split by marker:

- **`live`** — hard invariants. Every endpoint is a reachable STAC API, collection
  discovery works, per-collection counts reconcile against the unfiltered total,
  and the collections we depend on have not been emptied (floors sit ~10% below
  observed, so ordinary publication never trips them).
- **`watch`** — assertions that the catalogs are *still as last observed*. A
  failure here is a signal, not a defect: it means something moved and a decision
  is due. Currently watched: West integration's `/collections` 500, West's
  collection-id case sensitivity, the fact that **no catalog serves virtual-Zarr
  reference assets**, collections that are still empty (notably CMIP7 on East
  prod), and the retired `integration-testing` West host.

Each `watch` failure message says what to do about it. The most consequential are
CMIP7 appearing on East prod and reference assets reappearing anywhere.


## license

All the code in this repository is [MIT](https://choosealicense.com/licenses/mit/)-licensed, but we request that you please provide attribution if reusing any of our digital content (graphics, logo, articles, etc.).

## about us

CarbonPlan is a nonprofit organization that uses data and science for climate action. We aim to improve the transparency and scientific integrity of climate solutions with open data and tools. Find out more at [carbonplan.org](https://carbonplan.org/) or get in touch by [opening an issue](https://github.com/carbonplan/zarr-layer/issues/new) or [sending us an email](mailto:hello@carbonplan.org).
