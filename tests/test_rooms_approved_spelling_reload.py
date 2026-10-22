"""F8: approved room spellings must survive load_room_set."""

import json
import subprocess
import sys

import pytest

from mempalace.config import MempalaceConfig
from mempalace.rooms import (
    RoomSet,
    RoomSpec,
    _record_exemplars,
    load_room_set,
    save_room_set,
    snap_to_existing,
)


def _fresh_json(code, *args):
    return json.loads(
        subprocess.check_output([sys.executable, "-B", "-c", code, *map(str, args)], text=True)
    )


@pytest.mark.parametrize("existing", ["release_process", "Bug Fixes", "Release_3.6.0"])
def test_approved_room_spelling_and_exemplars_survive_reload(tmp_path, existing):
    config = MempalaceConfig(palace_path=str(tmp_path))
    rs = RoomSet(
        "w",
        [
            RoomSpec(
                "release-process"
                if existing == "release_process"
                else "bug-fixes"
                if existing == "Bug Fixes"
                else "release-3-6-0",
                "Approved room",
            )
        ],
    )
    assert snap_to_existing(rs, [existing])
    save_room_set(config, rs)
    loaded = load_room_set(config, "w")
    assert loaded.names() == [existing], "approved existing spelling changed on load"
    assert (
        _record_exemplars(loaded, [{"id": "evidence-drawer"}], [{"excerpt": 1, "room": existing}])
        == 1
    )
    save_room_set(config, loaded)
    saved = _fresh_json(
        """
import json, sys
from mempalace.config import MempalaceConfig
from mempalace.rooms import load_room_set
r = load_room_set(MempalaceConfig(palace_path=sys.argv[1]), "w")
print(json.dumps(r.to_dict()))
""",
        tmp_path,
    )
    assert saved["rooms"][0]["name"] == existing
    assert saved["rooms"][0]["exemplars"] == ["evidence-drawer"]
