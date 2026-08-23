"""Append-only event journal with an indexed SQLite mirror (ADR-002).

Durability model
----------------
The JSONL segment file is the **source of truth**. SQLite is a rebuildable projection used
for indexed queries by the API. If the two ever disagree, the JSONL wins and
``atlas store rebuild`` regenerates SQLite from it.

Ordering
--------
``seq`` is assigned inside a lock and is strictly increasing with no gaps within a journal
directory. On open, the next seq is recovered from the JSONL tail (not from SQLite), because
SQLite may be behind after an unclean shutdown.

Batching
--------
Live runs use ``batch_size=1`` so nothing is lost on a hard kill. Backtests write hundreds of
thousands of decision events and use a larger batch, accepting that a crashed backtest loses
its tail -- a backtest is reproducible, a live session is not.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from atlas.bus.events import Event
from atlas.core.clock import Clock, SystemClock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY,
    ts      INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    stream  TEXT    NOT NULL,
    run_id  TEXT    NOT NULL,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, seq);
CREATE INDEX IF NOT EXISTS ix_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_run  ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_stream ON events(stream, seq);
"""


class Journal:
    """Durable, ordered event log.

    Not safe to share across processes for *writing* (the seq counter is process-local).
    Multiple readers are fine: SQLite is opened in WAL mode.
    """

    def __init__(
        self,
        directory: Path | str,
        run_id: str = "",
        *,
        clock: Clock | None = None,
        batch_size: int = 1,
        mirror_sqlite: bool = True,
    ) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.clock = clock or SystemClock()
        self.batch_size = max(1, batch_size)
        self.jsonl_path = self.dir / "events.jsonl"
        self.db_path = self.dir / "atlas.db"

        self._lock = threading.RLock()
        self._pending: list[Event] = []
        self._listeners: list[Callable[[Event], None]] = []
        self._closed = False

        self._seq = self._recover_seq()
        self._fh = self.jsonl_path.open("a", encoding="utf-8")
        self._db: sqlite3.Connection | None = None
        if mirror_sqlite:
            self._db = sqlite3.connect(self.db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # -- recovery ---------------------------------------------------------------

    def _recover_seq(self) -> int:
        """Highest seq already on disk. Scans the JSONL tail rather than the whole file.

        A truncated final line (torn write from a hard kill) is tolerated: it is skipped and
        the next append overwrites nothing, because appends only ever move forward.
        """
        if not self.jsonl_path.exists() or self.jsonl_path.stat().st_size == 0:
            return 0
        size = self.jsonl_path.stat().st_size
        window = min(size, 1 << 20)
        with self.jsonl_path.open("rb") as fh:
            fh.seek(size - window)
            tail = fh.read().decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return int(json.loads(line)["seq"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue  # torn or malformed line; keep walking backwards
        return 0

    # -- writing ----------------------------------------------------------------

    def add_listener(self, fn: Callable[[Event], None]) -> None:
        """Register a synchronous fan-out callback (used by the live WebSocket bridge).

        Listener exceptions are swallowed: a broken dashboard subscriber must never be able
        to take down the trading engine.
        """
        self._listeners.append(fn)

    def append(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        stream: str = "system",
        ts: int | None = None,
    ) -> Event:
        if self._closed:
            raise RuntimeError("journal is closed")
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=ts if ts is not None else self.clock.now_ms(),
                kind=kind,
                stream=stream,
                run_id=self.run_id,
                payload=_jsonable(payload or {}),
            )
            self._pending.append(event)
            if len(self._pending) >= self.batch_size:
                self._flush_locked()
        for fn in self._listeners:
            with contextlib.suppress(Exception):
                fn(event)  # listener isolation: a broken subscriber must not stop the engine
        return event

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        lines = []
        rows = []
        for e in batch:
            rows.append(
                (
                    e.seq,
                    e.ts,
                    e.kind,
                    e.stream,
                    e.run_id,
                    json.dumps(e.payload, separators=(",", ":")),
                )
            )
            lines.append(
                json.dumps(
                    {
                        "seq": e.seq,
                        "ts": e.ts,
                        "kind": e.kind,
                        "stream": e.stream,
                        "run_id": e.run_id,
                        "payload": e.payload,
                    },
                    separators=(",", ":"),
                )
            )
        self._fh.write("\n".join(lines) + "\n")
        self._fh.flush()
        if self.batch_size == 1:
            # Live mode: pay for a real fsync so a power loss cannot lose an order event.
            os.fsync(self._fh.fileno())
        if self._db is not None:
            self._db.executemany(
                "INSERT OR REPLACE INTO events(seq,ts,kind,stream,run_id,payload)"
                " VALUES(?,?,?,?,?,?)",
                rows,
            )
            self._db.commit()

    # -- reading ----------------------------------------------------------------

    def read(
        self,
        since_seq: int = 0,
        *,
        kinds: Iterable[str] | None = None,
        stream: str | None = None,
        run_id: str | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        self.flush()
        if self._db is None:
            return [e for e in self.iter_jsonl() if e.seq > since_seq][:limit]
        sql = "SELECT seq,ts,kind,stream,run_id,payload FROM events WHERE seq > ?"
        args: list[Any] = [since_seq]
        if kinds is not None:
            kl = list(kinds)
            if not kl:
                return []
            sql += f" AND kind IN ({','.join('?' * len(kl))})"
            args.extend(kl)
        if stream:
            sql += " AND stream = ?"
            args.append(stream)
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        cur = self._db.execute(sql, args)
        return [
            Event(seq=r[0], ts=r[1], kind=r[2], stream=r[3], run_id=r[4], payload=json.loads(r[5]))
            for r in cur.fetchall()
        ]

    def latest(self, kind: str, *, stream: str | None = None) -> Event | None:
        self.flush()
        if self._db is None:
            found = [
                e
                for e in self.iter_jsonl()
                if e.kind == kind and (stream is None or e.stream == stream)
            ]
            return found[-1] if found else None
        sql = "SELECT seq,ts,kind,stream,run_id,payload FROM events WHERE kind = ?"
        args: list[Any] = [kind]
        if stream:
            sql += " AND stream = ?"
            args.append(stream)
        sql += " ORDER BY seq DESC LIMIT 1"
        row = self._db.execute(sql, args).fetchone()
        if not row:
            return None
        return Event(
            seq=row[0],
            ts=row[1],
            kind=row[2],
            stream=row[3],
            run_id=row[4],
            payload=json.loads(row[5]),
        )

    def iter_jsonl(self) -> Iterator[Event]:
        """Stream events straight from the source-of-truth file, skipping torn lines."""
        if not self.jsonl_path.exists():
            return
        with self.jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield Event(**d)

    def count(self) -> int:
        self.flush()
        if self._db is None:
            return sum(1 for _ in self.iter_jsonl())
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    @property
    def last_seq(self) -> int:
        return self._seq

    # -- maintenance ------------------------------------------------------------

    def rebuild_sqlite(self) -> int:
        """Regenerate the SQLite mirror from the JSONL source of truth."""
        if self._db is None:
            raise RuntimeError("sqlite mirror is disabled")
        self.flush()
        self._db.execute("DELETE FROM events")
        n = 0
        buf: list[tuple[Any, ...]] = []
        for e in self.iter_jsonl():
            buf.append(
                (
                    e.seq,
                    e.ts,
                    e.kind,
                    e.stream,
                    e.run_id,
                    json.dumps(e.payload, separators=(",", ":")),
                )
            )
            n += 1
            if len(buf) >= 5000:
                self._db.executemany("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)", buf)
                buf.clear()
        if buf:
            self._db.executemany("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)", buf)
        self._db.commit()
        return n

    def connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("sqlite mirror is disabled")
        return self._db

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._flush_locked()
            self._closed = True
            self._fh.close()
            if self._db is not None:
                self._db.commit()
                self._db.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _jsonable(obj: Any) -> Any:
    """Coerce pydantic models, enums, tuples and numpy scalars into JSON-safe values.

    Done eagerly at append time rather than at serialisation time so that a non-serialisable
    payload fails loudly at the call site instead of silently corrupting the journal later.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"))
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalar
        with contextlib.suppress(Exception):
            return obj.item()
    if hasattr(obj, "value"):  # enum
        return _jsonable(obj.value)
    return str(obj)
