#!/usr/bin/env bash
# ww-panel — standalone WaterWall tunnel control panel
#
#   curl -fsSL https://raw.githubusercontent.com/khodehamed/ww-panel/master/install.sh | sudo bash
#
# Does NOT stop, edit, or restart waterwall-proto51, waterwall-proto51-v2, or backhaul.
#
set -euo pipefail

REPO_RAW="${WWPANEL_RAW:-https://raw.githubusercontent.com/khodehamed/ww-panel/master}"
REPO_TARBALL="${WWPANEL_TARBALL:-https://github.com/khodehamed/ww-panel/archive/refs/heads/master.tar.gz}"
INSTALL_DIR="/opt/ww-panel"
SERVICE_NAME="ww-panel"
CONF_ENV="${INSTALL_DIR}/panel.env"
BIN_LINK="/usr/local/bin/wwpanel"
BIN_LINK2="/usr/local/bin/ww-panel"
DEFAULT_PORT="12010"
DEFAULT_BIND="0.0.0.0"
DEFAULT_USER="admin"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
CYN='\033[0;36m'
NC='\033[0m'

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo -e "${RED}Run as root:${NC} curl -fsSL ... | sudo bash"
    exit 1
  fi
}

msg()  { echo -e "${CYN}==>${NC} $*"; }
ok()   { echo -e "${GRN}OK${NC} $*"; }
warn() { echo -e "${YLW}WARN${NC} $*"; }
err()  { echo -e "${RED}ERR${NC} $*" >&2; exit 1; }

read_tty() {
  if [[ -r /dev/tty ]]; then
    read "$@" </dev/tty
  else
    read "$@"
  fi
}

