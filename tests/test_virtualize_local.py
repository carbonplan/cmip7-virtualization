"""Offline tests for virtualizing local NetCDF files into an Icechunk store.

Everything here runs against tiny synthetic NetCDF files written into ``tmp_path``,
so the whole module is offline and CI-safe. The shapes mirror the three cases in
``notebooks/reference-generation/local-ref-generation-ipsl.ipynb``: a multi-file
time concat, a single file, and an ``fx`` file with no time dimension.
"""

import os
from pathlib import Path

import icechunk as ic
import numpy as np
import pytest
import xarray as xr

from cmip7_virtualization.storage import (
    authorize_prefixes_from_registry,
    local_url_prefix,
    local_vcc,
    vccs_from_registry,
)
from cmip7_virtualization.virtualize import as_url, is_local, virtualize_from_urls

NY, NX, NV = 4, 5, 4

#: Combine kwargs matching what ``virtualize_from_urls`` passes, so the reference
#: ``open_mfdataset`` read is an apples-to-apples comparison (and stays quiet about
#: xarray's upcoming ``data_vars`` default change).
MFKW = {"data_vars": "minimal", "coords": "minimal", "compat": "override"}


def _write(path: Path, *, t0: int | None, nt: int = 3) -> Path:
    """Write a tiny NetCDF. ``t0=None`` produces an fx-style file with no time dim.

    Time-varying files also carry ``bounds_nav_lon``, a static ``(y, x, nvertex)``
    *data variable* — the IPSL/NEMO shape that ``data_vars="all"`` broadcasts.
    """
    bounds = np.arange(NY * NX * NV, dtype="f4").reshape(NY, NX, NV)
    if t0 is None:
        ds = xr.Dataset(
            {"areacello": (("y", "x"), np.arange(NY * NX, dtype="f4").reshape(NY, NX))}
        )
    else:
        data = np.arange(nt * NY * NX, dtype="f4").reshape(nt, NY, NX) + t0
        ds = xr.Dataset(
            {
                "tos": (("time", "y", "x"), data),
                "bounds_nav_lon": (("y", "x", "nvertex"), bounds),
            },
            coords={"time": np.arange(t0, t0 + nt, dtype="f8")},
        )
    ds.to_netcdf(path, engine="h5netcdf")
    ds.close()
    return path


def _write_with_level(path: Path, *, t0: int, nt: int = 3, nz: int = 2) -> Path:
    """A file with a second dimension coordinate (``olevel``), as in IPSL ``thetao``."""
    data = np.arange(nt * nz * NY * NX, dtype="f4").reshape(nt, nz, NY, NX) + t0
    ds = xr.Dataset(
        {"thetao": (("time", "olevel", "y", "x"), data)},
        coords={
            "time": np.arange(t0, t0 + nt, dtype="f8"),
            "olevel": np.arange(nz, dtype="f4"),
        },
    )
    ds.to_netcdf(path, engine="h5netcdf")
    ds.close()
    return path


@pytest.fixture
def parts(tmp_path):
    """Two time-contiguous NetCDF files."""
    return [_write(tmp_path / f"part{i}.nc", t0=3 * i) for i in range(2)]


@pytest.fixture
def fx_file(tmp_path):
    return _write(tmp_path / "areacello.nc", t0=None)


def _roundtrip(vds, registry, paths, repo_path) -> xr.Dataset:
    """Write *vds* to a local Icechunk repo, reopen it, return the read-back dataset."""
    prefixes = [local_url_prefix(paths)]
    config = ic.RepositoryConfig.default()
    for vcc in vccs_from_registry(registry, local_prefixes=prefixes):
        config.set_virtual_chunk_container(vcc)
    auth = authorize_prefixes_from_registry(registry, local_prefixes=prefixes)

    storage = ic.local_filesystem_storage(str(repo_path))
    repo = ic.Repository.create(
        storage=storage, config=config, authorize_virtual_chunk_access=auth
    )
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    session.commit("test")
    repo.save_config()

    reopened = ic.Repository.open(storage=storage, authorize_virtual_chunk_access=auth)
    return xr.open_zarr(
        reopened.readonly_session("main").store, consolidated=False, zarr_format=3
    )


# --- url normalization -------------------------------------------------------


def test_as_url_absolute_path(tmp_path):
    p = tmp_path / "a.nc"
    url = as_url(p)
    assert url.startswith("file:///")
    assert url.endswith("/a.nc")
    assert is_local(url)


def test_as_url_relative_path_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = as_url("sub/a.nc")
    # A relative url would be meaningless once baked into a chunk manifest.
    assert url == (tmp_path / "sub" / "a.nc").resolve().as_uri()


def test_as_url_passes_urls_through():
    assert as_url("s3://bucket/key.nc") == "s3://bucket/key.nc"
    assert as_url("https://host/key.nc") == "https://host/key.nc"
    assert as_url("file:///abs/key.nc") == "file:///abs/key.nc"
    assert not is_local("s3://bucket/key.nc")


def test_as_url_accepts_pathlike(tmp_path):
    assert as_url(os.fspath(tmp_path / "a.nc")) == as_url(tmp_path / "a.nc")


# --- local url prefix --------------------------------------------------------


def test_local_url_prefix_common_dir(tmp_path):
    prefix = local_url_prefix([tmp_path / "a.nc", tmp_path / "b.nc"])
    assert prefix == tmp_path.resolve().as_uri() + "/"


