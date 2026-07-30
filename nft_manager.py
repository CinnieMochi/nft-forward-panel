"""Safe, parameterised nftables operations for the forwarding panel.

The database is the source of truth.  This module renders the managed
`port-forward.conf` file and only changes the `ip port_forward` nft table.
It deliberately never flushes the complete ruleset.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


TABLE_NAME = "port_forward"
MANAGED_MARKER = "# Managed by nft-forward-panel. Do not edit while the panel is running."
RULE_RE = re.compile(
    r"^\s*tcp\s+dport\s+(?P<listen>\d+)\s+dnat\s+to\s+"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}):(?P<target>\d+)\s*$"
)


class NftOperationError(RuntimeError):
    """An error which is safe to present to a panel user."""


@dataclass(frozen=True)
class ForwardRule:
    id: int | None
    listen_port: int
    destination_ip: str
    destination_port: int
    owner_id: int | None = None
    inbound_limit_mbps: int = 0
    outbound_limit_mbps: int = 0


class NftManager:
    def __init__(
        self,
        forward_config: str,
        main_config: str,
        sysctl_config: str,
        command_timeout: int = 15,
    ) -> None:
        self.forward_config = Path(forward_config)
        self.main_config = Path(main_config)
        self.sysctl_config = Path(sysctl_config)
        self.backup_dir = self.forward_config.parent / "backups"
        self.command_timeout = command_timeout

    @staticmethod
    def validate_port(value: int | str) -> int:
        raw = str(value)
        if not re.fullmatch(r"[1-9]\d{0,4}", raw):
            raise NftOperationError("端口必须是 1–65535 之间的整数，且不能有前导零。")
        port = int(raw)
        if port > 65535:
            raise NftOperationError("端口必须是 1–65535 之间的整数。")
        return port

    @staticmethod
    def validate_ipv4(value: str) -> str:
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError as exc:
            raise NftOperationError("目标地址必须是有效的 IPv4 地址。") from exc
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address == ipaddress.IPv4Address("255.255.255.255")
        ):
            raise NftOperationError("目标地址不能是回环、链路本地、组播、未指定、保留或广播地址。")
        return str(address)

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise NftOperationError(f"系统缺少命令：{args[0]}。") from exc
        except subprocess.TimeoutExpired as exc:
            raise NftOperationError(f"命令执行超时：{args[0]}。") from exc
        if check and result.returncode != 0:
            message = (result.stderr or result.stdout or "未知错误").strip()
            raise NftOperationError(f"命令执行失败（{args[0]}）：{message}")
        return result

    def _exists_and_succeeds(self, args: list[str]) -> bool:
        try:
            return self._run(args, check=False).returncode == 0
        except NftOperationError:
            return False

    def local_ipv4(self) -> str:
        commands = [
            ["ip", "route", "get", "1.1.1.1"],
            ["ip", "-4", "addr", "show", "scope", "global"],
        ]
        for args in commands:
            result = self._run(args, check=False)
            match = re.search(r"\bsrc\s+((?:\d{1,3}\.){3}\d{1,3})\b", result.stdout)
            if not match:
                match = re.search(r"\binet\s+((?:\d{1,3}\.){3}\d{1,3})/", result.stdout)
            if match:
                return self.validate_ipv4(match.group(1))
        raise NftOperationError("无法检测本机 IPv4 地址；请检查网卡和默认路由。")

    def import_rules_from_config(self) -> list[ForwardRule]:
        """Read rules created by the original script for one-time DB adoption."""
        if not self.forward_config.exists():
            return []
        found: list[ForwardRule] = []
        for line in self.forward_config.read_text(encoding="utf-8", errors="replace").splitlines():
            match = RULE_RE.match(line)
            if not match:
                continue
            try:
                found.append(
                    ForwardRule(
                        id=None,
                        listen_port=self.validate_port(match.group("listen")),
                        destination_ip=self.validate_ipv4(match.group("ip")),
                        destination_port=self.validate_port(match.group("target")),
                    )
                )
            except NftOperationError:
                continue
        unique: dict[int, ForwardRule] = {rule.listen_port: rule for rule in found}
        return list(unique.values())

    def _render_config(self, rules: Iterable[ForwardRule]) -> str:
        sorted_rules = sorted(rules, key=lambda rule: rule.listen_port)
        lines = [
            "#!/usr/sbin/nft -f",
            MANAGED_MARKER,
            f"table ip {TABLE_NAME} {{",
            "    chain prerouting {",
            "        type nat hook prerouting priority -100; policy accept;",
        ]
        if sorted_rules:
            lines.insert(2, f"define LOCAL_IP = {self.local_ipv4()}")
            lines.insert(3, "")
        for rule in sorted_rules:
            lines.extend(
                [
                    "",
                    f"        # Forward {rule.listen_port} -> {rule.destination_ip}:{rule.destination_port}",
                    f'        tcp dport {rule.listen_port} counter dnat to {rule.destination_ip}:{rule.destination_port} comment "nfp:nat:{rule.listen_port}"',
                    f'        udp dport {rule.listen_port} counter dnat to {rule.destination_ip}:{rule.destination_port} comment "nfp:nat:{rule.listen_port}"',
                ]
            )
        lines.extend(
            [
                "    }",
                "",
                "    chain postrouting {",
                "        type nat hook postrouting priority 100; policy accept;",
            ]
        )
        for rule in sorted_rules:
            lines.extend(
                [
                    "",
                    f"        ip daddr {rule.destination_ip} tcp dport {rule.destination_port} ct status dnat snat to $LOCAL_IP",
                    f"        ip daddr {rule.destination_ip} udp dport {rule.destination_port} ct status dnat snat to $LOCAL_IP",
                ]
            )
        lines.append("    }")
        for rule in sorted_rules:
            for direction, limit_mbps in (
                ("out", rule.outbound_limit_mbps),
                ("in", rule.inbound_limit_mbps),
            ):
                lines.extend(["", f"    chain nfp_{direction}_{rule.listen_port} {{"])
                if limit_mbps:
                    lines.append(
                        f"        limit rate over {max(1, limit_mbps * 125)} "
                        "kbytes/second drop"
                    )
                lines.extend(
                    [
                        f'        counter comment "nfp:traffic-{direction}:{rule.listen_port}"',
                        "    }",
                    ]
                )

        lines.extend(
            [
                "",
                "    chain wire_prerouting {",
                # Destination NAT runs at -100. Count immediately afterwards
                # so the original tuple identifies the forwarding rule while
                # matching the host NIC receive-side view.
                "        type filter hook prerouting priority -90; policy accept;",
            ]
        )
        for rule in sorted_rules:
            lines.append("")
            for protocol in ("tcp", "udp"):
                lines.append(
                    f"        ct status dnat ct original protocol {protocol} "
                    f"ct original proto-dst {rule.listen_port} counter "
                    f'comment "nfp:wire-rx:{rule.listen_port}"'
                )
        lines.extend(
            [
                "    }",
                "",
                "    chain wire_postrouting {",
                # Run after this table's srcnat chain (priority 100), as well as
                # after the forwarding hook where per-rule policing happens.
                "        type filter hook postrouting priority 300; policy accept;",
            ]
        )
        for rule in sorted_rules:
            lines.append("")
            for protocol in ("tcp", "udp"):
                lines.append(
                    f"        ct status dnat ct original protocol {protocol} "
                    f"ct original proto-dst {rule.listen_port} counter "
                    f'comment "nfp:wire-tx:{rule.listen_port}"'
                )
        lines.append("    }")

        lines.extend(["", "    chain forwarding {"])
        lines.append("        type filter hook forward priority 20; policy accept;")
        for rule in sorted_rules:
            lines.append("")
            for protocol in ("tcp", "udp"):
                lines.extend([
                    f"        ip daddr {rule.destination_ip} {protocol} dport {rule.destination_port} ct status dnat ct original protocol {protocol} ct original proto-dst {rule.listen_port} jump nfp_out_{rule.listen_port}",
                    f"        ip saddr {rule.destination_ip} {protocol} sport {rule.destination_port} ct status dnat ct original protocol {protocol} ct original proto-dst {rule.listen_port} jump nfp_in_{rule.listen_port}",
                ])
        lines.extend(["    }", "}", ""])
        return "\n".join(lines)

    def traffic_counters(self) -> dict[int, dict[str, int]]:
        """Return cumulative nft byte counters keyed by listening port.

        ``inbound`` and ``outbound`` are logical, post-policing directions used
        for billing.  Newer rulesets also expose optional ``rx`` and ``tx``
        counters for the physical ingress/egress legs.  Omitting those keys for
        older live tables lets callers distinguish an upgrade baseline from a
        genuine zero-byte wire sample.
        """
        if not shutil.which("nft"):
            return {}
        result = self._run(["nft", "-j", "list", "table", "ip", TABLE_NAME], check=False)
        if result.returncode != 0:
            return {}
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            return {}
        counters: dict[int, dict[str, int]] = {}
        for item in payload.get("nftables", []):
            rule = item.get("rule", {})
            # NAT chains only see the first packet of a connection.  Logical
            # counters live in the forwarding hook, while optional wire-leg
            # counters use filter chains at prerouting and postrouting.
            traffic_match = re.fullmatch(
                r"nfp:traffic-(in|out):(\d+)",
                rule.get("comment", ""),
            )
            wire_match = re.fullmatch(
                r"nfp:wire-(rx|tx):(\d+)",
                rule.get("comment", ""),
            )
            if not traffic_match and not wire_match:
                continue
            direction, raw_port = (
                traffic_match.groups() if traffic_match else wire_match.groups()
            )
            total = sum(
                int(expression.get("counter", {}).get("bytes", 0))
                for expression in rule.get("expr", []) if "counter" in expression
            )
            port = int(raw_port)
            counters.setdefault(port, {"inbound": 0, "outbound": 0})
            if traffic_match:
                key = "inbound" if direction == "in" else "outbound"
            else:
                key = direction
            counters[port][key] = counters[port].get(key, 0) + total
        return counters

    def connection_snapshot(self) -> dict[str, object]:
        """Count tracked flows by listening port and transport protocol."""
        if not shutil.which("conntrack"):
            return {"ports": {}, "tcp_ports": {}, "udp_ports": {}}
        result = self._run(["conntrack", "-L", "-f", "ipv4", "-o", "extended"], check=False)
        if result.returncode not in (0, 1):
            return {"ports": {}, "tcp_ports": {}, "udp_ports": {}}
        counts: dict[int, int] = {}
        protocol_ports: dict[str, dict[int, int]] = {"tcp": {}, "udp": {}}
        for line in result.stdout.splitlines():
            # Depending on conntrack-tools version, extended output starts
            # with either "tcp/udp" or an address-family prefix such as
            # "ipv4 2 tcp/udp".
            protocol_match = re.match(r"^(?:(?:ipv4|ipv6)\s+\d+\s+)?(tcp|udp)\s", line)
            if not protocol_match:
                continue
            protocol = protocol_match.group(1)
            if protocol == "tcp" and re.search(r"\bTIME_WAIT\b", line):
                continue
            # The first tuple is the original direction, before DNAT. Its
            # destination port is the panel's local listening port.
            tuples = line.split("src=", 2)
            if len(tuples) < 2:
                continue
            original = tuples[1]
            match = re.search(r"\bdport=(\d+)\b", original)
            if match:
                port = int(match.group(1))
                counts[port] = counts.get(port, 0) + 1
                protocol_ports[protocol][port] = protocol_ports[protocol].get(port, 0) + 1
        return {"ports": counts, "tcp_ports": protocol_ports["tcp"], "udp_ports": protocol_ports["udp"]}

    def connection_counts(self) -> dict[int, int]:
        """Count tracked TCP/UDP flows by original local destination port."""
        return self.connection_snapshot()["ports"]

    def _ensure_main_include(self) -> None:
        include = f'include "{self.forward_config.parent}/*.conf"'
        if not self.main_config.exists():
            raise NftOperationError(
                f"主配置 {self.main_config} 不存在。请先安装并配置 nftables，不能由面板创建可能覆盖现有防火墙的主配置。"
            )
        current = self.main_config.read_text(encoding="utf-8", errors="replace")
        if include not in current:
            with self.main_config.open("a", encoding="utf-8") as handle:
                if current and not current.endswith("\n"):
                    handle.write("\n")
                handle.write(f"{include}\n")

    def _enable_ip_forward(self) -> None:
        self._run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        self.sysctl_config.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if self.sysctl_config.exists():
            lines = self.sysctl_config.read_text(encoding="utf-8", errors="replace").splitlines()
        replaced = False
        for index, line in enumerate(lines):
            if re.match(r"^\s*net\.ipv4\.ip_forward\s*=", line):
                lines[index] = "net.ipv4.ip_forward=1"
                replaced = True
        if not replaced:
            lines.append("net.ipv4.ip_forward=1")
        self._atomic_write(self.sysctl_config, "\n".join(lines) + "\n", 0o644)

    @staticmethod
    def _atomic_write(path: Path, content: str, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _backup_current_config(self) -> str | None:
        if not self.forward_config.exists():
            return None
        contents = self.forward_config.read_text(encoding="utf-8", errors="replace")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._atomic_write(self.backup_dir / f"port-forward.conf.{stamp}", contents, 0o600)
        return contents

    def _live_table_exists(self) -> bool:
        return self._exists_and_succeeds(
            ["nft", "list", "table", "ip", TABLE_NAME]
        )

    def _assert_table_is_managed(self) -> None:
        """Refuse to replace a same-named table not backed by our config file."""
        if not self.forward_config.exists():
            raise NftOperationError(
                f"nftables 表 ip {TABLE_NAME} 已存在，但面板配置文件不存在；"
                "为避免覆盖其他防火墙规则，已拒绝接管该表。"
            )
        current = self.forward_config.read_text(encoding="utf-8", errors="replace")
        has_managed_marker = MANAGED_MARKER in current.splitlines()
        owns_table = re.search(
            rf"(?m)^\s*table\s+ip\s+{re.escape(TABLE_NAME)}\s*\{{",
            current,
        )
        if not has_managed_marker or not owns_table:
            raise NftOperationError(
                f"nftables 表 ip {TABLE_NAME} 已存在，但并非由当前面板配置文件定义；"
                "为避免覆盖其他防火墙规则，已拒绝更新。"
            )

    @staticmethod
    def _render_live_transaction(content: str, replace_existing: bool) -> str:
        """Build one nft batch so replacing the managed table has no gap."""
        lines = content.splitlines()
        transaction: list[str] = []
        if lines and lines[0].startswith("#!"):
            transaction.append(lines.pop(0))
        transaction.append(
            "# Loaded as one nftables transaction; do not split the delete and create."
        )
        if replace_existing:
            transaction.append(f"delete table ip {TABLE_NAME}")
        transaction.extend(lines)
        return "\n".join(transaction) + "\n"

    def _run_config(self, content: str, *, check_only: bool) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".port-forward.transaction.",
            dir=self.forward_config.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            args = ["nft"]
            if check_only:
                args.append("-c")
            args.extend(["-f", temporary])
            self._run(args)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def ensure_ready(self) -> None:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise NftOperationError("面板必须以 root 身份运行，才能修改 nftables 规则。")
        if not shutil.which("nft"):
            raise NftOperationError("未安装 nftables；请先在服务器上安装 nftables。")
        self.forward_config.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_main_include()
        self._enable_ip_forward()

    def _prepare_rules(self, rules: Iterable[ForwardRule]) -> tuple[str, str]:
        seen_ports: set[int] = set()
        normalised: list[ForwardRule] = []
        for rule in rules:
            port = self.validate_port(rule.listen_port)
            if port in seen_ports:
                raise NftOperationError(f"监听端口 {port} 已存在。")
            seen_ports.add(port)
            normalised.append(
                ForwardRule(
                    id=rule.id,
                    listen_port=port,
                    destination_ip=self.validate_ipv4(rule.destination_ip),
                    destination_port=self.validate_port(rule.destination_port),
                    owner_id=rule.owner_id,
                    inbound_limit_mbps=max(0, int(rule.inbound_limit_mbps)),
                    outbound_limit_mbps=max(0, int(rule.outbound_limit_mbps)),
                )
            )
        replace_existing = self._live_table_exists()
        if replace_existing:
            self._assert_table_is_managed()
        rendered = self._render_config(normalised)
        transaction = self._render_live_transaction(rendered, replace_existing)
        self._run_config(transaction, check_only=True)
        return rendered, transaction

    def validate_rules(self, rules: Iterable[ForwardRule]) -> None:
        self._prepare_rules(rules)

    def apply_rules(self, rules: Iterable[ForwardRule]) -> None:
        """Validate and atomically replace only the dedicated nftables table."""
        rules = list(rules)
        self.ensure_ready()
        rendered, transaction = self._prepare_rules(rules)
        # nft applies the checked file as one netlink transaction, so
        # delete+create cannot expose an empty-table window and a failed batch
        # leaves the live rules unchanged.
        previous = self._backup_current_config()
        self._atomic_write(self.forward_config, rendered, 0o640)
        try:
            self._run_config(transaction, check_only=False)
        except NftOperationError as exc:
            if previous is not None:
                self._atomic_write(self.forward_config, previous, 0o640)
            else:
                self.forward_config.unlink(missing_ok=True)
            raise NftOperationError(
                f"规则未能加载；nft 原子事务未改变线上规则，配置文件已恢复：{exc}"
            ) from exc

    def listening_port_in_use(self, port: int) -> bool:
        result = self._run(["ss", "-H", "-lntu"], check=False)
        return bool(re.search(rf"(?:\[::\]|0\.0\.0\.0|\*|[^\s:]+):{port}(?:\s|$)", result.stdout))

    def _firewalld_active(self) -> bool:
        return self._exists_and_succeeds(["systemctl", "is-active", "--quiet", "firewalld"])

    def _ufw_active(self) -> bool:
        if not shutil.which("ufw"):
            return False
        return "Status: active" in self._run(["ufw", "status"], check=False).stdout

    def _iptables_available(self) -> bool:
        return bool(shutil.which("iptables")) and self._exists_and_succeeds(["iptables", "-S"])

    def _iptables_ensure(self, args: list[str]) -> None:
        check_args = ["iptables", "-C", *args]
        if not self._exists_and_succeeds(check_args):
            self._run(["iptables", "-I", *args])

    def _run_firewalld_idempotent_add(self, args: list[str]) -> None:
        """Treat firewalld's ALREADY_ENABLED response as a successful retry."""
        result = self._run(args, check=False)
        if result.returncode == 0:
            return
        output = "\n".join(
            part for part in (result.stderr, result.stdout) if part
        ).strip()
        if result.returncode == 11 or "already_enabled" in output.casefold():
            return
        message = (result.stderr or result.stdout or "未知错误").strip()
        raise NftOperationError(f"命令执行失败（{args[0]}）：{message}")

    def _run_idempotent_delete(
        self,
        args: list[str],
        *,
        absent_returncodes: frozenset[int],
        absent_markers: tuple[str, ...],
    ) -> None:
        result = self._run(args, check=False)
        if result.returncode == 0:
            return
        output = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
        normalized = output.casefold()
        known_absence = result.returncode in absent_returncodes and (
            not absent_markers or any(marker in normalized for marker in absent_markers)
        )
        if known_absence:
            return
        message = (result.stderr or result.stdout or "未知错误").strip()
        raise NftOperationError(f"命令执行失败（{args[0]}）：{message}")

    def firewall_open(self, rule: ForwardRule) -> list[str]:
        """Mirror the original script's firewall allowances; warnings are returned."""
        warnings: list[str] = []
        try:
            if self._firewalld_active():
                for protocol in ("tcp", "udp"):
                    port_spec = f"--add-port={rule.listen_port}/{protocol}"
                    # Update runtime and persistent state independently. A
                    # global reload rebuilds unrelated rules and can interrupt
                    # SSH or the panel itself.
                    self._run_firewalld_idempotent_add(
                        ["firewall-cmd", port_spec]
                    )
                    self._run_firewalld_idempotent_add(
                        ["firewall-cmd", "--permanent", port_spec]
                    )
            elif self._ufw_active():
                for protocol in ("tcp", "udp"):
                    self._run(["ufw", "allow", f"{rule.listen_port}/{protocol}"])
                    self._run(["ufw", "route", "allow", "proto", protocol, "to", rule.destination_ip, "port", str(rule.destination_port)])
            elif self._iptables_available():
                for protocol in ("tcp", "udp"):
                    self._iptables_ensure(["INPUT", "-p", protocol, "--dport", str(rule.listen_port), "-j", "ACCEPT"])
                    self._iptables_ensure(["FORWARD", "-d", rule.destination_ip, "-p", protocol, "--dport", str(rule.destination_port), "-j", "ACCEPT"])
                self._iptables_ensure(["FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
        except NftOperationError as exc:
            warnings.append(f"转发规则已生效，但防火墙放行失败：{exc}")
        return warnings

    def firewall_close(
        self,
        rule: ForwardRule,
        destination_still_used: bool,
        *,
        remove_listen_port: bool = True,
    ) -> list[str]:
        warnings: list[str] = []
        try:
            # A forced forwarding rule may reuse an SSH, panel, or other local
            # service port.  Keep that port's INPUT allowance while a local
            # socket is still listening, but continue cleaning route/FORWARD
            # allowances for the removed forwarding destination.
            remove_local_allowance = (
                remove_listen_port
                and not self.listening_port_in_use(rule.listen_port)
            )
            if self._firewalld_active():
                if remove_local_allowance:
                    for protocol in ("tcp", "udp"):
                        port_spec = f"--remove-port={rule.listen_port}/{protocol}"
                        self._run_idempotent_delete(
                            ["firewall-cmd", port_spec],
                            absent_returncodes=frozenset({12}),
                            absent_markers=(),
                        )
                        self._run_idempotent_delete(
                            ["firewall-cmd", "--permanent", port_spec],
                            absent_returncodes=frozenset({12}),
                            absent_markers=(),
                        )
            elif self._ufw_active():
                for protocol in ("tcp", "udp"):
                    if remove_local_allowance:
                        self._run_idempotent_delete(
                            ["ufw", "--force", "delete", "allow", f"{rule.listen_port}/{protocol}"],
                            absent_returncodes=frozenset({1}),
                            absent_markers=("non-existent rule", "nonexistent rule"),
                        )
                    if not destination_still_used:
                        self._run_idempotent_delete(
                            ["ufw", "--force", "route", "delete", "allow", "proto", protocol, "to", rule.destination_ip, "port", str(rule.destination_port)],
                            absent_returncodes=frozenset({1}),
                            absent_markers=("non-existent rule", "nonexistent rule"),
                        )
            elif self._iptables_available():
                for protocol in ("tcp", "udp"):
                    if remove_local_allowance:
                        self._run_idempotent_delete(
                            ["iptables", "-D", "INPUT", "-p", protocol, "--dport", str(rule.listen_port), "-j", "ACCEPT"],
                            absent_returncodes=frozenset({1}),
                            absent_markers=("bad rule", "does a matching rule exist", "no chain/target/match"),
                        )
                    if not destination_still_used:
                        self._run_idempotent_delete(
                            ["iptables", "-D", "FORWARD", "-d", rule.destination_ip, "-p", protocol, "--dport", str(rule.destination_port), "-j", "ACCEPT"],
                            absent_returncodes=frozenset({1}),
                            absent_markers=("bad rule", "does a matching rule exist", "no chain/target/match"),
                        )
        except NftOperationError as exc:
            warnings.append(f"转发规则已删除，但防火墙清理失败：{exc}")
        return warnings

    def status(self) -> dict[str, object]:
        firewall = "firewalld" if self._firewalld_active() else "ufw" if self._ufw_active() else "iptables" if self._iptables_available() else None
        return {
            "nft_available": bool(shutil.which("nft")),
            "nft_table_loaded": self._exists_and_succeeds(["nft", "list", "table", "ip", TABLE_NAME]),
            "ip_forward": self._run(["sysctl", "-n", "net.ipv4.ip_forward"], check=False).stdout.strip() == "1",
            "firewall_available": firewall is not None,
            "firewall": firewall or "未检测到",
        }
