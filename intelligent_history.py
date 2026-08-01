"""Privacy-preserving persistence for intelligent-generation telemetry."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Literal


HistoryKind = Literal["repeat", "mute"]
HistorySource = Literal["runtime", "manual_test"]
HistoryOutcome = Literal["success", "fallback", "failed"]
HistoryWindow = Literal["day", "24h", "2d", "3d", "7d"]

RETENTION = timedelta(days=7)
RETENTION_MS = int(RETENTION.total_seconds() * 1000)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


class HistoryStorageError(RuntimeError):
    """Raised when a history operation cannot safely complete."""


@dataclass(frozen=True, slots=True)
class IntelligentActionRecord:
    """One content-free intelligent generation attempt."""

    occurred_at_ms: int
    kind: HistoryKind
    source: HistorySource
    outcome: HistoryOutcome
    provider_id: str | None
    model: str
    group_id: str | None
    mute_duration_seconds: int | None
    latency_ms: int
    failure_code: str | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable record without hidden fields."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """A bounded windowed view of persisted intelligent actions."""

    window: HistoryWindow
    start_at_ms: int
    end_at_ms: int
    timezone_name: str
    start_display: str
    end_display: str
    summary: dict[str, int]
    page: int
    page_size: int
    total_pages: int
    records: tuple[IntelligentActionRecord, ...]

    def to_dict(self) -> dict[str, object]:
        """Return the public Page API representation."""
        return {
            "range": {
                "window": self.window,
                "start_at_ms": self.start_at_ms,
                "end_at_ms": self.end_at_ms,
                "timezone": self.timezone_name,
                "start_display": self.start_display,
                "end_display": self.end_display,
            },
            "summary": self.summary,
            "pagination": {
                "page": self.page,
                "page_size": self.page_size,
                "total": self.summary["total"],
                "total_pages": self.total_pages,
            },
            "records": [record.to_dict() for record in self.records],
        }


class IntelligentHistoryStore:
    """Serialize short-lived SQLite operations for intelligent action telemetry."""

    _VALID_KINDS = frozenset(("repeat", "mute"))
    _VALID_SOURCES = frozenset(("runtime", "manual_test"))
    _VALID_OUTCOMES = frozenset(("success", "fallback", "failed"))
    _VALID_WINDOWS = frozenset(("day", "24h", "2d", "3d", "7d"))

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        local_timezone: tzinfo | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._local_timezone = local_timezone
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> int:
        """Create the schema and remove strictly expired records."""
        async with self._lock:
            now_ms = self._now_ms()
            deleted = await self._run_sync(self._initialize_sync, now_ms)
            self._initialized = True
            return deleted

    async def append(self, record: IntelligentActionRecord) -> IntelligentActionRecord:
        """Persist one validated metadata-only record."""
        self._validate_record(record)
        async with self._lock:
            self._require_initialized()
            record_id = await self._run_sync(self._append_sync, record)
        return IntelligentActionRecord(
            occurred_at_ms=record.occurred_at_ms,
            kind=record.kind,
            source=record.source,
            outcome=record.outcome,
            provider_id=record.provider_id,
            model=record.model,
            group_id=record.group_id,
            mute_duration_seconds=record.mute_duration_seconds,
            latency_ms=record.latency_ms,
            failure_code=record.failure_code,
            id=record_id,
        )

    async def query(
        self,
        *,
        window: HistoryWindow,
        kind: HistoryKind | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> HistoryPage:
        """Return a bounded newest-first page and full-window summary counts."""
        self._validate_query(window=window, kind=kind, page=page, page_size=page_size)
        async with self._lock:
            self._require_initialized()
            now = self._now()
            start_at_ms, end_at_ms, timezone_name, display_timezone = (
                self._window_bounds(window, now)
            )
            summary, records = await self._run_sync(
                self._query_sync,
                start_at_ms,
                end_at_ms,
                kind,
                page,
                page_size,
            )
        total_pages = max(1, (summary["total"] + page_size - 1) // page_size)
        return HistoryPage(
            window=window,
            start_at_ms=start_at_ms,
            end_at_ms=end_at_ms,
            timezone_name=timezone_name,
            start_display=self._format_range_timestamp(
                start_at_ms,
                display_timezone,
            ),
            end_display=self._format_range_timestamp(
                end_at_ms,
                display_timezone,
            ),
            summary=summary,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            records=tuple(records),
        )

    async def purge_expired(self) -> int:
        """Delete only records strictly older than the rolling seven-day cutoff."""
        async with self._lock:
            self._require_initialized()
            cutoff_at_ms = self._now_ms() - RETENTION_MS
            return await self._run_sync(self._purge_sync, cutoff_at_ms)

    async def _run_sync(self, function, *args):
        """Keep the store lock held until a started SQLite worker has settled."""
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        was_cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                was_cancelled = True
        result = worker.result()
        if was_cancelled:
            raise asyncio.CancelledError
        return result

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise HistoryStorageError("history clock must return datetime")
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _now_ms(self) -> int:
        return int(self._now().timestamp() * 1000)

    def _window_bounds(
        self,
        window: HistoryWindow,
        now: datetime,
    ) -> tuple[int, int, str, tzinfo | None]:
        end_at_ms = int(now.timestamp() * 1000)
        if window == "day":
            if self._local_timezone is None:
                local_now = now.astimezone()
                local_start = datetime(
                    local_now.year,
                    local_now.month,
                    local_now.day,
                )
                start_at_ms = int(local_start.timestamp() * 1000)
                return (
                    start_at_ms,
                    end_at_ms,
                    local_now.tzname() or "local",
                    None,
                )
            local_now = now.astimezone(self._local_timezone)
            local_start = local_now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            start_at_ms = int(local_start.astimezone(timezone.utc).timestamp() * 1000)
            return (
                start_at_ms,
                end_at_ms,
                local_now.tzname() or str(self._local_timezone),
                self._local_timezone,
            )
        days = {"24h": 1, "2d": 2, "3d": 3, "7d": 7}[window]
        start_at_ms = end_at_ms - int(timedelta(days=days).total_seconds() * 1000)
        return start_at_ms, end_at_ms, "UTC", timezone.utc

    @staticmethod
    def _format_range_timestamp(
        timestamp_ms: int,
        display_timezone: tzinfo | None,
    ) -> str:
        """Format a range boundary in the same zone used for its query."""
        instant = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        return instant.astimezone(display_timezone).isoformat(timespec="seconds")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=5.0)

    def _initialize_sync(self, now_ms: int) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intelligent_action_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('repeat', 'mute')),
                    source TEXT NOT NULL CHECK (source IN ('runtime', 'manual_test')),
                    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'fallback', 'failed')),
                    provider_id TEXT,
                    model TEXT NOT NULL,
                    group_id TEXT,
                    mute_duration_seconds INTEGER,
                    latency_ms INTEGER NOT NULL,
                    failure_code TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intelligent_action_history_occurred
                ON intelligent_action_history (occurred_at_ms DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intelligent_action_history_kind_occurred
                ON intelligent_action_history (kind, occurred_at_ms DESC, id DESC)
                """
            )
            cursor = connection.execute(
                "DELETE FROM intelligent_action_history WHERE occurred_at_ms < ?",
                (now_ms - RETENTION_MS,),
            )
            connection.commit()
            return cursor.rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_sync(self, record: IntelligentActionRecord) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO intelligent_action_history (
                    occurred_at_ms,
                    kind,
                    source,
                    outcome,
                    provider_id,
                    model,
                    group_id,
                    mute_duration_seconds,
                    latency_ms,
                    failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.occurred_at_ms,
                    record.kind,
                    record.source,
                    record.outcome,
                    record.provider_id,
                    record.model,
                    record.group_id,
                    record.mute_duration_seconds,
                    record.latency_ms,
                    record.failure_code,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _query_sync(
        self,
        start_at_ms: int,
        end_at_ms: int,
        kind: HistoryKind | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, int], list[IntelligentActionRecord]]:
        conditions = ["occurred_at_ms >= ?", "occurred_at_ms <= ?"]
        params: list[object] = [start_at_ms, end_at_ms]
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        where_clause = " AND ".join(conditions)
        connection = self._connect()
        try:
            summary_row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN kind = 'repeat' THEN 1 ELSE 0 END), 0)
                        AS repeat,
                    COALESCE(SUM(CASE WHEN kind = 'mute' THEN 1 ELSE 0 END), 0)
                        AS mute,
                    COALESCE(SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END), 0)
                        AS success,
                    COALESCE(SUM(CASE WHEN outcome = 'fallback' THEN 1 ELSE 0 END), 0)
                        AS fallback,
                    COALESCE(SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END), 0)
                        AS failed
                FROM intelligent_action_history
                WHERE {where_clause}
                """,
                params,
            ).fetchone()
            offset = (page - 1) * page_size
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    occurred_at_ms,
                    kind,
                    source,
                    outcome,
                    provider_id,
                    model,
                    group_id,
                    mute_duration_seconds,
                    latency_ms,
                    failure_code
                FROM intelligent_action_history
                WHERE {where_clause}
                ORDER BY occurred_at_ms DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        finally:
            connection.close()
        summary = {
            "total": int(summary_row[0]),
            "repeat": int(summary_row[1]),
            "mute": int(summary_row[2]),
            "success": int(summary_row[3]),
            "fallback": int(summary_row[4]),
            "failed": int(summary_row[5]),
        }
        records = [
            IntelligentActionRecord(
                id=int(row[0]),
                occurred_at_ms=int(row[1]),
                kind=row[2],
                source=row[3],
                outcome=row[4],
                provider_id=row[5],
                model=row[6],
                group_id=row[7],
                mute_duration_seconds=row[8],
                latency_ms=int(row[9]),
                failure_code=row[10],
            )
            for row in rows
        ]
        return summary, records

    def _purge_sync(self, cutoff_at_ms: int) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM intelligent_action_history WHERE occurred_at_ms < ?",
                (cutoff_at_ms,),
            )
            connection.commit()
            return cursor.rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise HistoryStorageError("history store is not initialized")

    def _validate_record(self, record: IntelligentActionRecord) -> None:
        if record.kind not in self._VALID_KINDS:
            raise ValueError("invalid history kind")
        if record.source not in self._VALID_SOURCES:
            raise ValueError("invalid history source")
        if record.outcome not in self._VALID_OUTCOMES:
            raise ValueError("invalid history outcome")
        if not isinstance(record.occurred_at_ms, int) or isinstance(
            record.occurred_at_ms, bool
        ):
            raise ValueError("occurred_at_ms must be an integer")
        if not isinstance(record.latency_ms, int) or isinstance(record.latency_ms, bool):
            raise ValueError("latency_ms must be an integer")
        if record.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")
        if not isinstance(record.model, str):
            raise ValueError("model must be a string")
        if record.provider_id is not None and not isinstance(record.provider_id, str):
            raise ValueError("provider_id must be a string or None")
        if record.group_id is not None and not isinstance(record.group_id, str):
            raise ValueError("group_id must be a string or None")
        if record.mute_duration_seconds is not None:
            if not isinstance(record.mute_duration_seconds, int) or isinstance(
                record.mute_duration_seconds,
                bool,
            ):
                raise ValueError("mute_duration_seconds must be an integer or None")
            if record.mute_duration_seconds < 0:
                raise ValueError("mute_duration_seconds must not be negative")
        if record.failure_code is not None and not isinstance(record.failure_code, str):
            raise ValueError("failure_code must be a string or None")

    def _validate_query(
        self,
        *,
        window: HistoryWindow,
        kind: HistoryKind | None,
        page: int,
        page_size: int,
    ) -> None:
        if window not in self._VALID_WINDOWS:
            raise ValueError("invalid history window")
        if kind is not None and kind not in self._VALID_KINDS:
            raise ValueError("invalid history kind")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError("page must be a positive integer")
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
