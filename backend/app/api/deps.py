from __future__ import annotations

from functools import lru_cache

from app.container import Container, build_container


@lru_cache(maxsize=1)
def get_container() -> Container:
    """Process-wide container. Tests override this dependency with their own."""
    return build_container()
