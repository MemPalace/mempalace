"""Additive mining — verbatim history is never destroyed by re-mining.

Closes #1593 (miner.py + format_miner.py + diary_ingest.py stop
destroying prior drawers on re-mine) and #1580 (searcher neighbor
expansion scoped by parent_drawer_id, not just source_file).

Each test states EXPECTED behavior in its docstring. Tests fail
against pre-fix code because the current miners run
``collection.delete(where={"source_file": source_file})`` before
re-inserting fresh chunks — destroying prior versions.
"""

from pathlib import Path

import chromadb
import pytest

from mempalace.miner import mine


def _palace_collection(palace_path: Path):
    """Open the drawers collection from a freshly-built palace."""
    client = chromadb.PersistentClient(path=str(palace_path))
    return client.get_collection("mempalace_drawers")


def _drawer_contents_for_source(col, source_file: str) -> list[str]:
    """Return every drawer's content for a given source_file, ordered by
    filed_at (oldest first). Used to assert prior versions remain after
    re-mine of an edited file.
    """
    result = col.get(where={"source_file": source_file}, include=["documents", "metadatas"])
    pairs = list(zip(result["documents"], result["metadatas"]))
    pairs.sort(key=lambda pair: pair[1].get("filed_at", ""))
    return [doc for doc, _ in pairs]


class TestMinerPreservation:
    """miner.py — re-mining a project file must never destroy prior versions."""

    def test_remine_unchanged_file_preserves_drawer_count(self, tmp_path):
        """Re-mining a file that has NOT changed produces no destruction.

        EXPECTED: drawer count is identical before and after the second mine.
                  No drawer's content silently changes.
        FAIL SIGNAL: drawer count differs OR content shifts unexpectedly.
        """
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        notes.write_text("This is a stable note.\n" * 50, encoding="utf-8")

        palace = tmp_path / "palace"
        mine(str(project), str(palace))

        col = _palace_collection(palace)
        first_count = col.count()
        first_contents = sorted(_drawer_contents_for_source(col, str(notes)))

        # Re-mine the same unchanged file.
        mine(str(project), str(palace))
        col = _palace_collection(palace)
        second_count = col.count()
        second_contents = sorted(_drawer_contents_for_source(col, str(notes)))

        assert second_count == first_count, (
            f"Unchanged re-mine inflated drawer count from {first_count} to {second_count}"
        )
        assert second_contents == first_contents, "Unchanged re-mine altered drawer content"

    def test_remine_edited_file_preserves_prior_version(self, tmp_path):
        """When a file is edited and re-mined, the PRIOR version's content
        must still be retrievable from the palace.

        EXPECTED: a drawer containing the original phrase ("ALPHA_MARKER")
                  remains in the palace AFTER the file has been edited and
                  re-mined with that phrase replaced by "BETA_MARKER".
        FAIL SIGNAL: the ALPHA_MARKER content cannot be found anywhere
                     in the drawers collection.
        """
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        original = "The first version contains ALPHA_MARKER as the key phrase.\n" * 30
        notes.write_text(original, encoding="utf-8")

        palace = tmp_path / "palace"
        mine(str(project), str(palace))

        # Edit the file: replace ALPHA_MARKER with BETA_MARKER.
        edited = original.replace("ALPHA_MARKER", "BETA_MARKER")
        notes.write_text(edited, encoding="utf-8")
        mine(str(project), str(palace))

        col = _palace_collection(palace)
        all_docs = col.get(where={"source_file": str(notes)}, include=["documents"])
        joined = "\n".join(all_docs["documents"])

        assert "ALPHA_MARKER" in joined, (
            "ALPHA_MARKER (prior version's content) was destroyed by re-mine — "
            "violates #1593 verbatim-preservation principle"
        )
        assert "BETA_MARKER" in joined, (
            "BETA_MARKER (current version's content) is missing after re-mine"
        )

    def test_three_version_cycle_all_accessible(self, tmp_path):
        """A file edited and re-mined three times must yield all three
        versions retrievable from the palace.

        EXPECTED: drawers contain phrases from version 1 AND version 2 AND
                  version 3 simultaneously.
        FAIL SIGNAL: any earlier version's distinctive phrase is missing.
        """
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        palace = tmp_path / "palace"

        for version_marker in ("V1_PHRASE", "V2_PHRASE", "V3_PHRASE"):
            notes.write_text(
                f"This iteration uses {version_marker} as the distinctive marker.\n" * 30,
                encoding="utf-8",
            )
            mine(str(project), str(palace))

        col = _palace_collection(palace)
        all_docs = col.get(where={"source_file": str(notes)}, include=["documents"])
        joined = "\n".join(all_docs["documents"])

        for marker in ("V1_PHRASE", "V2_PHRASE", "V3_PHRASE"):
            assert marker in joined, (
                f"{marker} is missing — re-mine cycle destroyed an intermediate "
                "version, violating #1593"
            )

    def test_source_mtime_change_never_destroys_prior_content(self, tmp_path):
        """Touching a file's mtime (without changing content) must NEVER
        destroy prior drawer content. PR A is additive — a re-mine
        triggered by mtime change MAY add new layers (until PR B's
        content-hash dedup makes the no-op explicit), but it must never
        remove any prior drawer.

        EXPECTED: after touch() and re-mine, every prior chunk's content
                  is still retrievable from the palace (count may rise,
                  must not fall; content may duplicate, must not vanish).
        FAIL SIGNAL: any prior chunk's distinctive content is missing.
        """
        import os
        import time

        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        marker = "UNIQUE_MTIME_PRESERVATION_MARKER"
        notes.write_text(f"Content with {marker} inside.\n" * 30, encoding="utf-8")

        palace = tmp_path / "palace"
        mine(str(project), str(palace))
        col = _palace_collection(palace)
        before_count = col.count()

        # Bump mtime without touching content.
        future = time.time() + 60
        os.utime(notes, (future, future))

        mine(str(project), str(palace))
        col = _palace_collection(palace)

        # Count must not DROP (destruction would shrink it).
        assert col.count() >= before_count, (
            f"mtime bump caused drawer count to DROP "
            f"({before_count} -> {col.count()}) — destruction occurred"
        )
        # Marker must still be findable.
        all_docs = col.get(where={"source_file": str(notes)}, include=["documents"])
        joined = "\n".join(all_docs["documents"])
        assert marker in joined, "mtime bump destroyed prior content — violates #1593"


