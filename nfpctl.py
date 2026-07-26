#!/usr/bin/env python3
"""SSH fallback CLI for Mochi Forward.

This tool deliberately shares the WebUI database and NftManager rather than
editing nftables configuration directly. It is intended for root on the panel
host when the WebUI is unavailable.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_ENV_FILE = "/etc/nft-forward-panel.env"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_env_file(path: str) -> None:
    """Load a systemd-style KEY=value file without evaluating shell syntax."""
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"cannot read environment file {env_path}: {exc}")
    for line_number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            fail(f"invalid environment entry at {env_path}:{line_number}")
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(key):
            fail(f"invalid environment key at {env_path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mochi Forward SSH fallback CLI. Run as root on the panel host."
    )
    parser.add_argument(
        "--env-file", default=os.environ.get("PANEL_ENV_FILE", DEFAULT_ENV_FILE),
        help=f"systemd-style environment file (default: {DEFAULT_ENV_FILE})",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="list managed forwarding rules")
    subparsers.add_parser("status", help="show nftables and firewall status")

    add = subparsers.add_parser("add", help="add a TCP and UDP forwarding rule")
    add.add_argument("--port", required=True, help="local listening port")
    add.add_argument("--target", required=True, help="destination IPv4 address")
    add.add_argument("--target-port", help="destination port; defaults to --port")
    add.add_argument("--owner", help="existing account username or identity ID; defaults to an active admin")
    add.add_argument("--inbound-mbps", type=int, help="override inbound limit in Mbps")
    add.add_argument("--outbound-mbps", type=int, help="override outbound limit in Mbps")
    add.add_argument("--force-port-conflict", action="store_true", help="allow a port already used by a local service")

    remove = subparsers.add_parser("remove", help="remove a forwarding rule by ID")
    remove.add_argument("rule_id", type=int)
    remove.add_argument("--yes", action="store_true", help="skip confirmation")

    clear = subparsers.add_parser("clear", help="remove every managed forwarding rule")
    clear.add_argument("--yes", action="store_true", help="required confirmation")
    subparsers.add_parser("menu", help="open the interactive SSH menu")
    return parser


def require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        fail("this command must run as root, for example: sudo nfpctl list")


def ask_confirmation(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def db_connect(app: Any) -> sqlite3.Connection:
    connection = sqlite3.connect(app.config["DATABASE"], timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def manager_for(app: Any) -> Any:
    from nft_manager import NftManager

    return NftManager(app.config["FORWARD_CONFIG"], app.config["MAIN_CONFIG"], app.config["SYSCTL_CONFIG"])


def audit(connection: sqlite3.Connection, action: str, target: str, details: str) -> None:
    from app import now

    connection.execute(
        "INSERT INTO audit_events (actor_id, action, target, details, remote_addr, created_at) VALUES (NULL, ?, ?, ?, 'ssh-cli', ?)",
        (action, target, details, now()),
    )


def list_rules(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT r.*, u.username AS owner_name FROM forward_rules r
           JOIN users u ON u.id = r.owner_id ORDER BY r.listen_port"""
    ).fetchall()


def enabled_rules(connection: sqlite3.Connection, excluded_id: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM forward_rules WHERE paused_reason = ''"
    params: tuple[int, ...] = ()
    if excluded_id is not None:
        sql += " AND id != ?"
        params = (excluded_id,)
    return connection.execute(sql + " ORDER BY listen_port", params).fetchall()


def print_rules(connection: sqlite3.Connection) -> None:
    rules = list_rules(connection)
    if not rules:
        print("No managed forwarding rules.")
        return
    print(f"{'ID':<6} {'PORT':<8} {'DESTINATION':<24} {'OWNER':<16} {'LIMITS (IN/OUT)'}")
    for rule in rules:
        inbound = f"{rule['inbound_limit_mbps']} Mbps" if rule["inbound_limit_mbps"] else "unlimited"
        outbound = f"{rule['outbound_limit_mbps']} Mbps" if rule["outbound_limit_mbps"] else "unlimited"
        print(f"{rule['id']:<6} {rule['listen_port']:<8} {rule['destination_ip']}:{rule['destination_port']:<8} {rule['owner_name']:<16} {inbound} / {outbound}")


def default_owner(connection: sqlite3.Connection) -> sqlite3.Row:
    owner = connection.execute(
        "SELECT * FROM users WHERE active=1 AND role='admin' ORDER BY id LIMIT 1"
    ).fetchone()
    if owner is None:
        fail("no active administrator account exists; create or re-enable one in the database first")
    return owner


def get_owner(connection: sqlite3.Connection, username: str | None) -> sqlite3.Row:
    if not username:
        return default_owner(connection)
    owner = connection.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR identity_id = ?", (username, username)
    ).fetchone()
    if owner is None or not owner["active"]:
        fail("owner must be an existing active account")
    return owner


