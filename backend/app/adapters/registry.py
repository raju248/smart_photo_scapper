from __future__ import annotations

from .generic import GenericAlbumAdapter


ADAPTERS = [
    # Put site-specific adapters above GenericAlbumAdapter.
    GenericAlbumAdapter,
]


def get_adapter(album_url: str):
    for adapter_cls in ADAPTERS:
        if adapter_cls.matches(album_url):
            return adapter_cls()
    return GenericAlbumAdapter()
