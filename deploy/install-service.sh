#!/usr/bin/env bash
# Installe (ou met à jour) le service systemd de Samae sur le serveur.
#
#   cd ~/sae-project-bot && bash deploy/install-service.sh
#
# À lancer avec l'utilisateur qui possède le dossier du projet (pas root) ; sudo est demandé
# pour écrire dans /etc/systemd. Le script peut être relancé sans risque (après avoir changé
# DATABASE_PATH ou BACKUP_DIR dans .env, par exemple). Il ne lit dans .env que ces deux
# chemins : aucun secret n'est lu ni affiché. Guide : docs/deployment-vps.md
set -euo pipefail
source "$(dirname "$0")/lib.sh"

require_not_root
require_venv
cd "$APP_DIR"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"

info "Vérification de .env"
[ -f .env ] || die ".env introuvable dans $APP_DIR (copiez .env.example puis remplissez-le)."
chmod 600 .env
ok ".env présent, lisible par $USER_NAME seulement (chmod 600)"

info "Dossiers où le bot a le droit d'écrire"
DB_PATH="$(database_path)"
DB_DIR="$(dirname "$DB_PATH")"
BACKUP_DIR="$(env_value BACKUP_DIR)"
BACKUP_DIR="$(absolute "${BACKUP_DIR:-$DB_DIR/backups}")"
RW_PATHS=("$DB_DIR" "$APP_DIR/logs")
case "$BACKUP_DIR/" in
    "$DB_DIR"/*) ;;                       # déjà couvert par le dossier de la base
    *) RW_PATHS+=("$BACKUP_DIR") ;;
esac
for dir in "${RW_PATHS[@]}" "$BACKUP_DIR"; do
    mkdir -p "$dir"
    [ -w "$dir" ] || die "$dir n'est pas accessible en écriture pour $USER_NAME"
    ok "$dir"
done
chmod 700 "$DB_DIR"

info "Recherche d'un bot lancé à la main"
if ! service_active && pgrep -u "$USER_NAME" -f 'python.* -m src\.main' >/dev/null; then
    pgrep -a -u "$USER_NAME" -f 'python.* -m src\.main' || true
    die "un bot tourne déjà en dehors du service (ci-dessus). Arrêtez-le (Ctrl+C dans son terminal, ou kill <numéro>), puis relancez ce script."
fi
ok "aucun bot lancé en dehors du service"

info "Installation des fichiers systemd (sudo)"
sudo -v || die "sudo est nécessaire pour installer le service (voir docs/deployment-vps.md, « Sans sudo »)."
render() {
    sed -e "s|@USER@|$USER_NAME|g" -e "s|@GROUP@|$GROUP_NAME|g" -e "s|@APP_DIR@|$APP_DIR|g" \
        -e "s|@RW_PATHS@|${RW_PATHS[*]}|g" "$1"
}
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
render deploy/samae.service.in > "$TMP/samae.service"
render deploy/samae-check.service.in > "$TMP/samae-check.service"
sudo install -m 644 "$TMP/samae.service" /etc/systemd/system/samae.service
sudo install -m 644 "$TMP/samae-check.service" /etc/systemd/system/samae-check.service
sudo install -m 644 deploy/samae-check.timer /etc/systemd/system/samae-check.timer
sudo install -d -m 755 /etc/systemd/journald.conf.d
sudo install -m 644 deploy/journald-samae.conf /etc/systemd/journald.conf.d/samae.conf
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/samae.service /etc/systemd/system/samae-check.service 2>&1 \
    | grep -v -e '^$' || true
ok "service installé : /etc/systemd/system/samae.service"

info "Démarrage automatique au boot et lancement"
sudo systemctl enable samae.service samae-check.timer >/dev/null
sudo systemctl restart samae.service
sudo systemctl start samae-check.timer
if wait_healthy; then
    ok "Samae tourne en service. Il redémarrera seul après un crash ou un redémarrage du serveur."
else
    sudo journalctl -u samae -n 40 --no-pager || true
    die "le service ne démarre pas correctement : voir les journaux ci-dessus et docs/deployment-vps.md (Dépannage)."
fi
cat <<MSG

Commandes utiles :
  systemctl status samae          état du service
  sudo journalctl -u samae -f     journaux en direct (Ctrl+C pour quitter)
  sudo systemctl restart samae    redémarrer
  .venv/bin/python -m src.ops check   contrôle complet (bot, mémoire, disque, sauvegardes)
MSG
