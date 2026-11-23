#!/usr/bin/env bash
# Installeert picnic-service op een Linux-server met systemd (bijvoorbeeld een Raspberry Pi):
# Python 3.13 via uv, venv, afhankelijkheden, systemd-units en de API.
# Idempotent: opnieuw draaien na een git pull is veilig. Vraagt sudo voor systemd.
set -euo pipefail

UV_VERSION=0.12.15
UV_HOME="$HOME/.local/uv-tool"
UV="$UV_HOME/bin/uv"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$REPO"

echo "== Python 3.13 via uv"
if [[ ! -x "$UV" ]]; then
  python3 -m venv "$UV_HOME"
  "$UV_HOME/bin/pip" install --quiet "uv==$UV_VERSION"
fi
"$UV" python install 3.13
PY="$("$UV" python find 3.13)"

echo "== venv en afhankelijkheden"
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

echo "== configuratie en data"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "   .env aangemaakt uit .env.example: vul minimaal PICNIC_USERNAME, PICNIC_PASSWORD en API_KEY in."
fi
chmod 600 .env
mkdir -p data
chmod 700 data

echo "== systemd (als $RUN_USER, vanuit $REPO)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for unit in picnic-sync.service picnic-sync.timer picnic-api.service; do
  sed -e "s#@USER@#$RUN_USER#g" -e "s#@DIR@#$REPO#g" "systemd/$unit" > "$TMP/$unit"
done
sudo install -m 644 "$TMP/picnic-sync.service" "$TMP/picnic-sync.timer" "$TMP/picnic-api.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now picnic-sync.timer

echo "== API"
API_HOST="$(grep -E '^API_HOST=' .env | cut -d= -f2 || true)"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="$(grep -E '^API_PORT=' .env | cut -d= -f2 || true)"
API_PORT="${API_PORT:-8085}"
if ss -ltn "( sport = :$API_PORT )" | grep -q LISTEN && ! systemctl is-active --quiet picnic-api; then
  echo "   Poort $API_PORT is al in gebruik door iets anders (ss -ltnp). Kies een andere API_PORT in .env." >&2
elif grep -qE '^API_KEY=.{16,}' .env; then
  sudo systemctl enable picnic-api.service
  sudo systemctl restart picnic-api.service
  echo "   picnic-api draait op $API_HOST:$API_PORT."
else
  echo "   API_KEY ontbreekt in .env (minimaal 16 tekens); picnic-api niet gestart. Vul hem in en draai dit script opnieuw."
fi

cat <<MSG

Klaar. Vervolgstappen:
  . .venv/bin/activate
  picnic login          # eerste keer, en later als het token verlopen is (2FA-code via SMS of e-mail)
  picnic sync           # bestelhistorie ophalen; de eerste keer duurt dit even
  picnic choose melk    # laat zien wat /add zou kiezen, zonder iets toe te voegen
  curl -s http://$API_HOST:$API_PORT/health
MSG
