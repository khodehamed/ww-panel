"""Discover WaterWall tunnels, locked backhaul, and unmanaged configs.

Never edits or restarts backhaul. Never stops waterwall-proto51 / v2 unless
an explicit operator action targets that one unit.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

OPT_ROOT = Path("/opt")
SYSTEMD_DIRS = (
    Path("/etc/systemd/system"),
    Path("/lib/systemd/system"),
    Path("/usr/lib/systemd/system"),
)

KNOWN_PROFILES: dict[str, dict[str, str]] = {
    "waterwall-proto51": {
        "cli": "ww51",
        "unit": "waterwall-proto51",
        "tun": "wtun0",
        "tun_local": "10.10.0.1",
        "tun_peer": "10.10.0.2",
        "family": "v1",
    },
    "waterwall-proto51-v2": {
        "cli": "ww51v2",
        "unit": "waterwall-proto51-v2",
        "tun": "wtun2",
        "tun_local": "10.10.1.1",
        "tun_peer": "10.10.1.2",
        "family": "v2",
    },
}

ENV_KEYS = (
    "SIDE",
    "IRAN_IP",
    "KHAREJ_IP",
    "PROTO",
    "ENCRYPT",
    "AES_KEY",
    "PORTS",
    "OLDCPU",
    "PORT_OFFSET",
    "ENC_ALGO",
    "ENC_SALT",
)

_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def _run(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        data[key] = val
    return data


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "unknown"


def _is_backhaul_name(name: str) -> bool:
    n = name.lower()
    return n == "backhaul" or n.startswith("backhaul-") or n.startswith("backhaul_")


def _list_systemd_units(prefix: str) -> list[str]:
    found: set[str] = set()
    for d in SYSTEMD_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob(f"{prefix}*.service"):
            found.add(p.name[:-8] if p.name.endswith(".service") else p.name)
    return sorted(found)


def systemd_state(unit: str) -> dict[str, str]:
    if not unit:
        return {"active": "unknown", "enabled": "unknown", "result": ""}
    try:
        proc = _run(
            [
                "systemctl",
                "show",
                f"{unit}.service",
                "-p",
                "ActiveState",
                "-p",
                "UnitFileState",
                "-p",
                "Result",
            ],
            timeout=2.0,
        )
        parsed: dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                parsed[key] = val
        return {
            "active": parsed.get("ActiveState") or "unknown",
            "enabled": parsed.get("UnitFileState") or "unknown",
            "result": parsed.get("Result") or "",
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"active": "unknown", "enabled": "unknown", "result": ""}


def _ip_link_map() -> dict[str, dict[str, Any]]:
    ifaces: dict[str, dict[str, Any]] = {}
    try:
        proc = _run(["ip", "-j", "-d", "link", "show"], timeout=3.0)
        if proc.returncode == 0 and proc.stdout.strip().startswith("["):
            for item in json.loads(proc.stdout):
                name = item.get("ifname") or item.get("ifname", "")
                if not name:
                    continue
                flags = item.get("flags") or []
                ifaces[name] = {
                    "name": name,
                    "up": "UP" in flags or item.get("operstate") == "UP",
                    "operstate": item.get("operstate", ""),
                    "addrs": [],
                }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
        try:
            proc = _run(["ip", "-o", "link", "show"], timeout=3.0)
            for line in (proc.stdout or "").splitlines():
                m = re.search(r"^\d+:\s+([^:@]+)", line)
                if not m:
                    continue
                name = m.group(1).strip()
                ifaces[name] = {
                    "name": name,
                    "up": "UP" in line and "DOWN" not in line.split("state")[-1][:8]
                    if "state" in line
                    else "UP" in line,
                    "operstate": "",
                    "addrs": [],
                }
        except (OSError, subprocess.TimeoutExpired):
            return ifaces
    try:
        proc = _run(["ip", "-4", "-o", "addr", "show"], timeout=3.0)
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[1]
            cidr = parts[3]
            rec = ifaces.setdefault(
                name, {"name": name, "up": True, "operstate": "", "addrs": []}
            )
            rec["addrs"].append(cidr)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ifaces


def _first_ipv4(cidrs: list[str]) -> str:
    for c in cidrs:
        ip = c.split("/")[0]
        if _IP_RE.match(ip):
            return ip
    return ""


def _peer_from_local(local_ip: str, fallback_peer: str) -> str:
    if not local_ip or not _IP_RE.match(local_ip):
        return fallback_peer
    octs = [int(x) for x in local_ip.split(".")]
    if octs[3] == 1:
        octs[3] = 2
    elif octs[3] == 2:
        octs[3] = 1
    else:
        return fallback_peer
    return ".".join(str(x) for x in octs)


def ping_peer(ip: str) -> dict[str, Any]:
    if not ip or not _IP_RE.match(ip):
        return {"ok": False, "latency_ms": None, "error": "no-peer"}
    try:
        proc = _run(["ping", "-c", "1", "-W", "1", ip], timeout=2.2)
        out = (proc.stdout or "") + (proc.stderr or "")
        latency = None
        m = re.search(r"time[=<]([\d.]+)\s*ms", out)
        if m:
            latency = float(m.group(1))
        return {"ok": proc.returncode == 0, "latency_ms": latency, "error": "" if proc.returncode == 0 else "timeout"}
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "latency_ms": None, "error": "ping-failed"}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_waterwall_json(install_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "tun": "",
        "tun_cidr": "",
        "proto": "",
        "encrypt": None,
        "iran_ip": "",
        "kharej_ip": "",
        "side": "",
        "has_core": False,
        "configs": [],
    }
    core = _load_json(install_dir / "core.json")
    if isinstance(core, dict):
        info["has_core"] = True
        cfgs = core.get("configs") or []
        if isinstance(cfgs, list):
            info["configs"] = [str(x) for x in cfgs]
    nodes: list[dict[str, Any]] = []
    for name in ("config_ir.json", "config_kharej.json", "config.json"):
        data = _load_json(install_dir / name)
        if not isinstance(data, dict):
            continue
        cfg_name = str(data.get("name") or "")
        if cfg_name == "iran":
            info["side"] = "ir"
        elif cfg_name == "kharej":
            info["side"] = "kharej"
        raw_nodes = data.get("nodes") or []
        if isinstance(raw_nodes, list):
            nodes.extend(n for n in raw_nodes if isinstance(n, dict))
    src_up = dest_up = src_down = dest_down = capture = ""
    encrypt = False
    for n in nodes:
        ntype = n.get("type")
        settings = n.get("settings") if isinstance(n.get("settings"), dict) else {}
        if ntype == "TunDevice":
            info["tun"] = str(settings.get("device-name") or "")
            info["tun_cidr"] = str(settings.get("device-ip") or "")
        elif ntype == "IpManipulator":
            proto = settings.get("protoswap")
            if proto is not None:
                info["proto"] = str(proto)
        elif ntype in ("EncryptionClient", "EncryptionServer"):
            encrypt = True
        elif ntype == "IpOverrider":
            mode = settings.get("mode")
            direction = settings.get("direction")
            ipv4 = str(settings.get("ipv4") or "")
            if direction == "up" and mode == "source-ip":
                src_up = ipv4
            elif direction == "up" and mode == "dest-ip":
                dest_up = ipv4
            elif direction == "down" and mode == "source-ip":
                src_down = ipv4
            elif direction == "down" and mode == "dest-ip":
                dest_down = ipv4
        elif ntype == "RawSocket":
            capture = str(settings.get("capture-ip") or "")
    info["encrypt"] = encrypt
    if info["side"] == "ir":
        info["iran_ip"] = src_up
        info["kharej_ip"] = dest_up or capture
    elif info["side"] == "kharej":
        info["kharej_ip"] = src_up
        info["iran_ip"] = dest_up or capture
    else:
        info["iran_ip"] = src_up
        info["kharej_ip"] = dest_up
    if src_down:
        info["json_tun_peer"] = src_down
    if dest_down:
        info["json_tun_local"] = dest_down
    return info


def _profile_for_dir(dirname: str) -> dict[str, str]:
    if dirname in KNOWN_PROFILES:
        return dict(KNOWN_PROFILES[dirname])
    m = re.match(r"^waterwall-proto51-t(\d+)$", dirname)
    if m:
        n = int(m.group(1))
        return {
            "cli": "",
            "unit": dirname,
            "tun": f"wtun{n}",
            "tun_local": f"10.10.{n}.1",
            "tun_peer": f"10.10.{n}.2",
            "family": "custom",
        }
    return {
        "cli": "",
        "unit": dirname,
        "tun": "",
        "tun_local": "",
        "tun_peer": "",
        "family": "unknown",
    }


def health_status(systemd_active: bool, tun_up: bool, ping_ok: bool) -> str:
    if systemd_active and tun_up and ping_ok:
        return "UP"
    if systemd_active:
        return "DEGRADED"
    return "DOWN"


_health_status = health_status


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 6:
        return "*" * len(key)
    return key[:3] + "…" + key[-3:]


def _dir_tunnel(
    install_dir: Path, ifaces: dict[str, dict[str, Any]], *, do_ping: bool = False
) -> dict[str, Any] | None:
    name = install_dir.name
    if not name.startswith("waterwall-proto51"):
        return None
    env_path = install_dir / "tunnel.env"
    env = parse_env_file(env_path)
    has_env = bool(env)
    json_info = parse_waterwall_json(install_dir)
    has_json = bool(json_info.get("has_core") or json_info.get("tun") or json_info.get("configs"))
    if not has_env and not has_json and not (install_dir / "Waterwall").exists():
        return None

    profile = _profile_for_dir(name)
    unit = profile.get("unit") or name
    tun = env.get("TUN_DEVICE") or json_info.get("tun") or profile.get("tun") or ""
    tun_cidr = json_info.get("tun_cidr") or ""
    tun_local = profile.get("tun_local") or (tun_cidr.split("/")[0] if tun_cidr else "")
    tun_peer = profile.get("tun_peer") or str(json_info.get("json_tun_peer") or "")

    iface = ifaces.get(tun, {}) if tun else {}
    addrs = list(iface.get("addrs") or [])
    live_ip = _first_ipv4(addrs)
    if live_ip:
        tun_local = live_ip
    if not tun_peer:
        tun_peer = _peer_from_local(tun_local, "")

    sysd = systemd_state(unit)
    if do_ping and tun_peer:
        ping = ping_peer(tun_peer)
    elif not tun_peer:
        ping = {"ok": False, "latency_ms": None, "error": "no-peer"}
    else:
        ping = {"ok": False, "latency_ms": None, "error": "pending"}
    systemd_ok = sysd["active"] == "active"
    tun_up = bool(iface.get("up")) and bool(tun)
    status = _health_status(systemd_ok, tun_up, bool(ping.get("ok")))

    writable = has_env and not _is_backhaul_name(name) and not _is_backhaul_name(unit)
    unmanaged = has_json and not has_env
    if unmanaged:
        writable = False

    side = env.get("SIDE") or json_info.get("side") or ""
    encrypt = env.get("ENCRYPT")
    if encrypt is None or encrypt == "":
        encrypt = "1" if json_info.get("encrypt") else "0"

    return {
        "id": _slug(name),
        "name": name,
        "kind": "waterwall",
        "family": profile.get("family") or "unknown",
        "writable": writable,
        "locked": False,
        "readonly_reason": "" if writable else ("unmanaged-json" if unmanaged else ("no-tunnel-env" if not has_env else "")),
        "install_dir": str(install_dir),
        "env_path": str(env_path) if has_env else "",
        "cli": profile.get("cli") or "",
        "unit": unit,
        "systemd": sysd,
        "side": side,
        "local_role": "iran" if side == "ir" else ("kharej" if side == "kharej" else ""),
        "iran_ip": env.get("IRAN_IP") or json_info.get("iran_ip") or "",
        "kharej_ip": env.get("KHAREJ_IP") or json_info.get("kharej_ip") or "",
        "proto": env.get("PROTO") or json_info.get("proto") or "",
        "encrypt": str(encrypt),
        "aes_key": env.get("AES_KEY") or "",
        "aes_key_masked": _mask_key(env.get("AES_KEY") or ""),
        "ports": env.get("PORTS") or "",
        "oldcpu": env.get("OLDCPU") or "0",
        "port_offset": env.get("PORT_OFFSET") or "",
        "enc_algo": env.get("ENC_ALGO") or "",
        "enc_salt": env.get("ENC_SALT") or "",
        "tun": tun,
        "tun_up": tun_up,
        "tun_addrs": addrs,
        "tun_local": tun_local,
        "tun_peer": tun_peer,
        "ping": ping,
        "status": status,
        "unmanaged": unmanaged,
    }


def _backhaul_entries(ifaces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for unit in _list_systemd_units("backhaul"):
        if unit in seen or "-watchdog" in unit:
            continue
        seen.add(unit)
        sysd = systemd_state(unit)
        items.append(
            {
                "id": _slug(f"backhaul-{unit}"),
                "name": unit,
                "kind": "backhaul",
                "family": "backhaul",
                "writable": False,
                "locked": True,
                "readonly_reason": "backhaul-locked",
                "install_dir": "",
                "env_path": "",
                "cli": "",
                "unit": unit,
                "systemd": sysd,
                "side": "",
                "local_role": "",
                "iran_ip": "",
                "kharej_ip": "",
                "proto": "",
                "encrypt": "",
                "aes_key": "",
                "aes_key_masked": "",
                "ports": "",
                "oldcpu": "",
                "port_offset": "",
                "enc_algo": "",
                "enc_salt": "",
                "tun": "",
                "tun_up": False,
                "tun_addrs": [],
                "tun_local": "",
                "tun_peer": "",
                "ping": {"ok": False, "latency_ms": None, "error": "locked"},
                "status": "UP" if sysd["active"] == "active" else "DOWN",
                "unmanaged": False,
            }
        )

    for name, iface in ifaces.items():
        if name.lower() != "backhaul" and not name.lower().startswith("backhaul"):
            continue
        tid = _slug(f"iface-{name}")
        if any(x["id"] == tid or x.get("tun") == name for x in items):
            continue
        items.append(
            {
                "id": tid,
                "name": name,
                "kind": "backhaul",
                "family": "backhaul",
                "writable": False,
                "locked": True,
                "readonly_reason": "backhaul-interface-locked",
                "install_dir": "",
                "env_path": "",
                "cli": "",
                "unit": "",
                "systemd": {"active": "unknown", "enabled": "unknown", "result": ""},
                "side": "",
                "local_role": "",
                "iran_ip": "",
                "kharej_ip": "",
                "proto": "",
                "encrypt": "",
                "aes_key": "",
                "aes_key_masked": "",
                "ports": "",
                "oldcpu": "",
                "port_offset": "",
                "enc_algo": "",
                "enc_salt": "",
                "tun": name,
                "tun_up": bool(iface.get("up")),
                "tun_addrs": list(iface.get("addrs") or []),
                "tun_local": _first_ipv4(list(iface.get("addrs") or [])),
                "tun_peer": "",
                "ping": {"ok": False, "latency_ms": None, "error": "locked"},
                "status": "UP" if iface.get("up") else "DOWN",
                "unmanaged": False,
            }
        )
    return items


def discover_tunnels(*, do_ping: bool = False) -> list[dict[str, Any]]:
    ifaces = _ip_link_map()
    found: dict[str, dict[str, Any]] = {}

    if OPT_ROOT.is_dir():
        for path in sorted(OPT_ROOT.glob("waterwall-proto51*")):
            if not path.is_dir():
                continue
            rec = _dir_tunnel(path, ifaces, do_ping=do_ping)
            if rec:
                found[rec["id"]] = rec

    for unit in _list_systemd_units("waterwall-proto51"):
        if "-watchdog" in unit:
            continue
        tid = _slug(unit)
        if tid in found:
            continue
        install_dir = OPT_ROOT / unit
        if install_dir.is_dir():
            rec = _dir_tunnel(install_dir, ifaces, do_ping=do_ping)
            if rec:
                found[rec["id"]] = rec
                continue
        profile = _profile_for_dir(unit)
        sysd = systemd_state(unit)
        tun = profile.get("tun") or ""
        iface = ifaces.get(tun, {}) if tun else {}
        peer = profile.get("tun_peer") or ""
        if do_ping and peer:
            ping = ping_peer(peer)
        elif not peer:
            ping = {"ok": False, "latency_ms": None, "error": "no-peer"}
        else:
            ping = {"ok": False, "latency_ms": None, "error": "pending"}
        systemd_ok = sysd["active"] == "active"
        tun_up = bool(iface.get("up"))
        found[tid] = {
            "id": tid,
            "name": unit,
            "kind": "waterwall",
            "family": profile.get("family") or "unknown",
            "writable": False,
            "locked": False,
            "readonly_reason": "unit-only",
            "install_dir": str(install_dir),
            "env_path": "",
            "cli": profile.get("cli") or "",
            "unit": unit,
            "systemd": sysd,
            "side": "",
            "local_role": "",
            "iran_ip": "",
            "kharej_ip": "",
            "proto": "",
            "encrypt": "",
            "aes_key": "",
            "aes_key_masked": "",
            "ports": "",
            "oldcpu": "",
            "port_offset": "",
            "enc_algo": "",
            "enc_salt": "",
            "tun": tun,
            "tun_up": tun_up,
            "tun_addrs": list(iface.get("addrs") or []),
            "tun_local": profile.get("tun_local") or "",
            "tun_peer": profile.get("tun_peer") or "",
            "ping": ping,
            "status": _health_status(systemd_ok, tun_up, bool(ping.get("ok"))),
            "unmanaged": True,
        }

    items = list(found.values()) + _backhaul_entries(ifaces)
    items.sort(key=lambda x: (0 if x["kind"] == "waterwall" else 1, x["name"]))
    return items


def get_tunnel(tunnel_id: str) -> dict[str, Any] | None:
    for t in discover_tunnels():
        if t["id"] == tunnel_id:
            return t
    return None


def used_tuns_and_subnets() -> tuple[set[str], set[int]]:
    tuns: set[str] = set()
    thirds: set[int] = set()
    ifaces = _ip_link_map()
    for name in ifaces:
        if name.startswith("wtun"):
            tuns.add(name)
        if name.lower() == "backhaul" or name.lower().startswith("backhaul"):
            tuns.add(name)
        for cidr in ifaces[name].get("addrs") or []:
            ip = cidr.split("/")[0]
            if _IP_RE.match(ip) and ip.startswith("10.10."):
                thirds.add(int(ip.split(".")[2]))
    # Always reserve v1 + v2 resources so a new profile never collides.
    tuns.update({"wtun0", "wtun2"})
    thirds.update({0, 1})
    if OPT_ROOT.is_dir():
        for path in OPT_ROOT.glob("waterwall-proto51*"):
            info = parse_waterwall_json(path)
            tun = str(info.get("tun") or "")
            if tun:
                tuns.add(tun)
            cidr = str(info.get("tun_cidr") or "")
            ip = cidr.split("/")[0]
            if _IP_RE.match(ip) and ip.startswith("10.10."):
                thirds.add(int(ip.split(".")[2]))
            m = re.match(r"^waterwall-proto51-t(\d+)$", path.name)
            if m:
                n = int(m.group(1))
                tuns.add(f"wtun{n}")
                thirds.add(n)
    return tuns, thirds


def allocate_slot() -> dict[str, Any]:
    tuns, thirds = used_tuns_and_subnets()
    for n in range(3, 80):
        tun = f"wtun{n}"
        dirname = f"waterwall-proto51-t{n}"
        if tun in tuns or n in thirds or (OPT_ROOT / dirname).exists():
            continue
        return {
            "n": n,
            "id": dirname,
            "install_dir": str(OPT_ROOT / dirname),
            "unit": dirname,
            "tun": tun,
            "tun_local": f"10.10.{n}.1",
            "tun_peer": f"10.10.{n}.2",
            "tun_cidr": f"10.10.{n}.1/24",
            "enc_salt": f"waterwall-proto51-t{n}",
        }
    raise RuntimeError("no free tun/subnet slot (wtun3..wtun79)")


def list_wtun_ifaces() -> list[str]:
    return sorted(n for n in _ip_link_map() if n.startswith("wtun"))
