from __future__ import annotations

from typing import List

import xarray as xr
from obstore.store import from_url
from virtualizarr.parsers import HDFParser
from virtualizarr.registry import ObjectStoreRegistry
from virtualizarr.xarray import open_virtual_mfdataset


def virtualize_from_urls(
    urls: List[str], parser=None, *, s3_region: str = "us-east-2"
) -> tuple[xr.Dataset, ObjectStoreRegistry]:
    """Virtualize a list of NetCDF/HDF5 URLs into an xarray virtual dataset.

    Handles both anonymous HTTP sources (CEDA Thredds) and anonymous ``s3://``
    sources (esgf-world; ``skip_signature`` + ``s3_region``, default ``us-east-2``).
    Returns the virtual dataset and the ObjectStoreRegistry so callers can
    configure icechunk VirtualChunkContainers from ``registry.map.keys()``.
    """
    if parser is None:
        parser = HDFParser()

    def _store_for(bucket: str):
        if bucket.startswith("s3://"):
            return from_url(bucket, skip_signature=True, region=s3_region)
        return from_url(bucket)

    buckets = set("/".join(url.split("/")[:3]) for url in urls)
    registry = ObjectStoreRegistry({bucket: _store_for(bucket) for bucket in buckets})

    vds = open_virtual_mfdataset(
        urls=list(urls),
        parser=parser,
        registry=registry,
        coords='minimal',
        compat='override',
        # loadable_variables=['time','lon','lat','something']# we can apparently add values that are not found as dimensions here. 
    )
    return vds, registry
