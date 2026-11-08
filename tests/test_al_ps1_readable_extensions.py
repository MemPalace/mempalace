"""Business Central AL and PowerShell sources are mined (#278).

A BC repository is mostly ``.al`` files; without the extension the miner
filed only the ~1,100 JSON/YAML/Markdown files out of 16,000+ and skipped the
code. ``.ps1`` is the Windows counterpart of ``.sh``, which is already read.
``.xlf`` (XLIFF) stays out: it is XML, ``.xml`` itself is not read, and in BC
repos it is the generated translation output (``*.g.xlf``) of the AL code.
"""

from mempalace.miner import READABLE_EXTENSIONS, scan_project

AL_CODEUNIT = """codeunit 50100 "Customer Greeting"
{
    procedure Greet(Customer: Record Customer): Text
    begin
        exit('Hello ' + Customer.Name);
    end;
}
"""

PS1_SCRIPT = """param([string]$Name = "world")
Write-Host "Hello $Name"
"""


def _scanned(root):
    return sorted(p.relative_to(root).as_posix() for p in scan_project(str(root)))


def test_al_and_ps1_are_readable():
    assert {".al", ".ps1"} <= READABLE_EXTENSIONS


def test_xlf_is_not_readable():
    assert ".xlf" not in READABLE_EXTENSIONS


def test_scan_project_includes_al_and_ps1_case_insensitively(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "CustomerGreeting.Codeunit.al").write_text(AL_CODEUNIT, encoding="utf-8")
    (tmp_path / "src" / "Upper.Table.AL").write_text(AL_CODEUNIT, encoding="utf-8")
    (tmp_path / "build.ps1").write_text(PS1_SCRIPT, encoding="utf-8")
    (tmp_path / "Translations").mkdir()
    (tmp_path / "Translations" / "App.g.xlf").write_text(
        '<?xml version="1.0"?><xliff version="1.2"/>\n', encoding="utf-8"
    )

    assert _scanned(tmp_path) == [
        "build.ps1",
        "src/CustomerGreeting.Codeunit.al",
        "src/Upper.Table.AL",
    ]
