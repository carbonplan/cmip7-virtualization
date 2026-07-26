# cmip7-virtualization

This repository serves as central point for public discussions, examples, and code related to creating virtual zarr stores for CMIP7 data.


## Quick Start

```bash
uv sync
```


## Building a virtual store from local NetCDF files

`virtualize_from_urls` takes HTTP URLs, `s3://` URLs, **and** local paths — plain
strings or `pathlib.Path`, absolute or relative:

```python
from pathlib import Path
import icechunk as ic
from cmip7_virtualization import virtualize_from_urls
from cmip7_virtualization.storage import (
    authorize_prefixes_from_registry,
    local_url_prefix,
    vccs_from_registry,
)

paths = sorted(Path("data").glob("*.nc"))
vds, registry = virtualize_from_urls(paths)

# Local sources need an explicit directory prefix: icechunk refuses a bare
# "file:///" virtual-chunk container, and the registry cannot carry the path.
prefixes = [local_url_prefix(paths)]
config = ic.RepositoryConfig.default()
for vcc in vccs_from_registry(registry, local_prefixes=prefixes):
    config.set_virtual_chunk_container(vcc)

repo = ic.Repository.open_or_create(
    storage=ic.local_filesystem_storage("refs/my-store"),
    config=config,
    authorize_virtual_chunk_access=authorize_prefixes_from_registry(
        registry, local_prefixes=prefixes
    ),
)
session = repo.writable_session("main")
vds.vz.to_icechunk(session.store)
session.commit("virtual references")
repo.save_config()
```

Two things to know:

- **Every dimension coordinate must be loadable.** `combine_by_coords` needs a pandas
  index and a virtual `ManifestArray` has none, so a dimension coordinate left virtual
  aborts the build — even for a single file. `DEFAULT_LOADABLE_VARIABLES` covers the
  usual CMIP names plus NEMO's `olevel`; pass `loadable_variables=` for anything else.
- **`data_vars` defaults to `"minimal"`, not xarray's `"all"`.** With `"all"`, static
  grid variables get broadcast along the concat dimension — for IPSL/NEMO's
  `bounds_nav_lon` that turned 1.9 MB into 3.7 GB of duplicate chunk references.

A worked end-to-end example against real IPSL CMIP7 files, covering multi-file
concatenation, a 4-D field, and an `fx` field with no time dimension — plus
instructions for moving the sources to OSN — is in
[`notebooks/testing/local-ref-generation-ipsl.ipynb`](notebooks/testing/local-ref-generation-ipsl.ipynb).
It is committed with its outputs; re-execute it with:

```bash
uv run --with nbformat --with nbclient python -c "
import nbformat; from nbclient import NotebookClient
p = 'notebooks/testing/local-ref-generation-ipsl.ipynb'
nb = nbformat.read(p, as_version=4)
NotebookClient(nb, timeout=3600, kernel_name='python3').execute()
nbformat.write(nb, p)"
```

### Publishing to OSN

To make a store shareable the *source* NetCDF has to be reachable too — a local store's
manifests hold absolute paths from one machine. Upload the sources to the OSN bucket,
then rebuild with `s3_endpoint_url` set so both the reader and the virtual-chunk
container address the S3-compatible gateway:

```bash
export AWS_ACCESS_KEY_ID=$(op read "op://Work/z6baienaiyhiexztlbbonbeaka/Read-Write/Access_Key")
export AWS_SECRET_ACCESS_KEY=$(op read "op://Work/z6baienaiyhiexztlbbonbeaka/Read-Write/Secret_Access_Key")

aws s3 cp ./data/ s3://leap-pangeo-pipeline/cmip7-virtualization/source-data/<dataset>/ \
  --recursive --exclude "*" --include "*.nc" \
  --endpoint-url https://nyu1.osn.mghpcc.org
```

```python
from cmip7_virtualization import OSN_ENDPOINT_URL, osn_storage

vds, registry = virtualize_from_urls(
    urls, s3_endpoint_url=OSN_ENDPOINT_URL, s3_region="us-east-1"
)
vccs = vccs_from_registry(
    registry, s3_endpoint_url=OSN_ENDPOINT_URL, s3_region="us-east-1"
)
```

Credentials come from 1Password via the `op` CLI (desktop app unlocked, CLI integration
on). The bucket is public-read, so the virtual-chunk container stays anonymous; only
writing the store needs the keys.


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


## license

All the code in this repository is [MIT](https://choosealicense.com/licenses/mit/)-licensed, but we request that you please provide attribution if reusing any of our digital content (graphics, logo, articles, etc.).

## about us

CarbonPlan is a nonprofit organization that uses data and science for climate action. We aim to improve the transparency and scientific integrity of climate solutions with open data and tools. Find out more at [carbonplan.org](https://carbonplan.org/) or get in touch by [opening an issue](https://github.com/carbonplan/zarr-layer/issues/new) or [sending us an email](mailto:hello@carbonplan.org).
