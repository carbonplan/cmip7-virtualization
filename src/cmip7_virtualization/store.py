import icechunk as ic


def repo_exists(storage: ic.Storage) -> bool:
    """Return True if an icechunk repository already exists at *storage*."""
    try:
        ic.Repository.open(storage)
        return True
    except Exception:
        return False