load_env() {
  if [[ -f "$CONF_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$CONF_ENV"
  fi
  PANEL_PORT="${PANEL_PORT:-$DEFAULT_PORT}"
  PANEL_BIND="${PANEL_BIND:-$DEFAULT_BIND}"
  PANEL_USER="${PANEL_USER:-$DEFAULT_USER}"
}

validate_port() {
  [[ "$1" =~ ^[0-9]+$ ]] || return 1
  (( $1 >= 1 && $1 <= 65535 )) || return 1
}

ensure_deps() {
  msg "Installing panel dependencies (python3/venv/curl) — WaterWall services are not touched..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null || err "apt-get update failed"
  apt-get install -y python3 python3-venv python3-pip curl ca-certificates tar >/dev/null \
    || err "apt-get install failed (need python3 python3-venv curl)"
  command -v python3 >/dev/null || err "python3 missing"
  ok "Dependencies ready"
}

self_dir() {
  local src="${BASH_SOURCE[0]:-}"
  if [[ -n "$src" && -f "$src" ]]; then
    cd "$(dirname "$src")" && pwd
  else
    echo ""
  fi
}

sync_app_files() {
  mkdir -p "$INSTALL_DIR"
  local src_dir tarball
  src_dir="$(self_dir)"

  if [[ -n "$src_dir" && -f "${src_dir}/app/main.py" ]]; then
    msg "Copying panel files from ${src_dir}..."
    mkdir -p "${INSTALL_DIR}/app/static" "${INSTALL_DIR}/systemd"
    cp -f "${src_dir}/app/"*.py "${INSTALL_DIR}/app/" 2>/dev/null || true
    cp -f "${src_dir}/app/static/"* "${INSTALL_DIR}/app/static/" 2>/dev/null || true
    cp -f "${src_dir}/requirements.txt" "${INSTALL_DIR}/" 2>/dev/null || true
    cp -f "${src_dir}/LICENSE" "${INSTALL_DIR}/" 2>/dev/null || true
    cp -f "${src_dir}/README.md" "${INSTALL_DIR}/" 2>/dev/null || true
    cp -f "${src_dir}/systemd/ww-panel.service" "${INSTALL_DIR}/systemd/" 2>/dev/null || true
    if [[ -f "${src_dir}/install.sh" ]]; then
      cp -f "${src_dir}/install.sh" "${INSTALL_DIR}/install.sh"
    fi
  else
    msg "Downloading ww-panel from GitHub..."
    tarball="/tmp/ww-panel-master.tar.gz"
    if ! curl -fL --connect-timeout 20 --max-time 120 "$REPO_TARBALL" -o "$tarball"; then
      err "Download failed: $REPO_TARBALL"
    fi
    # Extract dir must NOT match ww-panel-* (GitHub tarball root is ww-panel-master).
    local extract_dir="/tmp/wwpanel-src-$$"
    rm -rf "$extract_dir"
    mkdir -p "$extract_dir"
    tar -xzf "$tarball" -C "$extract_dir"
    local extracted=""
    if [[ -f "${extract_dir}/ww-panel-master/app/main.py" ]]; then
      extracted="${extract_dir}/ww-panel-master"
    else
      extracted="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d \
        -exec test -f '{}/app/main.py' \; -print | head -n1)"
    fi
    [[ -n "$extracted" && -f "${extracted}/app/main.py" ]] || err "tarball layout unexpected"
    mkdir -p "${INSTALL_DIR}/app/static" "${INSTALL_DIR}/systemd"
    cp -a "${extracted}/app/." "${INSTALL_DIR}/app/"
    cp -f "${extracted}/requirements.txt" "${INSTALL_DIR}/"
    cp -f "${extracted}/LICENSE" "${INSTALL_DIR}/" 2>/dev/null || true
    cp -f "${extracted}/README.md" "${INSTALL_DIR}/" 2>/dev/null || true
    cp -f "${extracted}/systemd/ww-panel.service" "${INSTALL_DIR}/systemd/" 2>/dev/null || true
    cp -f "${extracted}/install.sh" "${INSTALL_DIR}/install.sh"
    rm -rf "$extract_dir" "$tarball"
  fi

  if [[ ! -f "${INSTALL_DIR}/install.sh" ]]; then
    curl -fsSL "${REPO_RAW}/install.sh" -o "${INSTALL_DIR}/install.sh" || warn "Could not cache install.sh"
  fi
  chmod +x "${INSTALL_DIR}/install.sh" 2>/dev/null || true
  [[ -f "${INSTALL_DIR}/app/main.py" ]] || err "app/main.py missing after sync"
  ok "Panel files in ${INSTALL_DIR}"
}

setup_venv() {
  msg "Creating Python venv + installing FastAPI/uvicorn..."
  python3 -m venv "${INSTALL_DIR}/venv"
  "${INSTALL_DIR}/venv/bin/pip" install -U pip >/dev/null
  "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" >/dev/null \
    || err "pip install failed"
  ok "venv ready"
}

gen_password() {
  local pass=""
  pass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12)[:16])' 2>/dev/null || true)"
  if [[ ${#pass} -lt 12 ]] && command -v openssl >/dev/null 2>&1; then
    pass="$(openssl rand -hex 8 2>/dev/null || true)"
  fi
  [[ ${#pass} -ge 12 ]] || err "failed to generate panel password"
  printf '%s' "$pass"
}

write_panel_env() {
  local port="$1" bind="$2" user="$3" password="$4"
  local salt hash secret
  salt="$(openssl rand -hex 16 2>/dev/null || python3 - <<'PY'
import secrets; print(secrets.token_hex(16))
PY
)"
  secret="$(openssl rand -hex 24 2>/dev/null || python3 - <<'PY'
import secrets; print(secrets.token_hex(24))
PY
)"
  hash="$("${INSTALL_DIR}/venv/bin/python" - "$password" "$salt" <<'PY'
import hashlib, sys
password, salt = sys.argv[1], sys.argv[2]
print(hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 180000).hex())
PY
)"
  cat > "$CONF_ENV" <<EOF
PANEL_PORT=${port}
PANEL_BIND=${bind}
PANEL_USER=${user}
PANEL_PASS_SALT=${salt}
PANEL_PASS_HASH=${hash}
PANEL_SECRET=${secret}
EOF
  chmod 600 "$CONF_ENV"
}

rewrite_password() {
  local user="$1" password="$2"
  load_env
  write_panel_env "${PANEL_PORT}" "${PANEL_BIND}" "$user" "$password"
}

write_unit() {
  local port="$1" bind="$2"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=WaterWall Tunnel Control Panel (ww-panel)
Documentation=https://github.com/khodehamed/ww-panel
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-${CONF_ENV}
ExecStart=${INSTALL_DIR}/venv/bin/python -m uvicorn app.main:app --host ${bind} --port ${port}
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
}

write_menu_wrapper() {
  cat > "$BIN_LINK" <<EOF
#!/usr/bin/env bash
if [[ -f "${INSTALL_DIR}/install.sh" ]]; then
  exec bash "${INSTALL_DIR}/install.sh" "\$@"
fi
exec bash -c "curl -fsSL ${REPO_RAW}/install.sh | sudo bash"
EOF
  chmod +x "$BIN_LINK"
  ln -sfn "$BIN_LINK" "$BIN_LINK2"
}

start_panel() {
  msg "Enabling ${SERVICE_NAME}.service (WaterWall units are left alone)..."
  systemctl daemon-reload || err "systemctl daemon-reload failed"
  systemctl enable "${SERVICE_NAME}.service" || err "Failed to enable ${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}.service" || true
  sleep 1
  local state
  state="$(systemctl is-active "${SERVICE_NAME}.service" 2>/dev/null || true)"
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  if [[ "$state" == "active" ]]; then
    ok "Panel is active"
    return 0
  fi
  journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
  err "Panel failed to start (state=${state:-unknown})"
}

do_install() {
  local port bind user pass keep_pass="0"
  load_env
  port="${PANEL_PORT:-$DEFAULT_PORT}"
  bind="${PANEL_BIND:-$DEFAULT_BIND}"
  user="${PANEL_USER:-$DEFAULT_USER}"

  echo
  echo -e "${CYN}ww-panel install${NC}"
  echo "  This installer NEVER stops or edits waterwall-proto51 / v2 / backhaul."
  echo "  Bind ${bind} — open the panel port on the firewall (ufw/iptables/security group)."
  echo
  read_tty -r -p "Panel listen port [${port}]: " tmp || true
  [[ -n "${tmp:-}" ]] && port="$tmp"
  validate_port "$port" || err "Invalid port"
  read_tty -r -p "Panel username [${user}]: " tmp || true
  [[ -n "${tmp:-}" ]] && user="$tmp"

  if [[ -f "$CONF_ENV" ]]; then
    read_tty -r -p "Keep existing password? [Y/n]: " tmp || true
    case "${tmp:-Y}" in
      n|N|no|NO) keep_pass=0 ;;
      *) keep_pass=1 ;;
    esac
  fi

  ensure_deps
  sync_app_files
  setup_venv

  if [[ "$keep_pass" == "1" ]]; then
    # Keep hashes; only refresh port/bind/user fields if needed.
    # shellcheck disable=SC1090
    source "$CONF_ENV"
    PANEL_PORT="$port"
    PANEL_BIND="$bind"
    PANEL_USER="$user"
    cat > "$CONF_ENV" <<EOF
PANEL_PORT=${PANEL_PORT}
PANEL_BIND=${PANEL_BIND}
PANEL_USER=${PANEL_USER}
PANEL_PASS_SALT=${PANEL_PASS_SALT}
PANEL_PASS_HASH=${PANEL_PASS_HASH}
PANEL_SECRET=${PANEL_SECRET}
EOF
    chmod 600 "$CONF_ENV"
    pass=""
  else
    pass="$(gen_password)"
    write_panel_env "$port" "$bind" "$user" "$pass"
  fi

  write_unit "$port" "$bind"
  write_menu_wrapper
  start_panel

  echo
  ok "ww-panel installed"
  echo "  dir     : ${INSTALL_DIR}"
  echo "  service : systemctl status ${SERVICE_NAME}"
  echo "  menu    : wwpanel   (or ww-panel)"
  echo "  listen  : ${bind}:${port}"
  echo "  login   : user=${user}"
  if [[ -n "$pass" ]]; then
    echo -e "  ${YLW}password (shown once):${NC} ${GRN}${pass}${NC}"
    echo "            save it now — it is stored as a hash only"
  else
    echo "  password: unchanged"
  fi
  echo "  URL     : http://<server-ip>:${port}/"
  echo "  firewall: allow TCP ${port} (panel binds ${bind})"
  echo "  safety  : waterwall-proto51 / waterwall-proto51-v2 / backhaul were NOT modified"
}

