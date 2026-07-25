"""Live read tests against Icechunk stores published by data centres.

These hit the network on purpose. They are the regression guard for the whole
point of this project: a store somebody else built and hosts must stay openable
and readable by us, with chunks resolving back to the original NetCDF on their
data node. A unit test with a local fixture cannot tell us that.

They are marked ``live`` and run by default. Skip them with::

    uv run pytest -m "not live"

A failure here means one of: the host is down, the store was rebuilt or moved,
the virtual-chunk source (dap.ceda.ac.uk) changed, or an icechunk upgrade broke
the read path. The assertions are written to tell those apart.
"""

import numpy as np
import pytest
import xarray as xr

from cmip7_virtualization.storage import open_http_repository

# Built by CEDA (daniel.westwood) from CMIP6 VolMIP data on dap.ceda.ac.uk, then
# rclone'd to a JASMIN group workspace and served as plain static files over HTTPS.
# Announced in ESGF #arco, 2026-06-19.
JASMIN_STORE = "https://gws-access.jasmin.ac.uk/public/eds_ai/test-icechunk"
CEDA_DAP = "https://dap.ceda.ac.uk/"

# Source dataset:
# CMIP6.VolMIP.MOHC.UKESM1-0-LL.volc-pinatubo-full.r9i1p1f2.day.zg.gn
EXPECTED_SHAPE = (1110, 8, 144, 192)  # time, plev, lat, lon

# Verified 2026-07-25 against both the direct HTTPS read and an independent local
# mirror of the store. Pinning them detects silent corruption of the chunk
# manifest — a store that opens and returns *wrong* numbers is the failure mode
# that structural assertions miss.
EXPECTED_MEAN_T0_P0 = 143.07415771484375  # zg[0, 0], plev=100000 Pa
EXPECTED_MEAN_T500_P3 = 5479.69140625  # zg[500:505, 3], plev=50000 Pa

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def jasmin_repo():
    """Open the JASMIN-hosted store once for the module."""
    return open_http_repository(JASMIN_STORE, virtual_prefixes=[CEDA_DAP])


@pytest.fixture(scope="module")
def jasmin_ds(jasmin_repo):
    session = jasmin_repo.readonly_session("main")
    return xr.open_zarr(session.store, consolidated=False, zarr_format=3)


def test_http_storage_available():
    """icechunk >= 2.0 — 1.x has ``http_store`` but no ``http_storage`` backend.

    Checked separately so a version regression reports as itself rather than as a
    confusing ``AttributeError`` inside every other test.
    """
    import icechunk

    assert hasattr(icechunk, "http_storage"), (
        f"icechunk {icechunk.__version__} has no http_storage; need >= 2.0"
    )


def test_repository_opens_over_https(jasmin_repo):
    """The store is reachable and its commit history is intact."""
    history = list(jasmin_repo.ancestry(branch="main"))
    assert len(history) >= 2
    assert history[-1].message == "Repository initialized"
    assert all(s.id for s in history)


def test_dataset_structure(jasmin_ds):
    """Metadata reads (store-side) without touching the source data node."""
    assert "zg" in jasmin_ds.data_vars
    assert jasmin_ds.zg.shape == EXPECTED_SHAPE
    assert jasmin_ds.zg.dtype == np.float32
    assert jasmin_ds.zg.dims == ("time", "plev", "lat", "lon")
    assert jasmin_ds.attrs["variable_id"] == "zg"
    assert jasmin_ds.attrs["activity_id"] == "VolMIP"
    # Coordinates are virtual too — reading them already crosses to dap.ceda.ac.uk.
    assert jasmin_ds.lat.min() < -89 and jasmin_ds.lat.max() > 89


def test_virtual_chunks_load(jasmin_ds):
    """Chunk data resolves from dap.ceda.ac.uk and the values are correct."""
    field = jasmin_ds.zg.isel(time=0, plev=0).load()

    assert field.shape == (144, 192)
    # zg on the 1000 hPa surface is NaN wherever the terrain sits above that
    # level, so ~40% of this field is legitimately missing. Asserting the NaNs
    # survive matters: it shows virtualization preserved the fill value rather
    # than silently substituting zeros.
    finite = np.isfinite(field)
    assert 0.5 < float(finite.mean()) < 0.8
    assert float(field.mean()) == pytest.approx(
        EXPECTED_MEAN_T0_P0, rel=1e-6
    )  # skips NaN


def test_virtual_chunks_load_across_manifest(jasmin_ds):
    """A second, distant region — the store spans several chunk manifests.

    Reading one slice only proves the first manifest resolves. This one sits far
    into the time axis and on a different pressure level, so it exercises a
    different manifest and a different source file.
    """
    field = jasmin_ds.zg.isel(time=slice(500, 505), plev=3).load()

    assert field.shape == (5, 144, 192)
    assert np.isfinite(field).all()
    assert float(field.mean()) == pytest.approx(EXPECTED_MEAN_T500_P3, rel=1e-6)
