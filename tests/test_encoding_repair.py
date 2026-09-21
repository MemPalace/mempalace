import json
import os
import stat
import unicodedata

import pytest

from mempalace.encoding_repair import (
    repair_collection,
    repair_mojibake,
    repair_mojibake_once,
    restore_collection,
)


CLEAN_MULTILINGUAL = [
    "La canción «PERÚ» abre el disco.",
    "Buried surface area 1250 Å².",
    "Volume measured as 42 Å³.",
    "CAFÉ® is a registered mark.",
    "RÉSUMÉ\u00a0: présentation générale.",
    "Already clean: café → ✅",
    "Été à Noël — déjà vu.",
    "L'œuvre d'André coûte 20 €.",
    "Größe, Fußgänger und Straße.",
    "Übermäßig süß — Öl und Äpfel.",
    "A ação começa em São João.",
    "Às vezes, o avô lê o jornal.",
    "CORAÇÃO, PERÚ e CAFÉ®.",
    "Zażółć gęślą jaźń.",
    "Łódź — źródło wiedzy.",
    "Średnica wynosi 25 µm.",
    "L'Àngels diu: «això és català».",
    "Per què l'aviació és útil?",
    "Temperatura: −5 °C ± 0,2 °C.",
    "Trademark™ and registered® symbols.",
    "Crème brûlée — déjà vu.",
    "São Tomé e Príncipe.",
    "François parle à Élise.",
    "Smörgåsbord, Ångström and Øresund.",
    "naïve façade coöperate.",
    "România, când și până.",
    "Guðrún lives in Reykjavík.",
    "Clean emoji: → ✅ 🚀.",
]


DAMAGED_CASES = [
    (
        "cafÃ©",
        "café",
    ),
    (
        "naÃ¯ve",
        "naïve",
    ),
    (
        "EspaÃ±a",
        "España",
    ),
    (
        "aÃ§Ã£o",
        "ação",
    ),
    (
        "MÃ¼nchen",
        "München",
    ),
    (
        "FranÃ§ais",
        "Français",
    ),
    (
        "Plan â†’ result â€” done.",
        "Plan → result — done.",
    ),
    (
        "Copyright Â© 2026",
        "Copyright © 2026",
    ),
    (
        "BOM ï»¿removed",
        "BOM \ufeffremoved",
    ),
    (
        "Emoji ðŸ˜€",
        "Emoji 😀",
    ),
    (
        "cafÃƒÂ©",
        "café",
    ),
    (
        "Clean prefix, cafÃ©, clean suffix.",
        "Clean prefix, café, clean suffix.",
    ),
]


AMBIGUOUS_CASES = [
    "Å‚",
    "Åº",
    "Ä™",
]


# An UPPERCASE Ã/Â ending a word, followed by ordinary typographic punctuation.
# Portuguese, Vietnamese and Turkish produce this constantly, and it matches the
# same two-character shape as mojibake — so it must survive untouched (#2193).
CLEAN_UPPERCASE_LEAD = [
    "“IRMÃ” é o título do filme.",
    "«MAÇÃ».",
    "O prémio «AMANHÃ» foi entregue.",
    "MANHÃ… tarde e noite.",
    "A palavra “LÃ” significa wool.",
    "TÍTULO: “A IRMÃ”, de 1998.",
    "IRMÃ–MÃE: a relação central.",
    "NÃO! disse a IRMÃ.",
    "BÃO số 5 đổ bộ.",
    "“NHÃ”: nghĩa là nhà.",
    "“HÂLÂ” bekliyoruz.",
    "İMÂ… edildi.",
    "HÂLÂ—yine de.",
    "HÂLÂ» ve zarar.",
    "IMÂ« edildi.",
    "IRMÃ”, MAÇÃ» e MANHÃ… juntas.",
]


# A single drawer holding genuine mojibake AND clean prose. The miner
# concatenates several sources into one drawer, so this is the normal case, not
# a corner case: repairing the damaged half must not corrupt the clean half.
MIXED_DRAWERS = [
    (
        "cafÃ© e «MAÇÃ».",
        "café e «MAÇÃ».",
    ),
    (
        "MÃ¼nchen. A IRMÃ” chegou.",
        "München. A IRMÃ” chegou.",
    ),
    (
        "EspaÃ±a e MANHÃ… fria.",
        "España e MANHÃ… fria.",
    ),
    (
        "aÃ§Ã£o «AMANHÃ» hoje.",
        "ação «AMANHÃ» hoje.",
    ),
    (
        "Copyright Â© 2026 — IRMÃ” Ltda.",
        "Copyright © 2026 — IRMÃ” Ltda.",
    ),
]


