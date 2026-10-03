"""Write tunnel.env, regenerate WaterWall JSON, apply via existing CLI, create isolated profiles.

This module never touches backhaul units/interfaces and never stops
waterwall-proto51 / waterwall-proto51-v2 unless the operator targets that
exact tunnel id.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .discover import (
    KNOWN_PROFILES,
    allocate_slot,
    get_tunnel,
    parse_env_file,
    parse_waterwall_json,
)

WW_RELEASE = os.environ.get("WATERWALL_RELEASE", "v1.46.3")
WW_REPO = f"https://github.com/radkesvat/WaterWall/releases/download/{WW_RELEASE}"
ENC_ALGO_DEFAULT = "chacha20-poly1305"
ENC_KDF_DEFAULT = 12000
PORT_OFFSET_DEFAULT = 10000
DEFAULT_PORTS = "443 2053 2083 2087 2096 8443"

_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_SAFE_UNIT = re.compile(r"^waterwall-proto51(-v2|-t\d+)?$")


class ApplyError(ValueError):
    pass


def _valid_ip(ip: str) -> bool:
    if not _IP_RE.match(ip or ""):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def _normalize_ports(raw: str) -> str:
    parts = re.split(r"[\s,;]+", (raw or "").strip())
    return " ".join(p for p in parts if p)


def _validate_ports(ports: str) -> list[int]:
    if not ports:
        return []
    out: list[int] = []
    for p in ports.split():
        if not p.isdigit():
            raise ApplyError(f"invalid port: {p}")
        n = int(p)
        if n < 1 or n > 65535:
            raise ApplyError(f"port out of range: {n}")
        out.append(n)
    return out


def _validate_writable(tunnel: dict[str, Any]) -> None:
    if tunnel.get("kind") == "backhaul" or tunnel.get("locked"):
        raise ApplyError("backhaul is discovered-locked (show only)")
    if not tunnel.get("writable"):
        reason = tunnel.get("readonly_reason") or "read-only"
        raise ApplyError(f"tunnel is not writable ({reason})")
    unit = tunnel.get("unit") or ""
    if unit and not _SAFE_UNIT.match(unit) and not unit.startswith("waterwall-proto51"):
        raise ApplyError("refusing to control unexpected unit")


def _backup_env(env_path: Path) -> str:
    if not env_path.is_file():
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = env_path.with_name(f"tunnel.env.bak.{stamp}")
    shutil.copy2(env_path, dest)
    return str(dest)


def write_tunnel_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"SIDE={values.get('SIDE', '')}",
        f"IRAN_IP={values.get('IRAN_IP', '')}",
        f"KHAREJ_IP={values.get('KHAREJ_IP', '')}",
        f"PROTO={values.get('PROTO', '51')}",
        f"ENCRYPT={values.get('ENCRYPT', '0')}",
        f"AES_KEY={values.get('AES_KEY', '')}",
        f'PORTS="{values.get("PORTS", "")}"',
        f"OLDCPU={values.get('OLDCPU', '0')}",
        f"PORT_OFFSET={values.get('PORT_OFFSET', str(PORT_OFFSET_DEFAULT))}",
        f"ENC_ALGO={values.get('ENC_ALGO', ENC_ALGO_DEFAULT)}",
        f"ENC_SALT={values.get('ENC_SALT', 'waterwall-proto51')}",
    ]
    if values.get("TUN_DEVICE"):
        lines.append(f"TUN_DEVICE={values['TUN_DEVICE']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def merge_env_update(current: dict[str, str], body: dict[str, Any]) -> dict[str, str]:
    out = dict(current)
    mapping = {
        "iran_ip": "IRAN_IP",
        "kharej_ip": "KHAREJ_IP",
        "proto": "PROTO",
        "ports": "PORTS",
        "encrypt": "ENCRYPT",
        "aes_key": "AES_KEY",
        "port_offset": "PORT_OFFSET",
        "enc_algo": "ENC_ALGO",
        "side": "SIDE",
        "oldcpu": "OLDCPU",
        "enc_salt": "ENC_SALT",
    }
    for src, dst in mapping.items():
        if src in body and body[src] is not None:
            out[dst] = str(body[src]).strip()
    if "PORTS" in out:
        out["PORTS"] = _normalize_ports(out.get("PORTS", ""))
    if out.get("SIDE") in ("iran", "Iran"):
        out["SIDE"] = "ir"
    encrypt = out.get("ENCRYPT", "0")
    if encrypt in ("true", "True", "yes", "Y", "y", "on"):
        out["ENCRYPT"] = "1"
    elif encrypt in ("false", "False", "no", "N", "n", "off", ""):
        out["ENCRYPT"] = "0"
    proto = out.get("PROTO", "51")
    if not proto.isdigit() or not (0 <= int(proto) <= 255):
        raise ApplyError("PROTO must be 0-255")
    if not _valid_ip(out.get("IRAN_IP", "")):
        raise ApplyError("invalid IRAN_IP")
    if not _valid_ip(out.get("KHAREJ_IP", "")):
        raise ApplyError("invalid KHAREJ_IP")
    ports = _validate_ports(out.get("PORTS", ""))
    offset_s = out.get("PORT_OFFSET") or str(PORT_OFFSET_DEFAULT)
    if not offset_s.isdigit() or not (1 <= int(offset_s) <= 60000):
        raise ApplyError("PORT_OFFSET must be 1-60000")
    offset = int(offset_s)
    if out.get("ENCRYPT") == "1":
        key = out.get("AES_KEY") or ""
        if len(key) != 32:
            raise ApplyError("AES_KEY must be exactly 32 characters when ENCRYPT=1")
        if not ports:
            raise ApplyError("PORTS required when ENCRYPT=1")
        for p in ports:
            if p + offset > 65535:
                raise ApplyError(f"PUBLIC+PORT_OFFSET overflow for port {p}")
        if not out.get("ENC_ALGO"):
            out["ENC_ALGO"] = ENC_ALGO_DEFAULT
    return out


def _run(cmd: list[str], timeout: float = 90.0, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=cwd,
    )


def _systemctl(action: str, unit: str) -> dict[str, Any]:
    if not unit or not (unit.startswith("waterwall-proto51")):
        raise ApplyError("refusing unexpected systemd unit")
    if "backhaul" in unit.lower():
        raise ApplyError("backhaul is locked")
    proc = _run(["systemctl", action, f"{unit}.service"], timeout=30.0)
    return {
        "ok": proc.returncode == 0,
        "action": action,
        "unit": unit,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
    }


def control_unit(tunnel_id: str, action: str) -> dict[str, Any]:
    if action not in ("restart", "start", "stop"):
        raise ApplyError("action must be restart|start|stop")
    tunnel = get_tunnel(tunnel_id)
    if not tunnel:
        raise ApplyError("tunnel not found")
    if tunnel.get("kind") == "backhaul" or tunnel.get("locked"):
        raise ApplyError("backhaul is discovered-locked (show only)")
    unit = tunnel.get("unit") or ""
    if not unit:
        raise ApplyError("no systemd unit for this tunnel")
    result = _systemctl(action, unit)
    time.sleep(1.0)
    result["tunnel"] = get_tunnel(tunnel_id)
    return result


def _find_cli(tunnel: dict[str, Any]) -> list[str] | None:
    cli = tunnel.get("cli") or ""
    if cli:
        path = shutil.which(cli) or f"/usr/local/bin/{cli}"
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return [path, "apply"]
    install_dir = Path(tunnel.get("install_dir") or "")
    script = install_dir / "install.sh"
    if script.is_file():
        return ["bash", str(script), "apply"]
    return None


def _forward_nodes(
    ports: list[int],
    encrypt: bool,
    key: str,
    offset: int,
    tun_peer: str,
    enc_algo: str,
    enc_salt: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for p in ports:
        iport = p + offset if encrypt else p
        listener = {
            "name": f"p{p}",
            "type": "TcpListener",
            "settings": {"address": "0.0.0.0", "port": p, "nodelay": True},
            "next": f"enc{p}" if encrypt else f"c{p}",
        }
        nodes.append(listener)
        if encrypt:
            nodes.append(
                {
                    "name": f"enc{p}",
                    "type": "EncryptionClient",
                    "settings": {
                        "algorithm": enc_algo,
                        "password": key,
                        "salt": enc_salt,
                        "kdf-iterations": ENC_KDF_DEFAULT,
                    },
                    "next": f"c{p}",
                }
            )
        nodes.append(
            {
                "name": f"c{p}",
                "type": "TcpConnector",
                "settings": {"nodelay": True, "address": tun_peer, "port": iport},
            }
        )
    return nodes


def _decrypt_nodes(
    ports: list[int],
    key: str,
    offset: int,
    tun_local: str,
    enc_algo: str,
    enc_salt: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for p in ports:
        iport = p + offset
        nodes.extend(
            [
                {
                    "name": f"p{p}",
                    "type": "TcpListener",
                    "settings": {"address": tun_local, "port": iport, "nodelay": True},
                    "next": f"enc{p}",
                },
                {
                    "name": f"enc{p}",
                    "type": "EncryptionServer",
                    "settings": {
                        "algorithm": enc_algo,
                        "password": key,
                        "salt": enc_salt,
                        "kdf-iterations": ENC_KDF_DEFAULT,
                    },
                    "next": f"c{p}",
                },
                {
                    "name": f"c{p}",
                    "type": "TcpConnector",
                    "settings": {"nodelay": True, "address": "127.0.0.1", "port": p},
                },
            ]
        )
    return nodes


def _packet_nodes(
    side: str,
    iran_ip: str,
    kh_ip: str,
    proto: int,
    tun: str,
    tun_cidr: str,
    tun_local: str,
    tun_peer: str,
) -> list[dict[str, Any]]:
    src_up = iran_ip if side == "ir" else kh_ip
    dest_up = kh_ip if side == "ir" else iran_ip
    capture = kh_ip if side == "ir" else iran_ip
    return [
        {
            "name": "my tun",
            "type": "TunDevice",
            "settings": {"device-name": tun, "device-ip": tun_cidr},
            "next": "ipovsrc",
        },
        {
            "name": "ipovsrc",
            "type": "IpOverrider",
            "settings": {"direction": "up", "mode": "source-ip", "ipv4": src_up},
            "next": "ipovdest",
        },
        {
            "name": "ipovdest",
            "type": "IpOverrider",
            "settings": {"direction": "up", "mode": "dest-ip", "ipv4": dest_up},
            "next": "manip",
        },
        {
            "name": "manip",
            "type": "IpManipulator",
            "settings": {"protoswap": proto},
            "next": "ipovsrc2",
        },
        {
            "name": "ipovsrc2",
            "type": "IpOverrider",
            "settings": {"direction": "down", "mode": "source-ip", "ipv4": tun_peer},
            "next": "ipovdest2",
        },
        {
            "name": "ipovdest2",
            "type": "IpOverrider",
            "settings": {"direction": "down", "mode": "dest-ip", "ipv4": tun_local},
            "next": "rd",
        },
        {
            "name": "rd",
            "type": "RawSocket",
            "settings": {"capture-filter-mode": "source-ip", "capture-ip": capture},
        },
    ]


def write_core_json(install_dir: Path, side: str, mtu: int) -> None:
    cfg = "config_ir.json" if side == "ir" else "config_kharej.json"
    data = {
        "log": {
            "path": "log/",
            "internal": {"loglevel": "INFO", "file": "internal.log", "console": True},
            "core": {"loglevel": "INFO", "file": "core.log", "console": True},
            "network": {"loglevel": "INFO", "file": "network.log", "console": True},
            "dns": {"loglevel": "SILENT", "file": "dns.log", "console": False},
        },
        "dns": {},
        "misc": {"workers": 1, "mtu": mtu, "ram-profile": "client", "libs-path": "libs/"},
        "configs": [cfg],
    }
    (install_dir / "core.json").write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")


def write_side_config(
    install_dir: Path,
    side: str,
    env: dict[str, str],
    tun: str,
    tun_cidr: str,
    tun_local: str,
    tun_peer: str,
) -> None:
    ports = _validate_ports(env.get("PORTS", ""))
    encrypt = env.get("ENCRYPT") == "1"
    key = env.get("AES_KEY") or ""
    offset = int(env.get("PORT_OFFSET") or PORT_OFFSET_DEFAULT)
    algo = env.get("ENC_ALGO") or ENC_ALGO_DEFAULT
    salt = env.get("ENC_SALT") or "waterwall-proto51"
    proto = int(env.get("PROTO") or 51)
    iran_ip = env["IRAN_IP"]
    kh_ip = env["KHAREJ_IP"]
    nodes = _packet_nodes(side, iran_ip, kh_ip, proto, tun, tun_cidr, tun_local, tun_peer)
    extra: list[dict[str, Any]] = []
    if side == "ir" and ports:
        extra = _forward_nodes(ports, encrypt, key, offset, tun_peer, algo, salt)
    elif side == "kharej" and encrypt and ports:
        extra = _decrypt_nodes(ports, key, offset, tun_local, algo, salt)
    if extra:
        nodes.extend(extra)
    name = "iran" if side == "ir" else "kharej"
    fname = "config_ir.json" if side == "ir" else "config_kharej.json"
    payload = {"name": name, "nodes": nodes}
    (install_dir / fname).write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")


def _tun_meta(tunnel: dict[str, Any]) -> tuple[str, str, str, str]:
    name = tunnel.get("name") or ""
    profile = KNOWN_PROFILES.get(name, {})
    tun = tunnel.get("tun") or profile.get("tun") or "wtun0"
    tun_local = tunnel.get("tun_local") or profile.get("tun_local") or "10.10.0.1"
    tun_peer = tunnel.get("tun_peer") or profile.get("tun_peer") or "10.10.0.2"
    if "/" in (tunnel.get("tun_addrs") or [""])[0] if tunnel.get("tun_addrs") else False:
        tun_cidr = tunnel["tun_addrs"][0]
    else:
        tun_cidr = f"{tun_local}/24"
    json_info = parse_waterwall_json(Path(tunnel.get("install_dir") or "/"))
    if json_info.get("tun_cidr"):
        tun_cidr = str(json_info["tun_cidr"])
    if json_info.get("tun"):
        tun = str(json_info["tun"])
    return tun, tun_cidr, tun_local, tun_peer


def regenerate_json(tunnel: dict[str, Any], env: dict[str, str]) -> None:
    install_dir = Path(tunnel["install_dir"])
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "log").mkdir(exist_ok=True)
    (install_dir / "libs").mkdir(exist_ok=True)
    side = env.get("SIDE") or "ir"
    if side not in ("ir", "kharej"):
        raise ApplyError("SIDE must be ir or kharej")
    mtu = 1320 if side == "ir" else 1380
    tun, tun_cidr, tun_local, tun_peer = _tun_meta(tunnel)
    write_core_json(install_dir, side, mtu)
    write_side_config(install_dir, side, env, tun, tun_cidr, tun_local, tun_peer)


def apply_update(tunnel_id: str, body: dict[str, Any]) -> dict[str, Any]:
    tunnel = get_tunnel(tunnel_id)
    if not tunnel:
        raise ApplyError("tunnel not found")
    _validate_writable(tunnel)
    env_path = Path(tunnel["env_path"] or Path(tunnel["install_dir"]) / "tunnel.env")
    current = parse_env_file(env_path)
    if not current:
        raise ApplyError("missing tunnel.env")
    merged = merge_env_update(current, body)
    backup = _backup_env(env_path)
    write_tunnel_env(env_path, merged)

    cli = _find_cli(tunnel)
    apply_log = ""
    used = ""
    if cli:
        used = " ".join(cli)
        try:
            proc = _run(cli, timeout=120.0)
            apply_log = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-6000:]
            if proc.returncode != 0:
                raise ApplyError(f"apply failed ({used}): {apply_log[-800:]}")
        except subprocess.TimeoutExpired as exc:
            raise ApplyError(f"apply timed out: {used}") from exc
    else:
        used = "python-configgen"
        regenerate_json(tunnel, merged)
        unit = tunnel.get("unit") or ""
        if unit:
            _systemctl("restart", unit)
            time.sleep(1.2)

    return {
        "ok": True,
        "backup": backup,
        "applied_via": used,
        "log": apply_log[-3000:],
        "tunnel": get_tunnel(tunnel_id),
    }


def write_service_unit(unit: str, install_dir: Path, description: str) -> None:
    if not unit.startswith("waterwall-proto51"):
        raise ApplyError("refusing unexpected unit name")
    text = f"""[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
WorkingDirectory={install_dir}
ExecStart={install_dir}/Waterwall
Restart=always
RestartSec=5
LimitNOFILE=1048576
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
"""
    path = Path(f"/etc/systemd/system/{unit}.service")
    path.write_text(text, encoding="utf-8")


def _copy_or_download_binary(dest_dir: Path, oldcpu: str) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "log").mkdir(exist_ok=True)
    (dest_dir / "libs").mkdir(exist_ok=True)
    target = dest_dir / "Waterwall"
    for src in (
        Path("/opt/waterwall-proto51-v2/Waterwall"),
        Path("/opt/waterwall-proto51/Waterwall"),
    ):
        if src.is_file():
            shutil.copy2(src, target)
            os.chmod(target, 0o755)
            return f"copied:{src}"
    arch = os.uname().machine if hasattr(os, "uname") else "x86_64"
    if arch in ("x86_64", "amd64"):
        asset = "Waterwall-linux-gcc-x64-old-cpu.zip" if oldcpu == "1" else "Waterwall-linux-gcc-x64.zip"
    elif arch in ("aarch64", "arm64"):
        asset = "Waterwall-linux-gcc-arm64-old-cpu.zip" if oldcpu == "1" else "Waterwall-linux-gcc-arm64.zip"
    else:
        raise ApplyError(f"unsupported arch: {arch}")
    url = f"{WW_REPO}/{asset}"
    tmp = Path(tempfile.gettempdir()) / asset
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as exc:
        raise ApplyError(
            f"WaterWall binary not found locally and download failed ({url}). "
            "Install waterwall-proto51-v2 first or place Waterwall under /opt."
        ) from exc
    shutil.unpack_archive(str(tmp), dest_dir)
    if not target.is_file():
        found = list(dest_dir.rglob("Waterwall"))
        if not found:
            raise ApplyError("Waterwall binary missing after extract")
        shutil.move(str(found[0]), target)
    os.chmod(target, 0o755)
    try:
        tmp.unlink()
    except OSError:
        pass
    return f"downloaded:{asset}"


def create_tunnel(body: dict[str, Any]) -> dict[str, Any]:
    side = str(body.get("side") or "").strip()
    if side in ("iran", "Iran"):
        side = "ir"
    if side not in ("ir", "kharej"):
        raise ApplyError("side must be ir or kharej")
    env = merge_env_update(
        {
            "SIDE": side,
            "IRAN_IP": str(body.get("iran_ip") or ""),
            "KHAREJ_IP": str(body.get("kharej_ip") or ""),
            "PROTO": str(body.get("proto") or "51"),
            "ENCRYPT": str(body.get("encrypt") or "0"),
            "AES_KEY": str(body.get("aes_key") or ""),
            "PORTS": _normalize_ports(str(body.get("ports") or DEFAULT_PORTS)),
            "OLDCPU": "1" if str(body.get("oldcpu") or "0") in ("1", "true", "yes") else "0",
            "PORT_OFFSET": str(body.get("port_offset") or PORT_OFFSET_DEFAULT),
            "ENC_ALGO": str(body.get("enc_algo") or ENC_ALGO_DEFAULT),
            "ENC_SALT": "",
        },
        {},
    )
    slot = allocate_slot()
    env["ENC_SALT"] = slot["enc_salt"]
    env["TUN_DEVICE"] = slot["tun"]
    install_dir = Path(slot["install_dir"])
    if install_dir.exists() and any(install_dir.iterdir()):
        raise ApplyError(f"target dir already exists: {install_dir}")
    install_dir.mkdir(parents=True, exist_ok=True)
    binary_src = _copy_or_download_binary(install_dir, env.get("OLDCPU", "0"))
    write_tunnel_env(install_dir / "tunnel.env", env)
    fake_tunnel = {
        "install_dir": str(install_dir),
        "tun": slot["tun"],
        "tun_local": slot["tun_local"],
        "tun_peer": slot["tun_peer"],
        "tun_addrs": [slot["tun_cidr"]],
        "name": slot["id"],
    }
    regenerate_json(fake_tunnel, env)
    write_service_unit(slot["unit"], install_dir, f"WaterWall Proto51 isolated tunnel {slot['id']}")
    _run(["systemctl", "daemon-reload"], timeout=20.0)
    enable = _run(["systemctl", "enable", f"{slot['unit']}.service"], timeout=20.0)
    start = _systemctl("restart", slot["unit"])
    time.sleep(1.5)
    created = get_tunnel(slot["id"])
    return {
        "ok": True,
        "slot": slot,
        "binary": binary_src,
        "enable": {
            "ok": enable.returncode == 0,
            "stderr": (enable.stderr or "")[-500:],
        },
        "start": start,
        "note": "Created an isolated profile. v1 (waterwall-proto51) and v2 were not modified.",
        "tunnel": created,
    }
