#!/bin/bash
#
# Mise à jour de syno-ia sur un NAS Synology.
#
# Prévu pour le Planificateur de tâches de DSM — tâche « Script défini par
# l'utilisateur », exécutée en **root** :
#
#     bash /volume1/docker/apps/syno-ia/scripts/update.sh
#
# Le Planificateur n'ouvre pas un shell de connexion : ni PATH complet, ni
# répertoire courant, ni alias. Tout est donc résolu en chemin absolu ici.
#
# Le script est idempotent : sans rien de neuf à tirer, il ne redémarre rien.
# Et il **vérifie** que la nouvelle image tourne réellement, au lieu de
# supposer qu'un « docker pull » a suffi.

set -euo pipefail

PROJET="${SYNO_IA_DIR:-/volume1/docker/apps/syno-ia}"
JOURNAL="${SYNO_IA_LOG:-$PROJET/update.log}"
SONDE="${SYNO_IA_URL:-http://127.0.0.1:8083}/api/health"
ATTENTE_MAX="${SYNO_IA_WAIT:-180}"

# ----------------------------------------------------------------- journal
mkdir -p "$(dirname "$JOURNAL")"
# Rotation simple : au-delà de 1 Mo, on repart d'un fichier neuf.
if [ -f "$JOURNAL" ] && [ "$(wc -c <"$JOURNAL")" -gt 1048576 ]; then
    mv -f "$JOURNAL" "$JOURNAL.1"
fi
exec >>"$JOURNAL" 2>&1

dire() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ------------------------------------------------------------ notification
# Remontée dans le centre de notifications de DSM. Cette commande n'est pas
# documentée publiquement et sa signature a varié selon les versions : on
# essaie les formes connues et on n'échoue jamais à cause d'elle. Le vrai
# filet de sécurité reste le code de retour du script, que le Planificateur
# sait envoyer par courriel.
avertir() {
    local titre="$1" message="$2" bin
    for bin in /usr/syno/bin/synodsmnotify /usr/bin/synodsmnotify; do
        [ -x "$bin" ] || continue
        "$bin" @administrators "$titre" "$message" >/dev/null 2>&1 && return 0
        "$bin" -c SYNO.SDS._ReservedApp.Notification @administrators \
            "$titre" "$message" >/dev/null 2>&1 && return 0
    done
    dire "Notification DSM indisponible (synodsmnotify absent ou refusé)."
}

echouer() {
    dire "ÉCHEC : $1"
    avertir "syno-ia : mise à jour en échec" "$1 — voir $JOURNAL"
    exit 1
}

# ---------------------------------------------------------------- binaires
# Container Manager installe Docker hors du PATH d'une tâche planifiée.
trouver_docker() {
    local chemin
    if [ -n "${SYNO_IA_DOCKER:-}" ]; then
        [ -x "$SYNO_IA_DOCKER" ] || return 1
        echo "$SYNO_IA_DOCKER"
        return 0
    fi
    for chemin in /usr/local/bin/docker /usr/bin/docker \
        /var/packages/ContainerManager/target/usr/bin/docker \
        /var/packages/Docker/target/usr/bin/docker; do
        [ -x "$chemin" ] && { echo "$chemin"; return 0; }
    done
    command -v docker 2>/dev/null && return 0
    return 1
}

DOCKER="$(trouver_docker)" || echouer "binaire « docker » introuvable."

# Compose v2 est une sous-commande ; v1 est un binaire séparé.
if "$DOCKER" compose version >/dev/null 2>&1; then
    COMPOSE=("$DOCKER" compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=("$(command -v docker-compose)")
else
    echouer "« docker compose » indisponible."
fi

# Nom de projet figé. Sans lui, Compose le déduit du nom du dossier : renommer
# ce dernier ferait repartir une **seconde** pile à côté de la première, les
# deux se disputant le port 8083 et le volume de données.
COMPOSE+=(-p "${SYNO_IA_PROJET:-syno-ia}")

[ -f "$PROJET/docker-compose.yml" ] || echouer "pas de docker-compose.yml dans $PROJET."
cd "$PROJET"

# --------------------------------------------------------------- empreinte
# L'empreinte du commit déployé est exposée par l'application elle-même :
# c'est la seule preuve fiable que la mise à jour a pris.
empreinte() {
    curl -fsS --max-time 10 "$SONDE" 2>/dev/null |
        sed -n 's/.*"build"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

AVANT="$(empreinte || true)"
dire "Mise à jour depuis $PROJET (build actuel : ${AVANT:-inconnu})"

# --------------------------------------------------------------- exécution
dire "Récupération de l'image…"
"${COMPOSE[@]}" pull || echouer "« docker compose pull » a échoué."

dire "Application de la nouvelle image…"
"${COMPOSE[@]}" up -d --remove-orphans || echouer "« docker compose up -d » a échoué."

# ------------------------------------------------------------ vérification
dire "Attente du service (au plus ${ATTENTE_MAX}s)…"
APRES=""
for _ in $(seq 1 "$ATTENTE_MAX"); do
    APRES="$(empreinte || true)"
    [ -n "$APRES" ] && break
    sleep 1
done

[ -n "$APRES" ] || echouer "le service ne répond pas sur $SONDE après ${ATTENTE_MAX}s."

if [ "$AVANT" = "$APRES" ]; then
    dire "Déjà à jour (build $APRES)."
else
    dire "Mise à jour réussie : ${AVANT:-inconnu} → $APRES"
fi

# Les couches de l'ancienne image ne servent plus et pèsent lourd sur un NAS.
# On ne retire que les images **sans étiquette**, jamais celles qui servent
# encore à d'autres conteneurs.
dire "Nettoyage des couches orphelines…"
"$DOCKER" image prune -f >/dev/null 2>&1 || dire "Nettoyage ignoré."

dire "Terminé."
