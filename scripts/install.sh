#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/leosysd/JY.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/polymarket-copy}"
SERVICE_NAME="${SERVICE_NAME:-polymarket-copy}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Please run as root, or install sudo first." >&2
    exit 1
  fi
  SUDO="sudo"
fi

echo "[1/6] Installing system packages..."
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip git ca-certificates curl

echo "[2/6] Preparing ${INSTALL_DIR}..."
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO chown -R "$(id -un):$(id -gn)" "$INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "[3/6] Updating existing Git checkout..."
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -z "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
  echo "[3/6] Cloning repository..."
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
else
  echo "Install dir exists and is not an empty Git repo: $INSTALL_DIR" >&2
  echo "Move it away or set INSTALL_DIR=/another/path and run again." >&2
  exit 2
fi

echo "[4/6] Installing Python environment..."
cd "$INSTALL_DIR"
python3 -m venv venv
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r requirements.txt
"$INSTALL_DIR/venv/bin/pip" install -e .

echo "[5/6] Installing jy command..."
tmp_wrapper="$(mktemp)"
cat > "$tmp_wrapper" <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/venv/bin/jy-cli" "\$@"
EOF
$SUDO install -m 755 "$tmp_wrapper" /usr/local/bin/jy
$SUDO ln -sf /usr/local/bin/jy /usr/local/bin/jy-cli
rm -f "$tmp_wrapper"

echo "[6/6] Installing systemd service..."
tmp_service="$(mktemp)"
cat > "$tmp_service" <<EOF
[Unit]
Description=Polymarket Realtime Copy Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/venv/bin/polymarket-copy-bot --config $INSTALL_DIR/.env
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
$SUDO install -m 644 "$tmp_service" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$tmp_service"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"

if [ -f "$INSTALL_DIR/.env" ]; then
  $SUDO chmod 600 "$INSTALL_DIR/.env"
  $SUDO systemctl restart "$SERVICE_NAME"
  echo "[OK] Service restarted: $SERVICE_NAME"
else
  echo "[OK] Service installed but not started because .env does not exist yet."
fi

echo ""
echo "Install complete."
echo "Next step: run"
echo ""
echo "  jy"
echo ""
echo "Then choose option 1 to set PRIVATE_KEY, Deposit Wallet, API URL, COPY_RATIO, and DRY_RUN."
