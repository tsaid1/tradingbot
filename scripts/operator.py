#!/usr/bin/env python3
"""Operator actions on the runtime service's state (mock mode). None of them sends an order.

    PYTHONPATH=src python scripts/operator.py --config runtime.toml list
    PYTHONPATH=src python scripts/operator.py --config runtime.toml settle TB-... NOT_AT_BROKER \\
        --note "checked TWS: no such order" --confirm "SETTLE TB-... NOT_AT_BROKER"
    PYTHONPATH=src python scripts/operator.py --config runtime.toml settle TB-... FILLED \\
        --quantity 2 --price 88.42 --note "..." --confirm "SETTLE TB-... FILLED"
    PYTHONPATH=src python scripts/operator.py --config runtime.toml clear 7 LEDGER_REVERSAL ...
    PYTHONPATH=src python scripts/operator.py --config runtime.toml ack 9 EXIT_WITHOUT_QUOTE ...
    PYTHONPATH=src python scripts/operator.py --config runtime.toml ack-signal 3 SIGNAL_HOLD ...
    PYTHONPATH=src python scripts/operator.py --config runtime.toml clear-lockout SIM-PAPER-0001 ...
    PYTHONPATH=src python scripts/operator.py --config runtime.toml apply
    PYTHONPATH=src python scripts/operator.py --config runtime.toml history

Every action is queued (append-only) with its note and typed confirmation, then applied by the
process that holds the execution store: the running service on its next cycle, or this command at
once when the service is stopped. ``apply`` applies whatever is pending. See docs/OPERATIONS.md.

Exit codes: 0 queued or applied, 3 refused, 2 bad input or config.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime

from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tradingbot.runtime.register import load_register


def _apply_now(config) -> list[dict] | None:
    """Apply pending requests here when no service holds the execution store; None if one does."""
    from tradingbot.execution.coordinator import ExecutionCoordinator, ExecutionStoreLocked
    from tradingbot.execution.sim_broker import SimBroker
    from tradingbot.runtime.operator_apply import LOCKED_OUT, apply_pending, session_state
    from tradingbot.runtime.service import execution_config
    from tradingbot.runtime.store import RuntimeStore

    if config.mode != "mock":
        raise ConfigError("only mock mode may run")
    try:
        broker = SimBroker(config.broker_db, account=config.mock.account)
        coordinator = ExecutionCoordinator(
            broker, config.execution_db, config.policy, execution_config(config)
        )
    except ExecutionStoreLocked:
        return None
    try:
        store = RuntimeStore(config.runtime_db)
        return apply_pending(
            coordinator=coordinator, runtime_store=store, queue=Q.OperatorQueue(config.operator_db),
            expected_account=config.execution.expected_account, clock=lambda: datetime.now(UTC),
            locked_out=session_state(store)["state"] == LOCKED_OUT,
        )
    finally:
        coordinator.close()


def _print_list(config) -> None:
    reg = load_register(config)
    print(json.dumps({k: reg[k] for k in ("halted", "session", "heartbeat_age_seconds",
                                          "unresolved_intents", "open", "alerts_open")},
                     indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--operator", default=None, help="your name (default: the login name)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="open exceptions, unresolved intents, open alerts (read-only)")
    sub.add_parser("history", help="every operator request and its result")
    sub.add_parser("apply", help="apply pending requests now (service stopped)")

    def action(name, help_, *positional):
        p = sub.add_parser(name, help=help_)
        for pos in positional:
            p.add_argument(pos)
        p.add_argument("--note", required=True, help="what you checked at the broker")
        p.add_argument("--confirm", required=True, help="the exact confirmation words")
        return p

    s = action("settle", "settle an intent of unknown outcome", "order_ref", "outcome")
    s.add_argument("--quantity", type=int)
    s.add_argument("--price")
    action("clear", "clear a verified exception (LEDGER_REVERSAL)", "exception_id", "kind")
    action("ack", "acknowledge an informational exception", "exception_id", "kind")
    action("ack-signal", "acknowledge a held signal", "exception_id", "kind")
    action("clear-lockout", "clear a broker identity lockout", "account")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "list":
        _print_list(config)
        return 0
    queue = Q.OperatorQueue(config.operator_db)
    if args.cmd == "history":
        print(json.dumps(queue.requests(), indent=2, default=str))
        return 0
    if args.cmd != "apply":
        spec = {
            "settle": (Q.SETTLE, lambda a: a.order_ref),
            "clear": (Q.CLEAR, lambda a: a.exception_id),
            "ack": (Q.ACKNOWLEDGE, lambda a: a.exception_id),
            "ack-signal": (Q.ACKNOWLEDGE_SIGNAL, lambda a: a.exception_id),
            "clear-lockout": (Q.CLEAR_IDENTITY_LOCKOUT, lambda a: a.account),
        }[args.cmd]
        try:
            request_id = queue.enqueue(
                at=datetime.now(UTC), operator=args.operator or getpass.getuser(), origin="cli",
                action=spec[0], target=spec[1](args), kind=getattr(args, "kind", None),
                outcome=getattr(args, "outcome", None), quantity=getattr(args, "quantity", None),
                avg_price=getattr(args, "price", None), note=args.note, confirmation=args.confirm,
            )
        except Q.OperatorInputError as exc:
            print(f"not queued: {exc}", file=sys.stderr)
            return 2
        print(f"queued as request {request_id}")
    try:
        results = _apply_now(config)
    except ConfigError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    if results is None:
        print("the service holds the execution store: it applies pending requests on its next "
              "cycle (see `history`)")
        return 0
    print(json.dumps(results, indent=2))
    return 3 if any(r["status"] == "REFUSED" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
