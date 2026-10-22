#!/usr/bin/env bash
# Met Samae à jour avec la dernière version validée de main, avec sauvegarde et retour arrière.
#
#   cd ~/sae-project-bot && bash deploy/update.sh
#
# Étapes : vérifications, sauvegarde vérifiée de la base, arrêt, mise à jour du code
# (avance rapide uniquement), dépendances, redémarrage, contrôle de connexion.
# Rien n'est supprimé : pas de reset, pas de clean ; .env, data/ et logs/ ne sont pas
# suivis par Git et ne sont jamais touchés. Guide : docs/deployment-vps.md
set -euo pipefail
source "$(dirname "$0")/lib.sh"

BRANCH="${1:-main}"
require_not_root
require_venv
cd "$APP_DIR"

info "Vérification du dépôt"
require_clean_tree
CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD || echo DETACHED)"
if [ "$CURRENT_BRANCH" != "$BRANCH" ] && [ "$CURRENT_BRANCH" != DETACHED ]; then
    die "le dépôt est sur la branche « $CURRENT_BRANCH » au lieu de « $BRANCH ». Revenez-y : git checkout $BRANCH"
fi
git fetch --quiet origin "$BRANCH"
PREVIOUS="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "origin/$BRANCH")"
if [ "$PREVIOUS" = "$TARGET" ]; then
    ok "déjà à jour ($(git log -1 --format='%h %s' HEAD))"
    exit 0
fi
git merge-base --is-ancestor "$PREVIOUS" "$TARGET" \
    || die "la version installée ($PREVIOUS) n'est pas un ancêtre de origin/$BRANCH : mise à jour manuelle nécessaire."
echo "Version installée : $(git log -1 --format='%h %s (%cs)' "$PREVIOUS")"
echo "Nouvelle version  : $(git log -1 --format='%h %s (%cs)' "$TARGET")"
git log --oneline "$PREVIOUS..$TARGET"
ask_yes "Mettre à jour vers cette version ?" || { echo "Rien n'a été modifié."; exit 0; }

info "Sauvegarde vérifiée de la base (bot encore en marche, copie cohérente)"
BACKUP_LINE="$("$PYTHON" -m src.ops backup --raison predeploiement)" || die "sauvegarde impossible : mise à jour annulée, rien n'a été modifié."
echo "$BACKUP_LINE"
BACKUP_FILE="$(printf '%s' "$BACKUP_LINE" | sed -n 's/^.*Sauvegarde vérifiée : \(.*\.sqlite3\) (.*$/\1/p')"
"$PYTHON" -m src.ops restore-test "$BACKUP_FILE" || die "la sauvegarde ne passe pas le test de restauration : mise à jour annulée."
DB_DIR="$(dirname "$(database_path)")"
printf '%s\n' "$PREVIOUS" > "$DB_DIR/version-precedente"
printf '%s %s -> %s sauvegarde=%s\n' "$(date -u +%FT%TZ)" "$PREVIOUS" "$TARGET" "$BACKUP_FILE" >> "$DB_DIR/deploiements.log"

info "Arrêt du bot"
if service_active; then sudo systemctl stop "$SERVICE"; fi
ok "arrêté"

info "Mise à jour du code"
if [ "$CURRENT_BRANCH" = DETACHED ]; then git checkout --quiet "$BRANCH"; fi
git merge --ff-only --quiet "origin/$BRANCH"
ok "$(git log -1 --format='%h %s' HEAD)"

info "Dépendances"
"$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt
"$PYTHON" -c "import src.main, src.discord_bot.bot" || warn "le nouveau code ne se charge pas"

info "Redémarrage"
sudo systemctl start "$SERVICE"
if wait_healthy; then
    ok "mise à jour terminée. Vérifiez sur Discord : /status puis /help."
    echo "En cas de problème plus tard : bash deploy/rollback.sh"
    exit 0
fi
sudo journalctl -u "$SERVICE" -n 40 --no-pager || true
warn "la nouvelle version ne démarre pas correctement (journaux ci-dessus)."
if ask_yes "Revenir tout de suite à la version précédente ($(git log -1 --format=%h "$PREVIOUS")) ?"; then
    exec bash "$APP_DIR/deploy/rollback.sh" "$PREVIOUS"
fi
die "version précédente non restaurée. Pour y revenir plus tard : bash deploy/rollback.sh"
