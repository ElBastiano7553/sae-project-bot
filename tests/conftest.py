"""Fixtures communes. Aucun test n'utilise de vrai token ni le réseau."""

from __future__ import annotations

import pytest

from src.database import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()
