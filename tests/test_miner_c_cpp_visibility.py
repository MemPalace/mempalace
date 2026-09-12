"""C/C++ sources must not be silently skipped by the project miner.

Same failure class as test_miner_jsonl_visibility.py: scan_project keeps
only files whose suffix is in READABLE_EXTENSIONS. The whitelist grew
per-stack (C#, PHP, Swift/Kotlin, LaTeX) but never gained C or C++, so
mining any C project silently drops every .c/.h file — no warning, no
log line.
"""

import tempfile
from pathlib import Path

from mempalace.miner import READABLE_EXTENSIONS, scan_project

C_CPP_EXTENSIONS = {".c", ".h", ".cpp", ".hpp", ".cc", ".hh"}


class TestCAndCppNotSilentlySkipped:
    def test_c_cpp_in_readable_extensions(self):
        missing = C_CPP_EXTENSIONS - READABLE_EXTENSIONS
        assert not missing, (
            f"READABLE_EXTENSIONS is missing C/C++ suffixes {sorted(missing)}; "
            "every such file in a mined project is silently skipped."
        )

    def test_scan_project_picks_up_c_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            c_path = tmpdir / "probe.c"
            c_path.write_text(
                '#include <stdio.h>\nint main(void) { printf("hello\\n"); return 0; }\n'
            )
            found = [p.name for p in scan_project(str(tmpdir))]
            assert "probe.c" in found, f"scan_project silently dropped probe.c. Returned: {found}"
