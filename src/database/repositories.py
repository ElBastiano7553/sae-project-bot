"""Dépôts de données : seules ces classes écrivent du SQL métier."""

from __future__ import annotations

from dataclasses import dataclass

from src.database.connection import Database


@dataclass(frozen=True)
class ManagedMessage:
    key: str
    channel_id: int
    message_id: int
    content_hash: str
    updated_at: str = ""
    last_action: str | None = None
    last_error: str | None = None
    last_checked_at: str | None = None


_MESSAGE_COLUMNS = "key, channel_id, message_id, content_hash, updated_at, last_action, last_error, last_checked_at"


class ManagedMessageRepository:
    """Identifiants des messages permanents publiés par le bot."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, key: str) -> ManagedMessage | None:
        row = self._conn.execute(f"SELECT {_MESSAGE_COLUMNS} FROM managed_messages WHERE key = ?", (key,)).fetchone()
        return ManagedMessage(**dict(row)) if row else None

    def get_by_message_id(self, message_id: int) -> ManagedMessage | None:
        row = self._conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM managed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return ManagedMessage(**dict(row)) if row else None

    def list_all(self) -> list[ManagedMessage]:
        rows = self._conn.execute(f"SELECT {_MESSAGE_COLUMNS} FROM managed_messages ORDER BY key").fetchall()
        return [ManagedMessage(**dict(row)) for row in rows]

    def upsert(self, key: str, channel_id: int, message_id: int, content_hash: str, action: str | None = None) -> None:
        """Enregistre l'emplacement et le contenu publiés. N'est appelé qu'après un succès côté Discord."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO managed_messages
                    (key, channel_id, message_id, content_hash, updated_at, last_action, last_error, last_checked_at)
                VALUES (?, ?, ?, ?, datetime('now'), ?, NULL, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at,
                    last_action = excluded.last_action,
                    last_error = NULL,
                    last_checked_at = excluded.last_checked_at
                """,
                (key, channel_id, message_id, content_hash, action),
            )

    def record_check(self, key: str, action: str, error: str | None = None) -> None:
        """Mémorise le résultat d'une vérification sans toucher à l'emplacement enregistré.

        Une erreur Discord ne modifie donc jamais ``channel_id`` / ``message_id`` / ``content_hash`` :
        la base continue de désigner le dernier message publié avec succès.
        """
        with self._conn:
            self._conn.execute(
                """
                UPDATE managed_messages
                   SET last_action = ?, last_error = ?, last_checked_at = datetime('now')
                 WHERE key = ?
                """,
                (action, error, key),
            )

    def delete(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM managed_messages WHERE key = ?", (key,))


class ChannelBindingRepository:
    """Association « fonction du bot » → identifiant du salon Discord."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get_all(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT key, channel_id FROM channel_bindings").fetchall()
        return {row["key"]: row["channel_id"] for row in rows}

    def replace_all(self, bindings: dict[str, int]) -> None:
        """Remplace toutes les associations en une seule transaction."""
        with self._conn:
            self._conn.execute("DELETE FROM channel_bindings")
            self._conn.executemany(
                "INSERT INTO channel_bindings (key, channel_id) VALUES (?, ?)",
                sorted(bindings.items()),
            )


