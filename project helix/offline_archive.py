"""Local-only integrity utilities for private Helix capture archives."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone


def atomic_json(path: Path, value: object) -> None:
    """Replace a JSON checkpoint only after its bytes are durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def index_scenario(directory: Path) -> dict:
    """Index existing scenario files, preserving audit originals unchanged.

    Indexing does not imply a complete or authorized-to-share capture.
    """
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entries = []
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or path.is_symlink() or path == manifest_path or path.name.startswith('.pending-'):
            continue
        entries.append({'path': path.relative_to(directory).as_posix(),
                        'sha256': sha256(path), 'bytes': path.stat().st_size})
    manifest['assets'] = entries
    manifest['indexed_at'] = datetime.now(timezone.utc).isoformat()
    atomic_json(manifest_path, manifest)
    return manifest