class TestFormatMinerPreservation:
    """format_miner.py — re-mining an RTF (or other office format) file
    must never destroy prior versions. Same #1593 architectural fix as
    miner.py, separately tested because the delete site is duplicated
    in format_miner.py:633.
    """

    def test_format_miner_remine_edited_preserves_prior_version(self, tmp_path):
        """Edit an RTF file, re-mine via format_miner — prior version's
        content must still be retrievable.

        EXPECTED: drawer for the source file contains both ALPHA_MARKER
                  (original) and BETA_MARKER (current) after the edit.
        FAIL SIGNAL: ALPHA_MARKER is missing — format_miner destroyed it.
        """
        pytest.importorskip("striprtf")
        from mempalace.format_miner import mine_formats

        project = tmp_path / "formats"
        project.mkdir()
        rtf_file = project / "letter.rtf"

        def _rtf(payload: str) -> str:
            return "{\\rtf1\\ansi\\deff0 " + payload + "}"

        # Need enough content to clear format_miner's MIN_CHUNK_SIZE (50 chars)
        rtf_file.write_text(
            _rtf("Original content with ALPHA_MARKER inside the letter.\n" * 20),
            encoding="utf-8",
        )

        palace = tmp_path / "palace"
        mine_formats(str(project), str(palace))

        rtf_file.write_text(
            _rtf("Edited content with BETA_MARKER inside the letter.\n" * 20),
            encoding="utf-8",
        )
        mine_formats(str(project), str(palace))

        col = _palace_collection(palace)
        all_docs = col.get(where={"source_file": str(rtf_file)}, include=["documents"])
        joined = "\n".join(all_docs["documents"])

        assert "ALPHA_MARKER" in joined, (
            "ALPHA_MARKER destroyed by format_miner re-mine — "
            "violates #1593 (format_miner.py:633 delete-on-remine)"
        )
        assert "BETA_MARKER" in joined, "BETA_MARKER missing after re-mine"