@pytest.mark.parametrize(
    "text",
    CLEAN_MULTILINGUAL,
)
def test_preserves_clean_multilingual_text(
    text,
):
    assert repair_mojibake(text) == text


@pytest.mark.parametrize(
    (
        "damaged",
        "expected",
    ),
    DAMAGED_CASES,
)
def test_repairs_high_confidence_mojibake(
    damaged,
    expected,
):
    assert repair_mojibake(damaged) == expected


@pytest.mark.parametrize(
    "text",
    AMBIGUOUS_CASES,
)
def test_leaves_ambiguous_sequences_for_manual_review(
    text,
):
    assert repair_mojibake(text) == text


def test_repair_is_idempotent():
    repaired = repair_mojibake("cafÃ© â†’ done")

    assert repair_mojibake(repaired) == repaired


@pytest.mark.parametrize(
    "text",
    CLEAN_UPPERCASE_LEAD,
)
def test_preserves_clean_uppercase_lead_prose(
    text,
):
    """An all-caps word ending in Ã/Â is prose, not mojibake (#2193)."""
    assert repair_mojibake(text) == text


@pytest.mark.parametrize(
    (
        "damaged",
        "expected",
    ),
    MIXED_DRAWERS,
)
def test_repairs_damaged_half_without_corrupting_clean_half(
    damaged,
    expected,
):
    """Corroboration must stay local: one damaged run does not condemn the drawer."""
    assert repair_mojibake(damaged) == expected


@pytest.mark.parametrize(
    "text",
    CLEAN_UPPERCASE_LEAD + CLEAN_MULTILINGUAL,
)
def test_repair_never_emits_control_characters(
    text,
):
    """ "İMÂ… edildi." must not become "İM\\u0085 edildi." — visible text for a control."""
    repaired = repair_mojibake(text)

    assert not [
        character
        for character in repaired
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r"
    ]


def test_repair_does_not_destroy_its_own_correct_output():
    """The multi-pass loop repaired correctly on pass 1 and corrupted on pass 2 (#2193)."""
    clean = "“IRMÃ” é o título."
    damaged = "".join(
        chr(byte_value)
        if byte_value in (0x81, 0x8D, 0x8F, 0x90, 0x9D)
        else bytes([byte_value]).decode("cp1252")
        for byte_value in clean.encode("utf-8")
    )

    first_pass = repair_mojibake_once(damaged)

    assert first_pass == clean
    assert repair_mojibake_once(first_pass) == clean
    assert repair_mojibake(damaged) == clean


@pytest.mark.parametrize(
    (
        "damaged",
        "expected",
    ),
    [
        ("coÃ»te", "coûte"),
        ("NoÃ«l", "Noël"),
        ("brÃ»lÃ©e", "brûlée"),
        # NBSP is the continuation byte for à, so the guillemet run chains onto
        # it and is repaired as part of a multi-unit run.
        ("catalÃ Â»", "català»"),
    ],
)
def test_still_repairs_ambiguous_window_with_local_evidence(
    damaged,
    expected,
):
    """A lowercase letter running into the lead proves corruption — repair it."""
    assert repair_mojibake(damaged) == expected


def test_rejects_invalid_max_passes():
    with pytest.raises(
        ValueError,
        match="max_passes",
    ):
        repair_mojibake(
            "cafÃ©",
            max_passes=0,
        )


class FakeCollection:
    name = "mempalace_drawers"

    def __init__(
        self,
        documents,
    ):
        self.ids = [f"drawer-{index}" for index in range(len(documents))]
        self.documents = list(documents)
        self.updates = []

    def get(
        self,
        *,
        limit,
        offset,
        include,
    ):
        del include

        end = offset + limit

        return {
            "ids": self.ids[offset:end],
            "documents": self.documents[offset:end],
        }

    def update(
        self,
        *,
        ids,
        documents,
    ):
        self.updates.append(
            {
                "ids": list(ids),
                "documents": list(documents),
            }
        )

        positions = {drawer_id: index for index, drawer_id in enumerate(self.ids)}

        for drawer_id, document in zip(
            ids,
            documents,
        ):
            self.documents[positions[drawer_id]] = document


