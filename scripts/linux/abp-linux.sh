#!/usr/bin/env bash
# Agentic Bot Platform (ABP): install, update and remove it on any Linux distribution.
#
#   abp-linux.sh install [--system] [--branch main] [--source URL|BUNDLE] [--no-service] [--port 8787]
#   abp-linux.sh update  [--check]          fast-forward to the branch's latest, reinstall, restart; rolls back if
#                                           the new version does not answer its health check
#   abp-linux.sh status
#   abp-linux.sh uninstall [--keep-data]
#
# Per user (default): code in ~/.local/share/agentic-bot-platform/app, state in .../home (ABP_HOME), commands in
# ~/.local/bin (abp, abp-server, abp-tui, abp-modkit, abp-update), a systemd --user service, and a desktop entry that opens the
# dashboard. --system (as root): /opt/agentic-bot-platform, /var/lib/abp, a service user `abp`, a system service
# and commands in /usr/local/bin.
#
# Distribution packages come from the native manager: apt (Debian, Ubuntu, Mint, Pop!_OS...), dnf (Fedora, RHEL,
# Rocky, Alma), zypper (openSUSE), pacman (Arch, Manjaro), apk (Alpine), xbps (Void). On NixOS use the flake
# instead (services.agentic-bot-platform; see docs/linux.md). Python 3.11+ is required; where the distribution's
# is older, uv provides one.
set -euo pipefail

REPO_DEFAULT="https://github.com/LoopyLuci/AgenticBotPlatform.git"
UNIT="agentic-bot-platform"   # (not NAME: /etc/os-release defines NAME)
cmd="${1:-help}"; shift || true
SYSTEM=0; BRANCH=main; SOURCE="$REPO_DEFAULT"; SERVICE=1; PORT=8787; CHECK=0; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --system) SYSTEM=1 ;; --branch) BRANCH="$2"; shift ;; --source) SOURCE="$2"; shift ;;
    --no-service) SERVICE=0 ;; --port) PORT="$2"; shift ;; --check) CHECK=1 ;; --keep-data) KEEP=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$SYSTEM" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "--system needs root (sudo)"
  BASE=/opt/$UNIT; APP=$BASE/app; HOME_DIR=/var/lib/abp; BIN=/usr/local/bin; RUN_AS=abp
else
  [ "$(id -u)" != 0 ] || die "run as your own user for a per-user install, or pass --system for a system service"
  BASE="${XDG_DATA_HOME:-$HOME/.local/share}/$UNIT"; APP=$BASE/app; HOME_DIR=$BASE/home; BIN="$HOME/.local/bin"; RUN_AS="$(id -un)"
fi
PY="$APP/.venv/bin/python"
STATE="$BASE/install.state"

sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; elif command -v sudo >/dev/null; then sudo "$@"; else die "need root to run: $*"; fi; }

