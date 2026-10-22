"""Alertes : seulement pour une erreur répétée, sans spam ni boucle."""

import datetime as dt

from src.database.repositories import BotErrorRepository
from src.services.alerts import AlertManager, fingerprint
from src.services.monitoring import RecentErrors


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += dt.timedelta(**delta)


def manager(db, clock, **kwargs):
    return AlertManager(BotErrorRepository(db), clock=clock, **kwargs)


def test_fingerprint_ignores_numbers_but_not_source():
    assert fingerprint("a", "salon 123 introuvable") == fingerprint("a", "salon 456 introuvable")
    assert fingerprint("a", "x") != fingerprint("b", "x")


def test_alert_only_after_threshold_within_window(db):
    clock = Clock()
    alerts = manager(db, clock)
    assert alerts.observe("src.x", "boum 1") is None
    clock.advance(minutes=11)  # la première sort de la fenêtre de 10 minutes
    assert alerts.observe("src.x", "boum 2") is None
    assert alerts.observe("src.x", "boum 3") is None
    alert = alerts.observe("src.x", "boum 4")
    assert alert is not None and alert.occurrences == 3
    assert alert.error.count == 4  # toutes les occurrences sont enregistrées


def test_cooldown_per_error(db):
    clock = Clock()
    alerts = manager(db, clock, threshold=1)
    assert alerts.observe("src.x", "boum") is not None
    clock.advance(minutes=30)
    assert alerts.observe("src.x", "boum") is None
    clock.advance(minutes=31)
    assert alerts.observe("src.x", "boum") is not None


def test_global_limit_per_hour(db):
    clock = Clock()
    alerts = manager(db, clock, threshold=1, max_per_hour=2)
    results = [alerts.observe(f"src.s{i}", "boum") for i in range(4)]
    assert [r is not None for r in results] == [True, True, False, False]
    clock.advance(minutes=61)
    assert alerts.observe("src.s9", "boum") is not None


def test_alert_module_errors_never_alert(db):
    alerts = manager(db, Clock(), threshold=1)
    assert alerts.observe("src.discord_bot.alerts", "envoi impossible") is None
    assert BotErrorRepository(db).recent() == []


def test_recent_errors_listener_has_no_reentrance():
    errors = RecentErrors()
    seen = []

    def listener(item):
        seen.append(item.message)
        errors.record("src.listener", "erreur dans le suivi")  # ne doit pas boucler

    errors.add_listener(listener)
    errors.record("src.x", "boum\ntrace")
    assert seen == ["boum"]
    assert errors.total == 2


def test_failing_listener_is_ignored():
    errors = RecentErrors()
    errors.add_listener(lambda item: 1 / 0)
    errors.record("src.x", "boum")
    assert errors.last.message == "boum"
