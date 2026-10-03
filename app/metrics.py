"""In-memory tun bandwidth sampler (/proc/net/dev) + ping cache."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

PROC_NET_DEV = "/proc/net/dev"
HISTORY_SECONDS = 300
SAMPLE_INTERVAL = 1.0


class MetricsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_bytes: dict[str, tuple[int, int, float]] = {}
        self._series: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=HISTORY_SECONDS)
        )
        self._ping: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_bytes()
            self._stop.wait(SAMPLE_INTERVAL)

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