distro_packages() {
  local distro
  distro=$( . /etc/os-release 2>/dev/null; echo "${ID:-}" )   # in a subshell: its NAME, VERSION... stay out
  if [ "$distro" = nixos ]; then
    die "NixOS: add the flake instead (nixosModules.default -> services.agentic-bot-platform.enable = true), or: nix run github:LoopyLuci/AgenticBotPlatform"
  fi
  local need=()
  command -v git >/dev/null || need+=(git)
  command -v curl >/dev/null || need+=(curl)
  if command -v apt-get >/dev/null; then
    python3 -c 'import venv, ensurepip' 2>/dev/null || need+=(python3-venv python3-pip)
    python3 -c 'import tkinter' 2>/dev/null || need+=(python3-tk)        # the CI/CD window (abp_cicd)
    command -v python3 >/dev/null || need+=(python3)
    [ ${#need[@]} -eq 0 ] || { sudo_ apt-get update -qq; sudo_ env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" ca-certificates; }
  elif command -v dnf >/dev/null; then
    command -v python3 >/dev/null || need+=(python3)
    python3 -c 'import tkinter' 2>/dev/null || need+=(python3-tkinter)
    [ ${#need[@]} -eq 0 ] || sudo_ dnf install -y -q "${need[@]}"
  elif command -v zypper >/dev/null; then
    command -v python3 >/dev/null || need+=(python3)
    python3 -c 'import tkinter' 2>/dev/null || need+=(python3-tk)
    [ ${#need[@]} -eq 0 ] || sudo_ zypper -n install "${need[@]}"
  elif command -v pacman >/dev/null; then
    command -v python3 >/dev/null || need+=(python)
    python3 -c 'import tkinter' 2>/dev/null || need+=(tk)
    [ ${#need[@]} -eq 0 ] || sudo_ pacman -S --noconfirm --needed "${need[@]}"
  elif command -v apk >/dev/null; then
    command -v python3 >/dev/null || need+=(python3)
    python3 -c 'import venv' 2>/dev/null || need+=(py3-virtualenv)
    python3 -c 'import tkinter' 2>/dev/null || need+=(py3-tkinter)
    [ ${#need[@]} -eq 0 ] || sudo_ apk add --no-cache "${need[@]}" build-base python3-dev libffi-dev
  elif command -v xbps-install >/dev/null; then
    command -v python3 >/dev/null || need+=(python3)
    [ ${#need[@]} -eq 0 ] || sudo_ xbps-install -Sy "${need[@]}"
  else
    [ ${#need[@]} -eq 0 ] || die "install these with your package manager, then run this again: ${need[*]}"
  fi
}

python_ok() { "$1" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

pick_python() {
  for p in python3.13 python3.12 python3.11 python3; do
    if command -v "$p" >/dev/null && python_ok "$(command -v "$p")"; then command -v "$p"; return; fi
  done
  say "the system Python is older than 3.11; getting one with uv" >&2
  if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then curl -LsSf https://astral.sh/uv/install.sh | sh >&2; fi
  local uv; uv="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$uv" python install 3.12 >&2
  "$uv" python find 3.12
}

deps() {
  [ -x "$PY" ] || "$(pick_python)" -m venv "$APP/.venv"
  "$PY" -m pip install -q --upgrade pip
  # the lock pins every package with hashes; fall back to requirements.txt only if a pinned wheel is unavailable here
  "$PY" -m pip install -q --require-hashes -r "$APP/requirements.lock" || "$PY" -m pip install -q -r "$APP/requirements.txt"
  "$PY" -m compileall -q "$APP/bot" "$APP"/abp_* >/dev/null 2>&1 || true
}

launchers() {
  mkdir -p "$BIN"
  local env="export ABP_CALLER_CWD=\"\$PWD\" PYTHONPATH=\"$APP\" ABP_HOME=\"$HOME_DIR\" DASHBOARD_PORT=\"\${DASHBOARD_PORT:-$PORT}\""
  for spec in "abp:abp_cli" "abp-server:bot.sentinel.guardian" "abp-tui:bot.tui" "abp-modkit:abp_modkit"; do
    local n="${spec%%:*}" m="${spec#*:}"
    printf '#!/bin/sh\n%s\ncd "$ABP_HOME" && exec "%s" -m %s "$@"\n' "$env" "$PY" "$m" > "$BIN/$n"
    chmod 755 "$BIN/$n"
  done
  printf '#!/bin/sh\nexec "%s" update "$@"\n' "$APP/scripts/linux/abp-linux.sh" > "$BIN/abp-update"
  chmod 755 "$BIN/abp-update"
}

unit() {
  cat <<EOF
[Unit]
Description=Agentic Bot Platform
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=ABP_HOME=$HOME_DIR
Environment=DASHBOARD_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$APP
WorkingDirectory=$HOME_DIR
ExecStart=$PY -m bot.sentinel.guardian
Restart=on-failure
RestartSec=10
KillMode=control-group
$( [ "$SYSTEM" = 1 ] && printf 'User=abp\nGroup=abp\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=full\nReadWritePaths=%s\n' "$HOME_DIR" )

[Install]
WantedBy=$( [ "$SYSTEM" = 1 ] && echo multi-user.target || echo default.target )
EOF
}

sctl() { if [ "$SYSTEM" = 1 ]; then systemctl "$@"; else systemctl --user "$@"; fi; }
have_systemd() { command -v systemctl >/dev/null && { [ "$SYSTEM" = 1 ] || systemctl --user show-environment >/dev/null 2>&1; }; }

service_install() {
  [ "$SERVICE" = 1 ] || return 0
  if ! have_systemd; then say "no systemd here: start it with abp-server (or your init system)"; return 0; fi
  local dir
  if [ "$SYSTEM" = 1 ]; then dir=/etc/systemd/system; else dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; fi
  mkdir -p "$dir"
  unit > "$dir/$UNIT.service"
  sctl daemon-reload
  sctl enable --now "$UNIT.service" >/dev/null
  if [ "$SYSTEM" = 0 ] && command -v loginctl >/dev/null; then
    loginctl enable-linger "$RUN_AS" 2>/dev/null || say "to keep ABP running after you log out: sudo loginctl enable-linger $RUN_AS"
  fi
}

restart() {
  if [ "$SERVICE" = 1 ] && have_systemd && sctl is-enabled "$UNIT.service" >/dev/null 2>&1; then sctl restart "$UNIT.service"
  else pkill -f "$APP/.venv/bin/python -m bot.sentinel.guardian" 2>/dev/null || true; fi
}

healthy() {
  for _ in $(seq "${1:-120}"); do
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

desktop_entry() {
  [ "$SYSTEM" = 1 ] && return 0
  local d="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$d"
  cat > "$d/$UNIT.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Agentic Bot Platform
Comment=Bots, agents, models and machines in one place
Exec=xdg-open http://127.0.0.1:$PORT/
Icon=applications-internet
Categories=Development;Network;
EOF
}

do_install() {
  say "installing ABP ($([ "$SYSTEM" = 1 ] && echo system service || echo "for $RUN_AS")) from $SOURCE ($BRANCH)"
  distro_packages
  if [ "$SYSTEM" = 1 ] && ! id abp >/dev/null 2>&1; then
    useradd --system --home-dir "$HOME_DIR" --shell /usr/sbin/nologin abp 2>/dev/null || adduser -S -h "$HOME_DIR" abp
  fi
  mkdir -p "$BASE" "$HOME_DIR"
  if [ -d "$APP/.git" ]; then say "already installed in $APP: updating instead"; do_update; return; fi
  git clone -q -b "$BRANCH" "$SOURCE" "$APP"
  deps
  launchers
  [ "$SYSTEM" = 1 ] && chown -R abp:abp "$HOME_DIR" && chmod 750 "$HOME_DIR"
  printf 'system=%s\nport=%s\nbranch=%s\nsource=%s\n' "$SYSTEM" "$PORT" "$BRANCH" "$SOURCE" > "$STATE"
  service_install
  desktop_entry
  if [ "$SERVICE" = 1 ] && have_systemd; then
    if healthy 180; then say "ABP is running: http://127.0.0.1:$PORT/ (the dashboard signs itself in on this machine)"
    else die "ABP did not answer on port $PORT; see: $( [ "$SYSTEM" = 1 ] && echo journalctl -u $UNIT || echo journalctl --user -u $UNIT )"; fi
  fi
  case ":$PATH:" in *":$BIN:"*) ;; *) say "add $BIN to your PATH for the abp commands" ;; esac
  say "done. Commands: abp (CLI), abp-tui, abp-server, abp-modkit, abp-update"
}

do_update() {
  [ -d "$APP/.git" ] || die "ABP is not installed in $APP (run: $0 install$( [ "$SYSTEM" = 1 ] && echo ' --system'))"
  [ -f "$STATE" ] && . <(sed 's/^/S_/' "$STATE") && PORT="${S_port:-$PORT}" && BRANCH="${S_branch:-$BRANCH}"
  cd "$APP"
  local src; src="$(git remote get-url origin)"
  git fetch -q origin "$BRANCH"
  local cur new; cur="$(git rev-parse HEAD)"; new="$(git rev-parse FETCH_HEAD)"
  if [ "$cur" = "$new" ]; then say "up to date ($(git log --oneline -1 | cut -c1-60))"; return 0; fi
  if ! git merge-base --is-ancestor "$cur" "$new"; then die "the installed copy has diverged from $src $BRANCH; not overwriting it"; fi
  say "$(git rev-list --count "$cur..$new") new commit(s): $(git log --oneline -1 "$new" | cut -c1-70)"
  [ "$CHECK" = 1 ] && return 0
  git merge -q --ff-only "$new"
  if deps && launchers && restart && { [ "$SERVICE" = 0 ] || ! have_systemd || healthy 180; }; then
    printf 'updated=%s\nfrom=%s\n' "$(date -u +%FT%TZ)" "$cur" >> "$STATE"
    say "updated to $(git log --oneline -1 | cut -c1-70)"
  else
    say "the new version did not come up; rolling back to ${cur:0:7}"
    git reset -q --hard "$cur"
    deps; launchers; restart
    healthy 180 && die "rolled back to ${cur:0:7}; it is running again. The update is not applied." || die "rolled back, but ABP still does not answer; see the service log"
  fi
}

do_status() {
  if [ ! -d "$APP/.git" ]; then echo "not installed ($APP)"; return 1; fi
  echo "installed: $APP ($(git -C "$APP" log --oneline -1 | cut -c1-70))"
  echo "state:     $HOME_DIR"
  have_systemd && echo "service:   $(sctl is-active "$UNIT.service" 2>/dev/null || true)"
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && echo "health:    ok on port $PORT" || echo "health:    not answering on port $PORT"
}

do_uninstall() {
  if have_systemd && sctl is-enabled "$UNIT.service" >/dev/null 2>&1; then sctl disable --now "$UNIT.service" >/dev/null; fi
  rm -f "$BIN"/abp "$BIN"/abp-server "$BIN"/abp-tui "$BIN"/abp-modkit "$BIN"/abp-update
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT.service" "/etc/systemd/system/$UNIT.service" 2>/dev/null || true
  rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/$UNIT.desktop"
  rm -rf "$APP" "$STATE"
  if [ "$KEEP" = 1 ]; then
    rmdir "$BASE" 2>/dev/null || true   # the install folder, unless the kept state is inside it
    say "removed ABP; kept its state in $HOME_DIR"
  else
    rm -rf "$HOME_DIR" "$BASE"; say "removed ABP and its state"
  fi
}

case "$cmd" in
  install) do_install ;;
  update) do_update ;;
  status) do_status ;;
  uninstall) do_uninstall ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
