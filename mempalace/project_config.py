"""Project config file resolution, shared by the miner and ``mempalace init``.

Kept free of heavy imports: ``room_detector_local`` uses it at import time,
and the miner pulls in the storage backends.
"""

import sys
from pathlib import Path
from typing import Optional

# Project config filenames, in resolution order (#2676). The first regular
# file wins; ``mempal.*`` is the legacy name. ``miner.SKIP_FILENAMES`` lists the
# same four so none of them is ever mined as content.
PROJECT_CONFIG_FILENAMES = (
    "mempalace.yaml",
    "mempalace.yml",
    "mempal.yaml",
    "mempal.yml",
)


def find_project_config(project_dir) -> Optional[Path]:
    """Return the project config file for ``project_dir``, or None.

    Checks :data:`PROJECT_CONFIG_FILENAMES` in order. ``is_file()`` rather
    than ``exists()``: the latter is true for a FIFO, and opening one would
    block in the kernel until a writer appears, so a config that is not a
    regular file is treated as absent. When more than one candidate exists,
    the first one is used and the rest are named on stderr so the shadowed
    file does not go silently unread.
    """
    resolved_project_dir = Path(project_dir).expanduser().resolve()
    found = [
        resolved_project_dir / name
        for name in PROJECT_CONFIG_FILENAMES
        if (resolved_project_dir / name).is_file()
    ]
    if not found:
        return None
    if len(found) > 1:
        ignored = ", ".join(p.name for p in found[1:])
        print(
            f"  Multiple project configs in {resolved_project_dir}: using "
            f"{found[0].name}, ignoring {ignored}.",
            file=sys.stderr,
        )
    return found[0]
