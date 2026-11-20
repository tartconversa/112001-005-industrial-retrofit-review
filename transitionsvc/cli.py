"""Command-line entry point.

Usage examples::

    python -m transitionsvc summary --data data
    python -m transitionsvc group-view --data data
    python -m transitionsvc allocate C10 --data data --person P-17:G1 --person P-42:G2
    python -m transitionsvc explain C10 --data data
    python -m transitionsvc funnel --data data
    python -m transitionsvc regional 2026-Q2 --data data
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from .app import Service
from .errors import AppError


def _emit(payload: Any, stream: TextIO, *, as_json: bool) -> None:
    if as_json:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        return
    _print_human(payload, stream)


def _print_human(payload: Any, stream: TextIO, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                stream.write(f"{pad}{key}:\n")
                _print_human(value, stream, indent + 1)
            else:
                stream.write(f"{pad}{key}: {value}\n")
    elif isinstance(payload, list):
        if not payload:
            stream.write(f"{pad}(空)\n")
        for index, item in enumerate(payload, start=1):
            if isinstance(item, (dict, list)):
                stream.write(f"{pad}[{index}]\n")
                _print_human(item, stream, indent + 1)
            else:
                stream.write(f"{pad}- {item}\n")
    else:
        stream.write(f"{pad}{payload}\n")


def _parse_person(value: str) -> tuple[str, str | None]:
    token, _, group = value.partition(":")
    return token.strip(), group.strip() or None


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=argparse.SUPPRESS, help="输入 CSV 目录（默认 data）")
    common.add_argument("--state", default=argparse.SUPPRESS, help="状态日志目录（默认 <data>/state）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出")

    parser = argparse.ArgumentParser(prog="transitionsvc", description="产业就业支持分析", parents=[common])
    parser.set_defaults(data="data", state=None, json=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common], **kwargs)

    add("summary", "汇总视图")
    add("funnel", "计划/录用/到岗/留任漏斗")
    add("groups", "按群体查看受影响人数与培训状态")
    add("skills", "按技能查看名额与岗位缺口")
    add("spend", "已承诺培训支出")
    add("equivalence", "岗位名称对应关系")
    add("lines", "生产线调整与受影响人数")

    person = add("person", "查看单个劳动者记录")
    person.add_argument("person_token")

    allocate = add("allocate", "分配课程名额")
    allocate.add_argument("course_id")
    allocate.add_argument("--person", action="append", default=[], metavar="TOKEN[:GROUP]")
    allocate.add_argument("--priority-group", action="append", default=[])

    release = add("release", "释放名额")
    release.add_argument("course_id")
    release.add_argument("person_token")
    release.add_argument("--reason", required=True)

    explain = add("explain", "解释名额分配依据")
    explain.add_argument("course_id")

    impact = add("impact", "就业结果变化对未执行支持的影响")
    impact.add_argument("course_id")

    regional = add("regional", "地区/周期汇总")
    regional.add_argument("period")
    return parser


COMMANDS = {
    "summary": lambda svc, args: svc.summary(),
    "funnel": lambda svc, args: svc.employment_funnel(),
    "groups": lambda svc, args: svc.group_view(),
    "skills": lambda svc, args: svc.skill_gap(),
    "spend": lambda svc, args: svc.spend_summary(),
    "equivalence": lambda svc, args: svc.job_equivalence(),
    "lines": lambda svc, args: svc.line_adjustment_view(),
    "person": lambda svc, args: svc.person_view(args.person_token),
    "allocate": lambda svc, args: svc.allocate_seats(
        args.course_id,
        [_parse_person(value) for value in args.person],
        priority_groups=args.priority_group,
    ),
    "release": lambda svc, args: svc.release_seat(
        args.course_id, args.person_token, reason=args.reason
    ),
    "explain": lambda svc, args: svc.explain_allocation(args.course_id),
    "impact": lambda svc, args: svc.impact_preview(args.course_id),
    "regional": lambda svc, args: svc.regional_summary(args.period),
}


def run(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    stream = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        store = Service(args.data)
        if args.state:
            from .store import Store

            store = Service(Store(args.data, args.state))
        payload = COMMANDS[args.command](store, args)
    except AppError as error:
        sys.stderr.write(f"错误[{error.code}]: {error.message}\n")
        return 2
    _emit(payload, stream, as_json=args.json)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
