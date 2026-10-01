#!/bin/bash
#
# Déploiement de syno-ia sur le NAS Synology.
#
# À placer sur le NAS (p. ex. /volume1/web/hooks/deploy-syno-ia.sh) et à
# déclencher par la tâche/webhook appelée par le workflow GitHub Actions.
# Inspiré du script de déploiement du projet jobs-crawler.
#
set -euo pipefail

# ================= CONFIG =================
LOG_FILE="/volume1/web/hooks/deploy-github-syno-ia.log"

NAS_USER="vvadmin"
NAS_HOST="127.0.0.1"
SSH_KEY="/var/services/web/.ssh/id_rsa"

DOCKER="/usr/local/bin/docker"

IMAGE="ghcr.io/flake9025/syno-ia"
# « latest »      : image légère (LLM distant, Ollama, ou mode extractif)
# « latest-llm »  : image avec llama.cpp compilé sans AVX (LLM 100 % local)
TAG="${SYNO_IA_TAG:-latest}"

CONTAINER="syno-ia"
APP_PORT="8080"      # port interne du conteneur
HOST_PORT="8083"     # port exposé sur le NAS (adapter si déjà pris)

# Authentification GHCR. Inutile si le paquet est public (recommandé) ; sinon
# renseignez un jeton disposant de la portée « read:packages ».
GHCR_USER="${GHCR_USER:-}"
GHCR_TOKEN="${GHCR_TOKEN:-}"

# Répertoires persistants sur le NAS
APP_DIR="/volume1/docker/apps/$CONTAINER"
DATA_DIR="$APP_DIR/data"        # index SQLite + modèles téléchargés
ENV_FILE="$APP_DIR/.env"        # configuration et compte de service DSM

# Partages à indexer. Ils sont montés EN LECTURE SEULE et AU MÊME CHEMIN que sur
# le NAS : l'identité des chemins permet de rejouer les ACL DSM à l'identique.
# Séparez plusieurs partages par des espaces.
INDEX_SHARES="${SYNO_IA_SHARES:-/volume1/documents}"

# ================= LOG =================
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOG_FILE" || true
}

# Construit les options -v ... :ro pour chaque partage indexé.
MOUNT_ARGS=""
for share in $INDEX_SHARES; do
  MOUNT_ARGS="$MOUNT_ARGS -v $share:$share:ro"
done
INDEX_ROOTS="$(echo "$INDEX_SHARES" | tr ' ' ',')"

REMOTE_COMMANDS=$(
  cat <<EOF
set -e

echo "=========================================="
echo "Deploying $CONTAINER"
echo "Image   : $IMAGE:$TAG"
echo "Port    : $HOST_PORT -> $APP_PORT"
echo "Partages: $INDEX_SHARES"

mkdir -p "$DATA_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "ATTENTION: $ENV_FILE introuvable."
  echo "Créez-le à partir de .env.example, avec au minimum :"
  echo "  APP_SECRET=\$(openssl rand -hex 32)"
  echo "  DSM_SERVICE_ACCOUNT=..."
  echo "  DSM_SERVICE_PASSWORD=..."
fi

echo "Pull image"
if [ -n "$GHCR_TOKEN" ]; then
  echo "$GHCR_TOKEN" | $DOCKER login ghcr.io -u "$GHCR_USER" --password-stdin
fi

if ! $DOCKER pull $IMAGE:$TAG; then
  echo "ERREUR: impossible de récupérer $IMAGE:$TAG."
  echo "Si le paquet GHCR est privé, rendez-le public depuis"
  echo "  https://github.com/users/flake9025/packages/container/syno-ia/settings"
  echo "ou exportez GHCR_USER et GHCR_TOKEN (portée read:packages)."
  exit 1
fi

echo "Stop & remove ancien conteneur"
$DOCKER stop "$CONTAINER" || true
$DOCKER rm   "$CONTAINER" || true

ENV_ARG=""
if [ -f "$ENV_FILE" ]; then
  ENV_ARG="--env-file $ENV_FILE"
fi

$DOCKER run -d \
  --name "$CONTAINER" \
  -p "$HOST_PORT:$APP_PORT" \
  -e PORT=$APP_PORT \
  -e DATA_DIR=/app/data \
  -e INDEX_MODE=mount \
  -e INDEX_ROOTS="$INDEX_ROOTS" \
  \$ENV_ARG \
  -v "$DATA_DIR:/app/data" \
  $MOUNT_ARGS \
  --memory=5g \
  --cpu-shares=512 \
  --restart unless-stopped \
  $IMAGE:$TAG

echo "Deploy OK for $CONTAINER"

# Attente de la sonde de santé (le premier démarrage télécharge le modèle
# d'embeddings, ce qui peut prendre quelques minutes).
for i in \$(seq 1 60); do
  if $DOCKER exec "$CONTAINER" curl -fsS "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1; then
    echo "Healthcheck OK"
    break
  fi
  sleep 5
done

# Nettoyage des images obsolètes (espace disque du NAS).
$DOCKER image prune -f || true

echo "Single-instance deploy OK"
EOF
)

log "=========================================="
log "START DEPLOY"
log "Deploy via SSH ($NAS_USER@$NAS_HOST)"

ssh -i "$SSH_KEY" \
  -o StrictHostKeyChecking=no \
  "$NAS_USER@$NAS_HOST" \
  "$REMOTE_COMMANDS" >> "$LOG_FILE" 2>&1

log "END DEPLOY"
log "=========================================="
