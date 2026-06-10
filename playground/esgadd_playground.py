#!/usr/bin/env python
"""Track C — end-to-end ``esgadd`` demo against the local ESGF-Playground.

Proves we can attach an Icechunk virtual-reference asset to an existing STAC
Item using the *production* publisher tool (``esgadd`` from ESGF/esg-publisher),
talking to the local ESGF-Playground — no production auth required.

Pipeline (subcommands, or ``all`` to run them in order):

    seed    Mirror a CMIP6 collection + a few real Items from the CEDA STAC
            catalog into the Playground (East node, stac-fastapi-es on :9010).
            The kerchunk ``reference_file`` asset is stripped so esgadd can land
            the icechunk reference cleanly at ``/assets/reference_file``.
    build   Build one Icechunk store from a seeded Item's NetCDF URLs using the
            installed ``cmip7_virtualization`` package, written to local disk.
    submit  Invoke ``esgadd --agg icechunk`` (subprocess) to PATCH the icechunk
            reference asset onto the Item via the Playground transaction path.
    verify  Read the Item back, assert the icechunk asset is present, and open
            the Icechunk store directly (xpystac can't read anonymous-HTTP
            virtual chunks yet — see ESGF-INTEL.md).

``esgadd`` lives in ESGF/esg-publisher (esgf-ng branch). It has heavy deps that
conflict with the virtualizarr stack, so install it in a SEPARATE environment
and point at it with ``--esgadd /path/to/esgadd`` (default: ``esgadd`` on PATH).
See README.md.

Example
-------
    # 0. bring up the Playground (separate checkout):
    #    cd ~/Code/ESGF-Playground && docker compose up -d
    python playground/esgadd_playground.py all --n 3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
import icechunk as ic
import xarray as xr

from cmip7_virtualization.catalog import urls_from_stac_item
from cmip7_virtualization.storage import http_vccs_from_registry
from cmip7_virtualization.virtualize import virtualize_from_urls

# --- Playground topology (from ESGF-Playground/docker-compose.yml) -----------
# East node stac-fastapi-es == discovery API *and* (transactions ext) our PATCH
# target. The bundled esgf-transaction-api on :9050 is create-only (no PATCH).
LOCAL_STAC = "http://localhost:9010"
# Source discovery catalog we mirror Items FROM.
# NOTE (2026-06-09): the CEDA East *production* catalog is currently EMPTY —
# every collection returns numberMatched=0 — so we point at the live, populated
# ESGF-West discovery API instead. (api.stac.esgf-west.org has no DNS yet; the
# integration/data-challenge host below is the one that actually serves items.)
# SOURCE_STAC = "https://api.stac.esgf.ceda.ac.uk"  # CEDA East prod — empty as of 2026-06-09
SOURCE_STAC = "https://discovery.integration.esgf-west.org"  # ESGF-West discovery (live, populated)
COLLECTION = "CMIP6"

REPO_ROOT = Path(__file__).resolve().parent.parent
ICECHUNK_ROOT = REPO_ROOT / "refs" / "icechunk"
CONFIG_DEFAULT = Path(__file__).resolve().parent / "esg-playground.yaml"


# --- helpers -----------------------------------------------------------------
def _store_dir(item_id: str) -> Path:
    return ICECHUNK_ROOT / item_id


def check_playground() -> None:
    """Fail fast with an actionable message if the Playground isn't reachable."""
    try:
        r = httpx.get(LOCAL_STAC, timeout=5)
        r.raise_for_status()
    except httpx.ConnectError as exc:
        raise SystemExit(
            f"Cannot reach the Playground at {LOCAL_STAC}.\n"
            "Start it:  cd ~/Code/ESGF-Playground && docker compose up -d"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SystemExit(
            "Playground reachable but not ready — stac-fastapi-es needs ~30-60s "
            "after 'docker compose up -d'. Wait and retry."
        ) from exc
    print(f"✓ Playground up at {LOCAL_STAC} ({r.json().get('title', 'n/a')})")


def get_item(item_id: str) -> dict:
    r = httpx.get(f"{LOCAL_STAC}/collections/{COLLECTION}/items/{item_id}", timeout=30)
    r.raise_for_status()
    return r.json()


# --- seed --------------------------------------------------------------------
def seed(n: int) -> list[str]:
    """Mirror the CMIP6 collection + ``n`` Items from CEDA into the Playground.

    Strips any existing ``reference_file`` (kerchunk) asset so esgadd's
    JSON-Patch ``add`` lands cleanly at ``/assets/reference_file`` rather than
    nesting under ``/assets/reference_file/alternate/<site>`` (which would need
    the ``alternate`` object to already exist — an RFC-6902 / esgadd gap noted
    in the README).
    """
    check_playground()

    collection = httpx.get(f"{SOURCE_STAC}/collections/{COLLECTION}", timeout=30).json()
    for field in ("assets", "links"):  # stac-fastapi-es rejects unknown top-level fields
        collection.pop(field, None)
    r = httpx.post(f"{LOCAL_STAC}/collections", json=collection, timeout=30)
    if r.status_code == 409:
        print(f"Collection {COLLECTION} already exists — skipping")
    elif r.status_code in (200, 201):
        print(f"✓ Collection created: {collection['id']}")
    else:
        r.raise_for_status()

    items = httpx.get(
        f"{SOURCE_STAC}/collections/{COLLECTION}/items?limit=50", timeout=30
    ).json()["features"]
    # keep only items that carry real NetCDF data assets we can virtualize
    items = [i for i in items if urls_from_stac_item(i)][:n]

    seeded: list[str] = []
    for item in items:
        item["assets"].pop("reference_file", None)  # let esgadd create it fresh
        item.pop("links", None)
        r = httpx.put(
            f"{LOCAL_STAC}/collections/{COLLECTION}/items/{item['id']}",
            json=item,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            r.raise_for_status()
        seeded.append(item["id"])
        print(f"✓ Seeded {item['id']}")

    if not seeded:
        raise SystemExit("No seedable CEDA items found (none had NetCDF data assets).")
    print(f"\nSeeded {len(seeded)} item(s). First: {seeded[0]}")
    return seeded


# --- build -------------------------------------------------------------------
def build(item_id: str) -> Path:
    """Virtualize a seeded Item's NetCDF URLs into a local Icechunk store."""
    check_playground()
    item = get_item(item_id)
    urls = urls_from_stac_item(item)
    if not urls:
        raise SystemExit(f"Item {item_id} has no NetCDF data assets to virtualize.")
    print(f"Virtualizing {len(urls)} file(s) for {item_id}")

    vds, registry = virtualize_from_urls(urls)

    store_dir = _store_dir(item_id)
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    storage = ic.local_filesystem_storage(str(store_dir))

    config = ic.RepositoryConfig.default()
    for vcc in http_vccs_from_registry(registry=registry):
        config.set_virtual_chunk_container(vcc)

    repo = ic.Repository.open_or_create(storage=storage, config=config)
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store)
    snapshot = session.commit("track-c icechunk reference")
    repo.save_config()
    print(f"✓ Icechunk store written: {store_dir}  (snapshot {snapshot})")
    return store_dir


# --- submit ------------------------------------------------------------------
def submit(
    item_id: str,
    agg_url: Optional[str],
    config_path: Path,
    esgadd_bin: str,
    dry_run: bool,
) -> None:
    """Run ``esgadd --agg icechunk`` to PATCH the reference asset onto the Item."""
    if agg_url is None:
        # The Playground does not dereference the href; default to a file:// URL
        # of the local store. For production, host on OSN/CEDA and pass --agg-url.
        agg_url = _store_dir(item_id).resolve().as_uri()

    cmd = [
        esgadd_bin,
        "--stac-api", LOCAL_STAC,
        "--dataset-id", item_id,
        "--agg", "icechunk",
        "--agg-url", agg_url,
        "--config", str(config_path),
        "--verbose",
    ]
    print("Running:\n  " + " ".join(cmd))
    if dry_run:
        print("(dry-run — not executing)")
        return
    check_playground()
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"'{esgadd_bin}' not found. Install ESGF/esg-publisher (esgf-ng branch) "
            "in a separate env and pass --esgadd /path/to/esgadd. See README.md."
        ) from exc
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"esgadd exited {proc.returncode}")
    print("✓ esgadd completed")


