from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

import xarray as xr
from obspec_utils.readers import EagerStoreReader
from obspec_utils.registry import ObjectStoreRegistry
from obspec_utils.wrappers import CachingReadableStore
from obstore.store import from_url
from virtualizarr.manifests import ManifestStore
from virtualizarr.parsers import HDFParser
from virtualizarr.parsers.hdf.hdf import _construct_manifest_group
from virtualizarr.xarray import open_virtual_mfdataset

# Must exceed a single file size, or an object is evicted before it can be reused. The
# obspec-utils default (256 MiB) is smaller than a typical CMIP6 file.
# CHECK: We might have to tune this for very large files and depending on the machine where its running.
DEFAULT_CACHE_SIZE = 2 * 1024**3

#: Scheme used for local-filesystem sources. Every url handed to virtualizarr must
#: carry a scheme — ``obspec_utils.registry.get_url_key`` raises ``ValueError`` on a
#: bare path — so local files travel as ``file:///abs/path.nc``.
LOCAL_SCHEME = "file"

#: Variables loaded as real (non-virtual) arrays.
#:
#: This is not a nice-to-have. ``open_virtual_mfdataset`` always routes through
#: ``xarray.combine_by_coords``, which needs a pandas index for **every dimension
#: coordinate** — a 1-D variable named after its own dimension. A ManifestArray has no
#: index, so any dimension coordinate left virtual aborts the combine with
#: ``ValueError: Every dimension requires a corresponding 1D coordinate and index ...``.
#: That bites even for a *single* file, since the combine still runs.
#:
#: Names that are absent from the data are silently ignored, so the list can be a
#: superset. It covers the usual CMIP verticals plus the NEMO/IPSL ``olevel``. If a new
#: model trips the error above, add the coordinate named in the message here or pass
#: ``loadable_variables=`` explicitly.
DEFAULT_LOADABLE_VARIABLES = (
    "time",
    "lat",
    "lon",
    "olevel",  # NEMO/IPSL ocean depth
    "lev",
    "depth",
    "plev",
    "height",
    "sdepth",
)


class EagerHDFParser:
    """HDFParser, but reading through an EagerStoreReader."""

    def __call__(self, url, registry):
        store, path = registry.resolve(url)
        reader = EagerStoreReader(store, path)
        try:
            group = _construct_manifest_group(filepath=url, reader=reader)
        finally:
            reader.close()
        return ManifestStore(group, registry=registry)


def as_url(path: str | os.PathLike) -> str:
    """Normalize a local filesystem path to a ``file://`` URL; pass URLs through.

    ``ObjectStoreRegistry`` keys on ``(scheme, netloc)`` and refuses a url without a
    scheme, so ``/data/foo.nc`` or a ``pathlib.Path`` cannot be handed to
    ``open_virtual_mfdataset`` as-is. Relative paths and ``~`` are resolved against
    the current working directory, because the resulting url is baked verbatim into
    the chunk manifest and must still mean something when the store is reopened.
    """
    text = os.fspath(path)
    if urlparse(text).scheme:
        return text
    return Path(text).expanduser().resolve().as_uri()


def is_local(url: str) -> bool:
    """True if *url* names a local file (``file://`` scheme)."""
    return urlparse(url).scheme == LOCAL_SCHEME


def virtualize_from_urls(
    urls: Iterable[str | os.PathLike],
    parser=None,
    *,
    s3_region: str = "us-east-2",
    s3_endpoint_url: str | None = None,
    cache_size: int | None = DEFAULT_CACHE_SIZE,
    data_vars: str = "minimal",
    loadable_variables: Iterable[str] = DEFAULT_LOADABLE_VARIABLES,
) -> tuple[xr.Dataset, ObjectStoreRegistry]:
    """Virtualize a list of NetCDF/HDF5 files into an xarray virtual dataset.

    Accepts three kinds of source:

    - anonymous **HTTP** (CEDA Thredds),
    - anonymous **``s3://``** (esgf-world; ``skip_signature`` + ``s3_region``, default
      ``us-east-2``; pass ``s3_endpoint_url`` for an S3-compatible store such as OSN),
    - **local paths** — plain strings, ``pathlib.Path``, or explicit ``file://`` urls.
      Bare paths are normalized by :func:`as_url`.

    Returns the virtual dataset and the ObjectStoreRegistry so callers can configure
    icechunk VirtualChunkContainers (see
    :func:`cmip7_virtualization.storage.vccs_from_registry`).

    ``cache_size`` is the per-host byte budget for caching fetched objects, or ``None``
    to disable. It is ignored for local sources: a local seek costs nothing, so neither
    the read-through cache nor the eager whole-file read buys anything — and eagerly
    slurping a multi-GB NetCDF into RAM to read its headers is actively harmful.

    ``data_vars`` defaults to ``"minimal"``, **not** xarray's ``"all"``. With ``"all"``,
    every variable lacking the concat dimension is broadcast along it: IPSL/NEMO output
    carries the curvilinear cell-corner arrays ``bounds_nav_lon``/``bounds_nav_lat`` as
    ``(y, x, nvertex)`` *data variables* rather than coordinates, and concatenating two
    3-hourly files turned them into ``(1944, y, x, nvertex)`` — 8 GB of manifest
    entries, all pointing at the same handful of source chunks. ``"minimal"`` only
    concatenates variables that already have the dimension, and (with
    ``compat="override"``) takes the rest from the first file.

    ``loadable_variables`` must cover every dimension coordinate — see
    :data:`DEFAULT_LOADABLE_VARIABLES`.
    """
    urls = [as_url(u) for u in urls]
    all_local = all(is_local(u) for u in urls)

    if parser is None:
        # pass HDFParser() as argument to restore the non optimized version
        parser = HDFParser() if all_local else EagerHDFParser()

    def _store_for(bucket: str):
        """Anonymous store for one host, read-through cached so bytes arrive once."""
        if bucket.startswith(f"{LOCAL_SCHEME}://"):
            # Rooted at "/" so any absolute path resolves; no caching wrapper.
            return from_url(f"{LOCAL_SCHEME}://")
        if bucket.startswith("s3://"):
            extra = {}
            if s3_endpoint_url:
                # Ceph/MinIO-style S3-compatible endpoints need path-style addressing.
                extra = {
                    "endpoint": s3_endpoint_url,
                    "virtual_hosted_style_request": False,
                }
            store = from_url(bucket, skip_signature=True, region=s3_region, **extra)
        else:
            store = from_url(bucket)
        if cache_size:
            store = CachingReadableStore(store, max_size=cache_size)
        return store

    # scheme://netloc — for file:// urls the netloc is empty, so this yields "file://".
    buckets = {"/".join(url.split("/")[:3]) for url in urls}
    registry = ObjectStoreRegistry({bucket: _store_for(bucket) for bucket in buckets})

    try:
        vds = open_virtual_mfdataset(
            urls=list(urls),
            parser=parser,
            registry=registry,
            coords="minimal",
            compat="override",
            data_vars=data_vars,
            loadable_variables=list(loadable_variables),
        )
    except ValueError as err:
        if "has no corresponding index" not in str(err):
            raise
        raise ValueError(
            f"{err}\n\n"
            "That coordinate is a dimension coordinate left as a virtual ManifestArray, "
            "so combine_by_coords has no index to order on. Add its name to "
            "`loadable_variables` (currently "
            f"{sorted(loadable_variables)}) or to "
            "cmip7_virtualization.virtualize.DEFAULT_LOADABLE_VARIABLES."
        ) from err
    return vds, registry


__all__: list[str] = ["EagerHDFParser", "as_url", "is_local", "virtualize_from_urls"]
