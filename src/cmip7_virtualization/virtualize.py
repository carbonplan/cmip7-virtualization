from __future__ import annotations

from typing import List

import xarray as xr
from obstore.store import from_url
from obspec_utils.readers import EagerStoreReader
from obspec_utils.wrappers import CachingReadableStore
from virtualizarr.parsers import HDFParser
from virtualizarr.registry import ObjectStoreRegistry
from virtualizarr.xarray import open_virtual_mfdataset
from virtualizarr.manifests import ManifestStore
from virtualizarr.parsers.hdf.hdf import _construct_manifest_group

# Must exceed a single file size, or an object is evicted before it can be reused. The
# obspec-utils default (256 MiB) is smaller than a typical CMIP6 file. 
# CHECK: We might have to tune this for very large files and depending on the machine where its running.
DEFAULT_CACHE_SIZE = 2 * 1024**3


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




def virtualize_from_urls(
    urls: List[str],
    parser=None,
    *,
    s3_region: str = "us-east-2",
    cache_size: int | None = DEFAULT_CACHE_SIZE,
) -> tuple[xr.Dataset, ObjectStoreRegistry]:
    """Virtualize a list of NetCDF/HDF5 URLs into an xarray virtual dataset.

    Handles both anonymous HTTP sources (CEDA Thredds) and anonymous ``s3://``
    sources (esgf-world; ``skip_signature`` + ``s3_region``, default ``us-east-2``).
    Returns the virtual dataset and the ObjectStoreRegistry so callers can
    configure icechunk VirtualChunkContainers from ``registry.map.keys()``.

    ``cache_size`` is the per-host byte budget for caching fetched objects, or
    ``None`` to disable.
    """
    if parser is None:
        # pass HDFParser() as argument to restore the non optimized version
        parser = EagerHDFParser()

    def _store_for(bucket: str):
        """Anonymous store for one host, read-through cached so bytes arrive once."""
        if bucket.startswith("s3://"):
            store = from_url(bucket, skip_signature=True, region=s3_region)
        else:
            store = from_url(bucket)
        if cache_size:
            store = CachingReadableStore(store, max_size=cache_size)
        return store

    buckets = {"/".join(url.split("/")[:3]) for url in urls}
    registry = ObjectStoreRegistry({bucket: _store_for(bucket) for bucket in buckets})

    vds = open_virtual_mfdataset(
        urls=list(urls),
        parser=parser,
        registry=registry,
        coords='minimal',
        compat='override',
        loadable_variables=['time', 'lon', 'lat', 'something_that_i_never_expect']
        # we might want to add more variables here. We can apparently also include variables that might not be in the data dimensions! That will be handy for e.g. depth dimensions.
    )
    return vds, registry
