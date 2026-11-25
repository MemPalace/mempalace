"""Infrastructure-as-code sources are mined (.tf/.tfvars/.hcl/.bicep) — #2990.

Terraform/HCL and Bicep are plain text, but none of their suffixes were in
``READABLE_EXTENSIONS``, so ``mempalace mine`` silently filed only the docs and
YAML around an IaC repo and reported success. ``.terraform`` is the IaC
equivalent of ``node_modules``: ``terraform init`` downloads third-party modules
there as ``.tf`` files, so it must be pruned like the other generated dirs.
"""

from mempalace import entity_detector
from mempalace.miner import READABLE_EXTENSIONS, scan_project

MAIN_TF = """resource "azurerm_resource_group" "this" {
  name     = "rg-demo"
  location = var.location
}
"""

MODULE_TF = """variable "location" {
  type = string
}
"""


def _scanned(root):
    return sorted(p.relative_to(root).as_posix() for p in scan_project(str(root)))


def test_iac_extensions_are_readable():
    iac = {".tf", ".tfvars", ".hcl", ".bicep", ".bicepparam"}
    assert iac <= READABLE_EXTENSIONS
    # The entity sampler has its own list; an IaC-only repo must still have
    # files to sample there too.
    assert iac <= entity_detector.READABLE_EXTENSIONS


def test_scan_project_includes_iac_files(tmp_path):
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text(MAIN_TF, encoding="utf-8")
    (tmp_path / "infra" / "prod.tfvars").write_text('location = "westeurope"\n', encoding="utf-8")
    (tmp_path / "infra" / "variables.hcl").write_text('location = "westeurope"\n', encoding="utf-8")
    (tmp_path / "main.bicep").write_text("param location string\n", encoding="utf-8")
    (tmp_path / "prod.bicepparam").write_text("using './main.bicep'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# infra\n", encoding="utf-8")

    assert _scanned(tmp_path) == [
        "README.md",
        "infra/main.tf",
        "infra/prod.tfvars",
        "infra/variables.hcl",
        "main.bicep",
        "prod.bicepparam",
    ]


def test_scan_project_prunes_terraform_module_dir(tmp_path):
    (tmp_path / "main.tf").write_text(MAIN_TF, encoding="utf-8")
    modules = tmp_path / ".terraform" / "modules" / "vnet"
    modules.mkdir(parents=True)
    (modules / "main.tf").write_text(MODULE_TF, encoding="utf-8")

    # No .gitignore: the downloaded module copy is pruned by SKIP_DIRS itself.
    assert _scanned(tmp_path) == ["main.tf"]
