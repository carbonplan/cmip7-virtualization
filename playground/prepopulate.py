"""Factored-out Playground prepopulation, driven by the existing OSN stores.

The Items that *match* our OSN Icechunk stores can't be pulled from a live STAC
catalog right now: the CEDA East production catalog is empty (2026-06-09) and the
ESGF-West discovery catalog has rotated to different data-challenge datasets —
neither serves our ``CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full…`` Items.

So we reconstruct minimal STAC Items straight from the **OSN store names**: each
store prefix *is* the ESGF dataset_id, and that DRS maps directly to the CEDA DAP
archive path where the source NetCDF files still live. The collection-level
metadata is mirrored from the live ESGF-West discovery catalog.

``dataset_id_to_source_urls`` is intentionally isolated — the exact source-URL
recovery approach is pending confirmation with ESGF (see ``plan.md`` Open
Questions). The default here (list the CEDA DAP archive directory) works today.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

import httpx
import obstore
from obstore.store import from_url

# --- OSN (where the existing Icechunk stores live) ---------------------------
OSN_BUCKET = "leap-pangeo-pipeline"
OSN_ENDPOINT = "https://nyu1.osn.mghpcc.org"
OSN_ROOT_PREFIX = "cmip7-virtualization"
# Public-read base for the store href esgadd records (no signing needed).
OSN_PUBLIC_BASE = f"{OSN_ENDPOINT}/{OSN_BUCKET}"

# --- Source NetCDF archive (CEDA DAP) ----------------------------------------
# DRS facets joined by "/" under this root give the archive directory, e.g.
# CMIP6.VolMIP.NERC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.ta.gn.v20230810
#   -> {DAP}/CMIP6/VolMIP/NERC/UKESM1-0-LL/volc-pinatubo-full/r9i1p1f2/day/ta/gn/v20230810/
DAP_ARCHIVE_BASE = "https://dap.ceda.ac.uk/badc/cmip6/data"
SOURCE_NODE = "ceda.ac.uk"

# Live source for collection-level metadata (CEDA East prod is empty; see module
# docstring). api.stac.esgf-west.org has no DNS yet — the integration host serves.
WEST_DISCOVERY = "https://discovery.integration.esgf-west.org"


# --- OSN discovery -----------------------------------------------------------
def osn_store(
    *,
    access_key_id: Optional[str] = None,
    secret_access_key: Optional[str] = None,
):
    """Anonymous-by-keys obstore handle on the OSN bucket.

    Keys default to the AWS env vars (``AWS_ACCESS_KEY_ID`` /
    ``AWS_SECRET_ACCESS_KEY``); callers that keep them in 1Password should
    ``op read`` them into the environment (or pass explicitly).
    """
    access_key_id = access_key_id or os.environ.get("AWS_ACCESS_KEY_ID")
    secret_access_key = secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (access_key_id and secret_access_key):
        raise RuntimeError(
            "OSN keys not found. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
            "(e.g. via `op read`) or pass them to osn_store()."
        )
    return from_url(
        f"s3://{OSN_BUCKET}",
        endpoint=OSN_ENDPOINT,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region="us-east-1",  # required even for non-AWS Ceph
        virtual_hosted_style_request=False,  # path-style for Ceph
    )


def list_osn_dataset_ids(store=None, root_prefix: str = OSN_ROOT_PREFIX) -> List[str]:
    """Return the dataset_ids of every Icechunk store under ``root_prefix``.

    Each immediate sub-prefix of ``root_prefix/`` is one store; its trailing path
    component is the ESGF dataset_id.
    """
    store = store if store is not None else osn_store()
    res = obstore.list_with_delimiter(store, prefix=f"{root_prefix}/")
    ids = []
    for cp in res["common_prefixes"]:
        ids.append(cp.rstrip("/").split("/")[-1])
    return sorted(ids)


def osn_store_href(dataset_id: str, root_prefix: str = OSN_ROOT_PREFIX) -> str:
    """Public-read URL of a dataset's OSN Icechunk store (for ``--agg-url``)."""
    return f"{OSN_PUBLIC_BASE}/{root_prefix}/{dataset_id}/"


# --- source-URL recovery (PENDING ESGF — keep isolated) ----------------------
def dataset_id_to_archive_url(dataset_id: str) -> str:
    """Map an ESGF dataset_id to its CEDA DAP archive directory URL."""
    facets = dataset_id.split(".")
    return f"{DAP_ARCHIVE_BASE}/{'/'.join(facets)}/"


