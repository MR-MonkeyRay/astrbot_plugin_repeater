"""Short-lived persistence for intelligent-generation history."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, replace
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
    """One intelligent generation attempt and its displayable details."""

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
    message_text: str | None = None
    prompt: str | None = None
    completion: str | None = None
    repeat_user_count: int | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable history record."""
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
    """Persist short-lived intelligent action history in daily JSON Lines files."""

    _VALID_KINDS = frozenset(("repeat", "mute"))
    _VALID_SOURCES = frozenset(("runtime", "manual_test"))
    _VALID_OUTCOMES = frozenset(("success", "fallback", "failed"))
    _VALID_WINDOWS = frozenset(("day", "24h", "2d", "3d", "7d"))
    _FILE_PREFIX = "intelligent_history-"
    _FILE_SUFFIX = ".jsonl"
    _ID_STRIDE = 1_000_000_000

    def __init__(
        self,
        data_dir: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        local_timezone: tzinfo | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._local_timezone = local_timezone
        self._lock = asyncio.Lock()
        self._initialized = False
        self._last_ids: dict[Path, int] = {}

    async def initialize(self) -> int:
        """Create the data directory and remove strictly expired records."""
        async with self._lock:
            now_ms = self._now_ms()
            deleted = await self._run_sync(self._initialize_sync, now_ms)
            self._initialized = True
            return deleted

    async def append(self, record: IntelligentActionRecord) -> IntelligentActionRecord:
        """Persist one validated history record."""
        self._validate_record(record)
        async with self._lock:
            self._require_initialized()
            return await self._run_sync(self._append_sync, record)

    async def clear(self) -> int:
        """Delete every persisted intelligent action record."""
        async with self._lock:
            self._require_initialized()
            return await self._run_sync(self._clear_sync)

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
        """Keep the store lock held until a started filesystem worker has settled."""
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

    def _local_datetime(self, timestamp_ms: int) -> datetime:
        instant = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        if self._local_timezone is None:
            return instant.astimezone()
        return instant.astimezone(self._local_timezone)

    def _record_path(self, timestamp_ms: int) -> Path:
        day = self._local_datetime(timestamp_ms).date().isoformat()
        return self.data_dir / f"{self._FILE_PREFIX}{day}{self._FILE_SUFFIX}"

    def _history_paths(self) -> list[Path]:
        if not self.data_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.data_dir.glob(
                f"{self._FILE_PREFIX}????-??-??{self._FILE_SUFFIX}"
            )
            if path.is_file()
        )

    def _paths_for_range(self, start_at_ms: int, end_at_ms: int) -> list[Path]:
        day = self._local_datetime(start_at_ms).date()
        end_day = self._local_datetime(end_at_ms).date()
        paths: list[Path] = []
        while day <= end_day:
            path = self.data_dir / (
                f"{self._FILE_PREFIX}{day.isoformat()}{self._FILE_SUFFIX}"
            )
            if path.is_file():
                paths.append(path)
            day += timedelta(days=1)
        return paths

    def _initialize_sync(self, now_ms: int) -> int:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self._purge_sync(now_ms - RETENTION_MS)

    def _append_sync(self, record: IntelligentActionRecord) -> IntelligentActionRecord:
        """Append one line; a failed write truncates the file back to its old size."""
        path = self._record_path(record.occurred_at_ms)
        path.parent.mkdir(parents=True, exist_ok=True)
        last_id = self._last_ids.get(path)
        if last_id is None:
            existing = self._read_records_sync(path) if path.exists() else []
            last_id = self._next_record_id(record.occurred_at_ms, existing) - 1
        persisted = replace(record, id=last_id + 1)
        payload = (self._serialize_record(persisted) + "\n").encode("utf-8")
        try:
            with path.open("ab") as output:
                original_size = output.tell()
                try:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                except OSError:
                    output.truncate(original_size)
                    raise
        except OSError as exc:
            raise HistoryStorageError(f"unable to append {path.name}") from exc
        self._last_ids[path] = last_id + 1
        return persisted

    def _query_sync(
        self,
        start_at_ms: int,
        end_at_ms: int,
        kind: HistoryKind | None,
        page: int,
        page_size: int,
    ) -> tuple[dict[str, int], list[IntelligentActionRecord]]:
        summary = {
            "total": 0,
            "repeat": 0,
            "mute": 0,
            "success": 0,
            "fallback": 0,
            "failed": 0,
        }
        matching: list[IntelligentActionRecord] = []
        for path in self._paths_for_range(start_at_ms, end_at_ms):
            for record in self._read_records_sync(path):
                if not start_at_ms <= record.occurred_at_ms <= end_at_ms:
                    continue
                if kind is not None and record.kind != kind:
                    continue
                summary["total"] += 1
                summary[record.kind] += 1
                summary[record.outcome] += 1
                matching.append(record)
        matching.sort(
            key=lambda record: (record.occurred_at_ms, record.id or 0),
            reverse=True,
        )
        offset = (page - 1) * page_size
        return summary, matching[offset : offset + page_size]

    def _clear_sync(self) -> int:
        self._last_ids.clear()
        deleted = 0
        for path in self._history_paths():
            deleted += len(self._read_records_sync(path))
            self._delete_file_sync(path)
        return deleted

    def _purge_sync(self, cutoff_at_ms: int) -> int:
        deleted = 0
        for path in self._history_paths():
            records = self._read_records_sync(path)
            retained = [
                record for record in records if record.occurred_at_ms >= cutoff_at_ms
            ]
            deleted += len(records) - len(retained)
            if not retained:
                self._last_ids.pop(path, None)
                self._delete_file_sync(path)
            elif len(retained) != len(records):
                self._rewrite_records_sync(path, retained)
        return deleted

    def _read_records_sync(self, path: Path) -> list[IntelligentActionRecord]:
        records: list[IntelligentActionRecord] = []
        try:
            with path.open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = self._record_from_dict(json.loads(line))
                        self._validate_record(record)
                    except (
                        json.JSONDecodeError,
                        KeyError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        raise HistoryStorageError(
                            f"invalid history record in {path.name} line {line_number}"
                        ) from exc
                    records.append(record)
        except UnicodeError as exc:
            raise HistoryStorageError(f"unable to decode {path.name}") from exc
        except OSError as exc:
            raise HistoryStorageError(f"unable to read {path.name}") from exc
        return records

    @staticmethod
    def _record_from_dict(payload: object) -> IntelligentActionRecord:
        if not isinstance(payload, dict):
            raise ValueError("history record must be a JSON object")
        return IntelligentActionRecord(
            occurred_at_ms=payload["occurred_at_ms"],
            kind=payload["kind"],
            source=payload["source"],
            outcome=payload["outcome"],
            provider_id=payload.get("provider_id"),
            model=payload["model"],
            group_id=payload.get("group_id"),
            mute_duration_seconds=payload.get("mute_duration_seconds"),
            latency_ms=payload["latency_ms"],
            failure_code=payload.get("failure_code"),
            message_text=payload.get("message_text"),
            prompt=payload.get("prompt"),
            completion=payload.get("completion"),
            repeat_user_count=payload.get("repeat_user_count"),
            id=payload.get("id"),
        )

    @staticmethod
    def _serialize_record(record: IntelligentActionRecord) -> str:
        return json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _next_record_id(
        self,
        occurred_at_ms: int,
        existing: list[IntelligentActionRecord],
    ) -> int:
        base = self._local_datetime(occurred_at_ms).date().toordinal()
        next_id = base * self._ID_STRIDE + len(existing) + 1
        for record in existing:
            if record.id is not None:
                next_id = max(next_id, record.id + 1)
        return next_id

    def _rewrite_records_sync(
        self,
        path: Path,
        records: list[IntelligentActionRecord],
    ) -> None:
        temporary_path = path.with_name(f".{path.name}.tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as output:
                for record in records:
                    output.write(self._serialize_record(record))
                    output.write("\n")
            temporary_path.replace(path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise HistoryStorageError(f"unable to rewrite {path.name}") from exc

    @staticmethod
    def _delete_file_sync(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise HistoryStorageError(f"unable to delete {path.name}") from exc

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
        if not isinstance(record.latency_ms, int) or isinstance(
            record.latency_ms, bool
        ):
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
        for field_name, value in (
            ("message_text", record.message_text),
            ("prompt", record.prompt),
            ("completion", record.completion),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string or None")
        if record.repeat_user_count is not None:
            if not isinstance(record.repeat_user_count, int) or isinstance(
                record.repeat_user_count,
                bool,
            ):
                raise ValueError("repeat_user_count must be an integer or None")
            if record.repeat_user_count < 0:
                raise ValueError("repeat_user_count must not be negative")
        if record.id is not None:
            if not isinstance(record.id, int) or isinstance(record.id, bool):
                raise ValueError("id must be an integer or None")
            if record.id < 1:
                raise ValueError("id must be positive")

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
