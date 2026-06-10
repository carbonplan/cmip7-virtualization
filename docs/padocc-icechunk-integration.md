# padocc ↔ Icechunk: how the package works, how to test our changes, and where it can go wrong

> Goal: add an **Icechunk** output to CEDA's [`padocc`](https://github.com/cedadev/padocc)
> (branch `cmip7_beta`) so CEDA-published data gets virtual Icechunk references
> *automatically*. This doc explains padocc for someone who's never seen it,
> gives a **test plan** for our change, and flags the **icechunk-specific traps**
> (Daniel built the kerchunk path and is up front about not knowing icechunk —
> the working reference is our own `cmip7_virtualization` package, see
> [Good examples to copy](#good-examples-to-copy-from-this-repo)).

## 1. How padocc works (the map)

padocc turns a *group* of NetCDF files (one ESGF dataset) into a single
**cloud-optimised aggregation** — today **kerchunk** or **zarr**; we're adding
**icechunk**. It runs in **phases**, each a class with a `_run` hook, dispatched
by `cloud_format`.

```
                       GroupOperation (padocc/groups/group.py)
                        orchestrates many ProjectOperations
                                     │  .run(phase, mode=cloud_format)
                                     ▼
              ProjectOperation.run()  (padocc/core/project.py)
              sets cloud_format, calls the phase operator's _run()
                                     │
   phase_map (padocc/phases/__init__.py):
   ┌──────────────┬───────────────────────────────────────────────┐
   │ 'scan'       │ ScanOperation        — sample files, estimate  │
   │              │                        chunk count → pick format│
   │ 'compute'    │ {'kerchunk': KerchunkDS,                       │
   │              │  'zarr':     ZarrDS,                           │
   │              │  'CFA':      ComputeOperation,                 │
   │              │  'icechunk': IcechunkDS}   ← OUR TARGET (stub)  │
   │ 'validate'   │ ValidateOperation    — open output, compare    │
   └──────────────┴───────────────────────────────────────────────┘

   COMPUTE phase data flow (the part we change):

   source NetCDF files (allfiles)
        │  per-file kerchunk scan (KerchunkConverter.SingleHdf5ToZarr …)
        ▼
   per-file kerchunk JSON caches:  {cache_dir}/0.json, 1.json, …
        │
        ▼  padocc/phases/aggregate.py
   ┌─────────────────────────────────────────────────────────────────────┐
   │  virtualise(cache_dir, output_file, agg_dims, data_vars, nfiles, …)  │
   │    open_virtual_dataset(<each j.json>, KerchunkJSONParser)           │
   │    xr.combine_nested(...)               → combined_vds               │
   │    combined_vds.virtualize.to_kerchunk(output_file, 'json')         │
   │    # <-- a COMMENTED-OUT icechunk block sits right here (Daniel's)   │
   └─────────────────────────────────────────────────────────────────────┘
        │
        ▼
   output: kerchunk JSON / parquet  (a single file)         ← per-format
   then ValidateOperation opens it and compares against the source.
```

Key objects:
- **`ComputeOperation`** (`compute.py:160`) — base: CFA aggregation, dim/chunk
  scheme, file ordering. `KerchunkDS` / `ZarrDS` subclass it and implement a
  real `_run` (`ZarrDS._run → create_store`). **`IcechunkDS` (`compute.py:1770`)
  is a STUB** — its `_run` only runs CFA via `super()._run` and `return True`; it
  **builds no store**.
- **`aggregate.virtualise`** (`aggregate.py:46`) — the VirtualiZarr combine. This
  is where per-file kerchunk caches are turned into the final virtual dataset and
  serialised. The icechunk write belongs here (or in `IcechunkDS.create_store`).
- **filehandlers** (`core/filehandlers.py`) — `KerchunkFile`, `ZarrStore`: the
  output abstraction. **There is no Icechunk store handler yet** (see pitfall 3).
- **CLI / config** — already wired: `-C/--cloud_format icechunk` (or
  `mode='icechunk'`); the `scan` phase can auto-pick icechunk when the estimated
  chunk count is large (today it falls back to kerchunk-parquet).

## 2. Where our change goes

Two layers, do the lower one first:

1. **`aggregate.py`** — generalise `virtualise()` (or add `virtualise_icechunk`)
   so that, for `cloud_format == 'icechunk'`, the combined `vds` is written with
   `to_icechunk` into a **configurable Icechunk `Storage`** (local for CI;
   OSN / S3 for prod) with the **correct VirtualChunkContainers** (see pitfalls).
   Reuse our verified helpers (`vccs_from_registry`, `osn_storage`,
   `aws_s3_storage`).
2. **`compute.py::IcechunkDS`** — implement `create_store` mirroring `ZarrDS`
   (`_run → create_store`), delegating the build to (1). Make the storage target
   come from padocc project config, not hard-coded.

## 3. Test plan

padocc already has a pytest suite driven by a fixture working directory
(`padocc/tests/auto_testdata_dir`) and `GroupOperation.run(...)`. Mirror it.

### 3a. Unit / functional (in padocc, mirror existing tests)
- **`TestIcechunkCompute`** (copy `tests/test_zarr_comp.py`): run
  `process.run('compute', mode='icechunk', forceful=True, proj_code='1DAgg')`
  and assert `results['Success'] == 1`. Confirms the phase runs + writes a store.
- **`TestIcechunkValidate`** (copy `tests/test_zarr_valid.py` /
  `test_validate.py`): run `validate` on the icechunk output; assert it opens and
  matches the source values along the aggregated dimension.
- **Store-shape assertions**: the output is an Icechunk **repo dir** (not one
  file) — assert `config.yaml`, `snapshots/`, `manifests/`, `refs/`, `chunks/`
  exist (cf. the OSN stores we inspected). Catches "wrote a file, not a store".

### 3b. The test that actually matters — **read the virtual chunks back**
A store can *write* successfully yet be **unreadable** because the virtual-chunk
URLs or authorization are wrong (the #1 icechunk trap). So the decisive test
**opens the store and pulls a real chunk**:

```python
import icechunk as ic, xarray as xr
repo = ic.Repository.open(
    storage=<the test store storage>,
    authorize_virtual_chunk_access={"<source-host-prefix>/": None},  # None = anon HTTP
)
ds = xr.open_zarr(repo.readonly_session("main").store)
xr.testing.assert_allclose(ds[var].isel(time=0).load(), expected)   # forces a chunk fetch
```

This is exactly what `playground/esgadd_playground.py::verify` and the
`virtualization-ingestion-poc.ipynb` do. **`.load()` is the assertion** — it
dereferences a virtual chunk against the real source. If the container URL is a
local `file://` path (pitfall 1), this fails the moment the test runs anywhere
but Daniel's laptop.

### 3c. Integration matrix (small, but covers the real failure modes)
| source | files | storage target | what it catches |
|---|---|---|---|
| CEDA DAP HTTP (anon) | 1 | local fs | base path; anon-HTTP authorization |
| CEDA DAP HTTP (anon) | N (concat) | local fs | multi-file combine + agg dims |
| HTTP (anon) | 1 | **OSN** (keys) | hosting on object store, public-read href |
| S3 (anon, esgf-world) | 1 | local fs | S3 source: `s3_store(region, anonymous)` + `s3_anonymous_credentials()` |

Run the **read-back** (3b) for every cell. Keep failures that are genuinely
"not supported yet" and document them (we already do this for xpystac-HTTP).

### 3d. How to run
- In a **padocc checkout**: `pytest padocc/tests/test_compute.py` etc. (uses the
  fixture data dir). Add our new test modules next to them.
- **Cross-check against our reference**: build the *same* dataset with
  `cmip7_virtualization.virtualize_from_urls` + our `build()` and assert the two
  stores open to **equal** datasets. If padocc's output diverges, the bug is in
  the padocc integration, not in icechunk.

## 4. Where it could go wrong (icechunk traps — Daniel hasn't used icechunk)

Grounded in the **actual commented-out block** in `aggregate.py`
(the `# TESTING` lines). Each is a real, easy-to-make icechunk mistake:

1. **Virtual-chunk container URLs must be the PUBLISHED data URLs, not local
   `file://`.** The sketch builds `containers[f'file://{file}']` with
   `local_filesystem_storage(uri)`. A virtual chunk records *where the bytes
   live*; if that's a local path, the store is unreadable once published (and in
   any test not on that machine). It must be the **http(s)/s3 host the data is
   served from** (`https://dap.ceda.ac.uk/…` or `s3://…`). → our
   `vccs_from_registry()` derives `scheme://netloc/` from the *source* registry —
   copy that.
2. **One container per source HOST, not per file.** The sketch makes a container
   for every file URL. Containers key on a **URL prefix** (`scheme://netloc/`);
   you want one per host, covering all files under it. Per-file containers are
   wrong granularity and won't match the chunk manifests.
3. **`set_virtual_chunk_container` takes ONE container per call, not a dict.** The
   sketch does `config.set_virtual_chunk_container(containers)` with a dict —
   wrong signature. It's `for vcc in containers: config.set_virtual_chunk_container(vcc)`.
4. **Read-side authorization is required and separate from write.** Writing the
   store does not make it readable. Opening needs
   `authorize_virtual_chunk_access={prefix: None}` (anonymous HTTP) or
   `{prefix: ic.s3_anonymous_credentials()}` (anonymous S3). Forgetting this →
   `open` works but `.load()` errors. (Our `authorize_prefixes_from_registry()`.)
5. **Output abstraction mismatch.** `virtualise(output_file=…)` is a *single
   file* path; an Icechunk store is an **object tree** behind a `Storage`
   (`local_filesystem_storage(path)` / `s3_storage(...)`). Don't write to one
   `output_file` — open_or_create a repo, `writable_session('main')`,
   `vds.vz.to_icechunk(session.store)`, `session.commit(...)`, `repo.save_config()`.
   padocc's `KerchunkFile`/`ZarrStore` filehandlers don't model this; an
   IcechunkStore handler (or a thin storage adapter) is needed.
6. **API drift: accessor + version.** The sketch uses
   `combined_vds.virtualize.to_icechunk` and `icechunk.Repository.create`. Current
   virtualizarr uses the **`.vz`** accessor (`combined_vds.vz.to_icechunk`), and
   you want **`open_or_create`** for idempotent re-runs. Pin icechunk/virtualizarr
   versions; both move fast. (We install virtualizarr from git HEAD — match it.)
7. **S3 source data needs region + anonymity, not a bare `http_store`.** For
   `s3://esgf-world` sources, the container store must be
   `ic.s3_store(region="us-east-2", anonymous=True)` — not `http_store()`. Mixed
   HTTP+S3 sources need a container per scheme. (Our `vccs_from_registry` already
   branches on scheme.)
8. **`IcechunkDS._run` currently builds nothing** — it must call a real
   `create_store`, not just CFA + `return True`. Easy to "pass" a test that only
   checks the phase exits 0; that's why 3b (read-back) is non-negotiable.
9. **`save_config()` + commit are mandatory.** Without `repo.save_config()` the
   virtual-chunk-container config isn't persisted, so a *fresh* `Repository.open`
   elsewhere can't resolve chunks. Without `commit` there's no snapshot to read.
10. **Chunk count / encoding.** Zarr-v3 + numcodecs emits warnings and some codecs
    aren't portable; very large datasets are the icechunk case (scan auto-switch
    >3M chunks). Validate on a small fixture first, then one large one.

## Good examples to copy (from this repo)

These are the **working** reference for every trap above:
- `src/cmip7_virtualization/virtualize.py` — `virtualize_from_urls` (HTTP **and**
  anon-S3 sources; builds the registry by host).
- `src/cmip7_virtualization/storage.py` — `vccs_from_registry` (one container per
  host, HTTP **and** S3), `osn_storage` / `aws_s3_storage`,
  `authorize_prefixes_from_registry` (read-side auth, `None` vs anon-S3 creds).
- `playground/esgadd_playground.py::build` — the full write path
  (open_or_create → writable_session → `vds.vz.to_icechunk` → commit →
  save_config) on OSN, with the S3 target alongside.
- `playground/esgadd_playground.py::verify` and
  `notebooks/virtualization-ingestion-poc.ipynb` — the **read-back** with
  `authorize_virtual_chunk_access` (the test that proves the store is real).
