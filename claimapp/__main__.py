"""命令行入口：只读报告与可持久化的业务动作。

    python3 -m claimapp report
    python3 -m claimapp lot L1
    python3 -m claimapp statement C2
    python3 -m claimapp correction X1
    python3 -m claimapp reserve --ledger claims.json --claim K1 --customer U9 \\
        --output O1-1 --boundary B2 --quantity 5 --at 2025-02-10T08:00:00+08:00
    python3 -m claimapp issue --ledger claims.json --claim K1 --at ...
    python3 -m claimapp release --ledger claims.json --claim K1 --quantity 2 --at ...
    python3 -m claimapp requests
    python3 -m claimapp check
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from . import ledger, reports
from .records import DataError, load_dataset
from .store import LedgerStore


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _load(args):
    return load_dataset(args.root)


def _store(args) -> LedgerStore:
    return LedgerStore(Path(args.ledger))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claimapp", description="生物质减排声明的证据与份额核对")
    parser.add_argument("--root", default=None, help="资料包根目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="核对资料、份额守恒与证据影响")
    sub.add_parser("report", help="完整核对报告")
    sub.add_parser("summary", help="共享假设、可承诺量与更正影响摘要")
    p = sub.add_parser("requests", help="并发请求判定")
    p.add_argument("--ledger", default=None, help="可选的持久化账本")

    p = sub.add_parser("lot", help="按来源核对全链条")
    p.add_argument("lot_id")

    p = sub.add_parser("statement", help="追溯一份声明")
    p.add_argument("claim_id")

    p = sub.add_parser("correction", help="追溯一次来源更正及受影响客户")
    p.add_argument("correction_id")

    p = sub.add_parser("reserve", help="登记预留")
    _claim_args(p)
    p.add_argument("--claim", required=True)

    p = sub.add_parser("issue", help="把预留签发为说明")
    p.add_argument("--ledger", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--at", required=True)

    p = sub.add_parser("release", help="客户缩减用量，释放未签发预留")
    p.add_argument("--ledger", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--quantity", required=True, type=float)
    p.add_argument("--at", required=True)

    p = sub.add_parser("ledger", help="查看持久化账本")
    p.add_argument("--ledger", required=True)

    for action in sub.choices.values():
        if not any(a.dest == "root" for a in action._actions):
            action.add_argument("--root", default=None, help="资料包根目录（也可放在子命令后）")

    args = parser.parse_args(argv)
    if args.root is None:
        args.root = str(Path(__file__).resolve().parents[1])
    try:
        dataset = _load(args)
        if args.command == "check":
            over = ledger.overcommitments(dataset)
            if over:
                _dump({"status": "error", "overcommitments": over})
                return 1
            _dump({"status": "ok", "overcommitments": [], "evidence": reports.emissions.evidence_label(dataset)})
            return 0
        if args.command == "report":
            _dump(reports.inventory(dataset))
        elif args.command == "summary":
            _dump(reports.summary(dataset))
        elif args.command == "requests":
            scoped = _store(args).dataset(dataset) if args.ledger else dataset
            _dump(ledger.settle_incoming(scoped))
        elif args.command == "lot":
            _dump(reports.lot_view(dataset, args.lot_id))
        elif args.command == "statement":
            _dump(reports.statement_view(dataset, args.claim_id))
        elif args.command == "correction":
            _dump(reports.correction_view(dataset, args.correction_id))
        elif args.command == "reserve":
            store = _store(args)
            _stamp(args.at)
            result = store.reserve(
                claim_id=args.claim,
                customer=args.customer,
                output_id=args.output,
                boundary_id=args.boundary,
                quantity=args.quantity,
                at=args.at,
                dataset=dataset,
            )
            _dump(result)
        elif args.command == "issue":
            store = _store(args)
            _dump(store.issue(args.claim, args.at, dataset))
        elif args.command == "release":
            store = _store(args)
            _dump(store.release(args.claim, args.quantity, args.at, dataset))
        elif args.command == "ledger":
            store = _store(args)
            if not store.path.exists():
                raise FileNotFoundError("账本文件不存在：" + str(store.path))
            _dump(store.as_dict())
        return 0
    except (DataError, ValueError, FileNotFoundError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


def _claim_args(parser) -> None:
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--boundary", required=True)
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--at", required=True)


def _stamp(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时刻必须带时区：" + value)
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
