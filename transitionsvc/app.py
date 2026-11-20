"""Core rules for transition-period employment support.

The service keeps four things strictly apart:

* employer *plans* (planned openings / recruitment commitments),
* *offers* made to a person,
* *actual starts*, and
* *retention* after starting.

It also never merges records whose identity is unresolved, never allocates more
seats than a course/provider offered, and never removes training spend that has
already been committed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .errors import AppError
from .store import (
    OUTCOME_EVENT_TYPES,
    Store,
    as_int,
    clean,
    new_id,
    now,
)

ACTIVE_COURSE_STATUSES = {"planned", "confirmed", "pending", "active"}
AFFECTED_STATUSES = {"at_risk", "separated", "laid_off", "redundant", "notice_period"}
SUPPORT_STATUSES = {"informed", "approved", "executed"}
UNEXECUTED_SUPPORT = SUPPORT_STATUSES - {"executed"}


def _split_tokens(value: str) -> list[str]:
    return [token for token in (part.strip() for part in value.split("|")) if token]


def _course_active(course: dict[str, str]) -> bool:
    status = (course.get("commitment_status") or "").strip().lower()
    return status in ACTIVE_COURSE_STATUSES


def _course_openings(course: dict[str, str]) -> int:
    return as_int(course.get("planned_openings"), "planned_openings")


def _course_seats(course: dict[str, str]) -> int:
    return as_int(course.get("seats"), "seats")


def _course_fee(course: dict[str, str]) -> int:
    return as_int(course.get("fee_per_seat"), "fee_per_seat")


class Service:
    def __init__(self, store: Store | str | Path) -> None:
        self.store = store if isinstance(store, Store) else Store(store)

    # ------------------------------------------------------------------
    # Workforce / group views
    # ------------------------------------------------------------------
    def group_view(self) -> list[dict[str, object]]:
        """Affected persons and training participation per support group."""
        canonical = self._canonical_map()
        groups: dict[str, dict[str, object]] = {}
        for row in self.store.workforce():
            group = clean(row.get("group_code"))
            if group is None:
                continue
            raw_token = clean(row.get("person_token"))
            if raw_token is None:
                continue
            token = canonical.get(raw_token, raw_token)
            entry = groups.setdefault(
                group,
                {"group_code": group, "persons": set(), "affected": set(), "in_training": set()},
            )
            entry["persons"].add(token)  # type: ignore[union-attr]
            status = (row.get("employment_status") or "").strip().lower()
            if status in AFFECTED_STATUSES:
                entry["affected"].add(token)  # type: ignore[union-attr]
            if status == "training":
                entry["in_training"].add(token)  # type: ignore[union-attr]

        allocation_by_group: dict[str, dict[str, set[str]]] = {}
        for row in self.store.journal("allocations"):
            group = row.get("group_code") or "未分组"
            bucket = allocation_by_group.setdefault(group, {"allocated": set(), "waitlisted": set()})
            raw_token = row.get("person_token", "")
            token = canonical.get(raw_token, raw_token)
            status = row.get("status")
            # A person may hold seats on more than one course; the person-level
            # figures stay distinct while seat counts reflect actual seats.
            if status == "allocated":
                bucket["allocated"].add(row.get("allocation_id", token))
            elif status == "waitlisted":
                bucket["waitlisted"].add(row.get("allocation_id", token))

        result: list[dict[str, object]] = []
        for group in sorted(set(groups) | set(allocation_by_group)):
            entry = groups.get(
                group,
                {"group_code": group, "persons": set(), "affected": set(), "in_training": set()},
            )
            alloc = allocation_by_group.get(group, {"allocated": set(), "waitlisted": set()})
            result.append(
                {
                    "group_code": group,
                    "persons": len(entry["persons"]),  # type: ignore[arg-type]
                    "affected_persons": len(entry["affected"]),  # type: ignore[arg-type]
                    "in_training": len(entry["in_training"]),  # type: ignore[arg-type]
                    "allocated_seats": len(alloc["allocated"]),
                    "waitlisted": len(alloc["waitlisted"]),
                }
            )
        return result

    def person_view(self, person_token: str) -> dict[str, object]:
        """One person's records, without silently merging unknown identities."""
        token = clean(person_token)
        if token is None:
            raise AppError("person_token 不能为空", code="invalid_person")
        identity_status = "single"
        tokens = [token]
        clash_id = None

        for clash in self.store.journal("clashes"):
            candidates = _split_tokens(clash.get("tokens", ""))
            if token not in candidates and clash.get("primary_token") != token:
                continue
            if clash.get("status") == "unresolved":
                identity_status = "unresolved"
                tokens = sorted(candidates)
                clash_id = clash.get("clash_id")
                break
            if clash.get("status") == "resolved":
                confirmed = clash.get("confirmed_token")
                identity_status = "resolved"
                tokens = [confirmed] if confirmed else [token]
                clash_id = clash.get("clash_id")
                break

        token_set = set(tokens)
        workforce = [
            row for row in self.store.workforce() if row.get("person_token") in token_set
        ]
        outcomes = [
            row for row in self.store.outcomes() if row.get("person_token") in token_set
        ]
        allocations = [
            row
            for row in self.store.journal("allocations")
            if row.get("person_token") in token_set
        ]
        supports = [
            row
            for row in self.store.journal("supports")
            if row.get("person_token") in token_set
        ]
        canonical = self._canonical_token(token)
        return {
            "person_token": token,
            "canonical_token": canonical,
            "identity_status": identity_status,
            "tokens": tokens,
            "clash_id": clash_id,
            "workforce_records": workforce,
            "outcomes": outcomes,
            "allocations": allocations,
            "supports": supports,
        }

    def _canonical_token(self, token: str) -> str:
        """Map a resolved alias token to its confirmed token; unresolved stay apart."""
        return self._canonical_map().get(token, token)

    def _canonical_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for clash in self.store.journal("clashes"):
            if clash.get("status") != "resolved":
                continue
            confirmed = clash.get("confirmed_token")
            if not confirmed:
                continue
            for token in _split_tokens(clash.get("tokens", "")):
                mapping[token] = confirmed
        return mapping

    def register_identity_clash(
        self, primary_token: str, tokens: Sequence[str], note: str = ""
    ) -> dict[str, object]:
        """Record candidate tokens as *unresolved*; never merge them yet."""
        primary = clean(primary_token)
        if primary is None:
            raise AppError("primary_token 不能为空", code="invalid_person")
        candidates: list[str] = []
        for value in [primary, *tokens]:
            token = clean(value)
            if token and token not in candidates:
                candidates.append(token)
        if len(candidates) < 2:
            raise AppError("身份待核至少需要两个候选代号", code="invalid_identity_clash")

        for clash in self.store.unresolved_clashes():
            if set(_split_tokens(clash.get("tokens", ""))) == set(candidates):
                return {**clash, "tokens": candidates}

        row = {
            "clash_id": new_id("CL"),
            "primary_token": primary,
            "tokens": "|".join(candidates),
            "status": "unresolved",
            "note": clean(note) or "",
            "confirmed_token": "",
            "created_at": now(),
            "resolved_at": "",
        }
        stored = self.store.append("clashes", row)
        return {**stored, "tokens": candidates}

    def resolve_identity(self, clash_id: str, confirmed_token: str) -> dict[str, object]:
        confirmed = clean(confirmed_token)
        rows = self.store.journal("clashes")
        target = None
        for row in rows:
            if row.get("clash_id") == clash_id:
                target = row
                break
        if target is None:
            raise AppError(f"身份待核记录不存在: {clash_id}", code="clash_not_found")
        if target.get("status") != "unresolved":
            raise AppError(f"身份待核记录已处理: {clash_id}", code="clash_already_resolved")
        candidates = _split_tokens(target.get("tokens", ""))
        if confirmed not in candidates:
            raise AppError(
                f"确认代号 {confirmed!r} 不在候选列表中", code="invalid_confirmed_token"
            )
        for row in rows:
            if row.get("clash_id") == clash_id:
                row["status"] = "resolved"
                row["confirmed_token"] = confirmed
                row["resolved_at"] = now()
        self.store.rewrite("clashes", rows)
        return {
            "clash_id": clash_id,
            "status": "resolved",
            "confirmed_token": confirmed,
            "tokens": [confirmed],
        }

    # ------------------------------------------------------------------
    # Job-code equivalence
    # ------------------------------------------------------------------
    def register_job_alias(
        self,
        alias_code: str,
        canonical_code: str,
        *,
        confirmed: bool = False,
        note: str = "",
    ) -> dict[str, object]:
        alias = clean(alias_code)
        canonical = clean(canonical_code)
        if alias is None or canonical is None:
            raise AppError("岗位别名与标准岗位代码不能为空", code="invalid_job_alias")
        if alias == canonical:
            raise AppError("岗位别名不能与标准代码相同", code="invalid_job_alias")
        row = {
            "alias_code": alias,
            "canonical_code": canonical,
            "status": "confirmed" if confirmed else "pending",
            "note": clean(note) or "",
            "recorded_at": now(),
        }
        return self.store.append("job_aliases", row)

    def job_equivalence(self) -> dict[str, list[dict[str, str]]]:
        latest: dict[str, dict[str, str]] = {}
        for row in self.store.journal("job_aliases"):
            latest[row.get("alias_code", "")] = row
        confirmed: list[dict[str, str]] = []
        pending: list[dict[str, str]] = []
        for row in latest.values():
            (confirmed if row.get("status") == "confirmed" else pending).append(row)
        return {
            "confirmed": sorted(confirmed, key=lambda row: row.get("alias_code", "")),
            "pending": sorted(pending, key=lambda row: row.get("alias_code", "")),
        }

    # ------------------------------------------------------------------
    # Seat allocation
    # ------------------------------------------------------------------
    def allocate_seats(
        self,
        course_id: str,
        candidates: Iterable[object],
        *,
        priority_groups: Sequence[str] | None = None,
    ) -> dict[str, object]:
        course = self.store.course(course_id)
        if not _course_active(course):
            raise AppError(
                f"课程 {course_id} 状态为 {course.get('commitment_status')!r}，不能分配名额",
                code="course_inactive",
            )
        capacity = _course_seats(course)
        if capacity < 0:
            raise AppError("课程名额不能为负数", code="invalid_capacity")

        requested = self._normalise_candidates(candidates)
        if not requested:
            raise AppError("没有可分配的人员", code="empty_candidates")

        unresolved = set()
        for clash in self.store.unresolved_clashes():
            unresolved.update(_split_tokens(clash.get("tokens", "")))
        blocked = sorted({token for token, _ in requested} & unresolved)
        if blocked:
            raise AppError(
                "以下人员身份尚未确认，不能分配名额: " + ", ".join(blocked),
                code="unresolved_identity",
            )

        if priority_groups:
            order = {group: index for index, group in enumerate(priority_groups)}
            requested.sort(key=lambda item: order.get(item[1] or "", len(order)))

        journal = self.store.journal("allocations")
        canonical = self._canonical_map()

        def person_of(row: dict[str, str]) -> str:
            raw = row.get("person_token", "")
            return canonical.get(raw, raw)

        allocated_now = sum(
            1
            for row in journal
            if row.get("course_id") == course_id and row.get("status") == "allocated"
        )
        already_allocated = {
            person_of(row)
            for row in journal
            if row.get("course_id") == course_id and row.get("status") == "allocated"
        }
        waitlisted_rows = {
            person_of(row): row
            for row in journal
            if row.get("course_id") == course_id and row.get("status") == "waitlisted"
        }

        basis = "提交顺序"
        if priority_groups:
            basis = "群体优先级（" + "、".join(priority_groups) + "）"

        allocated: list[str] = []
        waitlisted: list[str] = []
        decisions: list[dict[str, object]] = []
        new_rows: list[dict[str, object]] = []
        promoted: set[str] = set()

        for token, group in requested:
            person = canonical.get(token, token)
            if person in already_allocated:
                allocated.append(token)
                decisions.append(
                    {
                        "person_token": token,
                        "group_code": group,
                        "decision": "allocated",
                        "reason": f"已持有课程 {course_id} 名额，本次不重复占用",
                    }
                )
                continue
            if allocated_now < capacity:
                allocated_now += 1
                remaining = capacity - allocated_now
                reason = (
                    f"课程 {course_id}（技能 {course.get('skill_code')}）尚有剩余名额；"
                    f"依据：{basis}；分配后剩余 {remaining} 个名额"
                )
                new_rows.append(
                    {
                        "allocation_id": new_id("AL"),
                        "course_id": course_id,
                        "person_token": token,
                        "group_code": group or "",
                        "status": "allocated",
                        "reason": reason,
                        "allocated_at": now(),
                        "released_at": "",
                    }
                )
                allocated.append(token)
                already_allocated.add(person)
                if person in waitlisted_rows:
                    promoted.add(person)
                decisions.append(
                    {
                        "person_token": token,
                        "group_code": group,
                        "decision": "allocated",
                        "reason": reason,
                    }
                )
            else:
                existing = waitlisted_rows.get(person)
                if existing is None:
                    reason = (
                        f"课程 {course_id} 名额已满（{allocated_now}/{capacity}），"
                        f"按{basis}列入候补"
                    )
                    row = {
                        "allocation_id": new_id("AL"),
                        "course_id": course_id,
                        "person_token": token,
                        "group_code": group or "",
                        "status": "waitlisted",
                        "reason": reason,
                        "allocated_at": "",
                        "released_at": "",
                    }
                    new_rows.append(row)
                    waitlisted_rows[person] = {
                        field: ("" if value is None else str(value)) for field, value in row.items()
                    }
                else:
                    reason = existing.get("reason") or f"课程 {course_id} 名额已满，候补中"
                waitlisted.append(token)
                decisions.append(
                    {
                        "person_token": token,
                        "group_code": group,
                        "decision": "waitlisted",
                        "reason": reason,
                    }
                )

        for row in new_rows:
            self.store.append("allocations", row)

        if promoted:
            journal = self.store.journal("allocations")
            for row in journal:
                if (
                    row.get("course_id") == course_id
                    and canonical.get(row.get("person_token", ""), row.get("person_token", ""))
                    in promoted
                    and row.get("status") == "waitlisted"
                ):
                    row["status"] = "promoted"
                    row["released_at"] = now()
            self.store.rewrite("allocations", journal)

        return {
            "course_id": course_id,
            "capacity": capacity,
            "seats": capacity,
            "allocated": allocated,
            "waitlisted": waitlisted,
            "remaining_seats": capacity - allocated_now,
            "decisions": decisions,
        }

    @staticmethod
    def _normalise_candidates(candidates: Iterable[object]) -> list[tuple[str, str | None]]:
        result: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for item in candidates:
            if isinstance(item, str):
                token, group = item, None
            elif isinstance(item, dict):
                token = item.get("person_token") or item.get("token")
                group = item.get("group_code") or item.get("group")
            elif isinstance(item, (tuple, list)) and len(item) >= 1:
                token = item[0]
                group = item[1] if len(item) > 1 else None
            else:
                raise AppError(f"无法识别的人员参数: {item!r}", code="invalid_candidate")
            token = clean(token)
            if token is None:
                raise AppError("人员代号不能为空", code="invalid_candidate")
            if token in seen:
                continue
            seen.add(token)
            result.append((token, clean(group)))
        return result

    def release_seat(
        self,
        course_id: str,
        person_token: str,
        note: str | None = None,
        *,
        reason: str | None = None,
    ) -> dict[str, object]:
        effective = clean(reason) or clean(note)
        if effective is None:
            raise AppError("释放名额必须说明原因", code="missing_reason")
        token = clean(person_token)
        rows = self.store.journal("allocations")
        target = None
        for row in rows:
            if (
                row.get("course_id") == course_id
                and row.get("person_token") == token
                and row.get("status") == "allocated"
            ):
                target = row
                break
        if target is None:
            raise AppError(
                f"{token} 没有课程 {course_id} 的有效名额", code="allocation_not_found"
            )
        for row in rows:
            if row is target:
                row["status"] = "released"
                row["reason"] = effective
                row["released_at"] = now()
        self.store.rewrite("allocations", rows)
        course = self.store.course(course_id)
        allocated_now = sum(
            1
            for row in rows
            if row.get("course_id") == course_id and row.get("status") == "allocated"
        )
        return {
            "course_id": course_id,
            "person_token": token,
            "status": "released",
            "reason": effective,
            "remaining_seats": _course_seats(course) - allocated_now,
        }

    def explain_allocation(self, course_id: str) -> dict[str, object]:
        course = self.store.course(course_id)
        rows = [
            row for row in self.store.journal("allocations") if row.get("course_id") == course_id
        ]
        decisions = [
            {
                "person_token": row.get("person_token"),
                "group_code": row.get("group_code") or None,
                "decision": row.get("status"),
                "reason": row.get("reason"),
                "allocated_at": row.get("allocated_at") or None,
                "released_at": row.get("released_at") or None,
            }
            for row in rows
        ]
        allocated_count = sum(1 for row in rows if row.get("status") == "allocated")
        waitlisted_count = sum(1 for row in rows if row.get("status") == "waitlisted")
        capacity = _course_seats(course)
        return {
            "course_id": course_id,
            "capacity": capacity,
            "seats": capacity,
            "skill_code": course.get("skill_code"),
            "provider_id": course.get("provider_id"),
            "allocated_count": allocated_count,
            "waitlisted_count": waitlisted_count,
            "released_count": sum(1 for row in rows if row.get("status") == "released"),
            "remaining_seats": capacity - allocated_count,
            "decisions": decisions,
        }

    # ------------------------------------------------------------------
    # Training spend
    # ------------------------------------------------------------------
    def spend_summary(self) -> dict[str, object]:
        """Committed training spend survives releases and later corrections.

        Spend is committed once per (course, person): releasing a seat does not
        erase the commitment, and re-allocating the same person to the same
        course does not create a second charge. Only currently held seats count
        as outstanding.
        """
        by_course: dict[str, dict[str, int]] = {}
        committed_pairs: set[tuple[str, str]] = set()
        outstanding_pairs: set[tuple[str, str]] = set()
        committed = 0
        outstanding = 0
        allocations = self.store.journal("allocations")

        for row in allocations:
            status = row.get("status")
            if status not in {"allocated", "released"}:
                continue
            course_id = row.get("course_id", "")
            token = row.get("person_token", "")
            course = self.store.course(course_id)
            fee = _course_fee(course)
            bucket = by_course.setdefault(
                course_id,
                {"committed": 0, "outstanding": 0, "allocated_seats": 0, "released_seats": 0},
            )
            pair = (course_id, token)
            if pair not in committed_pairs:
                committed_pairs.add(pair)
                committed += fee
                bucket["committed"] += fee
            if status == "allocated" and pair not in outstanding_pairs:
                outstanding_pairs.add(pair)
                outstanding += fee
                bucket["outstanding"] += fee
                bucket["allocated_seats"] += 1
            elif status == "released":
                bucket["released_seats"] += 1

        return {
            "committed": committed,
            "outstanding": outstanding,
            "seat_commitments": len(committed_pairs),
            "by_course": dict(sorted(by_course.items())),
        }

    # ------------------------------------------------------------------
    # Outcomes and corrections
    # ------------------------------------------------------------------
    def record_outcome(
        self,
        *,
        person_token: str,
        course_id: str,
        event_date: str,
        event_type: str,
        job_code: str | None = None,
        employer_id: str | None = None,
        retained_90d: bool | None = None,
        as_actual_from_commitment: bool = False,
    ) -> dict[str, object]:
        if as_actual_from_commitment:
            raise AppError(
                "企业计划招聘不能直接记为实际到岗；必须提供真实到岗记录",
                code="commitment_not_placement",
            )
        kind = clean(event_type)
        if kind not in OUTCOME_EVENT_TYPES:
            raise AppError(f"未知就业事件类型: {event_type!r}", code="invalid_event_type")
        token = clean(person_token)
        date = clean(event_date)
        if token is None or date is None:
            raise AppError("person_token 与 event_date 不能为空", code="invalid_outcome")
        self.store.course(course_id)

        if kind in {"retained_90d"} and not self._has_started(token, course_id):
            raise AppError(
                "尚无实际到岗记录，不能记录持续任职", code="missing_start"
            )
        if kind == "started" and retained_90d is True:
            raise AppError(
                "持续任职需在到岗后单独记录，不能与到岗同时标注",
                code="invalid_retention",
            )

        row = {
            "outcome_id": new_id("OE"),
            "person_token": token,
            "course_id": course_id,
            "event_date": date,
            "event_type": kind,
            "job_code": clean(job_code) or "",
            "employer_id": clean(employer_id) or "",
            "retained_90d": "" if retained_90d is None else str(bool(retained_90d)).lower(),
            "source": "recorded",
        }
        stored = self.store.append("outcome_events", row)
        return dict(stored)

    def _has_started(self, person_token: str, course_id: str) -> bool:
        canonical = self._canonical_map()
        target = canonical.get(person_token, person_token)
        return any(
            canonical.get(str(row.get("person_token") or ""), row.get("person_token")) == target
            and row.get("course_id") == course_id
            and row.get("event_type") == "started"
            for row in self.store.outcomes()
        )

    def correct_outcome(
        self,
        outcome_id: str,
        *,
        event_type: str | None = None,
        new_event_date: str | None = None,
        date_unknown: bool = False,
        reason: str,
    ) -> dict[str, object]:
        explanation = clean(reason)
        if explanation is None:
            raise AppError("更正就业结果必须说明原因", code="missing_reason")
        if new_event_date is not None and date_unknown:
            raise AppError(
                "不能同时提供新日期并声明日期未知", code="conflicting_date"
            )
        current = None
        for row in self.store.outcomes():
            if row.get("outcome_id") == outcome_id:
                current = row
                break
        if current is None:
            raise AppError(f"就业记录不存在: {outcome_id}", code="outcome_not_found")

        new_kind = clean(event_type) or current.get("event_type")
        if new_kind not in OUTCOME_EVENT_TYPES:
            raise AppError(f"未知就业事件类型: {event_type!r}", code="invalid_event_type")
        if new_kind == "retained_90d" and not self._has_started(
            str(current.get("person_token")), str(current.get("course_id"))
        ):
            raise AppError("尚无实际到岗记录，不能记为持续任职", code="missing_start")

        # A correction states its intent explicitly: supply a new date, mark the
        # date unknown, or leave it unchanged. Silently carrying a doubtful old
        # date forward would present as known something we no longer know.
        if date_unknown:
            date_action = "unknown"
            resolved_date: str | None = None
        elif new_event_date is not None:
            date_action = "set"
            resolved_date = clean(new_event_date)
            if resolved_date is None:
                date_action = "unknown"
        else:
            date_action = "unchanged"
            resolved_date = current.get("event_date")
            if resolved_date is None:
                # Leaving an already-unknown date unchanged keeps it unknown;
                # it must not fall back to a value we previously doubted.
                date_action = "unknown"

        row = {
            "correction_id": new_id("CO"),
            "outcome_id": outcome_id,
            "previous_event_type": current.get("event_type") or "",
            "current_event_type": new_kind,
            "new_event_date": resolved_date or "",
            "date_action": date_action,
            "reason": explanation,
            "corrected_at": now(),
        }
        self.store.append("corrections", row)
        return {
            "correction_id": row["correction_id"],
            "outcome_id": outcome_id,
            "previous_event_type": row["previous_event_type"],
            "current_event_type": new_kind,
            "new_event_date": resolved_date,
            "date_action": date_action,
            "reason": explanation,
            "corrected_at": row["corrected_at"],
        }

    # ------------------------------------------------------------------
    # Funnel / gap / regional summaries
    # ------------------------------------------------------------------
    def employment_funnel(self) -> dict[str, object]:
        """Plans, offers, actual starts and retention, each counted separately.

        Headline placement counts are *distinct persons*: the same worker
        appearing under several anonymous records (or in several sources) is
        counted once. Per-course figures use distinct (person, course) pairs.
        """
        courses = self.store.courses()
        planned = sum(_course_openings(course) for course in courses)
        active_planned = sum(
            _course_openings(course) for course in courses if _course_active(course)
        )

        offers_overall: set[tuple[str, str]] = set()
        completed_overall: set[tuple[str, str]] = set()
        started_overall: set[str] = set()
        retained_overall: set[str] = set()
        canonical = self._canonical_map()
        by_course: dict[str, dict[str, object]] = {
            course.get("course_id", ""): {
                "planned_openings": _course_openings(course),
                "offers": set(),
                "completed": set(),
                "started": set(),
                "retained_90d": set(),
            }
            for course in courses
        }

        for row in self.store.outcomes():
            course_id = str(row.get("course_id") or "")
            raw_token = str(row.get("person_token") or "")
            token = canonical.get(raw_token, raw_token)
            if not token:
                continue
            employer = str(row.get("employer_id") or "")
            bucket = by_course.get(course_id)
            kind = row.get("event_type")

            if kind == "offer":
                offers_overall.add((token, employer or course_id))
                if bucket is not None:
                    bucket["offers"].add((token, employer))
            elif kind == "completed":
                completed_overall.add((token, course_id))
                if bucket is not None:
                    bucket["completed"].add(token)
            elif kind == "started":
                started_overall.add(token)
                if bucket is not None:
                    bucket["started"].add(token)

            if kind == "retained_90d" or (
                kind in {"started", "offer", "completed"} and row.get("retained_90d") is True
            ):
                retained_overall.add(token)
                if bucket is not None:
                    bucket["retained_90d"].add(token)

        started = len(started_overall)
        offers = len(offers_overall)
        retained = len(retained_overall)
        rendered: dict[str, dict[str, object]] = {}
        for course_id, bucket in by_course.items():
            rendered[course_id] = {
                "planned_openings": bucket["planned_openings"],
                "offers": len(bucket["offers"]),  # type: ignore[arg-type]
                "completed": len(bucket["completed"]),  # type: ignore[arg-type]
                "started": len(bucket["started"]),  # type: ignore[arg-type]
                "retained_90d": len(bucket["retained_90d"]),  # type: ignore[arg-type]
            }
        return {
            "planned_openings": planned,
            "active_planned_openings": active_planned,
            "offers": offers,
            "completed": len(completed_overall),
            "started": started,
            "retained_90d": retained,
            "placement_without_offer": started - len(
                {token for token, _ in offers_overall}
            ),
            "by_course": dict(sorted(rendered.items())),
        }

    def unmapped_jobs(self) -> list[dict[str, object]]:
        """Course/job codes that cannot yet be matched to one another.

        A course names the skill it trains and, separately, the employer job it
        is meant to feed. When those two strings differ and no confirmed alias
        links them, the link is unknown and is reported here instead of being
        silently treated as a match.
        """
        alias_map = {
            row["alias_code"]: row["canonical_code"]
            for row in self.job_equivalence()["confirmed"]
        }
        known = set(alias_map)
        known.update(alias_map.values())
        pending = {
            row["alias_code"]: row["canonical_code"]
            for row in self.job_equivalence()["pending"]
        }

        result: list[dict[str, object]] = []
        for course in self.store.courses():
            skill = clean(course.get("skill_code"))
            employer_job = clean(course.get("employer_job_code"))
            if skill is None and employer_job is None:
                continue
            if skill is None or employer_job is None:
                status = "missing_code"
            elif skill == employer_job:
                status = "matched"
            elif alias_map.get(skill) == employer_job or alias_map.get(employer_job) == skill:
                status = "matched"
            elif skill in pending or employer_job in pending:
                status = "pending_alias"
            elif skill in known and employer_job in known:
                status = "matched"
            else:
                status = "unresolved"
            result.append(
                {
                    "course_id": course.get("course_id"),
                    "skill_code": skill,
                    "employer_job_code": employer_job,
                    "status": status,
                }
            )
        return result

    def skill_gap(self) -> list[dict[str, object]]:
        alias_map = {
            row["alias_code"]: row["canonical_code"]
            for row in self.job_equivalence()["confirmed"]
        }
        skills: dict[str, dict[str, object]] = {}
        for course in self.store.courses():
            if not _course_active(course):
                continue
            skill = alias_map.get(course.get("skill_code", ""), course.get("skill_code", ""))
            employer_job = alias_map.get(
                course.get("employer_job_code", ""), course.get("employer_job_code", "")
            )
            entry = skills.setdefault(
                skill,
                {
                    "skill_code": skill,
                    "active_seats": 0,
                    "active_planned_openings": 0,
                    "courses": [],
                    "employer_jobs": [],
                },
            )
            entry["active_seats"] += _course_seats(course)  # type: ignore[operator]
            entry["active_planned_openings"] += _course_openings(course)  # type: ignore[operator]
            entry["courses"].append(course.get("course_id"))  # type: ignore[union-attr]
            if employer_job and employer_job not in entry["employer_jobs"]:  # type: ignore[operator]
                entry["employer_jobs"].append(employer_job)  # type: ignore[union-attr]

        allocations = self.store.journal("allocations")
        for entry in skills.values():
            course_ids = set(entry["courses"])  # type: ignore[arg-type]
            entry["allocated_seats"] = sum(
                1
                for row in allocations
                if row.get("course_id") in course_ids and row.get("status") == "allocated"
            )
            entry["waitlisted"] = sum(
                1
                for row in allocations
                if row.get("course_id") in course_ids and row.get("status") == "waitlisted"
            )
            entry["seat_gap"] = (  # type: ignore[operator]
                entry["active_seats"] - entry["active_planned_openings"]
            )
        unmapped = [
            item for item in self.unmapped_jobs() if item["status"] in {"unresolved", "pending_alias"}
        ]
        result = [dict(entry) for _, entry in sorted(skills.items())]
        for entry in result:
            entry["unresolved_job_links"] = [
                item for item in unmapped if item["course_id"] in entry["courses"]
            ]
        return result

    def regional_summary(self, period: str) -> dict[str, object]:
        label = clean(period)
        if label is None:
            raise AppError("period 不能为空", code="invalid_period")
        courses = [
            course for course in self.store.courses() if course.get("period") == label
        ]
        active = [course for course in courses if _course_active(course)]
        course_ids = {course.get("course_id", "") for course in active}
        planned = sum(_course_openings(course) for course in active)

        fulfilled = 0
        for row in self.store.journal("fulfillments"):
            if row.get("course_id") in course_ids:
                fulfilled += as_int(row.get("fulfilled_openings"), "fulfilled_openings")

        started: set[str] = set()
        retained: set[str] = set()
        offers: set[tuple[str, str]] = set()
        canonical = self._canonical_map()
        for row in self.store.outcomes():
            if row.get("course_id") not in course_ids:
                continue
            raw_token = str(row.get("person_token") or "")
            token = canonical.get(raw_token, raw_token)
            if not token:
                continue
            kind = row.get("event_type")
            if kind == "offer":
                offers.add((token, str(row.get("employer_id") or "")))
            elif kind == "started":
                started.add(token)
            if kind == "retained_90d" or (
                kind in {"started", "offer", "completed"} and row.get("retained_90d") is True
            ):
                retained.add(token)

        return {
            "period": label,
            "courses": sorted(course_ids),
            "planned_openings": planned,
            "fulfilled_openings": fulfilled,
            "offers": len(offers),
            "started": len(started),
            "retained_90d": len(retained),
            "opening_fulfillment_rate": round(fulfilled / planned, 3) if planned else None,
        }

    def line_adjustment_view(self) -> list[dict[str, object]]:
        """Production-line changes reported ahead of time, beside affected staff."""
        recent: dict[str, list[dict[str, str]]] = {}
        for row in self.store.production_lines():
            recent.setdefault(row.get("facility_id", ""), []).append(row)

        affected: dict[str, set[str]] = {}
        for row in self.store.workforce():
            if (row.get("employment_status") or "").strip().lower() in AFFECTED_STATUSES:
                affected.setdefault(row.get("employer_id", ""), set()).add(
                    row.get("person_token", "")
                )

        result: list[dict[str, object]] = []
        for facility, lines in sorted(recent.items()):
            result.append(
                {
                    "facility_id": facility,
                    "lines": [
                        {
                            "line_id": line.get("line_id"),
                            "product": line.get("product"),
                            "valid_from": clean(line.get("valid_from")),
                            "valid_to": clean(line.get("valid_to")),
                        }
                        for line in lines
                    ],
                    "affected_persons": len(affected.get(facility, set())),
                    "active_status": "at_risk" if facility in affected else "unknown",
                }
            )
        return result

    # ------------------------------------------------------------------
    # Employer fulfillment and support actions
    # ------------------------------------------------------------------
    def record_employer_fulfillment(
        self,
        *,
        course_id: str,
        employer_id: str,
        job_code: str,
        fulfilled_openings: int,
        report_date: str,
        note: str = "",
    ) -> dict[str, object]:
        self.store.course(course_id)
        employer = clean(employer_id)
        job = clean(job_code)
        date = clean(report_date)
        if employer is None or job is None or date is None:
            raise AppError("企业、岗位和报告日期不能为空", code="invalid_fulfillment")
        count = as_int(fulfilled_openings, "fulfilled_openings")
        if count < 0:
            raise AppError("兑现岗位数不能为负数", code="invalid_fulfillment")
        row = {
            "fulfillment_id": new_id("FU"),
            "course_id": course_id,
            "employer_id": employer,
            "job_code": job,
            "fulfilled_openings": count,
            "report_date": date,
            "note": clean(note) or "",
            "recorded_at": now(),
        }
        return self.store.append("fulfillments", row)

    def record_support(
        self,
        *,
        person_token: str,
        course_id: str | None,
        action_type: str,
        status: str,
        reason: str = "",
    ) -> dict[str, object]:
        token = clean(person_token)
        action = clean(action_type)
        kind = clean(status)
        if token is None or action is None:
            raise AppError("人员与支持类型不能为空", code="invalid_support")
        if kind not in SUPPORT_STATUSES:
            raise AppError(
                f"支持状态必须是 {sorted(SUPPORT_STATUSES)} 之一", code="invalid_support_status"
            )
        if course_id:
            self.store.course(course_id)
        row = {
            "action_id": new_id("SU"),
            "person_token": token,
            "course_id": course_id or "",
            "action_type": action,
            "status": kind,
            "reason": clean(reason) or "",
            "recorded_at": now(),
        }
        return self.store.append("supports", row)

    def support_status(self, person_token: str) -> dict[str, object]:
        """Latest state per action type; ``informed`` and ``approved`` are not ``executed``.

        A later ``executed`` record supersedes an earlier ``approved`` one, but
        the approval itself is retained in ``history``.
        """
        token = clean(person_token)
        if token is None:
            raise AppError("person_token 不能为空", code="invalid_person")
        canonical = self._canonical_map()
        target = canonical.get(token, token)
        matching = [
            row
            for row in self.store.journal("supports")
            if canonical.get(row.get("person_token", ""), row.get("person_token", "")) == target
        ]
        latest: dict[str, dict[str, str]] = {}
        for row in matching:
            latest[row.get("action_type", "")] = row
        actions = []
        for action_type, row in sorted(latest.items()):
            actions.append(
                {
                    "action_type": action_type,
                    "status": row.get("status"),
                    "executed": row.get("status") == "executed",
                    "course_id": row.get("course_id") or None,
                    "reason": row.get("reason") or None,
                    "recorded_at": row.get("recorded_at") or None,
                    "history": [
                        {
                            "status": old.get("status"),
                            "recorded_at": old.get("recorded_at") or None,
                            "reason": old.get("reason") or None,
                        }
                        for old in matching
                        if old.get("action_type") == action_type
                    ],
                }
            )
        return {
            "person_token": token,
            "actions": actions,
            "informed_only": [a for a in actions if a["status"] == "informed"],
            "awaiting_execution": [a for a in actions if a["status"] in UNEXECUTED_SUPPORT],
        }

    def impact_preview(self, course_id: str) -> dict[str, object]:
        """What outcome changes would do to support that has not run yet."""
        course = self.store.course(course_id)
        allocations = [
            row
            for row in self.store.journal("allocations")
            if row.get("course_id") == course_id and row.get("status") == "allocated"
        ]
        waitlisted = [
            row
            for row in self.store.journal("allocations")
            if row.get("course_id") == course_id and row.get("status") == "waitlisted"
        ]
        canonical = self._canonical_map()
        allocated_tokens = {
            canonical.get(str(row.get("person_token") or ""), str(row.get("person_token") or ""))
            for row in allocations
        }
        placed_tokens = {
            canonical.get(str(row.get("person_token") or ""), str(row.get("person_token") or ""))
            for row in self.store.outcomes()
            if row.get("event_type") == "started"
        }
        supports = [
            row
            for row in self.store.journal("supports")
            if row.get("course_id") == course_id
        ]
        latest_support: dict[tuple[str, str], dict[str, str]] = {}
        for row in supports:
            key = (
                canonical.get(row.get("person_token", ""), row.get("person_token", "")),
                row.get("action_type", ""),
            )
            latest_support[key] = row
        unexecuted_support = [
            row for row in latest_support.values() if row.get("status") in UNEXECUTED_SUPPORT
        ]
        fee = _course_fee(course)
        planned = _course_openings(course) if _course_active(course) else 0
        fulfilled = sum(
            as_int(row.get("fulfilled_openings"), "fulfilled_openings")
            for row in self.store.journal("fulfillments")
            if row.get("course_id") == course_id
        )
        return {
            "course_id": course_id,
            "capacity": _course_seats(course),
            "allocated_seats": len(allocations),
            "committed_spend": len(allocations) * fee,
            "planned_openings": planned,
            "fulfilled_openings": fulfilled,
            "unexecuted_support": unexecuted_support,
            "exposure": {
                "unfilled_planned_openings": max(planned - fulfilled, 0),
                "allocated_persons_without_placement": sorted(allocated_tokens - placed_tokens),
                "waitlisted_persons": sorted(row.get("person_token") for row in waitlisted),
                "support_actions_not_executed": len(unexecuted_support),
                "committed_spend": len(allocations) * fee,
            },
        }

    # ------------------------------------------------------------------
    # Top-level summary
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, object]:
        return {
            "funnel": self.employment_funnel(),
            "spend": self.spend_summary(),
            "groups": self.group_view(),
            "skills": self.skill_gap(),
            "job_equivalence": self.job_equivalence(),
            "line_adjustments": self.line_adjustment_view(),
        }
