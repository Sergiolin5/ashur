#!/bin/bash
# Ashur node installer for macOS (Mac M1).
# Run in Terminal, as your normal user (no sudo), with Tailscale on:
#   curl -fsSL http://zimaos:8711/install/mac.sh | bash
# The core serves this script and the node code (tailnet only), so it works with a private repo.
# Running it again updates the code and keeps the node's identity.
set -euo pipefail

STATE="$HOME/.ashur"
APP="$STATE/app"
VENV="$STATE/venv"
CONFIG="$STATE/node.yaml"
LABEL="com.ashur.node"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CORE="__ASHUR_CORE__"            # filled in by the core that serves this script

say() { printf '\033[36m[ashur]\033[0m %s\n' "$*"; }
ask() { local v; read -r -p "$1" v < /dev/tty; printf '%s' "$v"; }

mkdir -p "$STATE"

# 1. Python 3.12 through uv (no sudo, nothing installed system-wide)
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
    say "Instalando uv (gestor de Python, solo para tu usuario)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    UV="$HOME/.local/bin/uv"
fi

# 2. Code (replaced on every run; identity and logs live outside it)
say "Descargando Ashur..."
tmp="$(mktemp -d)"
if [[ "$CORE" == __* ]]; then                       # run from GitHub (public repo): the old way
    curl -fsSL "https://github.com/Sergiolin5/ashur/archive/refs/heads/main.tar.gz" | tar -xz -C "$tmp"
    src="$tmp/ashur-main"
else
    mkdir -p "$tmp/app"
    curl -fsSL "$CORE/install/code.tar.gz" | tar -xz -C "$tmp/app"
    src="$tmp/app"
fi
rm -rf "$APP"
mv "$src" "$APP"
rm -rf "$tmp"

# 3. Private Python environment
if [ ! -x "$VENV/bin/python" ]; then
    say "Creando entorno de Python 3.12..."
    "$UV" venv --quiet --python 3.12 "$VENV"
fi
"$UV" pip install --quiet --python "$VENV/bin/python" -r "$APP/requirements-node.txt"
PY="$VENV/bin/python"

# 4. Config (asked only the first time)
if [ ! -f "$CONFIG" ]; then
    while :; do
        device="$(ask 'Nombre de este equipo para Ashur (minúsculas, ej. mac-m1): ')"
        device="$(printf '%s' "$device" | tr -d '[:space:]')"
        [[ "$device" =~ ^[a-z0-9][a-z0-9_-]*$ ]] && break
    done
    if [[ "$CORE" == __* ]]; then
        server="$(ask 'Nombre del servidor en Tailscale (ej. zimaos): ' | tr -d '[:space:]')"
        core_url="http://$server:8711"
    else
        core_url="$CORE"
    fi
    echo "Política de recursos:"
    echo "  1 = portátil con batería (Mac M1)"
    echo "  2 = máquina principal compartida"
    choice="$(ask 'Elige 1 o 2: ')"
    policy="portable"; [ "$choice" = "2" ] && policy="shared_main_machine"
    cat > "$CONFIG" <<EOF
device_id: $device
persona: $device
core_url: $core_url
modules: [inventory, docs]
yield_policy: $policy
EOF
    say "Configuración guardada en $CONFIG"
fi

cd "$APP"

# 5. Enroll once (the key is typed or taken from ASHUR_ENROLL_KEY, never saved)
if [ ! -f "$STATE/node_token" ]; then
    say "Registrando el nodo. Pega la enroll key de keys.json del servidor:"
    if ! "$PY" -m node.agent --config "$CONFIG" --enroll < /dev/tty; then
        say "El registro falló. Revisa la clave, que Tailscale esté activo y que http://<servidor>:8711/health abra en esta Mac."
        exit 1
    fi
fi

# 6. Self-test
say "Autoprueba:"
"$PY" -m node.agent --config "$CONFIG" --selftest || say "Algún módulo no pasó la autoprueba; revisa $STATE/agent.log"

# 7. Start at login, inside your session. node.boot keeps the agent running and
#    applies the code updates the core ships; a fresh install starts from this copy.
rm -f "$STATE/current" "$STATE/pending" "$STATE/previous"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>node.boot</string>
    <string>--config</string><string>$CONFIG</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$STATE/launchd.log</string>
  <key>StandardErrorPath</key><string>$STATE/launchd.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "Listo. El nodo está corriendo y arrancará solo al iniciar sesión."
say "Registro: $STATE/agent.log"
say "Para un inventario completo, macOS pide 'Acceso total al disco' para este programa:"
say "  $("$PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