def test_dry_run_flags_only_damaged_documents():
    collection = FakeCollection(
        [
            CLEAN_MULTILINGUAL[0],
            "cafÃ©",
            CLEAN_MULTILINGUAL[1],
            "arrow â†’",
        ]
    )
    changes = []

    report = repair_collection(
        collection,
        apply=False,
        page_size=2,
        on_change=(
            lambda drawer_id, before, after: changes.append(
                (
                    drawer_id,
                    before,
                    after,
                )
            )
        ),
    )

    assert report == {
        "scanned": 4,
        "changed": 2,
        "updated": 0,
        "backup_path": None,
    }
    assert [change[0] for change in changes] == [
        "drawer-1",
        "drawer-3",
    ]
    assert collection.updates == []


def test_apply_requires_backup_path():
    with pytest.raises(
        ValueError,
        match="backup_path",
    ):
        repair_collection(
            FakeCollection(["cafÃ©"]),
            apply=True,
        )


def test_apply_writes_backup_before_update(
    tmp_path,
):
    backup = tmp_path / "backup.jsonl"

    class BackupCheckingCollection(FakeCollection):
        def update(
            self,
            *,
            ids,
            documents,
        ):
            lines = backup.read_text(encoding="utf-8").splitlines()

            assert len(lines) == 2
            assert json.loads(lines[1]) == {
                "id": "drawer-0",
                "original_document": ("cafÃ©"),
            }

            super().update(
                ids=ids,
                documents=documents,
            )

    collection = BackupCheckingCollection(["cafÃ©"])

    report = repair_collection(
        collection,
        apply=True,
        backup_path=backup,
    )

    assert report["updated"] == 1
    assert report["backup_path"] == str(backup)
    assert collection.documents == ["café"]

    if os.name != "nt":
        mode = stat.S_IMODE(backup.stat().st_mode)
        assert mode & 0o077 == 0


def test_apply_refuses_to_overwrite_existing_backup(
    tmp_path,
):
    backup = tmp_path / "backup.jsonl"
    backup.write_text(
        "do not overwrite",
        encoding="utf-8",
    )
    collection = FakeCollection(["cafÃ©"])

    with pytest.raises(FileExistsError):
        repair_collection(
            collection,
            apply=True,
            backup_path=backup,
        )

    assert backup.read_text(encoding="utf-8") == "do not overwrite"
    assert collection.updates == []


def test_apply_with_no_changes_does_not_create_empty_backup(
    tmp_path,
):
    backup = tmp_path / "backup.jsonl"

    report = repair_collection(
        FakeCollection(CLEAN_MULTILINGUAL[:3]),
        apply=True,
        backup_path=backup,
    )

    assert report["changed"] == 0
    assert report["updated"] == 0
    assert report["backup_path"] is None
    assert not backup.exists()


def test_backup_restores_original_documents(
    tmp_path,
):
    backup = tmp_path / "backup.jsonl"
    collection = FakeCollection(
        [
            "cafÃ©",
            CLEAN_MULTILINGUAL[1],
            "arrow â†’",
        ]
    )

    repair_collection(
        collection,
        apply=True,
        page_size=2,
        backup_path=backup,
    )

    assert collection.documents == [
        "café",
        CLEAN_MULTILINGUAL[1],
        "arrow →",
    ]

    report = restore_collection(
        collection,
        backup,
        batch_size=1,
    )

    assert report == {
        "validated": 2,
        "restored": 2,
    }
    assert collection.documents == [
        "cafÃ©",
        CLEAN_MULTILINGUAL[1],
        "arrow â†’",
    ]


def test_restore_validates_whole_backup_before_writing(
    tmp_path,
):
    backup = tmp_path / "backup.jsonl"
    backup.write_text(
        (
            '{"format":'
            '"mempalace-encoding-repair",'
            '"version":1}\n'
            '{"id":"drawer-0",'
            '"original_document":"cafÃ©"}\n'
            "not-json\n"
        ),
        encoding="utf-8",
    )
    collection = FakeCollection(["café"])

    with pytest.raises(
        ValueError,
        match="line 3",
    ):
        restore_collection(
            collection,
            backup,
        )

    assert collection.updates == []


