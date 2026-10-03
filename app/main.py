"""ww-panel FastAPI application — discover / health / edit / create WaterWall tunnels."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .apply import ApplyError, apply_update, control_unit, create_tunnel
from .discover import allocate_slot, discover_tunnels, get_tunnel, parse_env_file
from .metrics import STORE

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
PANEL_ENV_CANDIDATES = (
    Path(os.environ.get("WWPANEL_ENV", "")),
    Path("/opt/ww-panel/panel.env"),
    APP_DIR.parent / "panel.env",
)

COOKIE_NAME = "wwpanel"
SESSION_TTL = 12 * 3600
_login_hits: dict[str, list[float]] = {}


def _load_panel_env() -> dict[str, str]:
    for path in PANEL_ENV_CANDIDATES:
        if path and path.is_file():
            data = parse_env_file(path)
            data["_path"] = str(path)
            return data
    return {
        "PANEL_PORT": os.environ.get("PANEL_PORT", "12010"),
        "PANEL_BIND": os.environ.get("PANEL_BIND", "0.0.0.0"),
        "PANEL_USER": os.environ.get("PANEL_USER", "admin"),
        "PANEL_PASS_SALT": os.environ.get("PANEL_PASS_SALT", ""),
        "PANEL_PASS_HASH": os.environ.get("PANEL_PASS_HASH", ""),
        "PANEL_SECRET": os.environ.get("PANEL_SECRET", ""),
        "_path": "",
    }


def panel_cfg() -> dict[str, str]:
    return _load_panel_env()


def hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 180_000)
    return dk.hex()


def _secret() -> bytes:
    raw = panel_cfg().get("PANEL_SECRET") or os.environ.get("PANEL_SECRET") or "ww-panel-dev"
    return raw.encode("utf-8")


def _sign_session(username: str, exp: int) -> str:
    payload = f"{username}|{exp}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def _read_session(token: str) -> str | None:
    try:
        username, exp_s, sig = token.split("|", 2)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return None
    if exp < int(time.time()):
        return None
    expect = hmac.new(_secret(), f"{username}|{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    return username


def _check_password(username: str, password: str) -> bool:
    cfg = panel_cfg()
    want_user = cfg.get("PANEL_USER") or "admin"
    salt = cfg.get("PANEL_PASS_SALT") or ""
    digest = cfg.get("PANEL_PASS_HASH") or ""
    if not salt or not digest:
        # First-boot / missing env: refuse rather than open the panel.
        return False
    if not hmac.compare_digest(username, want_user):
        return False
    got = hash_password(password, salt)
    return hmac.compare_digest(got, digest)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limit_login(ip: str) -> None:
    now = time.time()
    hits = [t for t in _login_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 12:
        raise HTTPException(status_code=429, detail="too many login attempts")
    hits.append(now)
    _login_hits[ip] = hits


def _user_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        user = _read_session(token)
        if user:
            return user
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        import base64

        try:
            raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            username, password = raw.split(":", 1)
        except Exception:
            return None
        if _check_password(username, password):
            return username
    return None


def require_user(request: Request) -> str:
    user = _user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="auth required")
    return user


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STORE.start()
    yield
    STORE.stop()


app = FastAPI(title="ww-panel", version=__version__, lifespan=lifespan)


@app.exception_handler(ApplyError)
async def _apply_error(_: Request, exc: ApplyError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _public_tunnel(t: dict[str, Any]) -> dict[str, Any]:
    out = dict(t)
    out.pop("aes_key", None)
    live = STORE.latest_map().get(t.get("tun") or "", {})
    out["rx_bps"] = live.get("rx_bps", 0)
    out["tx_bps"] = live.get("tx_bps", 0)
    cached = STORE.cached_ping(t.get("id") or "")
    if cached and not (t.get("ping") or {}).get("ok"):
        out["ping"] = cached
    return out


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    cfg = panel_cfg()
    return {
        "ok": True,
        "service": "ww-panel",
        "version": __version__,
        "port": int(cfg.get("PANEL_PORT") or 12010),
        "bind": cfg.get("PANEL_BIND") or "0.0.0.0",
    }


@app.get("/api/panel")
def api_panel(_user: str = Depends(require_user)) -> dict[str, Any]:
    cfg = panel_cfg()
    return {
        "port": int(cfg.get("PANEL_PORT") or 12010),
        "bind": cfg.get("PANEL_BIND") or "0.0.0.0",
        "user": cfg.get("PANEL_USER") or "admin",
        "env_path": cfg.get("_path") or "",
        "note": "Bind is 0.0.0.0 — open the panel port on the firewall. This panel never edits backhaul.",
    }


@app.get("/api/me")
def api_me(user: str = Depends(require_user)) -> dict[str, Any]:
    return {"ok": True, "user": user}


@app.post("/api/login")
async def api_login(request: Request, response: Response) -> dict[str, Any]:
    _rate_limit_login(_client_ip(request))
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not _check_password(username, password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    exp = int(time.time()) + SESSION_TTL
    token = _sign_session(username, exp)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
        path="/",
    )
    return {"ok": True, "user": username}


@app.post("/api/logout")
def api_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/tunnels")
def api_tunnels(_user: str = Depends(require_user)) -> dict[str, Any]:
    items = [_public_tunnel(t) for t in discover_tunnels()]
    return {"ok": True, "tunnels": items}


@app.get("/api/tunnels/{tunnel_id}")
def api_tunnel(tunnel_id: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    t = get_tunnel(tunnel_id)
    if not t:
        raise HTTPException(status_code=404, detail="tunnel not found")
    return {"ok": True, "tunnel": _public_tunnel(t)}


@app.get("/api/tunnels/{tunnel_id}/metrics")
def api_metrics(tunnel_id: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    t = get_tunnel(tunnel_id)
    if not t:
        raise HTTPException(status_code=404, detail="tunnel not found")
    tun = t.get("tun") or ""
    snap = STORE.snapshot(tun)
    snap["tunnel_id"] = tunnel_id
    snap["ping"] = t.get("ping") or {}
    return snap


@app.put("/api/tunnels/{tunnel_id}")
async def api_edit(tunnel_id: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    body = await request.json()
    result = apply_update(tunnel_id, body if isinstance(body, dict) else {})
    if result.get("tunnel"):
        result["tunnel"] = _public_tunnel(result["tunnel"])
    return result


@app.post("/api/tunnels/{tunnel_id}/restart")
def api_restart(tunnel_id: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    result = control_unit(tunnel_id, "restart")
    if result.get("tunnel"):
        result["tunnel"] = _public_tunnel(result["tunnel"])
    return result


@app.post("/api/tunnels/{tunnel_id}/start")
def api_start(tunnel_id: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    result = control_unit(tunnel_id, "start")
    if result.get("tunnel"):
        result["tunnel"] = _public_tunnel(result["tunnel"])
    return result


@app.post("/api/tunnels/{tunnel_id}/stop")
def api_stop(tunnel_id: str, _user: str = Depends(require_user)) -> dict[str, Any]:
    result = control_unit(tunnel_id, "stop")
    if result.get("tunnel"):
        result["tunnel"] = _public_tunnel(result["tunnel"])
    return result


@app.post("/api/tunnels")
async def api_create(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
    body = await request.json()
    result = create_tunnel(body if isinstance(body, dict) else {})
    if result.get("tunnel"):
        result["tunnel"] = _public_tunnel(result["tunnel"])
    return result


@app.get("/api/slots/next")
def api_next_slot(_user: str = Depends(require_user)) -> dict[str, Any]:
    try:
        return {"ok": True, "slot": allocate_slot()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