def add_rule(app: Any, args: argparse.Namespace) -> None:
    from app import ApplyLock, desired_pause_reason, monthly_usage, now, row_to_rule
    from nft_manager import ForwardRule, NftOperationError

    connection = db_connect(app)
    try:
        manager = manager_for(app)
        owner = get_owner(connection, args.owner)
        port = manager.validate_port(args.port)
        target_port = manager.validate_port(args.target_port or port)
        target = manager.validate_ipv4(args.target)
        if not int(owner["port_min"]) <= port <= int(owner["port_max"]):
            fail(f"owner {owner['username']} may only use ports {owner['port_min']}-{owner['port_max']}")
        count = connection.execute("SELECT COUNT(*) FROM forward_rules WHERE owner_id=?", (owner["id"],)).fetchone()[0]
        if int(owner["max_rules"]) and count >= int(owner["max_rules"]):
            fail(f"owner {owner['username']} has reached the rule limit")
        inbound = int(owner["default_inbound_mbps"]) if args.inbound_mbps is None else args.inbound_mbps
        outbound = int(owner["default_outbound_mbps"]) if args.outbound_mbps is None else args.outbound_mbps
        if not 0 <= inbound <= 100000 or not 0 <= outbound <= 100000:
            fail("bandwidth limits must be between 0 and 100000 Mbps")
        pause_reason = desired_pause_reason(owner, monthly_usage(connection, owner))
        new_rule = ForwardRule(None, port, target, target_port, int(owner["id"]), inbound, outbound)
        if manager.listening_port_in_use(port) and not args.force_port_conflict:
            fail("local port is in use; inspect it first and re-run with --force-port-conflict only when intended")
        with ApplyLock(Path(app.config["DATA_DIR"])):
            existing = connection.execute("SELECT * FROM forward_rules ORDER BY listen_port").fetchall()
            if any(int(item["listen_port"]) == port for item in existing):
                fail(f"listening port {port} already has a managed rule")
            candidates = [row_to_rule(item) for item in existing if not item["paused_reason"]]
            if not pause_reason:
                candidates.append(new_rule)
            manager.apply_rules(candidates)
            cursor = connection.execute(
                "INSERT INTO forward_rules (listen_port, destination_ip, destination_port, owner_id, inbound_limit_mbps, outbound_limit_mbps, paused_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (port, target, target_port, owner["id"], inbound, outbound, pause_reason, now()),
            )
            warnings = manager.firewall_open(new_rule) if not pause_reason else []
            audit(connection, "rule_create_ssh", str(cursor.lastrowid), f"{port} -> {target}:{target_port}; owner={owner['username']}")
            connection.commit()
        print(f"Added rule #{cursor.lastrowid}: {port} -> {target}:{target_port} ({owner['username']})")
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    except (ValueError, NftOperationError) as exc:
        connection.rollback()
        fail(str(exc))
    finally:
        connection.close()