def test_collection_rejects_misaligned_results():
    class MisalignedCollection(FakeCollection):
        def get(
            self,
            *,
            limit,
            offset,
            include,
        ):
            del (
                limit,
                offset,
                include,
            )

            return {
                "ids": ["drawer-0"],
                "documents": [],
            }

    with pytest.raises(
        RuntimeError,
        match="misaligned",
    ):
        repair_collection(MisalignedCollection([]))


def test_real_chromadb_repair_path_preserves_review_cases(
    tmp_path,
):
    from mempalace.palace import (
        get_collection,
    )

    palace_path = str(tmp_path / "palace")
    collection = get_collection(palace_path)

    originals = {
        "clean-spanish": ("La canción «PERÚ» abre el disco."),
        "clean-scientific": ("Buried surface area 1250 Å²."),
        "clean-trademark": ("CAFÉ® is a registered mark."),
        "clean-french": ("RÉSUMÉ\u00a0: présentation générale."),
        "damaged-accent": ("EspaÃ±a y cafÃ©."),
        "damaged-punctuation": ("Plan â†’ result â€” done."),
    }

    expected = dict(originals)
    expected["damaged-accent"] = "España y café."
    expected["damaged-punctuation"] = "Plan → result — done."

    collection.upsert(
        ids=list(originals),
        documents=list(originals.values()),
    )

    changed_ids = []

    dry_run = repair_collection(
        collection,
        apply=False,
        page_size=2,
        on_change=(lambda drawer_id, _before, _after: changed_ids.append(drawer_id)),
    )

    assert dry_run["changed"] == 2
    assert set(changed_ids) == {
        "damaged-accent",
        "damaged-punctuation",
    }

    backup = tmp_path / "originals.jsonl"

    applied = repair_collection(
        collection,
        apply=True,
        page_size=2,
        backup_path=backup,
    )

    assert applied["updated"] == 2

    result = collection.get(
        ids=list(originals),
        include=["documents"],
    )
    by_id = dict(
        zip(
            result["ids"],
            result["documents"],
        )
    )

    assert by_id == expected

    restored = restore_collection(
        collection,
        backup,
        batch_size=1,
    )

    assert restored == {
        "validated": 2,
        "restored": 2,
    }

    result = collection.get(
        ids=list(originals),
        include=["documents"],
    )
    by_id = dict(
        zip(
            result["ids"],
            result["documents"],
        )
    )

    assert by_id == originals