# --- verify ------------------------------------------------------------------
def _find_icechunk_asset(item: dict, site: str) -> Optional[dict]:
    """Locate the icechunk asset esgadd added (top-level or nested alternate)."""
    ref = item.get("assets", {}).get("reference_file")
    if ref is None:
        return None
    if "icechunk" in (ref.get("type") or ""):
        return ref
    return (ref.get("alternate") or {}).get(site)


def verify(item_id: str, site: str) -> None:
    """Confirm the asset is in the catalog, then open the Icechunk store directly."""
    check_playground()
    item = get_item(item_id)
    asset = _find_icechunk_asset(item, site)
    if asset is None:
        raise SystemExit(
            f"No icechunk reference asset found on {item_id}. assets="
            f"{json.dumps(item.get('assets', {}), indent=2)}"
        )
    print("✓ Icechunk reference asset present in catalog:")
    print(json.dumps(asset, indent=2))

    store_dir = _store_dir(item_id)
    if not store_dir.exists():
        print(f"\n(store dir {store_dir} not local — skipping direct read)")
        return

    # Anonymous-HTTP virtual-chunk read (xpystac engine='stac' can't do this yet).
    # Derive the host prefixes to authorize anonymously from the item's data URLs.
    prefixes = {
        "/".join(url.split("/")[:3]) + "/": None for url in urls_from_stac_item(item)
    }
    repo = ic.Repository.open(
        storage=ic.local_filesystem_storage(str(store_dir)),
        authorize_virtual_chunk_access=prefixes,
    )
    ds = xr.open_zarr(repo.readonly_session("main").store)
    print(f"\n✓ Opened store via direct Icechunk read:\n{ds}")


