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
            "# Managed by nft-forward-panel. Do not edit while the panel is running.",
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
                    f'        tcp dport {rule.listen_port} counter comment "nfp:in:{rule.listen_port}" dnat to {rule.destination_ip}:{rule.destination_port}',
                    f'        udp dport {rule.listen_port} counter comment "nfp:in:{rule.listen_port}" dnat to {rule.destination_ip}:{rule.destination_port}',
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
        lines.extend(["    }", "", "    chain forwarding {"])
        lines.append("        type filter hook forward priority 10; policy accept;")
        for rule in sorted_rules:
            inbound_limit = (
                f" limit rate over {max(1, rule.inbound_limit_mbps * 125)} kbytes/second drop"
                if rule.inbound_limit_mbps else ""
            )
            outbound_limit = (
                f" limit rate over {max(1, rule.outbound_limit_mbps * 125)} kbytes/second drop"
                if rule.outbound_limit_mbps else ""
            )
            lines.extend([
                "",
                f'        ct direction original ct original proto-dst {rule.listen_port} counter comment "nfp:forward-in:{rule.listen_port}"{inbound_limit}',
                f'        ct direction reply ct original proto-dst {rule.listen_port} counter comment "nfp:out:{rule.listen_port}"{outbound_limit}',
            ])
        lines.extend(["    }", "}", ""])
        return "\n".join(lines)

    def traffic_counters(self) -> dict[int, dict[str, int]]:
        """Return cumulative nft byte counters keyed by listening port."""
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
            match = re.fullmatch(r"nfp:(in|out):(\d+)", rule.get("comment", ""))
            if not match:
                continue
            direction, raw_port = match.groups()
            total = sum(
                int(expression.get("counter", {}).get("bytes", 0))
                for expression in rule.get("expr", []) if "counter" in expression
            )
            port = int(raw_port)
            counters.setdefault(port, {"inbound": 0, "outbound": 0})
            counters[port]["inbound" if direction == "in" else "outbound"] += total
        return counters

    def connection_counts(self) -> dict[int, int]:
        """Count tracked TCP/UDP flows by original local destination port."""
        if not shutil.which("conntrack"):
            return {}
        result = self._run(["conntrack", "-L", "-o", "extended"], check=False)
        if result.returncode not in (0, 1):
            return {}
        counts: dict[int, int] = {}
        for line in result.stdout.splitlines():
            match = re.search(r"\bdport=(\d+)\b", line)
            if match:
                port = int(match.group(1))
                counts[port] = counts.get(port, 0) + 1
        return counts

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

    def ensure_ready(self) -> None:
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise NftOperationError("面板必须以 root 身份运行，才能修改 nftables 规则。")
        if not shutil.which("nft"):
            raise NftOperationError("未安装 nftables；请先在服务器上安装 nftables。")
        self.forward_config.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_main_include()
        self._enable_ip_forward()

    def validate_rules(self, rules: Iterable[ForwardRule]) -> None:
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
        content = self._render_config(normalised)
        descriptor, temporary = tempfile.mkstemp(prefix=".port-forward.check.", dir=self.forward_config.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            self._run(["nft", "-c", "-f", temporary])
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def apply_rules(self, rules: Iterable[ForwardRule]) -> None:
        """Validate, persist and load the dedicated table, restoring on failure."""
        rules = list(rules)
        self.ensure_ready()
        self.validate_rules(rules)
        previous = self._backup_current_config()
        self._atomic_write(self.forward_config, self._render_config(rules), 0o640)
        self._run(["nft", "delete", "table", "ip", TABLE_NAME], check=False)
        try:
            self._run(["nft", "-f", str(self.forward_config)])
        except NftOperationError as exc:
            self._run(["nft", "delete", "table", "ip", TABLE_NAME], check=False)
            if previous is not None:
                self._atomic_write(self.forward_config, previous, 0o640)
                self._run(["nft", "-f", str(self.forward_config)], check=False)
            else:
                self.forward_config.unlink(missing_ok=True)
            raise NftOperationError(f"规则未能加载，已尝试恢复上一版本：{exc}") from exc

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

    def firewall_open(self, rule: ForwardRule) -> list[str]:
        """Mirror the original script's firewall allowances; warnings are returned."""
        warnings: list[str] = []
        try:
            if self._firewalld_active():
                for protocol in ("tcp", "udp"):
                    self._run(["firewall-cmd", f"--add-port={rule.listen_port}/{protocol}", "--permanent"])
                self._run(["firewall-cmd", "--reload"])
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

    def firewall_close(self, rule: ForwardRule, destination_still_used: bool) -> list[str]:
        warnings: list[str] = []
        try:
            if self._firewalld_active():
                for protocol in ("tcp", "udp"):
                    self._run(["firewall-cmd", f"--remove-port={rule.listen_port}/{protocol}", "--permanent"], check=False)
                self._run(["firewall-cmd", "--reload"])
            elif self._ufw_active():
                for protocol in ("tcp", "udp"):
                    self._run(["ufw", "--force", "delete", "allow", f"{rule.listen_port}/{protocol}"], check=False)
                    if not destination_still_used:
                        self._run(["ufw", "--force", "route", "delete", "allow", "proto", protocol, "to", rule.destination_ip, "port", str(rule.destination_port)], check=False)
            elif self._iptables_available():
                for protocol in ("tcp", "udp"):
                    self._run(["iptables", "-D", "INPUT", "-p", protocol, "--dport", str(rule.listen_port), "-j", "ACCEPT"], check=False)
                    if not destination_still_used:
                        self._run(["iptables", "-D", "FORWARD", "-d", rule.destination_ip, "-p", protocol, "--dport", str(rule.destination_port), "-j", "ACCEPT"], check=False)
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
