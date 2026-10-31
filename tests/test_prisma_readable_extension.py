"""Regression coverage for #2675: ``.prisma`` in ``READABLE_EXTENSIONS``.

``schema.prisma`` files are plain text in the same class as ``.sql`` and were
silently skipped by ``mempalace mine`` because ``.prisma`` was absent from
``READABLE_EXTENSIONS``. The fix adds it back (``mempalace/miner.py``); these
tests are a genuine discriminator, not a no-op, because they exercise the real
``scan_project`` code path (``filepath.suffix.lower() in READABLE_EXTENSIONS``)
against a file on disk, and confirm the generated Prisma client copy under
``node_modules`` stays out of the palace via ``SKIP_DIRS``.
"""

from mempalace.miner import READABLE_EXTENSIONS, scan_project

SCHEMA = (
    "datasource db {\n"
    '  provider = "postgresql"\n'
    '  url      = env("DATABASE_URL")\n'
    "}\n"
    "\n"
    "model User {\n"
    "  id    Int    @id @default(autoincrement())\n"
    "  email String\n"
    "}\n"
)


def test_prisma_is_a_readable_extension():
    assert ".prisma" in READABLE_EXTENSIONS


def test_scan_project_includes_prisma_schema_but_not_generated_client(tmp_path):
    schema_dir = tmp_path / "prisma"
    schema_dir.mkdir(parents=True, exist_ok=True)
    # The hand-written schema is the real artifact and must be picked up.
    (schema_dir / "schema.prisma").write_text(SCHEMA, encoding="utf-8")
    client_dir = tmp_path / "node_modules" / ".prisma" / "client"
    client_dir.mkdir(parents=True, exist_ok=True)
    # ``prisma generate`` copies the schema into the generated client; no
    # .gitignore here, so only SKIP_DIRS (node_modules) keeps that copy out.
    (client_dir / "schema.prisma").write_text(SCHEMA, encoding="utf-8")

    files = scan_project(str(tmp_path))
    scanned = sorted(path.relative_to(tmp_path).as_posix() for path in files)
    assert scanned == ["prisma/schema.prisma"]