show_status() {
  load_env
  echo
  echo -e "${CYN}ww-panel${NC}"
  echo "  port    : ${PANEL_PORT}"
  echo "  bind    : ${PANEL_BIND}"
  echo "  user    : ${PANEL_USER}"
  echo "  env     : ${CONF_ENV}"
  echo "  note    : this panel only manages its own unit (${SERVICE_NAME})"
  echo
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  echo
  echo -e "${CYN}Discovered WaterWall units (read-only peek):${NC}"
  systemctl is-active waterwall-proto51.service 2>/dev/null && echo "  waterwall-proto51: active" || echo "  waterwall-proto51: not-active/missing"
  systemctl is-active waterwall-proto51-v2.service 2>/dev/null && echo "  waterwall-proto51-v2: active" || echo "  waterwall-proto51-v2: not-active/missing"
}

change_port() {
  load_env
  local port
  read_tty -r -p "New panel port [${PANEL_PORT}]: " port
  port="${port:-$PANEL_PORT}"
  validate_port "$port" || err "Invalid port"
  PANEL_PORT="$port"
  if [[ -f "$CONF_ENV" ]]; then
    sed -i "s/^PANEL_PORT=.*/PANEL_PORT=${port}/" "$CONF_ENV"
  else
    err "missing ${CONF_ENV} — run Install first"
  fi
  write_unit "$port" "${PANEL_BIND}"
  systemctl daemon-reload
  systemctl restart "${SERVICE_NAME}"
  ok "Panel now listening on ${PANEL_BIND}:${port}"
  echo "  Open TCP ${port} on the firewall. WaterWall tunnels were not restarted."
}

