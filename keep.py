"""
Nothing the app records is ever deleted: what you discard, delete or what another device deletes
is moved aside into a folder next to it, where it stays (and can be moved back).

    rides/discarded/   Discard, Stop on a ride under a minute, a ride with nothing recorded
    rides/deleted/     rides deleted from the history
    workouts/deleted/  workouts deleted on the Workouts page
    <folder>/.removed/ files another device deleted (sync)

These folders are never listed as rides and never synced (cloud_sync syncs no subfolders).
Standard library only.
"""
from __future__ import annotations

import os
from datetime import datetime


def set_aside(path: str, folder: str) -> str | None:
    """Move path into folder (made if needed); a file of the same name already there keeps its
    place and this one gets a time stamp. Returns where it went, None if there was no file."""
    if not os.path.exists(path):
        return None
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(path))
    if os.path.exists(dest):
        root, ext = os.path.splitext(os.path.basename(path))
        dest = os.path.join(folder, f"{root}.{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}{ext}")
    os.replace(path, dest)
    return dest


def set_aside_all(paths: list, folder: str) -> list:
    """set_aside for each; the ones that existed, where they went."""
    return [d for d in (set_aside(p, folder) for p in paths) if d]
