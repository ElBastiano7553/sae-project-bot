#!/usr/bin/env bash
# Revient à la version de Samae installée avant la dernière mise à jour (ou à un commit donné).
#
#   cd ~/sae-project-bot && bash deploy/rollback.sh [COMMIT]
#
# Le code est remis à ce commit (git checkout, rien n'est supprimé). Si la base a été migrée
# vers un schéma trop récent pour l'ancienne version, le script propose de restaurer la
# sauvegarde faite juste avant la mise à jour ; la base actuelle est alors déplacée, pas effacée.
# Guide : docs/deployment-vps.md
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_not_root
require_venv
cd "$APP_DIR"
DB_PATH="$(database_path)"
DB_DIR="$(dirname "$DB_PATH")"
TARGET="${1:-$(cat "$DB_DIR/version-precedente" 2>/dev/null || true)}"
[ -n "$TARGET" ] || die "aucune version précédente enregistrée : indiquez un commit (git log --oneline)."
git cat-file -e "$TARGET^{commit}" 2>/dev/null || die "commit inconnu : $TARGET"
require_clean_tree

echo "Version actuelle : $(git log -1 --format='%h %s (%cs)' HEAD)"
echo "Retour vers      : $(git log -1 --format='%h %s (%cs)' "$TARGET")"
ask_yes "Revenir à cette version ?" || { echo "Rien n'a été modifié."; exit 0; }

info "Arrêt du bot"
if service_active; then sudo systemctl stop "$SERVICE"; fi

info "Code de la version $(git log -1 --format=%h "$TARGET")"
git checkout --quiet --detach "$TARGET"
"$PYTHON" -m pip install --quiet --disable-pip-version-check -r requirements.txt

info "Compatibilité de la base"
SUPPORTED="$("$PYTHON" -c 'from src.database.connection import MIGRATIONS; print(len(MIGRATIONS))')"
CURRENT_SCHEMA="$("$PYTHON" -c 'import sqlite3, sys; c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); print(c.execute("PRAGMA user_version").fetchone()[0])' "$DB_PATH")"
echo "schéma de la base : v$CURRENT_SCHEMA ; cette version du bot gère jusqu'à v$SUPPORTED"
if [ "$CURRENT_SCHEMA" -gt "$SUPPORTED" ]; then
    BACKUP_FILE="$(tail -n 1 "$DB_DIR/deploiements.log" 2>/dev/null | sed -n 's/^.*sauvegarde=\(.*\)$/\1/p')"
    warn "la base a été migrée par la nouvelle version : l'ancienne ne peut pas l'ouvrir."
    [ -n "$BACKUP_FILE" ] || die "aucune sauvegarde de déploiement connue : voir docs/deployment-vps.md (Restaurer)."
    echo "Sauvegarde faite avant la mise à jour : $BACKUP_FILE"
    echo "Les changements faits depuis (réunions, événements…) ne seront plus dans la base restaurée."
    ask_yes "Restaurer cette sauvegarde (la base actuelle est conservée à côté) ?" \
        || die "rien n'a été restauré. Le bot reste arrêté : relancez ce script ou revenez à la nouvelle version (bash deploy/update.sh)."
    "$PYTHON" -m src.ops restore "$BACKUP_FILE" --confirm
fi

info "Redémarrage"
sudo systemctl start "$SERVICE"
if wait_healthy; then
    ok "retour arrière terminé : $(git log -1 --format='%h %s' HEAD) (HEAD détaché, c'est normal)."
    echo "La prochaine mise à jour (bash deploy/update.sh) revient automatiquement sur main."
    exit 0
fi
sudo journalctl -u "$SERVICE" -n 40 --no-pager || true
die "le bot ne démarre pas non plus avec cette version : voir docs/deployment-vps.md (Dépannage)."