set_password() {
  [[ -f "$CONF_ENV" ]] || err "Not installed"
  load_env
  local user pass
  read_tty -r -p "Username [${PANEL_USER}]: " user || true
  user="${user:-$PANEL_USER}"
  read_tty -r -s -p "New password (empty=auto): " pass || true
  echo
  if [[ -z "${pass:-}" ]]; then
    pass="$(gen_password)"
    echo -e "${YLW}Generated password (shown once):${NC} ${GRN}${pass}${NC}"
  fi
  [[ ${#pass} -ge 6 ]] || err "password too short"
  rewrite_password "$user" "$pass"
  systemctl restart "${SERVICE_NAME}" || true
  ok "Password updated for user=${user}"
}

uninstall_all() {
  msg "Uninstalling ww-panel only (WaterWall / backhaul stay running)..."
  systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload || true
  rm -rf "$INSTALL_DIR"
  rm -f "$BIN_LINK" "$BIN_LINK2"
  ok "Removed ${SERVICE_NAME}"
}

menu() {
  while true; do
    clear 2>/dev/null || true
    echo -e "${CYN}ww-panel · WaterWall Control Panel${NC}"
    echo "================================="
    echo "Does not touch waterwall-proto51 / v2 / backhaul."
    echo
    echo "1) Install / Reinstall"
    echo "2) Status"
    echo "3) Restart panel"
    echo "4) Change panel port"
    echo "5) Set password"
    echo "6) Uninstall"
    echo "0) Exit"
    echo
    read_tty -r -p "Select: " c || exit 0
    case "$c" in
      1) do_install ;;
      2) show_status ;;
      3) systemctl restart "$SERVICE_NAME"; show_status ;;
      4) change_port ;;
      5) set_password ;;
      6) uninstall_all ;;
      0) exit 0 ;;
      *) warn "Invalid option" ;;
    esac
    echo
    read_tty -r -p "Press Enter to continue..." _ || true
  done
}

main() {
  need_root
  case "${1:-}" in
    install) do_install ;;
    status) show_status ;;
    restart) systemctl restart "$SERVICE_NAME"; show_status ;;
    port|change-port) change_port ;;
    password|passwd) set_password ;;
    uninstall) uninstall_all ;;
    *) menu ;;
  esac
}

main "$@"
