from __future__ import annotations

import json
from pathlib import Path

import pytest

from cardsniper.cardsdb import import_cards
from cardsniper.config import Config, Secrets
from cardsniper.currency import Fx
from cardsniper.db import Database
from cardsniper.matching import CardIndex
from cardsniper.settings import UserSettings

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def scryfall_sample():
    with open(FIXTURES / "scryfall_sample.jsonl", encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path, stores=[], secrets=Secrets())


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    import_cards(database, scryfall_sample())
    return database


@pytest.fixture
def index(db) -> CardIndex:
    with db.session() as s:
        return CardIndex.from_session(s)


@pytest.fixture
def fx() -> Fx:
    return Fx(0.85)


@pytest.fixture
def settings() -> UserSettings:
    return UserSettings()


class FakeNotifier:
    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail
        self.channels = ["fake"]

    def send_deal(self, deal):
        if self.fail:
            return {"fake": "boom"}
        self.sent.append((deal.card_name, deal.price_gbp, deal.url))
        return {"fake": None}


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()
