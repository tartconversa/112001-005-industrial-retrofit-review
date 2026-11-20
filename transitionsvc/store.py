"""CSV-backed storage for the transition-support service.

Base input files are read-only; every mutation is written to an append-only
journal under ``<data_dir>/state`` so that original records and their
corrections can always be reconstructed separately.
"""

from __future__ import annotations

import csv
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import AppError

BASE_FILES = {
    "workforce": "workforce.csv",
    "courses": "courses_commitments.csv",
    "outcomes": "outcomes.csv",
    "production_lines": "production_lines.csv",
    "meters": "meters_output.csv",
    "retrofits": "retrofits.csv",
}

OUTCOME_EVENT_TYPES = {
    "completed",
    "offer",
    "started",
    "retained_90d",
    "withdrawn",
    "declined",
    "not_started",
}

TRUTHY = {"1", "true", "yes", "y", "是"}
FALSY = {"0", "false", "no", "n", "否"}


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: object, field: str) -> int:
    text = _clean(value)
    if text is None:
        raise AppError(f"字段 {field} 不能为空", code="invalid_number")
    try:
        return int(text)
    except ValueError:
        raise AppError(f"字段 {field} 必须是整数，收到 {text!r}", code="invalid_number") from None


def _as_optional_bool(value: object) -> bool | None:
    text = _clean(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in TRUTHY:
        return True
    if lowered in FALSY:
        return False
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return [
            {key: (value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
        ]


def _write_all(path: Path, rows: Iterable[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in fields})
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


JOURNALS: dict[str, list[str]] = {
    "allocations": [
        "allocation_id", "course_id", "person_token", "group_code",
        "status", "reason", "allocated_at", "released_at",
    ],
    "clashes": [
        "clash_id", "primary_token", "tokens", "status", "note",
        "confirmed_token", "created_at", "resolved_at",
    ],
    "corrections": [
        "correction_id", "outcome_id", "previous_event_type",
        "current_event_type", "new_event_date", "date_action", "reason", "corrected_at",
    ],
    "fulfillments": [
        "fulfillment_id", "course_id", "employer_id", "job_code",
        "fulfilled_openings", "report_date", "note", "recorded_at",
    ],
    "supports": [
        "action_id", "person_token", "course_id", "action_type",
        "status", "reason", "recorded_at",
    ],
    "job_aliases": [
        "alias_code", "canonical_code", "status", "note", "recorded_at",
    ],
    "outcome_events": [
        "outcome_id", "person_token", "course_id", "event_date", "event_type",
        "job_code", "employer_id", "retained_90d", "source",
    ],
}


class Store:
    """Loads base CSV inputs and persists append-only journals."""

    def __init__(self, data_dir: str | Path, state_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir) if state_dir else self.data_dir / "state"
        if not self.data_dir.exists():
            raise AppError(f"数据目录不存在: {self.data_dir}", code="missing_data")
        self._cache: dict[str, list[dict[str, str]]] = {}

    # -- base inputs -----------------------------------------------------
    def base(self, name: str) -> list[dict[str, str]]:
        if name not in BASE_FILES:
            raise AppError(f"未知数据集: {name}", code="unknown_dataset")
        if name not in self._cache:
            self._cache[name] = _read_csv(self.data_dir / BASE_FILES[name])
        return list(self._cache[name])

    def reload(self) -> None:
        """Drop cached base rows so externally updated CSV files are re-read."""
        self._cache.clear()

    def courses(self) -> list[dict[str, str]]:
        return self.base("courses")

    def course(self, course_id: str) -> dict[str, str]:
        for row in self.courses():
            if row.get("course_id") == course_id:
                return row
        raise AppError(f"课程不存在: {course_id}", code="course_not_found")

    def workforce(self) -> list[dict[str, str]]:
        return self.base("workforce")

    def production_lines(self) -> list[dict[str, str]]:
        return self.base("production_lines")

    def base_outcomes(self) -> list[dict[str, str]]:
        return self.base("outcomes")

    def outcomes(self) -> list[dict[str, object]]:
        """Base outcome rows merged with recorded events and corrections."""
        corrections = self.journal("corrections")
        latest: dict[str, dict[str, str]] = {}
        for correction in corrections:
            latest[correction["outcome_id"]] = correction

        def build(row: dict[str, str]) -> dict[str, object]:
            item: dict[str, object] = {
                "outcome_id": row.get("outcome_id", ""),
                "person_token": row.get("person_token", ""),
                "course_id": row.get("course_id", ""),
                "event_date": _clean(row.get("event_date")),
                "event_type": _clean(row.get("event_type")),
                "job_code": _clean(row.get("job_code")),
                "employer_id": _clean(row.get("employer_id")),
                "retained_90d": _as_optional_bool(row.get("retained_90d")),
                "original_event_type": _clean(row.get("event_type")),
                "original_event_date": _clean(row.get("event_date")),
                "corrected": False,
            }
            fix = latest.get(str(item["outcome_id"]))
            if fix:
                item["event_type"] = _clean(fix.get("current_event_type"))
                date_action = fix.get("date_action") or (
                    "set" if _clean(fix.get("new_event_date")) else "unchanged"
                )
                if date_action == "set":
                    item["event_date"] = _clean(fix.get("new_event_date"))
                elif date_action == "unknown":
                    item["event_date"] = None
                item["corrected"] = True
            return item

        merged = [build(row) for row in self.base_outcomes()]
        merged.extend(build(row) for row in self.journal("outcome_events"))
        return merged

    # -- journals --------------------------------------------------------
    def _journal_path(self, name: str) -> Path:
        return self.state_dir / f"{name}.csv"

    def journal(self, name: str) -> list[dict[str, str]]:
        if name not in JOURNALS:
            raise AppError(f"未知日志: {name}", code="unknown_dataset")
        return _read_csv(self._journal_path(name))

    def append(self, name: str, row: dict[str, object]) -> dict[str, str]:
        fields = JOURNALS[name]
        rows = self.journal(name)
        stored = {field: ("" if row.get(field) is None else str(row.get(field))) for field in fields}
        rows.append(stored)
        _write_all(self._journal_path(name), rows, fields)
        return stored

    def rewrite(self, name: str, rows: list[dict[str, object]]) -> None:
        _write_all(self._journal_path(name), rows, JOURNALS[name])

    # -- convenience views ----------------------------------------------
    def course_seat_allocation(
        self, course_id: str, *, include_released: bool = False
    ) -> list[dict[str, str]]:
        rows = [r for r in self.journal("allocations") if r.get("course_id") == course_id]
        if not include_released:
            rows = [r for r in rows if r.get("status") == "allocated"]
        return rows

    def unresolved_clashes(self) -> list[dict[str, str]]:
        return [r for r in self.journal("clashes") if r.get("status") == "unresolved"]

    def find_allocated(self, course_id: str, person_token: str) -> dict[str, str] | None:
        for row in self.journal("allocations"):
            if (row.get("course_id") == course_id
                    and row.get("person_token") == person_token
                    and row.get("status") == "allocated"):
                return row
        return None


def new_id(prefix: str) -> str:
    return _new_id(prefix)


def now() -> str:
    return _now()


def as_int(value: object, field: str) -> int:
    return _as_int(value, field)


def as_optional_bool(value: object) -> bool | None:
    return _as_optional_bool(value)


def clean(value: object) -> str | None:
    return _clean(value)
