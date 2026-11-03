import sqlite3
import time
from pathlib import Path

ITEM_COLUMNS = {
    # column -> type, added by migration for DBs created by older versions
    "batch": "INTEGER",
    "src_chat_id": "INTEGER",
    "src_message_id": "INTEGER",
    "forwarded": "INTEGER NOT NULL DEFAULT 0",
    "media": "TEXT",
    "file_id": "TEXT",
    "report_message_id": "INTEGER",  # bot's "✅ Saved" reply in private chat
}


class DB:
    REQUIRED_TABLES = {"settings", "topics", "items"}

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS topics (
                name      TEXT PRIMARY KEY COLLATE NOCASE,
                thread_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic      TEXT NOT NULL,
                thread_id  INTEGER,
                message_id INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                text       TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_topic ON items(topic);
            """
        )
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        for col, typ in ITEM_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
        self.conn.execute("UPDATE items SET batch = id WHERE batch IS NULL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_src ON items(src_chat_id, src_message_id)")
        self.conn.commit()

    # ---------- settings ----------
    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self.conn.commit()

    # ---------- topics ----------
    def topic(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM topics WHERE name=?", (name,)).fetchone()

    def topic_by_thread(self, thread_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM topics WHERE thread_id=?", (thread_id,)).fetchone()

    def topics(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT t.name, t.thread_id, COUNT(i.id) AS n FROM topics t "
            "LEFT JOIN items i ON i.thread_id = t.thread_id "
            "GROUP BY t.thread_id ORDER BY t.name COLLATE NOCASE"
        ).fetchall()

    def save_topic(self, name: str, thread_id: int) -> None:
        # one name per thread: drop stale names pointing at the same thread
        self.conn.execute("DELETE FROM topics WHERE thread_id=? AND name<>?", (thread_id, name))
        self.conn.execute(
            "INSERT INTO topics(name, thread_id) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET thread_id=excluded.thread_id",
            (name, thread_id),
        )
        self.conn.commit()

    def rename_topic(self, thread_id: int, new_name: str) -> None:
        self.save_topic(new_name, thread_id)
        self.conn.execute("UPDATE items SET topic=? WHERE thread_id=?", (new_name, thread_id))
        self.conn.commit()

    def delete_topic(self, name: str) -> None:
        self.conn.execute("DELETE FROM topics WHERE name=?", (name,))
        self.conn.commit()

    def delete_topic_with_items(self, thread_id: int) -> None:
        self.conn.execute("DELETE FROM items WHERE thread_id=?", (thread_id,))
        self.conn.execute("DELETE FROM topics WHERE thread_id=?", (thread_id,))
        self.conn.commit()

    def clear_topics(self) -> None:
        self.conn.execute("DELETE FROM topics")
        self.conn.commit()

    # ---------- items ----------
    def add_items(self, topic: str, thread_id: int | None, rows: list[dict]) -> int:
        """Insert related messages (album = one batch). Returns batch id."""
        now = int(time.time())
        batch = None
        for r in rows:
            cur = self.conn.execute(
                "INSERT INTO items(topic, thread_id, message_id, kind, text, created_at, batch, "
                "src_chat_id, src_message_id, forwarded, media, file_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (topic, thread_id, r["message_id"], r["kind"], r.get("text"), now, batch,
                 r.get("src_chat_id"), r.get("src_message_id"), int(r.get("forwarded", False)),
                 r.get("media"), r.get("file_id")),
            )
            if batch is None:
                batch = cur.lastrowid
                self.conn.execute("UPDATE items SET batch=? WHERE id=?", (batch, batch))
        self.conn.commit()
        return batch

    def batch_items(self, batch: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE batch=? ORDER BY message_id", (batch,)).fetchall()

    def batch_by_src(self, chat_id: int, message_id: int) -> int | None:
        """Find batch by the user's original private message or by the bot's report reply."""
        row = self.conn.execute(
            "SELECT batch FROM items WHERE src_chat_id=? AND (src_message_id=? OR report_message_id=?) "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, message_id, message_id),
        ).fetchone()
        return row["batch"] if row else None

    def last_batch(self, chat_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT batch FROM items WHERE src_chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
        ).fetchone()
        return row["batch"] if row else None

    def set_report(self, batch: int, message_id: int) -> None:
        self.conn.execute("UPDATE items SET report_message_id=? WHERE batch=?", (message_id, batch))
        self.conn.commit()

    def move_batch(self, batch: int, topic: str, thread_id: int, new_message_ids: list[int]) -> None:
        items = self.batch_items(batch)
        for item, new_id in zip(items, new_message_ids):
            self.conn.execute(
                "UPDATE items SET topic=?, thread_id=?, message_id=? WHERE id=?",
                (topic, thread_id, new_id, item["id"]),
            )
        self.conn.commit()

    def delete_batch(self, batch: int) -> None:
        self.conn.execute("DELETE FROM items WHERE batch=?", (batch,))
        self.conn.commit()

    def search(self, query: str, limit: int = 20, offset: int = 0, with_file: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM items WHERE (text LIKE ? OR topic LIKE ?)"
        if with_file:
            sql += " AND (file_id IS NOT NULL OR (kind IN ('text','link') AND text IS NOT NULL))"
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return self.conn.execute(sql, (f"%{query}%", f"%{query}%", limit, offset)).fetchall()

    def recent(self, limit: int = 10, topic: str | None = None) -> list[sqlite3.Row]:
        if topic:
            return self.conn.execute(
                "SELECT * FROM items WHERE topic=? COLLATE NOCASE ORDER BY id DESC LIMIT ?", (topic, limit)
            ).fetchall()
        return self.conn.execute("SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def random_item(self, topic: str | None = None) -> sqlite3.Row | None:
        if topic:
            return self.conn.execute(
                "SELECT * FROM items WHERE topic=? COLLATE NOCASE ORDER BY RANDOM() LIMIT 1", (topic,)
            ).fetchone()
        return self.conn.execute("SELECT * FROM items ORDER BY RANDOM() LIMIT 1").fetchone()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def stats(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT kind, COUNT(*) AS n FROM items GROUP BY kind ORDER BY n DESC").fetchall()

    # ---------- backup / restore ----------
    def backup_to(self, path: str | Path) -> None:
        """Consistent snapshot of the live DB into `path`."""
        dst = sqlite3.connect(path)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()

    @classmethod
    def validate(cls, path: str | Path) -> int:
        """Raise ValueError if `path` is not a valid storagebot DB. Returns item count."""
        try:
            src = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
            try:
                if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("integrity check failed")
                tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                missing = cls.REQUIRED_TABLES - tables
                if missing:
                    raise ValueError(f"missing tables: {', '.join(sorted(missing))}")
                return src.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                src.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"not a SQLite database ({e})") from e

    def restore_from(self, path: str | Path) -> None:
        """Replace live DB contents with `path` (in place), then upgrade schema if old."""
        src = sqlite3.connect(path)
        try:
            src.backup(self.conn)
        finally:
            src.close()
        self._migrate()
