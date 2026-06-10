from typing import Dict, List


def files_from_stac_item(stac_item: Dict) -> Dict[str, str]:
    """Return {asset_id: href} for all data assets (excludes reference/kerchunk assets)."""
    return {
        aid: a["href"]
        for aid, a in stac_item["assets"].items()
        if "reference" not in a.get("roles", [])
    }


def urls_from_stac_item(stac_item: Dict) -> List[str]:
    """Return ordered list of NetCDF hrefs for a single STAC item."""
    return list(files_from_stac_item(stac_item).values())