class KeyValueRepository:
    """Petit stockage clé/valeur pour l'état du bot (ex. : setup confirmé)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


@dataclass(frozen=True)
class EventSummary:
    """Résumé d'un événement GitLab enregistré dans l'historique (texte brut, déjà nettoyé)."""

    dedup_key: str | None = None
    action: str = ""
    ref: str = ""
    title: str = ""
    url: str | None = None
    author: str = ""
    occurred_at: str | None = None  # ISO 8601 ; converti en UTC « AAAA-MM-JJ HH:MM:SS » dans event_at
    # Lot 2 : données utilisées par /stats, /leaderboard et le tableau de bord.
    user_id: int | None = None  # auteur de l'action (pousseur, auteur de l'action sur la MR…)
    user_username: str = ""
    subject_id: int | None = None  # numéro de MR (iid), identifiant de pipeline ou de déploiement
    subject_author_id: int | None = None  # auteur de la MR
    commit_count: int | None = None  # pushs : nombre total de commits annoncé par GitLab
    environment: str = ""  # déploiements


EVENT_COLUMNS = (
    "event_type, status, action, ref, title, url, author, occurred_at, received_at, "
    "event_at, user_id, subject_id, subject_author_id, commit_count, environment, source"
)


@dataclass(frozen=True)
class EventRecord:
    event_type: str
    status: str
    action: str | None
    ref: str | None
    title: str | None
    url: str | None
    author: str | None
    occurred_at: str | None
    received_at: str
    event_at: str | None = None
    user_id: int | None = None
    subject_id: int | None = None
    subject_author_id: int | None = None
    commit_count: int | None = None
    environment: str | None = None
    source: str = "webhook"


class GitLabEventRepository:
    """Événements GitLab déjà acceptés, identifiés par la clé de livraison fournie par GitLab.

    Statuts : ``queued`` (en attente d'envoi), ``sent`` (publié sur Discord),
    ``failed`` (envoi impossible), ``skipped`` (aucun salon associé),
    ``recorded`` (conservé dans l'historique sans notification : branche non suivie…).
    """

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def claim(
        self,
        event_key: str,
        event_type: str,
        notification: str | None = None,
        summary: EventSummary | None = None,
        *,
        status: str = "queued",
    ) -> bool:
        """Enregistre l'événement. Retourne ``False`` s'il avait déjà été reçu (doublon).

        ``notification`` (JSON) permet de renvoyer la notification si le bot s'arrête avant l'envoi.
        ``summary`` alimente l'historique ; son ``dedup_key`` (contrainte d'unicité) écarte le même
        fait GitLab livré sous un autre identifiant. Les deux vérifications tiennent en une seule
        insertion atomique : deux requêtes simultanées ne peuvent pas passer toutes les deux.
        ``status`` vaut ``recorded`` pour un événement conservé dans l'historique sans être notifié.
        """
        summary = summary or EventSummary()
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO gitlab_events
                    (event_key, event_type, status, notification, dedup_key, action, ref, title, url, author,
                     occurred_at, event_at, user_id, subject_id, subject_author_id, commit_count, environment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(datetime(?), datetime('now')), ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    event_type,
                    status,
                    notification,
                    summary.dedup_key,
                    summary.action or None,
                    summary.ref or None,
                    summary.title or None,
                    summary.url,
                    summary.author or None,
                    summary.occurred_at,
                    summary.occurred_at,
                    summary.user_id,
                    summary.subject_id,
                    summary.subject_author_id,
                    summary.commit_count,
                    summary.environment or None,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and summary.user_id is not None and summary.author:
                # Nom affiché des membres GitLab, pour /leaderboard (le dernier nom reçu l'emporte).
                self._conn.execute(
                    """
                    INSERT INTO gitlab_users (id, name, username, updated_at) VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name, username = excluded.username, updated_at = excluded.updated_at
                    """,
                    (summary.user_id, summary.author, summary.user_username or None),
                )
        return inserted

    def recent(
        self,
        kinds: tuple[str, ...] = (),
        limit: int = 10,
        statuses: tuple[str, ...] = (),
        *,
        since: str | None = None,
        search: str | None = None,
    ) -> list[EventRecord]:
        """Derniers événements (les plus récents d'abord), filtrables par type, statut, période et texte.

        ``since`` : date UTC « AAAA-MM-JJ HH:MM:SS ». ``search`` : texte cherché dans le titre,
        la branche et l'auteur (sans distinction de casse pour les lettres sans accent).
        """
        clauses, params = [], []
        if kinds:
            clauses.append(f"event_type IN ({', '.join('?' * len(kinds))})")
            params.extend(kinds)
        if statuses:
            clauses.append(f"status IN ({', '.join('?' * len(statuses))})")
            params.extend(statuses)
        if since:
            clauses.append("event_at >= ?")
            params.append(since)
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR ref LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM gitlab_events {where} ORDER BY event_at DESC, rowid DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def latest_pipeline(self, ref: str) -> EventRecord | None:
        """Dernier pipeline enregistré sur une branche (tel que reçu)."""
        row = self._conn.execute(
            f"""
            SELECT {EVENT_COLUMNS} FROM gitlab_events
             WHERE event_type = 'pipeline' AND ref = ?
             ORDER BY event_at DESC, rowid DESC LIMIT 1
            """,
            (ref,),
        ).fetchone()
        return EventRecord(**dict(row)) if row else None

    def latest_per_subject(self, kind: str, limit: int = 10, *, key: str = "subject_id") -> list[EventRecord]:
        """Dernier événement de chaque MR, pipeline (``key="subject_id"``) ou branche (``key="ref"``)."""
        if key not in {"subject_id", "ref", "environment"}:
            raise ValueError(key)
        rows = self._conn.execute(
            f"""
            SELECT {EVENT_COLUMNS} FROM gitlab_events AS e
             WHERE event_type = ? AND {key} IS NOT NULL
               AND rowid = (SELECT rowid FROM gitlab_events AS last
                             WHERE last.event_type = e.event_type AND last.{key} = e.{key}
                             ORDER BY last.event_at DESC, last.rowid DESC LIMIT 1)
             ORDER BY event_at DESC, rowid DESC LIMIT ?
            """,
            (kind, int(limit)),
        ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def in_period(self, since: str | None, kinds: tuple[str, ...] = ()) -> list[EventRecord]:
        """Tous les événements d'une période (pour les statistiques ; volume d'une petite équipe)."""
        clauses, params = ["1 = 1"], []
        if since:
            clauses.append("event_at >= ?")
            params.append(since)
        if kinds:
            clauses.append(f"event_type IN ({', '.join('?' * len(kinds))})")
            params.extend(kinds)
        rows = self._conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM gitlab_events WHERE {' AND '.join(f'({c})' for c in clauses)} "
            "ORDER BY event_at, rowid",
            params,
        ).fetchall()
        return [EventRecord(**dict(row)) for row in rows]

    def coverage(self) -> tuple[str | None, int]:
        """Date du plus ancien événement conservé et nombre total d'événements."""
        row = self._conn.execute("SELECT MIN(event_at) AS first, COUNT(*) AS total FROM gitlab_events").fetchone()
        return row["first"], int(row["total"])

    def user_names(self) -> dict[int, str]:
        rows = self._conn.execute("SELECT id, name FROM gitlab_users").fetchall()
        return {row["id"]: row["name"] for row in rows}

    def pending(self, max_age_hours: int) -> list[tuple[str, str | None]]:
        """Événements acceptés mais jamais envoyés (bot arrêté entre-temps), du plus ancien au plus récent."""
        rows = self._conn.execute(
            """
            SELECT event_key, notification FROM gitlab_events
            WHERE status = 'queued' AND received_at >= datetime('now', ?)
            ORDER BY received_at, rowid
            """,
            (f"-{int(max_age_hours)} hours",),
        ).fetchall()
        return [(row["event_key"], row["notification"]) for row in rows]

    def release(self, event_key: str) -> None:
        """Oublie un événement qui n'a pas pu être pris en charge, pour que GitLab puisse le renvoyer."""
        with self._conn:
            self._conn.execute("DELETE FROM gitlab_events WHERE event_key = ?", (event_key,))

    def set_status(self, event_key: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE gitlab_events SET status = ?, updated_at = datetime('now') WHERE event_key = ?",
                (status, event_key),
            )

    def get_status(self, event_key: str) -> str | None:
        row = self._conn.execute("SELECT status FROM gitlab_events WHERE event_key = ?", (event_key,)).fetchone()
        return row["status"] if row else None

    def expire_queued(self, max_age_hours: int) -> int:
        """Abandonne les notifications trop anciennes pour être encore utiles (statut ``failed``)."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE gitlab_events SET status = 'failed', updated_at = datetime('now')
                WHERE status = 'queued' AND received_at < datetime('now', ?)
                """,
                (f"-{int(max_age_hours)} hours",),
            )
        return cursor.rowcount

    def last_received_at(self) -> str | None:
        row = self._conn.execute("SELECT MAX(received_at) AS last FROM gitlab_events").fetchone()
        return row["last"] if row else None

    def purge_older_than(self, days: int) -> int:
        """Supprime les événements anciens : GitLab ne renvoie jamais un événement après quelques jours."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM gitlab_events WHERE received_at < datetime('now', ?)", (f"-{int(days)} days",)
            )
        return cursor.rowcount


@dataclass(frozen=True)
class AdminAction:
    id: int
    at: str
    user_id: int
    user_name: str
    action: str
    target: str
    outcome: str


class AdminActionRepository:
    """Journal des actions d'administration (qui a fait quoi, quand, avec quel résultat)."""

    MAX_TEXT = 300

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def record(self, user_id: int, user_name: str, action: str, target: str = "", outcome: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO admin_actions (user_id, user_name, action, target, outcome) VALUES (?, ?, ?, ?, ?)",
                (int(user_id), user_name[:100], action[:50], target[: self.MAX_TEXT], outcome[: self.MAX_TEXT]),
            )

    def recent(self, limit: int = 15) -> list[AdminAction]:
        rows = self._conn.execute(
            "SELECT id, at, user_id, user_name, action, target, outcome FROM admin_actions ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [AdminAction(**dict(row)) for row in rows]

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM admin_actions WHERE at < datetime('now', ?)", (f"-{int(days)} days",))
        return cursor.rowcount


@dataclass(frozen=True)
class BotError:
    fingerprint: str
    source: str
    message: str
    first_at: str
    last_at: str
    count: int
    alerted_at: str | None


class BotErrorRepository:
    """Erreurs du bot regroupées par empreinte (même source, même message) : historique et alertes."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def record(self, fingerprint: str, source: str, message: str) -> BotError:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO bot_errors (fingerprint, source, message) VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    message = excluded.message, last_at = datetime('now'), count = count + 1
                """,
                (fingerprint, source[:100], message[:300]),
            )
        return self.get(fingerprint)  # type: ignore[return-value]

    def get(self, fingerprint: str) -> BotError | None:
        row = self._conn.execute("SELECT * FROM bot_errors WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return BotError(**dict(row)) if row else None

    def mark_alerted(self, fingerprint: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE bot_errors SET alerted_at = datetime('now') WHERE fingerprint = ?", (fingerprint,))

    def alerts_since(self, since: str) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM bot_errors WHERE alerted_at >= ?", (since,)).fetchone()
        return int(row["n"])

    def recent(self, since: str | None = None, limit: int = 10) -> list[BotError]:
        where, params = ("WHERE last_at >= ?", [since]) if since else ("", [])
        rows = self._conn.execute(
            f"SELECT * FROM bot_errors {where} ORDER BY last_at DESC LIMIT ?", (*params, int(limit))
        ).fetchall()
        return [BotError(**dict(row)) for row in rows]

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM bot_errors WHERE last_at < datetime('now', ?)", (f"-{int(days)} days",))
        return cursor.rowcount


@dataclass(frozen=True)
class AIUsage:
    requests: int
    input_tokens: int
    output_tokens: int


class AIRequestRepository:
    """Demandes faites à l'IA : qui, quelle commande, quel modèle, combien de tokens (jamais le texte)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def record(self, user_id: int, command: str, model: str | None, status: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO ai_requests (user_id, command, model, input_tokens, output_tokens, status) VALUES (?, ?, ?, ?, ?, ?)",
                (int(user_id), command[:30], (model or "")[:100] or None, int(input_tokens), int(output_tokens), status[:20]),
            )

    def usage_since(self, since: str, user_id: int | None = None) -> AIUsage:
        where, params = "at >= ?", [since]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(int(user_id))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o "
            f"FROM ai_requests WHERE {where}",
            params,
        ).fetchone()
        return AIUsage(int(row["n"]), int(row["i"]), int(row["o"]))

    def last_request_at(self, user_id: int) -> str | None:
        row = self._conn.execute("SELECT MAX(at) AS last FROM ai_requests WHERE user_id = ?", (int(user_id),)).fetchone()
        return row["last"] if row else None

    def purge_older_than(self, days: int) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM ai_requests WHERE at < datetime('now', ?)", (f"-{int(days)} days",))
        return cursor.rowcount
