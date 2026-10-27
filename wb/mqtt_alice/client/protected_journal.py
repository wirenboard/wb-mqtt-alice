"""Opt-in durable reservations. Storage only: never publishes or replays intents.

The existing Alice client does not import this module unless explicitly enabled
by its protected integration. All processes other than the single writer must
use the authenticated client interface, not open another writable connection.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from datetime import datetime, timezone


MAX_RECORDS = 4096
MAX_BYTES = 16 * 1024 * 1024
KINDS = frozenset(("command", "incident", "channel", "tombstone"))
FORBIDDEN = frozenset(("password", "passwd", "token", "access_token", "refresh_token",
                       "authorization", "cookie", "credentials", "secret", "private_key", "oauth"))


class JournalError(Exception):
    pass


class Unavailable(JournalError):
    pass


class Conflict(JournalError):
    pass


class CapacityBlocked(JournalError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def encode(value):
    def private(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) or key.lower() in FORBIDDEN for key in item):
                raise JournalError("Sensitive or invalid journal field")
            for child in item.values():
                private(child)
        elif isinstance(item, list):
            for child in item:
                private(child)
        elif isinstance(item, str):
            if re.search(r"(?i)(?:password|token|authorization|secret)\s*[=:]\s*\S+", item):
                raise JournalError("Sensitive journal value")
            if re.search(r"(?i)(?:https?|mqtts?|ssh)://[^\s/@:]+:[^\s/@]+@|[?&](?:token|password|key|secret)=", item):
                raise JournalError("Sensitive journal URL")
    private(value)
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise JournalError("Invalid JSON journal value") from None
    if len(raw.encode("utf-8")) > 65536:
        raise JournalError("Journal record exceeds limit")
    return raw


class ProtectedJournal:
    def __init__(self, path, *, enabled=False, max_records=MAX_RECORDS, max_bytes=MAX_BYTES):
        self.connection, self.lock_fd, self.faulted = None, None, False
        if enabled is not True:
            raise Unavailable("Protected journal is opt-in")
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS:
            raise JournalError("Invalid record limit")
        if type(max_bytes) is not int or not 16384 <= max_bytes <= MAX_BYTES:
            raise JournalError("Invalid allocation limit")
        self.max_records, self.max_bytes = max_records, max_bytes
        self.path = Path(path).absolute()
        try:
            for parent in self.path.parents:
                if parent.is_symlink():
                    raise Unavailable("Unsafe journal parent")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.stat().st_mode & 0o077:
                raise Unavailable("Journal directory must be private")
            lock = self.path.with_name(self.path.name + ".writer.lock")
            self.lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = self.path.with_name(self.path.name + suffix)
                if candidate.is_symlink():
                    raise Unavailable("Unsafe journal path")
                if candidate.exists():
                    meta = candidate.stat()
                    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_uid != os.geteuid():
                        raise Unavailable("Journal file permissions not verified")
            if not self.path.exists():
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                self._sync_parent()
            self.connection = sqlite3.connect(str(self.path), timeout=0, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            if self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise Unavailable("Rollback journal not available")
            self.connection.execute("PRAGMA synchronous=EXTRA")
            if self.connection.execute("PRAGMA synchronous").fetchone()[0] != 3:
                raise Unavailable("Durable commit mode not available")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise Unavailable("Unsupported protected journal schema")
            if version == 0:
                self.connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE records (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL,
                        payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                    INSERT INTO metadata VALUES ('revision', 0);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Unavailable("Protected journal integrity failure")
            for row in self.connection.execute("SELECT id FROM records").fetchall():
                self.get(row[0])  # Validate stored digests before startup recovery.
            # Any previously committed unfinished issue is ambiguous after restart.
            # There is deliberately no callback, publisher, pending queue or retry.
            if self.connection.execute("SELECT 1 FROM records WHERE state IN ('reserved','dispatched') LIMIT 1").fetchone():
                self._transaction(lambda: self.connection.execute(
                    "UPDATE records SET state='unknown', updated_at=? WHERE state IN ('reserved','dispatched')", (timestamp(),)))
        except JournalError:
            self.close()
            raise
        except (OSError, sqlite3.Error):
            self.close()
            raise Unavailable("Protected journal unavailable; no commands permitted") from None

    def _sync_parent(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def _ready(self):
        if self.connection is None or self.faulted:
            raise Unavailable("Protected journal is closed or faulted")

    def _transaction(self, operation):
        self._ready()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            result = operation()
            self.connection.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")
            if self._allocated() > self.max_bytes:
                raise CapacityBlocked("Protected journal allocation exhausted")
            self.connection.execute("COMMIT")
            return result
        except (JournalError, sqlite3.Error, OSError) as error:
            try:
                self.connection.execute("ROLLBACK")
            except sqlite3.Error:
                self.faulted = True
            if isinstance(error, JournalError):
                raise
            self.faulted = True
            raise Unavailable("Durable write failed; no commands permitted") from None

    def _allocated(self):
        return self.connection.execute("PRAGMA page_count").fetchone()[0] * self.connection.execute("PRAGMA page_size").fetchone()[0]

    def get(self, identity):
        self._ready()
        try:
            row = self.connection.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()
        except sqlite3.Error:
            raise Unavailable("Protected journal read unavailable") from None
        if row is None:
            return None
        value = dict(row)
        try:
            value["payload"] = json.loads(value["payload"])
            value["result"] = json.loads(value["result"]) if value["result"] is not None else None
            raw = encode(value["payload"])
            if value["kind"] not in KINDS or hashlib.sha256(raw.encode("utf-8")).hexdigest() != value["digest"]:
                raise ValueError()
        except (ValueError, TypeError, JournalError):
            self.faulted = True
            raise Unavailable("Stored reservation failed verification") from None
        return value

    def reserve(self, kind, identity, payload):
        self._ready()
        if kind not in KINDS or not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}", identity):
            raise JournalError("Invalid reservation identity or type")
        if not isinstance(payload, dict):
            raise JournalError("Reservation payload must be an object")
        raw = encode(payload)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        previous = self.get(identity)
        if previous:
            if previous["kind"] != kind or previous["digest"] != digest:
                raise Conflict("Existing reservation has a different type or digest")
            return dict(created=False, record=previous)
        def insert():
            count = self.connection.execute("SELECT count(*) FROM records").fetchone()[0]
            if count >= self.max_records or self._allocated() >= self.max_bytes:
                raise CapacityBlocked("Protected journal capacity exhausted")
            now = timestamp()
            self.connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                                    (identity, kind, digest, raw, "reserved", None, now, now))
        self._transaction(insert)
        return dict(created=True, record=self.get(identity))

    def dispatched(self, identity, digest):
        def change():
            row = self.get(identity)
            if not row or row["digest"] != digest or row["state"] != "reserved" or row["kind"] not in ("command", "channel"):
                raise Conflict("Reservation cannot be dispatched")
            self.connection.execute("UPDATE records SET state='dispatched',updated_at=? WHERE id=?", (timestamp(), identity))
        self._transaction(change)

    def finish(self, identity, state, result, *, reconcile_ref=None):
        encode(result)
        if reconcile_ref is not None and (not isinstance(reconcile_ref, str) or not reconcile_ref.strip()):
            raise JournalError("Reconciliation requires an evidence reference")
        def change():
            row = self.get(identity)
            if not row:
                raise Conflict("Unknown reservation")
            allowed = {"command": {"rejected", "readback_confirmed", "unknown"},
                       "channel": {"accepted", "failed", "unknown"},
                       "incident": {"recovered", "unknown"}, "tombstone": set()}[row["kind"]]
            if state not in allowed:
                raise Conflict("Invalid reservation result")
            if state in ("accepted", "readback_confirmed") and row["state"] == "reserved":
                raise Conflict("Successful result requires committed dispatch")
            if row["state"] not in ("reserved", "dispatched", "unknown"):
                if row["state"] == state and row["result"] == result:
                    return
                raise Conflict("Terminal result is immutable")
            if row["state"] == "unknown" and state != "unknown" and not reconcile_ref:
                raise Conflict("Unknown requires explicit read-only reconciliation")
            stored = dict(result=result, reconcile_ref=reconcile_ref) if reconcile_ref else result
            self.connection.execute("UPDATE records SET state=?,result=?,updated_at=? WHERE id=?",
                                    (state, encode(stored), timestamp(), identity))
        self._transaction(change)

    def status(self):
        self._ready()
        try:
            count = self.connection.execute("SELECT count(*) FROM records").fetchone()[0]
            allocated = self._allocated()
            ratio = max(count / self.max_records, allocated / self.max_bytes)
            return dict(record_count=count, db_allocated_bytes=allocated, usage_ratio=ratio,
                        warning=ratio >= 0.8, capacity_blocked=ratio >= 1,
                        unknown_count=self.connection.execute("SELECT count(*) FROM records WHERE state='unknown'").fetchone()[0],
                        revision=self.connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0],
                        sqlite=dict(journal_mode=self.connection.execute("PRAGMA journal_mode").fetchone()[0],
                                    synchronous=self.connection.execute("PRAGMA synchronous").fetchone()[0]))
        except sqlite3.Error:
            raise Unavailable("Protected journal status unavailable") from None
