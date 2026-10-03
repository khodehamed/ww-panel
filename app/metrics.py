"""Background tun bandwidth sampler + ping cache. HTTP never waits on ping."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

PROC_NET_DEV = "/proc/net/dev"
HISTORY_SECONDS = 300
SAMPLE_INTERVAL = 1.0
PING_INTERVAL = 12.0
DISCOVER_INTERVAL = 6.0


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_bytes: dict[str, tuple[int, int, float]] = {}
        self._series: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=HISTORY_SECONDS)
        )
        self._ping: dict[str, dict[str, Any]] = {}
        self._tunnels: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._refresh_discovery()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ww-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def snapshot(self, tun: str, limit: int = HISTORY_SECONDS) -> dict[str, Any]:
        with self._lock:
            series = list(self._series.get(tun, []))[-limit:]
            last = series[-1] if series else None
        return {
            "iface": tun,
            "rx_bps": last["rx_bps"] if last else 0,
            "tx_bps": last["tx_bps"] if last else 0,
            "points": series,
        }

    def latest_map(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for tun, series in self._series.items():
                if not series:
                    continue
                last = series[-1]
                out[tun] = {
                    "rx_bps": last["rx_bps"],
                    "tx_bps": last["tx_bps"],
                    "ts": last["ts"],
                }
        return out

    def cached_ping(self, tunnel_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._ping.get(tunnel_id)
            return dict(rec) if rec else None

    def cached_tunnels(self) -> list[dict[str, Any]]:
        """Cheap snapshot: systemd/env from memory + cached ping/bps. No subprocess."""
        from .discover import health_status

        with self._lock:
            items = [dict(t) for t in self._tunnels]
            pings = {k: dict(v) for k, v in self._ping.items()}
            latest: dict[str, dict[str, Any]] = {}
            for tun, series in self._series.items():
                if series:
                    last = series[-1]
                    latest[tun] = {"rx_bps": last["rx_bps"], "tx_bps": last["tx_bps"]}
        out: list[dict[str, Any]] = []
        for t in items:
            rec = dict(t)
            rec.pop("aes_key", None)
            tun = rec.get("tun") or ""
            live = latest.get(tun) or {}
            rec["rx_bps"] = live.get("rx_bps", rec.get("rx_bps", 0))
            rec["tx_bps"] = live.get("tx_bps", rec.get("tx_bps", 0))
            tid = rec.get("id") or ""
            ping = pings.get(tid)
            if ping:
                rec["ping"] = ping
                if rec.get("kind") != "backhaul" and not rec.get("locked"):
                    rec["status"] = health_status(
                        (rec.get("systemd") or {}).get("active") == "active",
                        bool(rec.get("tun_up")),
                        bool(ping.get("ok")),
                    )
            out.append(rec)
        return out

    def all_metrics(self, limit: int = 90) -> dict[str, Any]:
        with self._lock:
            items = [dict(t) for t in self._tunnels]
            pings = {k: dict(v) for k, v in self._ping.items()}
            series_copy = {tun: list(series)[-limit:] for tun, series in self._series.items()}
        tunnels: list[dict[str, Any]] = []
        for t in items:
            tid = t.get("id") or ""
            tun = t.get("tun") or ""
            series = series_copy.get(tun, [])
            last = series[-1] if series else None
            tunnels.append(
                {
                    "id": tid,
                    "tun": tun,
                    "rx_bps": last["rx_bps"] if last else 0,
                    "tx_bps": last["tx_bps"] if last else 0,
                    "ping": pings.get(tid) or t.get("ping") or {},
                    "points": series,
                }
            )
        return {"ok": True, "tunnels": tunnels}

    def _loop(self) -> None:
        last_ping = 0.0
        last_discover = time.time()
        while not self._stop.is_set():
            self._sample_bytes()
            now = time.time()
            if now - last_discover >= DISCOVER_INTERVAL:
                self._refresh_discovery()
                last_discover = now
            if now - last_ping >= PING_INTERVAL:
                self._refresh_pings()
                last_ping = now
            self._stop.wait(SAMPLE_INTERVAL)

    def _refresh_discovery(self) -> None:
        from .discover import discover_tunnels

        try:
            items = discover_tunnels(do_ping=False)
        except Exception:
            return
        with self._lock:
            self._tunnels = items

    def _refresh_pings(self) -> None:
        from .discover import ping_peer

        with self._lock:
            peers = [
                (t.get("id") or "", t.get("tun_peer") or "")
                for t in self._tunnels
                if t.get("kind") != "backhaul" and not t.get("locked")
            ]
        results: dict[str, dict[str, Any]] = {}
        for tid, peer in peers:
            if self._stop.is_set():
                return
            if not tid:
                continue
            results[tid] = ping_peer(peer)
        with self._lock:
            self._ping.update(results)

    def _sample_bytes(self) -> None:
        counters = _read_proc_net_dev()
        now = time.time()
        with self._lock:
            for iface, (rx, tx) in counters.items():
                if not iface.startswith("wtun") and iface.lower() != "backhaul":
                    continue
                prev = self._last_bytes.get(iface)
                self._last_bytes[iface] = (rx, tx, now)
                if not prev:
                    continue
                dt = max(now - prev[2], 0.001)
                rx_bps = max(0.0, (rx - prev[0]) * 8.0 / dt)
                tx_bps = max(0.0, (tx - prev[1]) * 8.0 / dt)
                self._series[iface].append(
                    {
                        "ts": now,
                        "rx_bps": round(rx_bps, 1),
                        "tx_bps": round(tx_bps, 1),
                        "rx_bytes": rx,
                        "tx_bytes": tx,
                    }
                )


STORE = MetricsStore()


def _read_proc_net_dev() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    try:
        with open(PROC_NET_DEV, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        iface = name.strip()
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            rx = int(cols[0])
            tx = int(cols[8])
        except ValueError:
            continue
        out[iface] = (rx, tx)
    return out
