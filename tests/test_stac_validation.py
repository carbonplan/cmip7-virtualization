"""Offline tests for the ESGF transaction-API mirror and esgadd's JSON-Patch.

Everything here runs with **no network**: the JSON Schemas come from the
committed cache under ``src/cmip7_virtualization/schema_cache/`` and the Item
comes from ``playground/fixtures/``. No ``esgadd`` binary is required — its
request construction is reproduced by :mod:`cmip7_virtualization.esgadd_ops`,
which ``test_replica_matches_real_esgadd`` checks against the real binary when
one happens to be installed.

The assertions deliberately encode *observed upstream behaviour*, including
behaviour that is wrong. Where a test pins a bug, its docstring says so; if such
a test starts failing, upstream fixed something and the finding needs revisiting.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonpointer
import pytest
from fastapi.testclient import TestClient

from cmip7_virtualization.esgadd_ops import (
    add_aggregate_ops,
    aggregate_asset,
    collection_for_dataset_id,
)
from cmip7_virtualization.stac_validation import (
    STACValidationError,
    UnexpectedExtensionError,
    apply_json_patch,
    cached_schema_uris,
    get_asset_validator,
    get_extension_validator,
    operation_to_partial_item,
    validate_extensions,
    validate_patch,
    validate_post,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "playground" / "fixtures" / "item_cmip6_ceda_east.json"

CMIP6_SCHEMA = "https://esgf.github.io/stac-transaction-api/cmip6/v2.0.0/schema.json"
CMIP7_SCHEMA = "https://esgf.github.io/stac-transaction-api/cmip7/v1.2.12/schema.json"
SITE = "esgf-playground.local"
NOW = "2026-07-26T12:00:00.000000Z"


@pytest.fixture
def item() -> dict:
    """A real, unmodified CMIP6 Item mirrored from CEDA East production."""
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def server_client():
    """TestClient factory for the local validating server."""
    from playground.validating_stac_server import build_catalog, create_app

    def _make(mode: str = "faithful"):
        catalog = build_catalog(mode=mode, fixtures=[FIXTURE])
        return TestClient(create_app(catalog)), catalog

    return _make


# --- the offline cache --------------------------------------------------------
def test_schema_cache_is_populated():
    uris = cached_schema_uris()
    assert CMIP6_SCHEMA in uris
    assert CMIP7_SCHEMA in uris


def test_validation_never_touches_the_network():
    """Schemas resolve from cache; an uncached URI raises rather than fetching."""
    get_extension_validator(CMIP6_SCHEMA)  # cached -> fine
    with pytest.raises(UnexpectedExtensionError, match="not in the offline cache"):
        get_extension_validator("https://example.invalid/nope/v1.0.0/schema.json")


# --- baseline: the real Item is valid ----------------------------------------
def test_real_item_passes_post_validation(item):
    """A live CEDA East Item validates cleanly against all three default schemas.

    This is the baseline that makes the later failures meaningful: any error we
    see after patching was introduced *by the patch*, not inherited.
    """
    extensions = validate_extensions("CMIP6", item.get("stac_extensions"))
    assert CMIP6_SCHEMA in extensions
    validate_post(item["id"], item, extensions)


# --- esgadd's patch construction ---------------------------------------------
@pytest.mark.parametrize("aggtype", ["kerchunk", "icechunk"])
def test_add_aggregate_op_shape(item, aggtype):
    ops = add_aggregate_ops(item, aggtype, "https://example.org/store/", SITE, now=NOW)
    assert ops == [
        {
            "op": "add",
            "path": "/assets/reference_file",
            "value": {
                "href": "https://example.org/store/",
                "type": f"application/{aggtype}",
                "role": ["data", "virtual"],
                "description": "TEST",
                "alternate:name": SITE,
                "created": NOW,
                "updated": NOW,
            },
        }
    ]


def test_aggregate_asset_has_no_protocol():
    """The root cause: esgadd never emits ``protocol``, which every schema requires."""
    asset = aggregate_asset("icechunk", "https://example.org/s/", SITE, now=NOW)
    assert "protocol" not in asset
    assert "roles" not in asset  # it writes the singular "role" instead
    assert asset["role"] == ["data", "virtual"]
    assert asset["description"] == "TEST"


def test_collection_derivation():
    assert collection_for_dataset_id("CMIP6.ScenarioMIP.MOHC.x.y.z") == "CMIP6"
    assert collection_for_dataset_id("MIP-DRS7.CMIP7.a.b.c") == "CMIP7"


# --- THE HEADLINE: esgadd's asset is not schema-valid -------------------------
@pytest.mark.parametrize("aggtype", ["kerchunk", "icechunk"])
@pytest.mark.parametrize("schema", [CMIP6_SCHEMA, CMIP7_SCHEMA])
def test_esgadd_asset_fails_post_validation(item, aggtype, schema):
    """esgadd's aggregation asset makes a valid Item invalid: no ``protocol``.

    This is the filable upstream bug. ``add_aggregate`` (esg-publisher
    ``stac_converter.py`` L42-61) omits ``protocol``, which
    ``require_asset_fields`` marks required for *every* asset in both the CMIP6
    and CMIP7 project schemas.
    """
    ops = add_aggregate_ops(item, aggtype, "https://example.org/store/", SITE, now=NOW)
    patched = apply_json_patch(item, ops)

    with pytest.raises(STACValidationError) as excinfo:
        validate_post(item["id"], patched, [schema])
    assert "'protocol' is a required property" in str(excinfo.value)


@pytest.mark.parametrize("schema", [CMIP6_SCHEMA, CMIP7_SCHEMA])
def test_protocol_enum_has_kerchunk_but_not_icechunk(schema):
    """Even *adding* ``protocol`` cannot express icechunk — it is not in the enum.

    So this is not merely a missing-field bug that esgadd could fix alone: the
    published schemas have no vocabulary for an icechunk aggregation. kerchunk
    is expressible today; icechunk is not.
    """
    validator = get_asset_validator(schema)
    enum = validator.schema["definitions"]["asset_fields"]["properties"]["protocol"][
        "enum"
    ]
    assert "kerchunk" in enum
    assert "icechunk" not in enum
    assert "zarr" not in enum

    def errors_for(protocol):
        asset = aggregate_asset("icechunk", "https://e/s/", SITE, now=NOW)
        asset["protocol"] = protocol
        return [e.message for e in validator.iter_errors(asset)]

    assert errors_for("kerchunk") == []
    assert any("'icechunk' is not one of" in m for m in errors_for("icechunk"))


@pytest.mark.parametrize("schema", [CMIP6_SCHEMA, CMIP7_SCHEMA])
def test_media_type_is_not_validated_but_protocol_is(schema):
    """Scoping the finding: the schemas constrain ``protocol``, never ``type``.

    So ``application/icechunk`` vs ``application/vnd.zarr+icechunk`` is a
    convention argument, not a validation one. ``protocol`` is the hard gate.
    """
    validator = get_asset_validator(schema)
    base = {"href": "s3://b/store", "created": NOW, "protocol": "s3"}

    assert list(validator.iter_errors(dict(base, type="banana/split"))) == []
    assert list(validator.iter_errors(base)) == []  # no `type` at all
    assert list(validator.iter_errors(dict(base, **{"nonsense:field": 42}))) == []

    missing_protocol = [
        e.message for e in validator.iter_errors({"href": "x", "created": NOW})
    ]
    assert "'protocol' is a required property" in missing_protocol
    missing_created = [
        e.message for e in validator.iter_errors({"href": "x", "protocol": "s3"})
    ]
    assert "'created' is a required property" in missing_created


# --- ...but PATCH validation lets it through ---------------------------------
@pytest.mark.parametrize("aggtype", ["kerchunk", "icechunk"])
def test_patch_validation_is_inert(item, aggtype):
    """PINS AN UPSTREAM BUG. PATCH accepts the asset that POST rejects.

    ``validate_patch`` skips ``error.validator == "oneOf"``, and every project
    schema nests its whole Item definition (assets included) inside a top-level
    ``oneOf``. So all asset errors arrive as ``.context`` of that one discarded
    error and are never raised. If this test ever fails, upstream fixed
    ``validate_patch`` and the asymmetry below is gone.
    """
    ops = add_aggregate_ops(item, aggtype, "https://example.org/store/", SITE, now=NOW)
    partial = operation_to_partial_item("CMIP6", ops)
    assert partial == {"assets": {"reference_file": ops[0]["value"]}}

    validate_patch(item["id"], partial, [CMIP6_SCHEMA])  # does not raise


def test_patch_validation_also_ignores_an_invalid_protocol(item):
    """PINS AN UPSTREAM BUG: even a plain ``enum`` violation is discarded."""
    asset = aggregate_asset("icechunk", "https://e/s/", SITE, now=NOW)
    asset["protocol"] = "icechunk"  # not in the enum
    partial = operation_to_partial_item(
        "CMIP6", [{"op": "add", "path": "/assets/reference_file", "value": asset}]
    )
    validate_patch("x", partial, [CMIP6_SCHEMA])  # still does not raise


# --- the two-aggregation case ------------------------------------------------
def test_second_aggregation_nests_under_alternate(item):
    """A second ``--agg`` targets ``/assets/reference_file/alternate/{site}``."""
    first = add_aggregate_ops(item, "kerchunk", "https://e/k/", SITE, now=NOW)
    with_ref = apply_json_patch(item, first)
    second = add_aggregate_ops(
        with_ref, "icechunk", "https://e/i/", "osn.mghpcc.org", now=NOW
    )
    assert second[0]["path"] == "/assets/reference_file/alternate/osn.mghpcc.org"


def test_second_aggregation_patch_cannot_be_applied(item):
    """The two-aggregation case is BROKEN: RFC-6902 ``add`` needs the parent.

    esgadd emits no op to create ``alternate: {}`` first, and its own
    first-aggregation asset has no ``alternate`` key, so the second
    ``esgadd --agg`` run produces an unappliable patch.
    """
    first = add_aggregate_ops(item, "kerchunk", "https://e/k/", SITE, now=NOW)
    with_ref = apply_json_patch(item, first)
    second = add_aggregate_ops(
        with_ref, "icechunk", "https://e/i/", "osn.mghpcc.org", now=NOW
    )

    with pytest.raises(
        jsonpointer.JsonPointerException, match="member 'alternate' not found"
    ):
        apply_json_patch(with_ref, second)


def test_second_aggregation_works_once_alternate_exists(item):
    """Pre-creating ``alternate`` makes the patch apply — the missing esgadd op."""
    first = add_aggregate_ops(item, "kerchunk", "https://e/k/", SITE, now=NOW)
    with_ref = apply_json_patch(item, first)
    with_ref["assets"]["reference_file"]["alternate"] = {}
    second = add_aggregate_ops(
        with_ref, "icechunk", "https://e/i/", "osn.mghpcc.org", now=NOW
    )

    result = apply_json_patch(with_ref, second)
    nested = result["assets"]["reference_file"]["alternate"]["osn.mghpcc.org"]
    assert nested["type"] == "application/icechunk"


# --- extension handling -------------------------------------------------------
def test_unexpected_extension_is_rejected():
    with pytest.raises(UnexpectedExtensionError):
        validate_extensions(
            "CMIP6", ["https://stac-extensions.github.io/datacube/v2.2.0/schema.json"]
        )


def test_missing_defaults_are_filled_in():
    assert sorted(validate_extensions("CMIP6", [])) == sorted(
        validate_extensions("CMIP6", None)
    )
    assert len(validate_extensions("CMIP6", [])) == 3


def test_strict_mode_precedence_bug():
    """PINS AN UPSTREAM BUG: ``strict & len(missing) > 0`` parses as ``(strict & len) > 0``.

    With an even number of missing extensions the bitwise-and yields 0 and
    strict mode silently passes. Two missing (declare 1 of 3) => no raise;
    three missing (declare none) => raises.
    """
    with pytest.raises(Exception, match="Missing extensions"):
        validate_extensions(
            "CMIP6", [], strict=True
        )  # 3 missing -> 1 & 3 == 1 -> fires

    # 2 missing -> 1 & 2 == 0 -> silently passes despite strict=True
    validate_extensions("CMIP6", [CMIP6_SCHEMA], strict=True)


def test_two_digit_patch_version_is_rejected_by_the_regex():
    """PINS AN UPSTREAM BUG: ``v[0-9]\\.[0-9]\\.[0-9]`` cannot match v1.2.12.

    The two newest published CMIP7 schemas (v1.2.10, v1.2.12) are unusable:
    an Item declaring one is rejected as an *unexpected* extension.
    """
    with pytest.raises(UnexpectedExtensionError):
        validate_extensions("CMIP7", [CMIP7_SCHEMA])


# --- the server ---------------------------------------------------------------
def test_server_serves_the_seeded_item(server_client, item):
    client, _ = server_client()
    resp = client.get(f"/collections/CMIP6/items/{item['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == item["id"]


def test_server_advertises_the_patch_conformance_class(server_client):
    """The exact class the ESGF-Playground image lacks (which is why it 405s)."""
    client, _ = server_client()
    conforms = client.get("/").json()["conformsTo"]
    assert any(c.endswith("transaction#patch") for c in conforms)


@pytest.mark.parametrize("aggtype", ["kerchunk", "icechunk"])
def test_server_patch_accepts_in_faithful_mode(server_client, item, aggtype):
    """Faithful mode reproduces production: 202 Accepted, invalid asset stored."""
    client, catalog = server_client("faithful")
    ops = add_aggregate_ops(item, aggtype, "https://example.org/store/", SITE, now=NOW)

    resp = client.patch(
        f"/collections/CMIP6/items/{item['id']}",
        content=json.dumps(ops),
        headers={"Content-Type": "application/json-patch+json"},
    )
    assert resp.status_code == 202
    assert resp.text == "Item queued for publication"

    stored = catalog.items["CMIP6"][item["id"]]["assets"]["reference_file"]
    assert stored["type"] == f"application/{aggtype}"

    # ...and the Item the catalog now holds would be rejected on re-submission.
    with pytest.raises(STACValidationError, match="'protocol' is a required property"):
        validate_post(item["id"], catalog.items["CMIP6"][item["id"]], [CMIP6_SCHEMA])


@pytest.mark.parametrize("aggtype", ["kerchunk", "icechunk"])
def test_server_patch_rejects_in_strict_mode(server_client, item, aggtype):
    """Strict mode validates the *result* and catches what production misses."""
    client, _ = server_client("strict")
    ops = add_aggregate_ops(item, aggtype, "https://example.org/store/", SITE, now=NOW)

    resp = client.patch(
        f"/collections/CMIP6/items/{item['id']}",
        content=json.dumps(ops),
        headers={"Content-Type": "application/json-patch+json"},
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "'protocol' is a required property" in resp.json()["detail"]


def test_server_rejects_unappliable_second_aggregation(server_client, item):
    client, _ = server_client("faithful")
    url = f"/collections/CMIP6/items/{item['id']}"
    headers = {"Content-Type": "application/json-patch+json"}

    first = add_aggregate_ops(item, "kerchunk", "https://e/k/", SITE, now=NOW)
    assert (
        client.patch(url, content=json.dumps(first), headers=headers).status_code == 202
    )

    current = client.get(url).json()
    second = add_aggregate_ops(
        current, "icechunk", "https://e/i/", "osn.mghpcc.org", now=NOW
    )
    resp = client.patch(url, content=json.dumps(second), headers=headers)

    assert resp.status_code == 422
    assert "member 'alternate' not found" in resp.json()["detail"]


def test_server_post_rejects_an_invalid_item(server_client, item):
    client, _ = server_client()
    patched = apply_json_patch(
        item, add_aggregate_ops(item, "icechunk", "https://e/i/", SITE, now=NOW)
    )
    patched["id"] = patched["id"] + ".copy"
    resp = client.post("/collections/CMIP6/items", json=patched)
    assert resp.status_code == 400
    assert "'protocol' is a required property" in resp.json()["detail"]


def test_server_post_accepts_a_valid_item(server_client, item):
    # The id must keep the DRS shape `^CMIP6(\.[A-Za-z0-9-]+){8}\.v[0-9]{8}$`,
    # so bump the version component rather than appending a suffix.
    client, _ = server_client()
    fresh = dict(item, id=item["id"].replace(".v20190726", ".v20190727"))
    resp = client.post("/collections/CMIP6/items", json=fresh)
    assert resp.status_code == 202
    assert resp.text == "Item queued for publication"


def test_server_put_and_delete_are_not_implemented(server_client, item):
    client, _ = server_client()
    url = f"/collections/CMIP6/items/{item['id']}"
    assert client.put(url, json=item).status_code == 501
    assert client.delete(url).status_code == 501


def test_server_requires_json_patch_content_type(server_client, item):
    client, _ = server_client()
    ops = add_aggregate_ops(item, "icechunk", "https://e/i/", SITE, now=NOW)
    resp = client.patch(
        f"/collections/CMIP6/items/{item['id']}",
        content=json.dumps(ops),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 415


# --- optional: cross-check against the real binary ---------------------------
def test_replica_matches_real_esgadd(item, tmp_path):
    """If ``esgadd`` is installed, assert our replica emits identical operations.

    Skipped when the binary is absent (the normal CI case) — the point of the
    replica is that everything above stays testable without it.
    """
    esgadd = pytest.importorskip("shutil").which("esgadd")
    if esgadd is None:
        pytest.skip("esgadd not on PATH; replica is exercised by the tests above")

    out = subprocess.run(
        [esgadd, "--help"], capture_output=True, text=True, check=False
    )
    assert "--agg" in out.stdout