class TestDiaryIngestPreservation:
    """diary_ingest.py — re-ingesting a diary file must never destroy
    prior versions. Closes the hidden #1593 violation: today,
    diary_ingest fires destructive full_rebuild whenever
    ``content_changed`` is True — meaning ANY edit to a diary file
    silently destroys the prior version.
    """

    def test_diary_content_change_does_not_destroy_prior_version(self, tmp_path):
        """Edit a diary entry's content (NOT just append) and re-ingest —
        the prior content must still be retrievable. This is the
        content_changed = True path that today triggers destructive
        full_rebuild.

        EXPECTED: drawer for the diary date contains both ORIGINAL_PHRASE
                  and EDITED_PHRASE after the edit.
        FAIL SIGNAL: ORIGINAL_PHRASE missing — content_changed branch
                     destroyed the prior version.
        """
        from mempalace.diary_ingest import ingest_diaries

        diary_dir = tmp_path / "diary"
        diary_dir.mkdir()
        diary_file = diary_dir / "2026-05-25.md"
        diary_file.write_text(
            "## Morning\n"
            "Today I worked on the project and noted ORIGINAL_PHRASE as the key idea "
            "behind the architecture decision I am writing about right now.\n",
            encoding="utf-8",
        )

        palace = tmp_path / "palace"
        ingest_diaries(str(diary_dir), str(palace))

        # Edit the diary entry's CONTENT (in-place rewrite, not append).
        diary_file.write_text(
            "## Morning\n"
            "Today I worked on the project and noted EDITED_PHRASE as the key idea "
            "behind the architecture decision I am writing about right now.\n",
            encoding="utf-8",
        )
        ingest_diaries(str(diary_dir), str(palace))

        col = _palace_collection(palace)
        all_docs = col.get(include=["documents", "metadatas"])
        joined = "\n".join(all_docs["documents"])

        assert "ORIGINAL_PHRASE" in joined, (
            "ORIGINAL_PHRASE destroyed by diary_ingest re-ingest — "
            "violates #1593; the content_changed=True branch at "
            "diary_ingest.py:183 still triggers destructive full_rebuild"
        )
        assert "EDITED_PHRASE" in joined, "EDITED_PHRASE missing after re-ingest"


class TestDeleteVerb:
    """``mempalace delete`` — the SOLE destruction path under the
    additive-only ingestion model. These tests verify the verb destroys
    what it should, refuses what it shouldn't, and honors --dry-run /
    --force flags as specified."""

    def _mine_and_get_first_metadata(self, tmp_path):
        """Helper — mines a one-file palace, returns palace + first drawer's metadata."""
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        notes.write_text("Content for delete-verb tests.\n" * 30, encoding="utf-8")
        palace = tmp_path / "palace"
        mine(str(project), str(palace))
        col = _palace_collection(palace)
        first = col.get(include=["metadatas"], limit=1)
        return palace, first["ids"][0], first["metadatas"][0]

    def test_delete_drawer_id_removes_one_layer_with_force(self, tmp_path):
        """``mempalace delete <drawer_id> --force`` removes exactly that
        one drawer row and no others.

        EXPECTED: count drops by 1; the named drawer is gone; others remain.
        FAIL SIGNAL: count drops by more than 1, or the named drawer still
                     present, or unrelated drawers got removed.
        """
        import subprocess
        import sys as _sys

        palace, drawer_id, _meta = self._mine_and_get_first_metadata(tmp_path)
        col = _palace_collection(palace)
        before_count = col.count()

        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "delete",
                drawer_id,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"delete failed: {result.stderr}"

        col = _palace_collection(palace)
        after_count = col.count()
        assert after_count == before_count - 1, (
            f"delete drawer_id removed wrong count: {before_count} -> {after_count}"
        )
        remaining = col.get(ids=[drawer_id])
        assert not remaining["ids"], "deleted drawer still present"

    def test_delete_stack_id_removes_all_layers(self, tmp_path):
        """``mempalace delete <stack_id> --force`` removes every layer of
        one logical chunk position, leaving other stacks alone.

        EXPECTED: every row with the target stack_id is gone; rows with
                  other stack_ids remain.
        FAIL SIGNAL: a row with the target stack_id survives, or rows
                     with other stack_ids got destroyed too.
        """
        import subprocess
        import sys as _sys

        # Mine, edit, re-mine → at least one stack has 2 layers.
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        notes.write_text("First version content marker FIRST_V.\n" * 30, encoding="utf-8")
        palace = tmp_path / "palace"
        mine(str(project), str(palace))

        notes.write_text("Second version content marker SECOND_V.\n" * 30, encoding="utf-8")
        mine(str(project), str(palace))

        col = _palace_collection(palace)
        all_meta = col.get(include=["metadatas"])
        # Find any stack_id with > 1 layer.
        from collections import Counter

        stack_counts = Counter(
            m.get("stack_id") for m in all_meta["metadatas"] if m.get("stack_id")
        )
        target_stack_id, layer_count = stack_counts.most_common(1)[0]
        assert layer_count >= 2, "test setup: expected at least one multi-layer stack"

        before_count = col.count()
        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "delete",
                target_stack_id,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"delete failed: {result.stderr}"

        col = _palace_collection(palace)
        after_count = col.count()
        assert after_count == before_count - layer_count, (
            f"delete stack_id removed wrong count: {before_count} -> {after_count}, "
            f"expected to remove {layer_count} layers"
        )
        remaining_in_stack = col.get(where={"stack_id": target_stack_id})
        assert not remaining_in_stack["ids"], "deleted stack still has layers"

    def test_delete_dry_run_destroys_nothing(self, tmp_path):
        """``mempalace delete <id> --dry-run`` reports what would be
        destroyed but destroys nothing.

        EXPECTED: drawer count unchanged; named drawer still present.
        FAIL SIGNAL: count drops at all.
        """
        import subprocess
        import sys as _sys

        palace, drawer_id, _meta = self._mine_and_get_first_metadata(tmp_path)
        col = _palace_collection(palace)
        before_count = col.count()

        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "delete",
                drawer_id,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

        col = _palace_collection(palace)
        after_count = col.count()
        assert after_count == before_count, (
            f"--dry-run destroyed drawers: {before_count} -> {after_count}"
        )

    def test_delete_unrecognized_prefix_errors_cleanly(self, tmp_path):
        """An identifier without a recognized prefix exits nonzero with a
        clear message — no silent fall-through to mass destruction.

        EXPECTED: exit code 2; stderr explains the prefix requirement.
        FAIL SIGNAL: exit 0 (success), or any drawer destroyed.
        """
        import subprocess
        import sys as _sys

        palace, _drawer_id, _meta = self._mine_and_get_first_metadata(tmp_path)
        col = _palace_collection(palace)
        before_count = col.count()

        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "delete",
                "junk-identifier-no-prefix",
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2, (
            f"expected exit 2 for unrecognized prefix; got {result.returncode}"
        )
        assert "no recognized prefix" in result.stderr.lower()

        col = _palace_collection(palace)
        assert col.count() == before_count, "unrecognized prefix triggered destruction"


