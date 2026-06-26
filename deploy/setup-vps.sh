#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
#  setup-vps.sh
#  Bot Crypto — Deploy completo em VPS Ubuntu 22.04+
#  Uso: sudo bash deploy/setup-vps.sh
# ═══════════════════════════════════════════════════════════════════

APP_NAME="bot-crypto"
APP_DIR="/opt/${APP_NAME}"
REPO_URL=""                     # ← preencher com seu repositório git
BRANCH="main"
UBUNTU_USER="bot"               # usuário não-root para rodar o bot

# ── Cores ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# ── 1. Verificar root ──────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Este script precisa ser executado como root (sudo)."
fi

# ── 2. Sistema ─────────────────────────────────────────────────────
log "Atualizando pacotes do sistema..."
apt-get update -qq && apt-get upgrade -y -qq

log "Instalando dependências..."
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    git curl build-essential supervisor

# ── 3. Criar usuário não-root ──────────────────────────────────────
if ! id -u "${UBUNTU_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${UBUNTU_USER}"
    log "Usuário '${UBUNTU_USER}' criado."
fi

# ── 4. Clonar repositório ──────────────────────────────────────────
if [[ -z "${REPO_URL}" ]]; then
    warn "REPO_URL vazio. Pule esta etapa e clone manualmente em ${APP_DIR}."
    warn "Exemplo: git clone <seu-repo> ${APP_DIR}"
else
    if [[ -d "${APP_DIR}/.git" ]]; then
        log "Atualizando repositório..."
        cd "${APP_DIR}"
        git fetch origin
        git reset --hard "origin/${BRANCH}"
    else
        log "Clonando repositório..."
        git clone -b "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
    fi
    chown -R "${UBUNTU_USER}:${UBUNTU_USER}" "${APP_DIR}"
fi

# ── 5. Virtualenv + dependências ───────────────────────────────────
log "Criando ambiente virtual..."
sudo -u "${UBUNTU_USER}" python3.11 -m venv "${APP_DIR}/venv"

log "Instalando dependências Python..."
sudo -u "${UBUNTU_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel
sudo -u "${UBUNTU_USER}" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

# ── 6. Arquivo .env ────────────────────────────────────────────────
if [[ ! -f "${APP_DIR}/.env" ]]; then
    warn "Criando .env modelo. Edite com suas chaves ANTES de iniciar."
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chown "${UBUNTU_USER}:${UBUNTU_USER}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
fi

# ── 7. Service systemd ─────────────────────────────────────────────
log "Instalando serviço systemd..."
cp "${APP_DIR}/deploy/bot-crypto.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable bot-crypto.service

log "Configurando supervisor para o dashboard (fallback opcional)..."
if [[ -f "${APP_DIR}/deploy/bot-crypto-dashboard.conf" ]]; then
    cp "${APP_DIR}/deploy/bot-crypto-dashboard.conf" /etc/supervisor/conf.d/
    supervisorctl reread
    supervisorctl update
fi

# ── 8. Permissões ──────────────────────────────────────────────────
chown -R "${UBUNTU_USER}:${UBUNTU_USER}" "${APP_DIR}"
chmod 750 "${APP_DIR}"

# ── 9. Logrotate ───────────────────────────────────────────────────
cat > /etc/logrotate.d/bot-crypto <<EOF
/var/log/bot-crypto*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
EOF

# ── Final ──────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Instalação concluída!${NC}"
echo ""
echo "  Próximos passos:"
echo "    1. Edite ${APP_DIR}/.env com suas chaves:"
echo "       sudo -u ${UBUNTU_USER} nano ${APP_DIR}/.env"
echo ""
echo "    2. Inicie o bot:"
echo "       sudo systemctl start bot-crypto.service"
echo "       sudo systemctl status bot-crypto.service"
echo ""
echo "    3. Dashboard manual (ou via supervisor):"
echo "       sudo -u ${UBUNTU_USER} ${APP_DIR}/venv/bin/streamlit run \\"
echo "           ${APP_DIR}/dashboard/app.py --server.port 8501"
echo ""
echo "    4. Logs:"
echo "       journalctl -u bot-crypto.service -f"
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
