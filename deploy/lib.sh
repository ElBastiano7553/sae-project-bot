# Fonctions communes aux scripts de deploy/ (chargé avec « source », ne pas lancer seul).
# shellcheck shell=bash

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python"
SERVICE=samae

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m /!\\\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERREUR\033[0m %s\n' "$*" >&2; exit 1; }

require_not_root() {
    if [ "$(id -u)" -eq 0 ]; then
        die "lancez ce script avec l'utilisateur du bot (pas root) ; il demandera sudo quand il le faut."
    fi
}

require_venv() {
    [ -x "$PYTHON" ] || die "environnement virtuel introuvable ($PYTHON). Créez-le : python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 3.11 ou plus récent est nécessaire dans .venv"
}

# Lit UNE variable de .env sans jamais afficher les autres (aucun secret n'est lu ici).
env_value() {
    local key="$1" line
    line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$APP_DIR/.env" 2>/dev/null | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
    printf '%s' "$line"
}

# Chemin absolu (les chemins relatifs de .env partent du dossier du projet, comme dans src/config.py).
absolute() {
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '%s' "$APP_DIR/$1" ;;
    esac
}

database_path() {
    local raw
    raw="$(env_value DATABASE_PATH)"
    absolute "${raw:-data/sae_bot.sqlite3}"
}

# Le service tourne-t-il ?
service_active() { systemctl is-active --quiet "$SERVICE"; }

# Attend que le bot soit connecté à Discord (fichier de vie récent et « ready »), 150 s au plus.
wait_healthy() {
    local i
    for i in $(seq 1 30); do
        sleep 5
        if ! service_active; then
            warn "le service s'est arrêté"
            return 1
        fi
        if (cd "$APP_DIR" && "$PYTHON" -m src.healthcheck >/dev/null 2>&1); then
            ok "bot connecté à Discord ($((i * 5)) s) : $(cd "$APP_DIR" && "$PYTHON" -m src.healthcheck)"
            return 0
        fi
    done
    warn "le bot ne s'est pas connecté en 150 s : $(cd "$APP_DIR" && "$PYTHON" -m src.healthcheck || true)"
    return 1
}

# Refuse de continuer si des fichiers suivis par Git ont été modifiés sur le serveur (rien n'est écrasé).
require_clean_tree() {
    local changes
    changes="$(git -C "$APP_DIR" status --porcelain --untracked-files=no)"
    if [ -n "$changes" ]; then
        printf '%s\n' "$changes"
        die "des fichiers du dépôt ont été modifiés sur le serveur (liste ci-dessus). Rien n'a été fait.
Pour voir les différences : git diff. Pour les mettre de côté sans les perdre : git stash push -m \"modifs serveur\"."
    fi
}

ask_yes() {
    local answer
    read -r -p "$1 [o/N] " answer
    case "$answer" in o|O|oui|OUI|y|Y) return 0 ;; *) return 1 ;; esac
}