_NC_HREF = re.compile(r'href="([^"]+\.nc)"')


def dataset_id_to_source_urls(dataset_id: str, *, timeout: float = 30.0) -> List[str]:
    """List the source NetCDF URLs for a dataset by scraping its DAP directory.

    NOTE: this is the placeholder approach pending an ESGF-confirmed method (the
    canonical Item would normally carry these hrefs). It works today because the
    CEDA DAP file archive is still up even though the STAC catalog is empty.
    """
    archive = dataset_id_to_archive_url(dataset_id)
    r = httpx.get(archive, timeout=timeout)
    r.raise_for_status()
    names = sorted(set(_NC_HREF.findall(r.text)))
    return [archive + n for n in names]


# --- Item construction -------------------------------------------------------
def minimal_item(dataset_id: str, urls: List[str], *, collection: str) -> dict:
    """Build a minimal STAC Item keyed by ``dataset_id`` with NetCDF data assets.

    No ``reference_file`` asset is added — esgadd creates that. A whole-globe
    geometry keeps stac-fastapi-es happy without per-dataset bounds.
    """
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": dataset_id,
        "collection": collection,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]],
        },
        "bbox": [-180, -90, 180, 90],
        "properties": {"datetime": None, "start_datetime": None, "end_datetime": None},
        "assets": {
            f"data{i:04d}": {
                "href": u,
                "type": "application/netcdf",
                "roles": ["data"],
                "alternate:name": SOURCE_NODE,
            }
            for i, u in enumerate(urls)
        },
    }


# --- Playground seeding ------------------------------------------------------
def ensure_collection(
    stac_url: str,
    collection: str = "CMIP6",
    *,
    source_stac: str = WEST_DISCOVERY,
    timeout: float = 30.0,
) -> None:
    """Create ``collection`` in the Playground, mirroring metadata from West.

    The West collection id is lowercase (``cmip6``); we rewrite it to
    ``collection`` so it matches the dataset_id project prefix esgadd derives.
    stac-fastapi-es rejects unknown top-level fields, so ``assets``/``links`` are
    stripped. Idempotent: an existing collection (409) is left as-is.
    """
    src = httpx.get(f"{source_stac}/collections/{collection.lower()}", timeout=timeout).json()
    src["id"] = collection
    for field in ("assets", "links", "item_assets"):
        src.pop(field, None)
    r = httpx.post(f"{stac_url}/collections", json=src, timeout=timeout)
    if r.status_code == 409:
        print(f"Collection {collection} already exists — skipping")
    elif r.status_code in (200, 201):
        print(f"✓ Collection created: {collection}")
    else:
        r.raise_for_status()


def prepopulate(
    stac_url: str,
    dataset_ids: Optional[List[str]] = None,
    *,
    collection: str = "CMIP6",
    ensure_coll: bool = True,
    timeout: float = 30.0,
) -> List[str]:
    """Seed the Playground with matching Items for the OSN stores.

    ``dataset_ids`` defaults to **every** store under the OSN root prefix. For
    each, a minimal Item (with CEDA DAP data assets) is PUT into ``stac_url``.
    Returns the list of seeded dataset_ids.
    """
    if dataset_ids is None:
        dataset_ids = list_osn_dataset_ids()
    if not dataset_ids:
        raise RuntimeError("No OSN stores found to prepopulate.")

    if ensure_coll:
        ensure_collection(stac_url, collection, timeout=timeout)

    seeded: List[str] = []
    for ds_id in dataset_ids:
        urls = dataset_id_to_source_urls(ds_id)
        item = minimal_item(ds_id, urls, collection=collection)
        r = httpx.put(
            f"{stac_url}/collections/{collection}/items/{ds_id}",
            json=item,
            timeout=timeout,
        )
        if r.status_code not in (200, 201):
            r.raise_for_status()
        seeded.append(ds_id)
        print(f"✓ Seeded {ds_id}  ({len(urls)} data asset(s))")
    return seeded


if __name__ == "__main__":
    # Quick standalone check: list the OSN stores we'd seed.
    for ds_id in list_osn_dataset_ids():
        print(ds_id, "->", osn_store_href(ds_id))
