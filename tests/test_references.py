"""Unit tests for the reference asset model + selection policy."""

import pytest

from cmip7_virtualization.references import (
    MEDIA_TYPES,
    build_reference_asset,
    is_reference_asset,
    reference_asset_key,
    select_reference,
)


def test_asset_key():
    assert reference_asset_key("icechunk", "s3") == "reference_icechunk_s3"
    assert reference_asset_key("kerchunk", "osn") == "reference_kerchunk_osn"


def test_build_asset_icechunk_s3():
    a = build_reference_asset(
        "icechunk", "s3", "s3://bucket/key/", source_node="esgf-world",
        region="us-east-1", anonymous=True,
    )
    assert a["type"] == MEDIA_TYPES["icechunk"] == "application/vnd.zarr+icechunk"
    assert a["roles"] == ["virtual", "data"]
    assert a["cmip7:engine"] == "icechunk"
    assert a["cmip7:storage"] == "s3"
    assert a["cmip7:source_node"] == "esgf-world"
    assert a["xarray:storage_options"] == {"region": "us-east-1", "anonymous": True}


def test_build_asset_unknown_engine():
    with pytest.raises(ValueError):
        build_reference_asset("zarr", "s3", "s3://b/k", source_node="x")


def test_build_asset_no_storage_options_when_unset():
    a = build_reference_asset("icechunk", "osn", "https://osn/x", source_node="ceda")
    assert "xarray:storage_options" not in a


def _assets():
    return {
        "data0000": {"href": "https://dap.ceda.ac.uk/a.nc", "type": "application/netcdf", "roles": ["data"]},
        "reference_file": build_reference_asset("kerchunk", "http", "https://dap.ceda.ac.uk/a.json", source_node="ceda"),
        "reference_icechunk_osn": build_reference_asset("icechunk", "osn", "https://nyu1.osn.mghpcc.org/b/k", source_node="ceda"),
        "reference_icechunk_s3": build_reference_asset("icechunk", "s3", "s3://bucket/k", source_node="ceda"),
    }


def test_is_reference_asset():
    a = _assets()
    assert not is_reference_asset(a["data0000"])
    assert is_reference_asset(a["reference_file"])
    assert is_reference_asset(a["reference_icechunk_s3"])


def test_select_prefers_icechunk_then_s3():
    key, _ = select_reference(_assets())
    assert key == "reference_icechunk_s3"


def test_select_prefers_osn_when_s3_absent():
    a = _assets()
    del a["reference_icechunk_s3"]
    key, _ = select_reference(a)
    assert key == "reference_icechunk_osn"


def test_select_storage_preference_override():
    key, _ = select_reference(_assets(), prefer_storage=("osn", "s3", "http"))
    assert key == "reference_icechunk_osn"


def test_select_engine_preference_override():
    # prefer kerchunk -> the CEDA reference_file wins despite icechunk options
    key, _ = select_reference(_assets(), prefer_engine=("kerchunk", "icechunk"))
    assert key == "reference_file"


def test_select_ignores_data_assets():
    only_data = {"data0000": {"href": "x.nc", "type": "application/netcdf", "roles": ["data"]}}
    with pytest.raises(ValueError):
        select_reference(only_data)


def test_select_classifies_by_media_type_without_props():
    # assets lacking cmip7:* props still classify via media_type + href
    assets = {
        "k": {"href": "https://dap.ceda.ac.uk/a.json", "type": MEDIA_TYPES["kerchunk"], "roles": ["virtual"]},
        "i": {"href": "s3://bucket/k", "type": MEDIA_TYPES["icechunk"], "roles": ["virtual"]},
    }
    key, _ = select_reference(assets)
    assert key == "i"
