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

### Continuous integration

| Workflow | Trigger | What it runs |
| --- | --- | --- |
| `tests.yml` | every PR, push to `main`, manual | the whole suite, as two checks: **offline (no network)** and **live (hits real ESGF catalogs)** |
| `catalog-monitor.yml` | daily 06:00 UTC, manual | the whole suite, and files a GitHub issue when it fails |

The split in `tests.yml` is so the check name alone tells you what broke —
`offline` red is our code, `live` red is usually somebody else's catalog. Between
them they run every test in the repo.

`catalog-monitor.yml` is the part that catches change nobody asked about. On
failure it opens an issue labelled **`catalog-drift`** containing the failed test
names and a log tail, or comments on the existing open one rather than filing a
duplicate every morning. The label is created on first use.

Three things about the monitor worth knowing before you wonder why nothing
happened:

- `schedule` **only ever fires on the repository's default branch**. The timer
  starts when this lands on `main`, not when the branch is pushed.
- `workflow_dispatch` needs the workflow on the default branch too — dispatching
  it from a feature branch returns `HTTP 404: workflow catalog-monitor.yml not
  found on the default branch`. So there is no way to trigger the monitor before
  it merges.
- GitHub disables scheduled workflows in repositories with no activity for 60
  days, and re-enables them on the next push.

In practice that means the suite itself is proven on every PR by `tests.yml`,
which runs exactly the same tests; the only part that cannot be exercised until
this reaches `main` is the issue-filing step. Once merged: **Actions → Catalog
monitor → Run workflow**, or `gh workflow run catalog-monitor.yml`.


## license

All the code in this repository is [MIT](https://choosealicense.com/licenses/mit/)-licensed, but we request that you please provide attribution if reusing any of our digital content (graphics, logo, articles, etc.).

## about us

CarbonPlan is a nonprofit organization that uses data and science for climate action. We aim to improve the transparency and scientific integrity of climate solutions with open data and tools. Find out more at [carbonplan.org](https://carbonplan.org/) or get in touch by [opening an issue](https://github.com/carbonplan/zarr-layer/issues/new) or [sending us an email](mailto:hello@carbonplan.org).