# --- cli ---------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--item-id", default=None, help="Dataset/Item id to act on.")
    common.add_argument("--config", type=Path, default=CONFIG_DEFAULT, help="esgadd YAML config.")
    common.add_argument("--esgadd", default="esgadd", help="Path to the esgadd executable.")
    common.add_argument("--agg-url", default=None, help="Asset href (default: file:// of the local store).")
    common.add_argument("--site", default="esgf-playground.local", help="data_node / alternate:name (must match config).")
    common.add_argument("--dry-run", action="store_true", help="Print the esgadd command without running it.")

    sp = sub.add_parser("seed", parents=[common]); sp.add_argument("--n", type=int, default=3)
    sub.add_parser("build", parents=[common])
    sub.add_parser("submit", parents=[common])
    sub.add_parser("verify", parents=[common])
    sa = sub.add_parser("all", parents=[common]); sa.add_argument("--n", type=int, default=3)

    a = p.parse_args(argv)

    if a.cmd in ("seed", "all"):
        seeded = seed(a.n)
        a.item_id = a.item_id or seeded[0]
    if a.cmd == "seed":
        return

    if a.item_id is None:
        raise SystemExit("--item-id is required for build/submit/verify (or run 'seed'/'all').")

    if a.cmd in ("build", "all"):
        build(a.item_id)
    if a.cmd in ("submit", "all"):
        submit(a.item_id, a.agg_url, a.config, a.esgadd, a.dry_run)
    if a.cmd in ("verify", "all"):
        verify(a.item_id, a.site)


if __name__ == "__main__":
    main()
