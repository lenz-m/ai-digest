from __future__ import annotations

import json

import pytest

from pipeline import config as config_module
from pipeline.trust import (
    INDEPENDENT_ANALYSIS,
    INDEPENDENT_NEWS,
    VENDOR,
    DEFAULT_TIER,
    TrustStore,
)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    cfg = config_module.Config(trust_cache=tmp_path / "cache" / "trust_tiers.json")
    monkeypatch.setattr(config_module, "CONFIG", cfg)
    monkeypatch.setattr("pipeline.trust.CONFIG", cfg)
    return cfg


def test_seeded_sources_get_expected_tiers(isolated_config):
    store = TrustStore()
    assert store.get_tier("Stratechery") == INDEPENDENT_ANALYSIS
    assert store.get_tier("The Bay Area Times") == INDEPENDENT_NEWS
    assert store.get_tier("GCP") == VENDOR
    assert store.get_tier("OpenAI") == VENDOR


def test_unknown_source_gets_default_tier(isolated_config):
    store = TrustStore()
    assert store.get_tier("Some Source I Never Classified") == DEFAULT_TIER


def test_describe_returns_human_phrase_per_tier(isolated_config):
    store = TrustStore()
    assert "vendor-published" in store.describe("GCP")
    assert "independent analysis" in store.describe("Stratechery")


def test_materialize_seed_writes_editable_file(isolated_config):
    store = TrustStore()
    store.materialize_seed()
    assert isolated_config.trust_cache.exists()
    data = json.loads(isolated_config.trust_cache.read_text())
    assert data["Stratechery"] == INDEPENDENT_ANALYSIS
    assert data["GCP"] == VENDOR


def test_hand_edited_override_wins_over_seed(isolated_config):
    # simulate the user correcting a classification in the cache file
    isolated_config.trust_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.trust_cache.write_text(json.dumps({"GCP": INDEPENDENT_ANALYSIS}))

    store = TrustStore()
    assert store.get_tier("GCP") == INDEPENDENT_ANALYSIS  # override, not the VENDOR seed


def test_materialize_seed_preserves_existing_overrides(isolated_config):
    isolated_config.trust_cache.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.trust_cache.write_text(json.dumps({"GCP": INDEPENDENT_ANALYSIS}))

    store = TrustStore()
    store.materialize_seed()
    data = json.loads(isolated_config.trust_cache.read_text())
    assert data["GCP"] == INDEPENDENT_ANALYSIS  # user edit survived
    assert data["Stratechery"] == INDEPENDENT_ANALYSIS  # seed filled in the rest