def remove_rule(app: Any, rule_id: int, confirmed: bool) -> None:
    from app import ApplyLock, row_to_rule
    from nft_manager import NftOperationError

    connection = db_connect(app)
    try:
        manager = manager_for(app)
        row = connection.execute("SELECT * FROM forward_rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            fail("managed rule not found")
        if not confirmed and not ask_confirmation(f"Remove {row['listen_port']} -> {row['destination_ip']}:{row['destination_port']}?"):
            print("Cancelled.")
            return
        with ApplyLock(Path(app.config["DATA_DIR"])):
            all_rules = connection.execute("SELECT * FROM forward_rules ORDER BY listen_port").fetchall()
            remaining_rows = [item for item in all_rules if int(item["id"]) != rule_id]
            manager.apply_rules([row_to_rule(item) for item in remaining_rows if not item["paused_reason"]])
            removed = row_to_rule(row)
            shared_destination = any(
                item["destination_ip"] == removed.destination_ip and int(item["destination_port"]) == removed.destination_port
                for item in remaining_rows
            )
            connection.execute("DELETE FROM forward_rules WHERE id=?", (rule_id,))
            warnings = manager.firewall_close(removed, shared_destination)
            audit(connection, "rule_delete_ssh", str(rule_id), f"{removed.listen_port} -> {removed.destination_ip}:{removed.destination_port}")
            connection.commit()
        print(f"Removed rule #{rule_id}.")
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    except NftOperationError as exc:
        connection.rollback()
        fail(str(exc))
    finally:
        connection.close()


def clear_rules(app: Any, confirmed: bool) -> None:
    if not confirmed:
        fail("clear requires --yes")
    connection = db_connect(app)
    try:
        rows = list_rules(connection)
        if not rows:
            print("No managed forwarding rules.")
            return
        from app import ApplyLock, row_to_rule
        from nft_manager import NftOperationError
        manager = manager_for(app)
        with ApplyLock(Path(app.config["DATA_DIR"])):
            manager.apply_rules([])
            connection.execute("DELETE FROM forward_rules")
            for row in rows:
                warnings = manager.firewall_close(row_to_rule(row), False)
                for warning in warnings:
                    print(f"Warning: {warning}", file=sys.stderr)
            audit(connection, "rule_clear_ssh", "all", f"removed={len(rows)}")
            connection.commit()
        print(f"Removed {len(rows)} managed forwarding rule(s).")
    except NftOperationError as exc:
        connection.rollback()
        fail(str(exc))
    finally:
        connection.close()


def print_status(app: Any) -> None:
    status = manager_for(app).status()
    print(f"nftables installed: {'yes' if status['nft_available'] else 'no'}")
    print(f"managed table loaded: {'yes' if status['nft_table_loaded'] else 'no'}")
    print(f"IPv4 forwarding: {'enabled' if status['ip_forward'] else 'disabled'}")
    print(f"firewall integration: {status['firewall']}")
    connection = db_connect(app)
    try:
        print(f"managed database rules: {len(list_rules(connection))}")
    finally:
        connection.close()


def interactive_menu(app: Any) -> None:
    while True:
        print("\nMochi Forward SSH fallback")
        print("1) List rules")
        print("2) Add rule")
        print("3) Remove rule")
        print("4) Status")
        print("5) Clear all rules")
        print("0) Exit")
        try:
            choice = input("Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "1":
            connection = db_connect(app)
            try:
                print_rules(connection)
            finally:
                connection.close()
        elif choice == "2":
            port = input("Local port: ").strip()
            target = input("Destination IPv4: ").strip()
            target_port = input("Destination port (blank = local port): ").strip()
            owner = input("Owner username (blank = active admin): ").strip()
            conflict = ask_confirmation("Continue if this local port is occupied?")
            add_rule(app, argparse.Namespace(port=port, target=target, target_port=target_port or None, owner=owner or None, inbound_mbps=None, outbound_mbps=None, force_port_conflict=conflict))
        elif choice == "3":
            raw_id = input("Rule ID: ").strip()
            try:
                remove_rule(app, int(raw_id), False)
            except ValueError:
                print("Invalid rule ID.", file=sys.stderr)
        elif choice == "4":
            print_status(app)
        elif choice == "5":
            if ask_confirmation("Remove every managed forwarding rule?"):
                clear_rules(app, True)
        elif choice == "0":
            return
        else:
            print("Invalid selection.", file=sys.stderr)


def main() -> None:
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--env-file", default=os.environ.get("PANEL_ENV_FILE", DEFAULT_ENV_FILE))
    preliminary_args, _ = preliminary.parse_known_args()
    load_env_file(preliminary_args.env_file)

    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args.command = "menu"
    require_root()

    # Import after loading the protected environment file, so the Flask app
    # receives exactly the same paths and secret as the systemd service.
    from app import create_app
    app = create_app()

    if args.command == "list":
        connection = db_connect(app)
        try:
            print_rules(connection)
        finally:
            connection.close()
    elif args.command == "status":
        print_status(app)
    elif args.command == "add":
        add_rule(app, args)
    elif args.command == "remove":
        remove_rule(app, args.rule_id, args.yes)
    elif args.command == "clear":
        clear_rules(app, args.yes)
    else:
        interactive_menu(app)


if __name__ == "__main__":
    main()