def test_local_url_prefix_accepts_file_urls(tmp_path):
    urls = [as_url(tmp_path / "a.nc"), as_url(tmp_path / "sub" / "b.nc")]
    assert local_url_prefix(urls) == tmp_path.resolve().as_uri() + "/"


def test_local_url_prefix_empty():
    with pytest.raises(ValueError):
        local_url_prefix([])


def test_icechunk_rejects_filesystem_root_prefix():
    """The reason ``local_url_prefix`` has to exist at all.

    Note the rejection lands on ``set_virtual_chunk_container``, not on the
    ``VirtualChunkContainer`` constructor — building one with ``file:///``
    succeeds and only blows up when it is registered.
    """
    config = ic.RepositoryConfig.default()
    with pytest.raises(ValueError, match="must include a path"):
        config.set_virtual_chunk_container(local_vcc("file:///"))


def test_vccs_from_registry_requires_local_prefixes(parts):
    _, registry = virtualize_from_urls(parts)
    with pytest.raises(ValueError, match="local_prefixes"):
        vccs_from_registry(registry)


# --- end-to-end round trips --------------------------------------------------


def test_multifile_concat_roundtrip(parts, tmp_path):
    vds, registry = virtualize_from_urls(parts)
    assert vds.sizes["time"] == 6

    actual = _roundtrip(vds, registry, parts, tmp_path / "repo")
    expected = xr.open_mfdataset(parts, engine="h5netcdf", combine="by_coords", **MFKW)

    np.testing.assert_array_equal(actual["tos"].values, expected["tos"].values)
    np.testing.assert_array_equal(actual["time"].values, expected["time"].values)


def test_single_file_roundtrip(parts, tmp_path):
    vds, registry = virtualize_from_urls(parts[:1])

    actual = _roundtrip(vds, registry, parts[:1], tmp_path / "repo")
    expected = xr.open_dataset(parts[0], engine="h5netcdf")

    np.testing.assert_array_equal(actual["tos"].values, expected["tos"].values)


def test_fx_no_time_roundtrip(fx_file, tmp_path):
    vds, registry = virtualize_from_urls([fx_file])
    assert "time" not in vds.dims

    actual = _roundtrip(vds, registry, [fx_file], tmp_path / "repo")
    expected = xr.open_dataset(fx_file, engine="h5netcdf")

    np.testing.assert_array_equal(
        actual["areacello"].values, expected["areacello"].values
    )


def test_bare_string_paths_accepted(parts, tmp_path):
    """A plain path string, not a file:// url — the ergonomic case."""
    vds, registry = virtualize_from_urls([str(p) for p in parts])
    actual = _roundtrip(vds, registry, parts, tmp_path / "repo")
    assert actual.sizes["time"] == 6


def test_manifest_points_at_source_files(parts):
    """Virtual references: chunks stay in the NetCDF, they are not copied."""
    vds, _ = virtualize_from_urls(parts)
    paths = {e["path"] for e in vds["tos"].data.manifest.dict().values()}
    assert paths == {as_url(p) for p in parts}


# --- the two IPSL/NEMO gotchas -----------------------------------------------


def test_static_data_var_is_not_broadcast_along_time(parts):
    """``data_vars="minimal"`` (our default) keeps static grid variables 3-D.

    IPSL/NEMO ships ``bounds_nav_lon`` as a ``(y, x, nvertex)`` *data variable*, not a
    coordinate. xarray's ``data_vars="all"`` default broadcasts it along the concat
    dimension, multiplying the manifest by the number of timesteps.
    """
    vds, _ = virtualize_from_urls(parts)
    assert vds["bounds_nav_lon"].dims == ("y", "x", "nvertex")

    inflated, _ = virtualize_from_urls(parts, data_vars="all")
    assert inflated["bounds_nav_lon"].dims == ("time", "y", "x", "nvertex")
    assert inflated["bounds_nav_lon"].sizes["time"] == 6
    # ...and the manifest grows accordingly, all entries aliasing the same chunks.
    assert len(inflated["bounds_nav_lon"].data.manifest.dict()) > len(
        vds["bounds_nav_lon"].data.manifest.dict()
    )


def test_vertical_dim_coord_roundtrip(tmp_path):
    """``olevel`` is in DEFAULT_LOADABLE_VARIABLES, so a 4-D ocean field just works."""
    paths = [_write_with_level(tmp_path / f"lev{i}.nc", t0=3 * i) for i in range(2)]
    vds, registry = virtualize_from_urls(paths)
    assert vds.sizes == {"time": 6, "olevel": 2, "y": NY, "x": NX}

    actual = _roundtrip(vds, registry, paths, tmp_path / "repo")
    expected = xr.open_mfdataset(paths, engine="h5netcdf", combine="by_coords", **MFKW)
    np.testing.assert_array_equal(actual["thetao"].values, expected["thetao"].values)


def test_unloadable_dim_coord_raises_actionable_error(tmp_path):
    """A dimension coordinate left virtual has no index; say so usefully."""
    paths = [_write_with_level(tmp_path / "lev0.nc", t0=0)]
    with pytest.raises(ValueError, match="loadable_variables"):
        virtualize_from_urls(paths, loadable_variables=["time"])
