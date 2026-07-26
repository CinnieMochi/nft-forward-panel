"""Traffic sampling and target reachability for the forwarding panel."""

from __future__ import annotations

import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from nft_manager import NftManager


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def probe_tcp(address: str, port: int, timeout: float = 1.5) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            latency = round((time.monotonic() - started) * 1000, 1)
            return {"reachable": True, "latency_ms": latency}
    except OSError:
        return {"reachable": False, "latency_ms": None}


class TrafficMonitor:
    def __init__(self, manager: NftManager) -> None:
        self.manager = manager

    def sample(self, connection: sqlite3.Connection, rules: Iterable[sqlite3.Row]) -> dict[int, dict[str, float | int]]:
        now_dt = utc_now()
        now_text = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        bucket = now_dt.replace(minute=(now_dt.minute // 5) * 5, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S UTC")
        raw = self.manager.traffic_counters()
        result: dict[int, dict[str, float | int]] = {}
        for rule in rules:
            rule_id, port = int(rule["id"]), int(rule["listen_port"])
            current = raw.get(port, {"inbound": 0, "outbound": 0})
            previous = connection.execute(
                "SELECT inbound_bytes, outbound_bytes, sampled_at FROM rule_counter_state WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
            delta_in = delta_out = 0
            elapsed = 5.0
            if previous:
                try:
                    previous_dt = datetime.strptime(previous["sampled_at"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                    elapsed = max(0.1, (now_dt - previous_dt).total_seconds())
                except ValueError:
                    pass
                delta_in = current["inbound"] - int(previous["inbound_bytes"])
                delta_out = current["outbound"] - int(previous["outbound_bytes"])
                if delta_in < 0:
                    delta_in = current["inbound"]
                if delta_out < 0:
                    delta_out = current["outbound"]
            connection.execute(
                """INSERT INTO rule_counter_state(rule_id, inbound_bytes, outbound_bytes, sampled_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(rule_id) DO UPDATE SET inbound_bytes=excluded.inbound_bytes,
                   outbound_bytes=excluded.outbound_bytes, sampled_at=excluded.sampled_at""",
                (rule_id, current["inbound"], current["outbound"], now_text),
            )
            if delta_in or delta_out:
                connection.execute(
                    """INSERT INTO traffic_buckets(rule_id, owner_id, bucket_at, inbound_bytes, outbound_bytes)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(rule_id, bucket_at) DO UPDATE SET
                       inbound_bytes=inbound_bytes+excluded.inbound_bytes,
                       outbound_bytes=outbound_bytes+excluded.outbound_bytes""",
                    (rule_id, int(rule["owner_id"]), bucket, delta_in, delta_out),
                )
            result[rule_id] = {
                "inbound_bps": round(delta_in / elapsed, 1),
                "outbound_bps": round(delta_out / elapsed, 1),
            }
        cutoff = (now_dt - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S UTC")
        connection.execute("DELETE FROM traffic_buckets WHERE bucket_at < ?", (cutoff,))
        connection.commit()
        return result
