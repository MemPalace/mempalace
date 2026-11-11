"""After Done: Archive, optional carry-on follow-ups and a pinned Pocket of key facts.

Done is final: a card that is done can be archived, carried on or have facts
pinned to it. Archive hides, it never deletes: an archived card stays readable
and restorable, and nothing leaves storage (rows are flagged, unpinned or
restored, never removed). Only signed-in people (owner or member) act here;
connections cannot, and a card an agent finished stays frozen for agents.
"""

from commonplace.extensions import ToolError

from .store import now, task_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS card_archive (
 task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
 archived INTEGER NOT NULL CHECK(archived IN (0, 1)),
 archived_by TEXT, archived_at TEXT, restored_by TEXT, restored_at TEXT
);
CREATE TABLE IF NOT EXISTS card_followups (
 task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
 origin_id INTEGER NOT NULL REFERENCES tasks(id),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS card_followups_by_origin ON card_followups(origin_id, task_id);
CREATE TABLE IF NOT EXISTS card_pocket (
 id INTEGER PRIMARY KEY,
 task_id INTEGER NOT NULL REFERENCES tasks(id),
 text TEXT NOT NULL,
 pinned_by TEXT NOT NULL, pinned_at TEXT NOT NULL,
 unpinned_by TEXT, unpinned_at TEXT
);
CREATE INDEX IF NOT EXISTS card_pocket_by_task ON card_pocket(task_id, id);
CREATE TRIGGER IF NOT EXISTS card_archive_changed_i AFTER INSERT ON card_archive
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS card_archive_changed_u AFTER UPDATE ON card_archive
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS card_pocket_changed_i AFTER INSERT ON card_pocket
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS card_pocket_changed_u AFTER UPDATE ON card_pocket
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
"""
POCKET_LIMIT = 25
FACT_LIMIT = 2000
ACTIONS = ("archive", "restore", "follow-up", "pocket", "unpocket")


def archived_ids(conn) -> set:
    return {r[0] for r in conn.execute("SELECT task_id FROM card_archive WHERE archived=1")}


def archived_count(conn, board_id) -> int:
    return conn.execute(
        "SELECT count(*) FROM card_archive a JOIN tasks t ON t.id=a.task_id "
        "WHERE a.archived=1 AND t.board_id=?",
        (board_id,),
    ).fetchone()[0]


def is_archived(conn, number) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM card_archive WHERE task_id=? AND archived=1", (number,)
        ).fetchone()
    )


def refuse_if_archived(conn, row):
    if is_archived(conn, row["id"]):
        raise ToolError(f"no: #{row['id']} is archived; restore it first")


def person_only(principal, what):
    if not principal.person:
        raise ToolError(f"no: only a signed-in person can {what}")


def card_fields(conn, row, principal=None):
    """Lifecycle part of the public card: archive state, follow-up links and the Pocket."""
    number = row["id"]
    archive = conn.execute("SELECT * FROM card_archive WHERE task_id=?", (number,)).fetchone()
    archived = bool(archive and archive["archived"])
    origin = conn.execute(
        "SELECT origin_id FROM card_followups WHERE task_id=?", (number,)
    ).fetchone()
    person = bool(principal and principal.person)
    final = row["status"] == "done"
    return {
        "final": final,
        "archived": archived,
        "archived_at": archive["archived_at"] if archived else None,
        "archived_by": archive["archived_by"] if archived else None,
        "follow_up_of": origin[0] if origin else None,
        "follow_ups": [
            r[0]
            for r in conn.execute(
                "SELECT task_id FROM card_followups WHERE origin_id=? ORDER BY task_id", (number,)
            )
        ],
        "pocket": [
            {
                "id": r["id"],
                "text": r["text"],
                "pinned_by": r["pinned_by"],
                "pinned_at": r["pinned_at"],
            }
            for r in conn.execute(
                "SELECT * FROM card_pocket WHERE task_id=? AND unpinned_at IS NULL ORDER BY id",
                (number,),
            )
        ],
        "can_archive": person and final and not archived,
        "can_restore": person and archived,
        "can_follow_up": person and final,
        "can_pocket": person and not archived,
    }


class LifecycleMixin:
    """Service methods; BoardService mixes this in and supplies the transaction and card helpers."""

    def _lifecycle_row(self, conn, task, version, what):
        person_only(self.principal, what)
        row, board = self._get(conn, task)
        self._write(board)
        self._version(conn, row, version)
        return row, board

    def _lifecycle_touch(self, conn, row, text, change, **extra):
        conn.execute(
            "UPDATE tasks SET version=version+1, updated_at=?, updated_by=? WHERE id=?",
            (now(), self.principal.actor, row["id"]),
        )
        self._log(conn, row["id"], "edit", text=text)
        self._emit(conn, row["id"], "card.updated", change=change, **extra)
        return self._card(conn, task_row(conn, row["id"]), self.principal)

    def task_archive(self, task, version=None):
        with self.transaction() as conn:
            row, _board = self._lifecycle_row(conn, task, version, "archive a card")
            if row["status"] != "done":
                raise ToolError("no: only a done card can be archived")
            if is_archived(conn, row["id"]):
                return self._card(conn, row, self.principal)
            conn.execute(
                "INSERT INTO card_archive(task_id, archived, archived_by, archived_at) "
                "VALUES(?,1,?,?) ON CONFLICT(task_id) DO UPDATE SET archived=1, "
                "archived_by=excluded.archived_by, archived_at=excluded.archived_at",
                (row["id"], self.principal.actor, now()),
            )
            return self._lifecycle_touch(conn, row, "Archived", "archived")

    def task_restore(self, task, version=None):
        with self.transaction() as conn:
            row, _board = self._lifecycle_row(conn, task, version, "restore a card")
            if not is_archived(conn, row["id"]):
                return self._card(conn, row, self.principal)
            conn.execute(
                "UPDATE card_archive SET archived=0, restored_by=?, restored_at=? WHERE task_id=?",
                (self.principal.actor, now(), row["id"]),
            )
            return self._lifecycle_touch(conn, row, "Restored from the archive", "restored")

    def task_follow_up(self, task, title=None, body="", version=None):
        """Carry on from a done card: a new backlog card on the same board, linked to the original."""
        with self.transaction() as conn:
            row, board = self._lifecycle_row(conn, task, version, "carry on a card")
            if row["status"] != "done":
                raise ToolError("no: only a done card can be carried on")
            title = f"Carry on: {row['title']}"[:5000] if title is None or title == "" else title
            created = self._new_card(
                conn,
                board,
                title=title,
                body=body if body else f"Carry on from #{row['id']} {row['title']}",
                status="backlog",
            )
            conn.execute(
                "INSERT INTO card_followups(task_id, origin_id, created_by, created_at) "
                "VALUES(?,?,?,?)",
                (created["number"], row["id"], self.principal.actor, now()),
            )
            self._lifecycle_touch(
                conn,
                row,
                f"Carry-on card #{created['number']} created",
                "follow_up",
                follow_up=created["number"],
            )
            return {
                "origin": self._card(conn, task_row(conn, row["id"]), self.principal),
                "card": self._card(conn, task_row(conn, created["number"]), self.principal),
            }

    def pocket_pin(self, task, text, version=None):
        from .service import validate_text

        with self.transaction() as conn:
            row, _board = self._lifecycle_row(conn, task, version, "pin a fact")
            refuse_if_archived(conn, row)
            text = validate_text(text, "fact", required=True, limit=FACT_LIMIT)
            active = [
                r[0]
                for r in conn.execute(
                    "SELECT text FROM card_pocket WHERE task_id=? AND unpinned_at IS NULL",
                    (row["id"],),
                )
            ]
            if text in active:
                raise ToolError("no: that fact is already pinned")
            if len(active) >= POCKET_LIMIT:
                raise ToolError(
                    f"no: the Pocket holds at most {POCKET_LIMIT} facts; unpin one first"
                )
            conn.execute(
                "INSERT INTO card_pocket(task_id, text, pinned_by, pinned_at) VALUES(?,?,?,?)",
                (row["id"], text, self.principal.actor, now()),
            )
            return self._lifecycle_touch(conn, row, "Pinned a fact to the Pocket", "pocket")

    def pocket_unpin(self, task, pin, version=None):
        """Unpinning keeps the row (marked), so the verbatim fact is never lost."""
        with self.transaction() as conn:
            row, _board = self._lifecycle_row(conn, task, version, "unpin a fact")
            refuse_if_archived(conn, row)
            changed = conn.execute(
                "UPDATE card_pocket SET unpinned_by=?, unpinned_at=? "
                "WHERE id=? AND task_id=? AND unpinned_at IS NULL",
                (self.principal.actor, now(), pin if type(pin) is int else -1, row["id"]),
            ).rowcount
            if not changed:
                raise ToolError("no: that fact is not pinned to this card")
            return self._lifecycle_touch(conn, row, "Unpinned a fact from the Pocket", "pocket")
