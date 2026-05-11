from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import storage  # noqa: E402
from src.config import ConfigError, load_config  # noqa: E402


def _resolve_db_path() -> Path:
    try:
        return load_config().db_path
    except ConfigError:
        return Path(__file__).resolve().parent.parent / "data" / "bot.db"


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sys.stdout.write(line + "\n")
    sys.stdout.write("  ".join("-" * w for w in widths) + "\n")
    for row in rows:
        sys.stdout.write(
            "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + "\n"
        )


def _truncate(value: str | None, width: int) -> str:
    if value is None:
        return ""
    flat = value.replace("\n", " ").replace("\r", " ")
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def cmd_contacts(db_path: Path, _args: argparse.Namespace) -> int:
    contacts = storage.get_all_contacts(db_path)
    if not contacts:
        sys.stdout.write("(no contacts)\n")
        return 0
    headers = ("chat_id", "tg_user_id", "username", "name", "msgs", "last_seen_at")
    rows: list[tuple[str, ...]] = []
    for c in contacts:
        name = " ".join(
            x for x in (c.get("first_name"), c.get("last_name")) if x
        )
        rows.append(
            (
                str(c["chat_id"]),
                str(c["tg_user_id"]),
                str(c.get("username") or ""),
                _truncate(name, 30),
                str(c["message_count"]),
                str(c["last_seen_at"]),
            )
        )
    _print_table(headers, rows)
    return 0


def cmd_messages(db_path: Path, args: argparse.Namespace) -> int:
    messages = storage.get_recent_messages(db_path, int(args.chat_id), int(args.limit))
    if not messages:
        sys.stdout.write("(no messages)\n")
        return 0
    headers = ("id", "tg_msg_id", "dir", "sender_id", "created_at", "text")
    rows: list[tuple[str, ...]] = []
    for m in messages:
        rows.append(
            (
                str(m["id"]),
                str(m["tg_message_id"]),
                str(m["direction"]),
                str(m["sender_id"]),
                str(m["created_at"]),
                _truncate(m.get("text"), 80),
            )
        )
    _print_table(headers, rows)
    return 0


def cmd_events(db_path: Path, args: argparse.Namespace) -> int:
    events = storage.get_recent_events(db_path, int(args.limit))
    if not events:
        sys.stdout.write("(no events)\n")
        return 0
    headers = ("id", "event_type", "created_at", "payload")
    rows: list[tuple[str, ...]] = []
    for e in events:
        payload: Any
        try:
            payload = json.loads(e["payload_json"])
            payload_text = json.dumps(payload, default=str, ensure_ascii=False)
        except (ValueError, TypeError):
            payload_text = e["payload_json"]
        rows.append(
            (
                str(e["id"]),
                str(e["event_type"]),
                str(e["created_at"]),
                _truncate(payload_text, 100),
            )
        )
    _print_table(headers, rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only inspection CLI for the local SQLite DB."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("contacts", help="List contacts with message counts.")

    p_msg = sub.add_parser("messages", help="Show recent messages for a chat_id.")
    p_msg.add_argument("chat_id", type=int)
    p_msg.add_argument("--limit", type=int, default=30)

    p_evt = sub.add_parser("events", help="Show recent operational events.")
    p_evt.add_argument("--limit", type=int, default=50)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = _resolve_db_path()
    if not db_path.exists():
        sys.stderr.write(f"DB not found at {db_path}\n")
        return 1
    if args.command == "contacts":
        return cmd_contacts(db_path, args)
    if args.command == "messages":
        return cmd_messages(db_path, args)
    if args.command == "events":
        return cmd_events(db_path, args)
    sys.stderr.write(f"Unknown command: {args.command}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
