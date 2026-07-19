"""Repository-backed sticker catalog shared by discussion features.

Sticker binaries are trusted release assets, not user uploads.  The catalog is
derived from ``assets/stickers/*.webp`` so a missing/empty directory simply
produces an empty picker.  Callers receive resolved paths from this module and
must never turn a browser-supplied sticker id into a filesystem path directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


STICKER_ID_MAX_CHARS = 200
STICKER_DIRECTORY = Path(__file__).resolve().parents[1] / "assets" / "stickers"


@dataclass(frozen=True)
class Sticker:
    sticker_id: str
    label: str
    path: Path


def _label(sticker_id: str) -> str:
    readable = sticker_id.replace("_", " ").replace("-", " ").strip()
    return readable or sticker_id


@lru_cache(maxsize=1)
def _catalog() -> tuple[Sticker, ...]:
    if not STICKER_DIRECTORY.is_dir():
        return ()
    root = STICKER_DIRECTORY.resolve()
    items = []
    for candidate in STICKER_DIRECTORY.iterdir():
        if candidate.suffix.lower() != ".webp" or not candidate.is_file():
            continue
        sticker_id = candidate.stem
        if not sticker_id or len(sticker_id) > STICKER_ID_MAX_CHARS:
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        items.append(Sticker(sticker_id, _label(sticker_id), resolved))
    return tuple(sorted(items, key=lambda item: (item.label.casefold(), item.sticker_id)))


def list_stickers() -> list[Sticker]:
    return list(_catalog())


def get_sticker(sticker_id: str) -> Sticker | None:
    requested = str(sticker_id or "")
    return next((item for item in _catalog() if item.sticker_id == requested), None)


def clear_catalog_cache() -> None:
    """Refresh test/development discovery after repository assets change."""
    _catalog.cache_clear()