class TestShowVerb:
    """``mempalace show`` — display a drawer or a stack of layers with
    vertical navigation indicators."""

    def test_show_drawer_id_renders_single_layer(self, tmp_path):
        """``mempalace show <drawer_id>`` renders that one row's content
        with wing/room/filed_at header.

        EXPECTED: stdout contains the drawer's text and a header line
                  with wing/room.
        FAIL SIGNAL: empty stdout, missing content, or missing header.
        """
        import subprocess
        import sys as _sys

        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        unique = "UNIQUE_SHOW_VERB_MARKER_42"
        notes.write_text(f"Content with {unique} inside it.\n" * 30, encoding="utf-8")
        palace = tmp_path / "palace"
        mine(str(project), str(palace))

        col = _palace_collection(palace)
        first = col.get(include=["metadatas"], limit=1)
        drawer_id = first["ids"][0]

        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "show",
                drawer_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"show failed: {result.stderr}"
        assert unique in result.stdout, "show output missing the drawer's content"
        assert "Drawer:" in result.stdout, "show output missing the Drawer: header"

    def test_show_stack_id_renders_layer_indicator(self, tmp_path):
        """``mempalace show <stack_id>`` for a multi-layer stack renders
        the latest layer with ``[layer 1 of N]`` indicator + a navigation
        hint to the older layer.

        EXPECTED: header includes 'layer 1 of N' (N >= 2); navigation
                  hint mentions 'older'.
        FAIL SIGNAL: missing layer indicator, missing nav hint.
        """
        import subprocess
        import sys as _sys

        # Mine, edit, re-mine → multi-layer stack exists.
        project = tmp_path / "project"
        project.mkdir()
        notes = project / "notes.md"
        notes.write_text("First version V1_SHOW_MARKER content.\n" * 30, encoding="utf-8")
        palace = tmp_path / "palace"
        mine(str(project), str(palace))

        notes.write_text("Second version V2_SHOW_MARKER content.\n" * 30, encoding="utf-8")
        mine(str(project), str(palace))

        col = _palace_collection(palace)
        all_meta = col.get(include=["metadatas"])
        from collections import Counter

        stack_counts = Counter(
            m.get("stack_id") for m in all_meta["metadatas"] if m.get("stack_id")
        )
        target_stack_id, layer_count = stack_counts.most_common(1)[0]
        assert layer_count >= 2, "test setup: expected multi-layer stack"

        result = subprocess.run(
            [
                _sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "show",
                target_stack_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"show failed: {result.stderr}"
        assert f"layer 1 of {layer_count}" in result.stdout, (
            f"show output missing 'layer 1 of {layer_count}' indicator"
        )
        assert "older" in result.stdout.lower(), "show output missing 'older' navigation hint"


class TestNeighborExpansionScopedByParentDrawerId:
    """#1580 — searcher._expand_with_neighbors must not stitch chunks
    across unrelated drawers that share an empty source_file."""

    def test_neighbors_do_not_cross_parent_drawer_id(self, tmp_path):
        """Two unrelated drawer-groups that share a non-empty source_file
        must NOT interleave their chunks during neighbor expansion.

        EXPECTED: expanding neighbors of chunk 0 from group A returns
                  content from group A only. Group B's distinctive marker
                  is NEVER in the expanded text.
        FAIL SIGNAL: group B's content appears in the expansion result —
                     parent_drawer_id scope is not being applied.

        Uses non-empty source_file with two parent_drawer_ids deliberately
        sharing it. This is the case where the ``if not src`` early-return
        guard in _expand_with_neighbors does NOT short-circuit — so the
        $and-filter logic actually runs.

        Uses ``palace.get_collection()`` (the production path through the
        ``ChromaCollection`` wrapper) rather than a raw chromadb client.
        The wrapper's ``GetResult`` dataclass supports both ``.documents``
        and ``["documents"]`` access; the raw client only supports the
        latter. Testing through the wrapper matches what production
        actually exercises — flagged by @mvalentsev on PR #1628.
        """
        from mempalace.palace import get_collection

        palace = tmp_path / "palace"
        col = get_collection(str(palace), create=True)

        # Two distinct mining passes of the same source_file produce two
        # parent_drawer_ids that share source_file — the exact shape #1580
        # protects against.
        source_file = str(tmp_path / "shared.md")
        marker_a = "DRAWER_A_UNIQUE_MARKER_1580"
        marker_b = "DRAWER_B_UNIQUE_MARKER_1580"
        col.add(
            ids=["a_chunk_0", "a_chunk_1"],
            documents=[f"chunk 0 of group A — {marker_a}", "chunk 1 of group A"],
            metadatas=[
                {
                    "source_file": source_file,
                    "chunk_index": 0,
                    "parent_drawer_id": "parent_GROUP_A",
                    "filed_at": "2026-01-01T00:00:00",
                },
                {
                    "source_file": source_file,
                    "chunk_index": 1,
                    "parent_drawer_id": "parent_GROUP_A",
                    "filed_at": "2026-01-01T00:00:00",
                },
            ],
        )
        col.add(
            ids=["b_chunk_0", "b_chunk_1"],
            documents=[f"chunk 0 of group B — {marker_b}", "chunk 1 of group B"],
            metadatas=[
                {
                    "source_file": source_file,
                    "chunk_index": 0,
                    "parent_drawer_id": "parent_GROUP_B",
                    "filed_at": "2026-02-01T00:00:00",
                },
                {
                    "source_file": source_file,
                    "chunk_index": 1,
                    "parent_drawer_id": "parent_GROUP_B",
                    "filed_at": "2026-02-01T00:00:00",
                },
            ],
        )

        # Expand neighbors of group A's chunk 0.
        from mempalace.searcher import _expand_with_neighbors

        a_chunk_0_doc = f"chunk 0 of group A — {marker_a}"
        a_chunk_0_meta = {
            "source_file": source_file,
            "chunk_index": 0,
            "parent_drawer_id": "parent_GROUP_A",
            "filed_at": "2026-01-01T00:00:00",
        }
        expanded = _expand_with_neighbors(col, a_chunk_0_doc, a_chunk_0_meta)
        expanded_text = expanded.get("text", "")

        assert marker_a in expanded_text, (
            "Group A's own content is missing from its own neighbor expansion"
        )
        assert marker_b not in expanded_text, (
            "Neighbor expansion of group A's chunk 0 returned group B's content — "
            "violates #1580; parent_drawer_id scope is not being applied"
        )