def test_backup_header_resolves_wrapped_chroma_collection_name(
    tmp_path,
):
    from mempalace.backends.chroma import (
        ChromaCollection,
    )

    raw = FakeCollection(["cafÃ©"])
    raw.name = "mempalace_drawers"

    wrapped = ChromaCollection(raw)
    backup = tmp_path / "wrapped-backup.jsonl"

    report = repair_collection(
        wrapped,
        apply=True,
        backup_path=backup,
    )

    lines = [
        json.loads(line) for line in backup.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    assert report["updated"] == 1
    assert lines[0] == {
        "collection": "mempalace_drawers",
        "format": "mempalace-encoding-repair",
        "version": 1,
    }
    assert lines[1] == {
        "id": "drawer-0",
        "original_document": "cafÃ©",
    }


def test_restore_rejects_backup_for_another_collection(
    tmp_path,
):
    backup = tmp_path / "wrong-collection.jsonl"
    backup.write_text(
        (
            '{"collection":"source_collection",'
            '"format":"mempalace-encoding-repair",'
            '"version":1}\n'
            '{"id":"drawer-0",'
            '"original_document":"cafÃ©"}\n'
        ),
        encoding="utf-8",
    )

    collection = FakeCollection(["café"])
    collection.name = "different_collection"

    with pytest.raises(
        ValueError,
        match="source_collection",
    ):
        restore_collection(
            collection,
            backup,
        )

    assert collection.updates == []


UNDEFINED_CP1252_CONTINUATION_CASES = [
    (
        "Ã\x81",
        "Á",
    ),
    (
        "Ã\x8d",
        "Í",
    ),
    (
        "Ã\x8f",
        "Ï",
    ),
    (
        "Ã\x90",
        "Ð",
    ),
    (
        "Ã\x9d",
        "Ý",
    ),
    (
        ("Ã\x81LVARO vive en PARÃ\x8dS. Ã\x8dNDICE: pÃ¡gina 12."),
        ("ÁLVARO vive en PARÍS. ÍNDICE: página 12."),
    ),
    (
        "Dijo â€œholaâ€\x9d y se fue.",
        "Dijo “hola” y se fue.",
    ),
]


def _undefined_cp1252_review_rows():
    originals = {
        "spanish-controls": ("Ã\x81LVARO vive en PARÃ\x8dS. Ã\x8dNDICE: pÃ¡gina 12."),
        "all-five-controls": ("Valores: Ã\x81 Ã\x8d Ã\x8f Ã\x90 Ã\x9d."),
        "curly-quotes": ("Dijo â€œholaâ€\x9d y se fue."),
        "mixed-damage": ("Texto mixto: cafÃ©, flecha â†’ y PARÃ\x8dS."),
    }

    expected = {
        "spanish-controls": ("ÁLVARO vive en PARÍS. ÍNDICE: página 12."),
        "all-five-controls": ("Valores: Á Í Ï Ð Ý."),
        "curly-quotes": ("Dijo “hola” y se fue."),
        "mixed-damage": ("Texto mixto: café, flecha → y PARÍS."),
    }

    return originals, expected


@pytest.mark.parametrize(
    (
        "damaged",
        "expected",
    ),
    UNDEFINED_CP1252_CONTINUATION_CASES,
)
def test_repairs_undefined_cp1252_continuation_bytes(
    damaged,
    expected,
):
    assert repair_mojibake(damaged) == expected


@pytest.mark.parametrize(
    "text",
    [
        ("ÁLVARO vive en PARÍS. ÍNDICE: página 12."),
        "Dijo “hola” y se fue.",
    ],
)
def test_clean_undefined_cp1252_outputs_remain_unchanged(
    text,
):
    assert repair_mojibake(text) == text


def test_apply_completes_undefined_cp1252_rows_in_one_pass(
    tmp_path,
):
    originals, expected = _undefined_cp1252_review_rows()
    collection = FakeCollection(list(originals.values()))
    backup = tmp_path / "undefined-controls.jsonl"

    applied = repair_collection(
        collection,
        apply=True,
        page_size=2,
        backup_path=backup,
    )

    assert applied["scanned"] == 4
    assert applied["changed"] == 4
    assert applied["updated"] == 4
    assert collection.documents == list(expected.values())

    second_run = repair_collection(
        collection,
        apply=False,
        page_size=2,
    )

    assert second_run["scanned"] == 4
    assert second_run["changed"] == 0
    assert second_run["updated"] == 0

    undefined_controls = {
        0x81,
        0x8D,
        0x8F,
        0x90,
        0x9D,
    }

    assert all(
        not any(ord(character) in undefined_controls for character in document)
        for document in collection.documents
    )


def test_real_chromadb_completes_undefined_cp1252_rows_in_one_pass(
    tmp_path,
):
    from mempalace.palace import (
        get_backend_for_palace,
        get_collection,
    )

    palace_path = str(tmp_path / "palace")
    originals, expected = _undefined_cp1252_review_rows()

    try:
        collection = get_collection(palace_path)

        collection.upsert(
            ids=list(originals),
            documents=list(originals.values()),
        )

        changed_ids = []

        dry_run = repair_collection(
            collection,
            apply=False,
            page_size=2,
            on_change=(lambda drawer_id, _before, _after: changed_ids.append(drawer_id)),
        )

        assert dry_run["scanned"] == 4
        assert dry_run["changed"] == 4
        assert dry_run["updated"] == 0
        assert set(changed_ids) == set(originals)

        backup = tmp_path / "real-undefined-controls.jsonl"

        applied = repair_collection(
            collection,
            apply=True,
            page_size=2,
            backup_path=backup,
        )

        assert applied["scanned"] == 4
        assert applied["changed"] == 4
        assert applied["updated"] == 4

        result = collection.get(
            ids=list(originals),
            include=["documents"],
        )
        by_id = dict(
            zip(
                result["ids"],
                result["documents"],
            )
        )

        assert by_id == expected

        second_run = repair_collection(
            collection,
            apply=False,
            page_size=2,
        )

        assert second_run["scanned"] == 4
        assert second_run["changed"] == 0
        assert second_run["updated"] == 0
    finally:
        try:
            backend = get_backend_for_palace(palace_path)
            close_palace = getattr(
                backend,
                "close_palace",
                None,
            )

            if callable(close_palace):
                close_palace(palace_path)
        except Exception:
            pass
