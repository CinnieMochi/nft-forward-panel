"""Traffic sampling and target reachability for the forwarding panel."""

from __future__ import annotations

import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from nft_manager import NftManager


SAMPLE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f UTC"
LEGACY_SAMPLE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
MIN_SAMPLE_INTERVAL_SECONDS = 0.75


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def reset_counter_baselines(
    connection: sqlite3.Connection,
    rules: Iterable[Any],
) -> None:
    """Start a fresh zero baseline after nftables recreated the managed table.

    ``NftManager.apply_rules`` deletes and recreates the table, so all nft
    counters start at zero.  Persisting that zero immediately lets the next
    sample account for every byte forwarded after the reload instead of
    discarding the first sample.
    """
    sampled_at = utc_now().strftime(SAMPLE_TIME_FORMAT)
    rule_ids: list[int] = []
    for rule in rules:
        try:
            raw_rule_id = rule["id"]
        except (KeyError, IndexError, TypeError):
            raw_rule_id = getattr(rule, "id", None)
        if raw_rule_id is not None:
            rule_ids.append(int(raw_rule_id))

    connection.execute("DELETE FROM rule_counter_state")
    connection.executemany(
        """INSERT INTO rule_counter_state
           (rule_id, inbound_bytes, outbound_bytes, rx_bytes, tx_bytes,
            sampled_at, inbound_bps, outbound_bps, rx_bps, tx_bps)
           VALUES (?, 0, 0, 0, 0, ?, 0, 0, 0, 0)""",
        [(rule_id, sampled_at) for rule_id in rule_ids],
    )


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
        now_text = now_dt.strftime(SAMPLE_TIME_FORMAT)
        bucket = now_dt.replace(minute=(now_dt.minute // 5) * 5, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S UTC")
        raw = self.manager.traffic_counters()
        result: dict[int, dict[str, float | int]] = {}
        for rule in rules:
            rule_id, port = int(rule["id"]), int(rule["listen_port"])
            current = raw.get(port, {"inbound": 0, "outbound": 0})
            current_in = int(current.get("inbound", 0))
            current_out = int(current.get("outbound", 0))
            logical_total = current_in + current_out
            # Old managed nft tables do not have physical ingress/egress
            # counters.  Until the next table reload, approximate each NIC leg
            # with the complete logical flow.  This intentionally makes usage
            # RX + TX, matching the host-interface accounting requested by the
            # panel rather than the former one-copy request/reply accounting.
            current_rx = int(current.get("rx", logical_total))
            current_tx = int(current.get("tx", logical_total))
            previous = connection.execute(
                """SELECT inbound_bytes, outbound_bytes, rx_bytes, tx_bytes,
                          sampled_at, inbound_bps, outbound_bps, rx_bps, tx_bps
                   FROM rule_counter_state WHERE rule_id = ?""",
                (rule_id,),
            ).fetchone()
            if port not in raw:
                if previous and any(float(previous[name]) for name in (
                    "inbound_bps", "outbound_bps", "rx_bps", "tx_bps"
                )):
                    connection.execute(
                        """UPDATE rule_counter_state
                           SET inbound_bps=0, outbound_bps=0, rx_bps=0, tx_bps=0
                           WHERE rule_id=?""",
                        (rule_id,),
                    )
                result[rule_id] = {
                    "inbound_bps": 0.0,
                    "outbound_bps": 0.0,
                    "rx_bps": 0.0,
                    "tx_bps": 0.0,
                }
                continue
            delta_in = delta_out = delta_rx = delta_tx = 0
            elapsed = 5.0
            preserve_previous_rate = False
            if previous:
                previous_dt = None
                for sample_format in (SAMPLE_TIME_FORMAT, LEGACY_SAMPLE_TIME_FORMAT):
                    try:
                        previous_dt = datetime.strptime(previous["sampled_at"], sample_format).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if previous_dt is not None:
                    elapsed = max(0.0, (now_dt - previous_dt).total_seconds())
                if elapsed < MIN_SAMPLE_INTERVAL_SECONDS:
                    # A rule reload may immediately follow this sample.  Always
                    # settle byte deltas so recreating the nft table cannot
                    # discard them, while retaining the last stable rate to
                    # avoid a sub-second speed spike in the UI.
                    preserve_previous_rate = True
                previous_in = int(previous["inbound_bytes"])
                previous_out = int(previous["outbound_bytes"])
                previous_rx = int(previous["rx_bytes"])
                previous_tx = int(previous["tx_bytes"])
                previous_logical_total = previous_in + previous_out
                # A partially completed in-place upgrade may already have the
                # new state columns at their DEFAULT 0 while the live nft table
                # is still the old logical-only version.  Derive the matching
                # old-table baseline so the first sampler cannot turn all
                # lifetime traffic into a speed/usage spike.
                if "rx" not in current and previous_rx == 0 and previous_logical_total:
                    previous_rx = previous_logical_total
                if "tx" not in current and previous_tx == 0 and previous_logical_total:
                    previous_tx = previous_logical_total
                delta_in = max(0, current_in - previous_in)
                delta_out = max(0, current_out - previous_out)
                delta_rx = max(0, current_rx - previous_rx)
                delta_tx = max(0, current_tx - previous_tx)
            if preserve_previous_rate and previous:
                inbound_bps = float(previous["inbound_bps"])
                outbound_bps = float(previous["outbound_bps"])
                rx_bps = float(previous["rx_bps"])
                tx_bps = float(previous["tx_bps"])
            else:
                inbound_bps = round(delta_in / elapsed, 1)
                outbound_bps = round(delta_out / elapsed, 1)
                rx_bps = round(delta_rx / elapsed, 1)
                tx_bps = round(delta_tx / elapsed, 1)
            connection.execute(
                """INSERT INTO rule_counter_state
                   (rule_id, inbound_bytes, outbound_bytes, rx_bytes, tx_bytes,
                    sampled_at, inbound_bps, outbound_bps, rx_bps, tx_bps)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(rule_id) DO UPDATE SET inbound_bytes=excluded.inbound_bytes,
                   outbound_bytes=excluded.outbound_bytes, rx_bytes=excluded.rx_bytes,
                   tx_bytes=excluded.tx_bytes, sampled_at=excluded.sampled_at,
                   inbound_bps=excluded.inbound_bps, outbound_bps=excluded.outbound_bps,
                   rx_bps=excluded.rx_bps, tx_bps=excluded.tx_bps""",
                (
                    rule_id, current_in, current_out, current_rx, current_tx,
                    now_text, inbound_bps, outbound_bps, rx_bps, tx_bps,
                ),
            )
            if delta_in or delta_out or delta_rx or delta_tx:
                connection.execute(
                    """INSERT INTO traffic_buckets
                       (rule_id, owner_id, bucket_at, inbound_bytes,
                        outbound_bytes, rx_bytes, tx_bytes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(rule_id, owner_id, bucket_at) DO UPDATE SET
                       inbound_bytes=inbound_bytes+excluded.inbound_bytes,
                       outbound_bytes=outbound_bytes+excluded.outbound_bytes,
                       rx_bytes=rx_bytes+excluded.rx_bytes,
                       tx_bytes=tx_bytes+excluded.tx_bytes""",
                    (
                        rule_id,
                        int(rule["owner_id"]),
                        bucket,
                        delta_in,
                        delta_out,
                        delta_rx,
                        delta_tx,
                    ),
                )
            result[rule_id] = {
                "inbound_bps": inbound_bps,
                "outbound_bps": outbound_bps,
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
            }
        cutoff = (now_dt - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S UTC")
        connection.execute("DELETE FROM traffic_buckets WHERE bucket_at < ?", (cutoff,))
        connection.commit()
        return result
