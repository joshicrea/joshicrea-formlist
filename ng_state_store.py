"""Isolated NG/state transaction store prototype.

This module is intentionally not wired into the production send path.  It
requires an explicit SQLite path so tests can exercise the transaction model
without reading or migrating the real NG list or the real send-state database.
"""

import secrets
import sqlite3
import time
import re
from pathlib import Path

import filelock
import ng_list
import state
import hashlib


PREPARED = state.PREPARED
CLICKING = "クリック開始"
CANONICAL = "canonical"
SCREEN_SOURCE = "screen"
SCREEN_REASON = "画面で「送らない」"
SCREEN_TOKEN_MARK = " @@ng:"
NG_ACTIVE_INDEX_SQL = (
    "CREATE UNIQUE INDEX idx_ng_active_value "
    "ON ng_entries(kind, value, COALESCE(source, '')) WHERE active=1"
)


class NgMigrationError(RuntimeError):
    pass


class NgStateStore:
    def __init__(self, db_path):
        if not db_path:
            raise ValueError("db_path is required; production defaults are not used")
        self.db_path = Path(db_path)
        if self.db_path.resolve() == Path(state.DB).resolve():
            raise ValueError("production state.DB is not allowed for isolated NG/state tests")
        self.on_before_click_begin = None
        self._schema_ready = False

    def _conn(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            if not self._schema_ready:
                self._ensure_schema(c)
                self._schema_ready = True
            return c
        except Exception:
            c.close()
            raise

    def _ng_active_index_current(self, conn):
        index_rows = conn.execute("PRAGMA index_list('ng_entries')").fetchall()
        index_row = next((row for row in index_rows if row[1] == "idx_ng_active_value"), None)
        if not index_row or int(index_row[2]) != 1 or int(index_row[4]) != 1:
            return False
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_ng_active_value'"
        ).fetchone()
        if not row or not row[0]:
            return False
        normalized = re.sub(r"\s+", " ", row[0].lower()).replace('"', "").strip()
        return normalized == (
            "create unique index idx_ng_active_value "
            "on ng_entries(kind, value, coalesce(source, '')) where active=1"
        )

    def _ensure_schema(self, c):
        begun = False
        try:
            c.execute("BEGIN IMMEDIATE")
            begun = True
            self._ensure_schema_locked(c)
            c.execute("COMMIT")
        except Exception:
            if begun:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _ensure_schema_locked(self, c):
        c.execute(
            """CREATE TABLE IF NOT EXISTS sends (
                key TEXT PRIMARY KEY,
                url TEXT,
                company TEXT,
                state TEXT,
                memo TEXT,
                created REAL,
                updated REAL,
                owner TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS ng_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('domain', 'form')),
                value TEXT NOT NULL,
                reason TEXT,
                company TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created REAL,
                updated REAL,
                display_line TEXT DEFAULT '',
                source TEXT DEFAULT '',
                nonce TEXT DEFAULT ''
            )"""
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(ng_entries)")}
        for col in ("reason", "company", "display_line", "source"):
            if col not in cols:
                c.execute(f"ALTER TABLE ng_entries ADD COLUMN {col} TEXT DEFAULT ''")
        cols = {r[1] for r in c.execute("PRAGMA table_info(ng_entries)")}
        if "generation" not in cols:
            c.execute("ALTER TABLE ng_entries ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
        if "nonce" not in cols:
            c.execute("ALTER TABLE ng_entries ADD COLUMN nonce TEXT DEFAULT ''")
        if not self._ng_active_index_current(c):
            c.execute("DROP INDEX IF EXISTS idx_ng_active_value")
            c.execute(NG_ACTIVE_INDEX_SQL)
        c.execute(
            """CREATE TABLE IF NOT EXISTS migration_control (
                name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated REAL,
                source_path TEXT DEFAULT '',
                backup_path TEXT DEFAULT '',
                snapshot_sha256 TEXT DEFAULT ''
            )"""
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(migration_control)")}
        for col in ("source_path", "backup_path", "snapshot_sha256"):
            if col not in cols:
                c.execute(f"ALTER TABLE migration_control ADD COLUMN {col} TEXT DEFAULT ''")

    def migration_status(self):
        row = self.migration_row()
        return row["status"] if row else "unknown"

    def migration_row(self):
        c = self._conn()
        try:
            row = c.execute(
                """SELECT status, source_path, backup_path, snapshot_sha256
                   FROM migration_control WHERE name='ng_canonical'"""
            ).fetchone()
            if not row:
                return None
            return {
                "status": row[0],
                "source_path": row[1] or "",
                "backup_path": row[2] or "",
                "snapshot_sha256": row[3] or "",
            }
        finally:
            c.close()

    def set_migration_status(self, status, source_path=None, backup_path=None, snapshot_sha256=None):
        c = self._conn()
        try:
            prev = c.execute(
                """SELECT source_path, backup_path, snapshot_sha256
                   FROM migration_control WHERE name='ng_canonical'"""
            ).fetchone()
            source_path = str(source_path) if source_path is not None else (prev[0] if prev else "")
            backup_path = str(backup_path) if backup_path is not None else (prev[1] if prev else "")
            snapshot_sha256 = snapshot_sha256 if snapshot_sha256 is not None else (prev[2] if prev else "")
            c.execute(
                """INSERT OR REPLACE INTO migration_control
                   (name, status, updated, source_path, backup_path, snapshot_sha256)
                   VALUES ('ng_canonical', ?, ?, ?, ?, ?)""",
                (status, time.time(), source_path, backup_path, snapshot_sha256),
            )
        finally:
            c.close()

    def migrate_ng_txt(self, src, backup, after_backup=None, fail_after_rows=None, fail_before_canonical=False,
                       crash_after_start=False):
        src = Path(src)
        backup = Path(backup)
        with filelock.locked(self.db_path.with_name(self.db_path.name + ".migration")):
            if self.migration_status() == CANONICAL:
                return {"imported": 0, "duplicates": 0, "errors": 0, "skipped": "already_canonical"}
            self.set_migration_status("migrating")
            if crash_after_start:
                raise SystemExit("injected migration crash after durable migrating status")
            with filelock.locked(src):
                try:
                    if not src.exists():
                        raise NgMigrationError("legacy NG list is missing")
                    snapshot = src.read_bytes()
                    snapshot_sha = self._sha256(snapshot)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if backup.exists():
                        raise NgMigrationError("migration backup already exists")
                    backup.write_bytes(snapshot)
                    self.set_migration_status(
                        "migrating",
                        self._stable_path(src),
                        self._stable_path(backup),
                        snapshot_sha,
                    )
                except Exception:
                    self.set_migration_status("failed")
                    raise
                if after_backup:
                    after_backup()
                parsed, errors, duplicates = self._parse_legacy_ng_snapshot(snapshot)
                if errors:
                    self.set_migration_status("failed")
                    raise NgMigrationError("legacy NG list contains unparseable rows")
                if not src.exists():
                    self.set_migration_status("failed")
                    raise NgMigrationError("legacy NG list was deleted during migration")
                if src.read_bytes() != snapshot:
                    self.set_migration_status("failed")
                    raise NgMigrationError("legacy NG list changed during migration")

                return self._commit_parsed_migration(parsed, duplicates, fail_after_rows, fail_before_canonical)

    def resume_failed_migration(self, src, backup, fail_after_rows=None, fail_before_canonical=False,
                                after_validate=None):
        src = Path(src)
        backup = Path(backup)
        with filelock.locked(self.db_path.with_name(self.db_path.name + ".migration")):
            row = self.migration_row()
            if row and row["status"] == CANONICAL:
                return {"imported": 0, "duplicates": 0, "errors": 0, "skipped": "already_canonical"}
            if not row or row["status"] != "failed":
                raise NgMigrationError("migration is not in failed state")
            if row["source_path"] != self._stable_path(src) or row["backup_path"] != self._stable_path(backup):
                raise NgMigrationError("migration source or backup does not match failed run")
            if not row["snapshot_sha256"] or not src.exists() or not backup.exists():
                raise NgMigrationError("failed migration snapshot is not resumable")
            snapshot = backup.read_bytes()
            if self._sha256(snapshot) != row["snapshot_sha256"]:
                raise NgMigrationError("backup snapshot does not match failed run")
            if src.read_bytes() != snapshot:
                raise NgMigrationError("source no longer matches failed backup snapshot")
            parsed, errors, duplicates = self._parse_legacy_ng_snapshot(snapshot)
            if errors:
                raise NgMigrationError("legacy NG list contains unparseable rows")
            self.set_migration_status("migrating")
            if after_validate:
                after_validate()
            return self._commit_parsed_migration(parsed, duplicates, fail_after_rows, fail_before_canonical)

    def _commit_parsed_migration(self, parsed, duplicates, fail_after_rows=None, fail_before_canonical=False):
        c = self._conn()
        inserted = 0
        try:
            c.execute("BEGIN IMMEDIATE")
            self._set_migration_status_locked(c, "migrating")
            for kind, value, reason, company, display_line in parsed:
                row = c.execute(
                    "SELECT id FROM ng_entries WHERE kind=? AND value=?",
                    (kind, value),
                ).fetchone()
                if not row:
                    now = time.time()
                    c.execute(
                        """INSERT INTO ng_entries
                           (kind, value, reason, company, active, created, updated, display_line, source)
                           VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'legacy_txt')""",
                        (kind, value, reason, company, now, now, display_line),
                    )
                    inserted += 1
                if fail_after_rows is not None and inserted >= fail_after_rows:
                    raise NgMigrationError("injected migration failure after rows")
            if fail_before_canonical:
                raise NgMigrationError("injected migration failure before canonical")
            self._set_migration_status_locked(c, CANONICAL)
            c.execute("COMMIT")
            return {"imported": inserted, "duplicates": duplicates, "errors": 0}
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self.set_migration_status("failed")
            raise
        finally:
            c.close()

    def _set_migration_status_locked(self, conn, status):
        conn.execute(
            """UPDATE migration_control SET status=?, updated=?
               WHERE name='ng_canonical'""",
            (status, time.time()),
        )

    def legacy_or_db_add_line(self, src, line):
        src = Path(src)
        with filelock.locked(src):
            status = self.migration_status()
            if status == CANONICAL:
                kind, value, reason, company, display_line = self._parse_legacy_line(line)
                if kind == "form":
                    return bool(self._add_ng("form", value, reason, company, display_line=display_line, source="legacy_writer"))
                return bool(self._add_ng("domain", value, reason, company, display_line=display_line, source="legacy_writer"))
            if status == "legacy_compat":
                src.parent.mkdir(parents=True, exist_ok=True)
                with open(src, "a", encoding="utf-8-sig") as f:
                    f.write(line.rstrip() + "\n")
                return True
            return False

    def sync_legacy_txt(self, src):
        """引っ越し(canonical)のあと、テキストNGに増えた行(送信後に見つかったお断り等)をDBへ足す。DBに有効な行が無いものだけ足す"""
        src = Path(src)
        if self.migration_status() != CANONICAL or not src.exists():
            return {"imported": 0, "errors": 0}
        with filelock.locked(src):
            snapshot = src.read_bytes()
        parsed, errors, _dup = self._parse_legacy_ng_snapshot(snapshot)
        c = self._conn()
        try:
            known = {(k, v) for k, v in c.execute("SELECT kind, value FROM ng_entries WHERE active=1")}
        finally:
            c.close()
        imported = 0
        for kind, value, reason, company, display_line in parsed:
            if (kind, value) in known:
                continue
            if self._add_ng(kind, value, reason, company, display_line=display_line, source="legacy_writer"):
                imported += 1
        return {"imported": imported, "errors": len(errors)}

    def _parse_legacy_ng_snapshot(self, snapshot):
        parsed = []
        seen = set()
        errors = []
        duplicates = 0
        text = snapshot.decode("utf-8-sig")
        for n, raw in enumerate(text.splitlines(), 1):
            if not raw.split("#")[0].strip():
                continue
            try:
                kind, value, reason, company, display_line = self._parse_legacy_line(raw)
            except ValueError:
                errors.append(n)
                continue
            key = (kind, value)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            parsed.append((kind, value, reason, company, display_line))
        return parsed, errors, duplicates

    def _parse_legacy_line(self, raw):
        item = raw.split("#")[0].strip()
        if item.startswith("@"):
            kind = "form"
            value = ng_list.norm_url(item[1:])
        else:
            kind = "domain"
            value = ng_list.domain(item.lower()) or item.lower()
        if not value or "." not in value or any(ch.isspace() for ch in value):
            raise ValueError("unparseable NG row")
        reason, company = self._parse_legacy_comment(raw)
        return kind, value, reason, company, raw.strip()

    def _parse_legacy_comment(self, raw):
        comment = raw.split("#", 1)[1].strip() if "#" in raw else ""
        reason = comment
        company = ""
        for part in (p.strip() for p in comment.split(";")):
            if part.startswith("reason="):
                reason = part.split("=", 1)[1].strip()
            elif part.startswith("company="):
                company = part.split("=", 1)[1].strip()
        return reason, company

    def add_domain_ng(self, url, reason="", company=""):
        value = ng_list.domain(url)
        if not value:
            return None
        return self._add_ng("domain", value, reason, company)

    def add_form_ng(self, url, reason="", company=""):
        value = ng_list.norm_url(url)
        if not value:
            return None
        return self._add_ng("form", value, reason, company)

    def add_screen_domain_ng(self, url, company=""):
        value = ng_list.domain(url)
        if not value:
            return None
        return self._add_ng("domain", value, SCREEN_REASON, company, source=SCREEN_SOURCE, nonce=secrets.token_urlsafe(16))

    def add_screen_form_ng(self, url, company=""):
        value = ng_list.norm_url(url)
        if not value:
            return None
        return self._add_ng("form", value, SCREEN_REASON, company, source=SCREEN_SOURCE, nonce=secrets.token_urlsafe(16))

    def _add_ng(self, kind, value, reason, company, display_line="", source="", nonce=""):
        now = time.time()
        c = self._conn()
        begun = False
        try:
            c.execute("BEGIN IMMEDIATE")
            begun = True
            rows = c.execute(
                "SELECT id, source FROM ng_entries WHERE kind=? AND value=? AND active=1 ORDER BY id",
                (kind, value),
            ).fetchall()
            same_source = next((row for row in rows if (row[1] or "") == (source or "")), None)
            if same_source:
                c.execute("COMMIT")
                return same_source[0]
            if not source:
                non_screen = next((row for row in rows if (row[1] or "") != SCREEN_SOURCE), None)
                if non_screen:
                    c.execute("COMMIT")
                    return non_screen[0]
            if source == SCREEN_SOURCE and rows:
                c.execute("COMMIT")
                return rows[0][0]
            cur = c.execute(
                """INSERT INTO ng_entries
                   (kind, value, reason, company, active, created, updated, display_line, source, nonce)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                (kind, value, reason, company, now, now, display_line, source, nonce),
            )
            c.execute("COMMIT")
            return cur.lastrowid
        except sqlite3.Error:
            if begun:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            c.close()

    def screen_ng_lines(self):
        c = self._conn()
        try:
            rows = c.execute(
                """SELECT id, kind, value, reason, company, generation, nonce
                   FROM ng_entries
                   WHERE active=1 AND source=?
                   ORDER BY id""",
                (SCREEN_SOURCE,),
            ).fetchall()
            return [self._screen_line(*row) for row in rows]
        finally:
            c.close()

    def undo_screen_ng_line(self, line):
        token = self._screen_token_from_line(line)
        if token is None:
            return "stale"
        entry_id, generation, nonce = token
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT active, source, generation, nonce, kind, value, reason, company FROM ng_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return "stale"
            active, source, current_generation, current_nonce, kind, value, reason, company = row
            if int(active) != 1:
                c.execute("ROLLBACK")
                return "inactive"
            if source != SCREEN_SOURCE:
                c.execute("ROLLBACK")
                return "source"
            if int(current_generation) != generation or current_nonce != nonce:
                c.execute("ROLLBACK")
                return "stale"
            if self._screen_line(entry_id, kind, value, reason, company, current_generation, current_nonce) != str(line or "").strip():
                c.execute("ROLLBACK")
                return "stale"
            n = c.execute(
                """UPDATE ng_entries
                   SET active=0, updated=?
                   WHERE id=? AND active=1 AND source=? AND generation=? AND nonce=?""",
                (time.time(), entry_id, SCREEN_SOURCE, generation, nonce),
            ).rowcount
            c.execute("COMMIT")
            return "ok" if n == 1 else "stale"
        except sqlite3.Error:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return "stale"
        finally:
            c.close()

    def _screen_line(self, entry_id, kind, value, reason, company, generation, nonce):
        head = ("@" if kind == "form" else "") + value
        comment = " ".join(x for x in (reason or SCREEN_REASON, company or "") if x)
        return f"{head}  # {comment}{SCREEN_TOKEN_MARK}{entry_id}:{generation}:{nonce}"

    def _screen_token_from_line(self, line):
        marker = SCREEN_TOKEN_MARK
        raw = str(line or "")
        if marker not in raw:
            return None
        token = raw.rsplit(marker, 1)[1].strip()
        try:
            entry_id, generation, nonce = token.split(":", 2)
            entry_id, generation = int(entry_id), int(generation)
        except (TypeError, ValueError):
            return None
        if entry_id < 1 or generation < 1 or not nonce:
            return None
        return entry_id, generation, nonce


    def undo_ng(self, entry_id):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute(
                "UPDATE ng_entries SET active=0, updated=? WHERE id=? AND active=1",
                (time.time(), entry_id),
            ).rowcount
            c.execute("COMMIT")
            return n == 1
        except sqlite3.Error:
            c.execute("ROLLBACK")
            return False
        finally:
            c.close()

    def is_ng(self, urls):
        keys = self._candidate_keys(urls)
        if not keys[0] and not keys[1]:
            return False
        c = self._conn()
        try:
            for kind, values in (("domain", keys[0]), ("form", keys[1])):
                for value in values:
                    if c.execute(
                        "SELECT 1 FROM ng_entries WHERE kind=? AND value=? AND active=1",
                        (kind, value),
                    ).fetchone():
                        return True
            return False
        finally:
            c.close()

    def reserve(self, urls, company=""):
        return self.reserve_for_click(urls, company)

    def reserve_send(self, urls, company=""):
        keys = state.keys_of(urls)
        if not keys:
            return None, ("スキップ", "このフォームは別の実行が送信中か送信済み(送らない)")
        token = secrets.token_hex(8)
        now = time.time()
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            if self._is_ng_locked(c, urls):
                c.execute("ROLLBACK")
                return None, ("要手動", "NGリストに該当したため送らない")
            for key in keys:
                row = c.execute("SELECT state FROM sends WHERE key=?", (key,)).fetchone()
                if row and row[0] != PREPARED:
                    c.execute("ROLLBACK")
                    return None, ("スキップ", "確保が別実行へ引き継がれたため送らない")
            for key in keys:
                c.execute(
                    "INSERT OR REPLACE INTO sends VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, key, company, PREPARED, "", now, now, token),
                )
            c.execute("COMMIT")
            return token, None
        except sqlite3.Error:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def start_click(self, urls, token):
        return self.start_click_if_not_ng(urls, token)

    def start_click_send(self, urls, token):
        keys = state.keys_of(urls)
        if not keys or not token:
            return False, ("スキップ", "確保が別実行へ引き継がれたため送らない")
        c = self._conn()
        try:
            if self.on_before_click_begin:
                self.on_before_click_begin()
            c.execute("BEGIN IMMEDIATE")
            status = self._migration_status_locked(c)
            if status != CANONICAL:
                c.execute("ROLLBACK")
                return False, ("スキップ", "NG移行状態がcanonicalではないため送らない: " + status)
            if self._is_ng_locked(c, urls):
                c.execute("ROLLBACK")
                return False, ("要手動", "NGリストに該当したため送らない")
            changed = 0
            for key in keys:
                changed += c.execute(
                    "UPDATE sends SET state=?, updated=? WHERE key=? AND state=? AND owner=?",
                    (CLICKING, time.time(), key, PREPARED, token),
                ).rowcount
            if changed != len(keys):
                c.execute("ROLLBACK")
                return False, ("スキップ", "確保が別実行へ引き継がれたため送らない")
            c.execute("COMMIT")
            return True, None
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False, ("スキップ", "NG移行状態を確認できないため送らない: " + str(e)[:80])
        finally:
            c.close()

    def mark(self, urls, result_state, memo="", token=None):
        keys = state.keys_of(urls)
        if not keys:
            return
        c = self._conn()
        try:
            for key in keys:
                if token:
                    c.execute(
                        "UPDATE sends SET state=?, memo=?, updated=? WHERE key=? AND owner=?",
                        (result_state, memo[:200], time.time(), key, token),
                    )
                else:
                    c.execute(
                        "UPDATE sends SET state=?, memo=?, updated=? WHERE key=?",
                        (result_state, memo[:200], time.time(), key),
                    )
        finally:
            c.close()

    def send_gate_refusal_reason(self, urls, token=None):
        try:
            c = self._conn()
            try:
                status = self._migration_status_locked(c)
            finally:
                c.close()
        except sqlite3.Error as e:
            return "migration", "NG移行状態を確認できないため送らない: " + str(e)[:80]
        if status != CANONICAL:
            return "migration", "NG移行状態がcanonicalではないため送らない: " + status
        if self.is_ng(urls):
            return "ng", "NGリストに該当したため送らない"
        return "handoff", "確保が別実行へ引き継がれたため送らない"
    def reserve_for_click(self, urls, company=""):
        keys = state.keys_of(urls)
        if not keys:
            return None
        token = secrets.token_hex(8)
        now = time.time()
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            if self._is_ng_locked(c, urls):
                c.execute("ROLLBACK")
                return None
            for key in keys:
                row = c.execute("SELECT state FROM sends WHERE key=?", (key,)).fetchone()
                if row and row[0] != PREPARED:
                    c.execute("ROLLBACK")
                    return None
            for key in keys:
                c.execute(
                    "INSERT OR REPLACE INTO sends VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, key, company, PREPARED, "", now, now, token),
                )
            c.execute("COMMIT")
            return token
        except sqlite3.Error:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def start_click_if_not_ng(self, urls, token):
        keys = state.keys_of(urls)
        if not keys or not token:
            return False
        c = self._conn()
        try:
            if self.on_before_click_begin:
                self.on_before_click_begin()
            c.execute("BEGIN IMMEDIATE")
            if self._migration_status_locked(c) != CANONICAL:
                c.execute("ROLLBACK")
                return False
            if self._is_ng_locked(c, urls):
                c.execute("ROLLBACK")
                return False
            changed = 0
            for key in keys:
                changed += c.execute(
                    "UPDATE sends SET state=?, updated=? "
                    "WHERE key=? AND state=? AND owner=?",
                    (CLICKING, time.time(), key, PREPARED, token),
                ).rowcount
            if changed != len(keys):
                c.execute("ROLLBACK")
                return False
            c.execute("COMMIT")
            return True
        except sqlite3.Error:
            c.execute("ROLLBACK")
            return False
        finally:
            c.close()

    def _migration_status_locked(self, conn):
        row = conn.execute(
            "SELECT status FROM migration_control WHERE name='ng_canonical'"
        ).fetchone()
        return row[0] if row else "unknown"

    def _sha256(self, data):
        return hashlib.sha256(data).hexdigest()

    def _stable_path(self, path):
        return str(Path(path).resolve())

    def _candidate_keys(self, urls):
        domains = {ng_list.domain(url) for url in urls if url}
        forms = {ng_list.norm_url(url) for url in urls if url}
        return {d for d in domains if d}, {f for f in forms if f}

    def _is_ng_locked(self, conn, urls):
        domains, forms = self._candidate_keys(urls)
        for kind, values in (("domain", domains), ("form", forms)):
            for value in values:
                if conn.execute(
                    "SELECT 1 FROM ng_entries WHERE kind=? AND value=? AND active=1",
                    (kind, value),
                ).fetchone():
                    return True
        return False

