"""Read immutable resources packaged alongside a Python Plugin entrypoint."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path


_MAX_RESOURCE_BYTES = 4 * 1024 * 1024


def _validate_name(name: str) -> str:
    value = str(name or "")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("Plugin resource name must be a relative path")
    return value


def _read_archive(path: Path, name: str) -> bytes | None:
    if not path.is_file() or not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as archive:
        try:
            info = archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > _MAX_RESOURCE_BYTES:
            raise ValueError("Plugin resource exceeds the size limit")
        return archive.read(info)


def load_resource_text(name: str, *, anchor: str | Path | None = None, encoding: str = "utf-8") -> str:
    """Load a read-only project resource in source and ``.pyz`` execution."""
    resource = _validate_name(name)
    candidates: list[Path] = []
    if anchor is not None:
        anchor_path = Path(anchor)
        source_path = anchor_path.parent / resource
        if source_path.is_file():
            if source_path.stat().st_size > _MAX_RESOURCE_BYTES:
                raise ValueError("Plugin resource exceeds the size limit")
            return source_path.read_text(encoding=encoding)
        candidates.append(anchor_path.parent)
    candidates.append(Path(sys.argv[0]))
    for candidate in candidates:
        data = _read_archive(candidate, resource)
        if data is not None:
            return data.decode(encoding)
    raise FileNotFoundError(resource)
