# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Unit tests for the pure environment-rendering helpers."""

import pytest

import hindsight_env
from defaults import API_KEY_TENANT_EXTENSION


def _api_env(**overrides):
    kwargs = {
        "llm_provider": "none",
        "llm_model": "none",
        "llm_base_url": "",
        "log_level": "info",
        "worker_id": "hindsight-0",
        "extra_env": "",
        "secret_content": {},
        "database": None,
    }
    kwargs.update(overrides)
    return hindsight_env.api_environment(**kwargs)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("llm-api-key", "HINDSIGHT_API_LLM_API_KEY"),
        ("tenant-api-key", "HINDSIGHT_API_TENANT_API_KEY"),
        ("hindsight-cp-access-key", "HINDSIGHT_CP_ACCESS_KEY"),
        ("hindsight-api-embeddings-openai-api-key", "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY"),
        (" database-url ", "HINDSIGHT_API_DATABASE_URL"),
    ],
)
def test_normalise_secret_key(key, expected):
    assert hindsight_env.normalise_secret_key(key) == expected


def test_parse_extra_env_skips_comments_and_garbage():
    env = hindsight_env.parse_extra_env(
        "A=1\n# comment\n\n  B = two words \nno-equals-sign\n=nokey\nC=x=y"
    )
    assert env == {"A": "1", "B": "two words", "C": "x=y"}


def test_defaults_match_workshop_sdk():
    env = _api_env()
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "none"
    assert env["HINDSIGHT_API_WORKER_ID"] == "hindsight-0"
    assert env["HINDSIGHT_API_HOST"] == "0.0.0.0"
    assert env["HINDSIGHT_API_PORT"] == "8888"
    assert "HINDSIGHT_API_LLM_BASE_URL" not in env
    assert "HINDSIGHT_API_DATABASE_URL" not in env
    assert hindsight_env.uses_embedded_database(env)


def test_precedence_options_then_extra_env_then_secret():
    env = _api_env(
        llm_provider="openai",
        extra_env="HINDSIGHT_API_LLM_PROVIDER=groq\nHINDSIGHT_API_LLM_API_KEY=plain",
        secret_content={"llm-api-key": "sk-secret"},
    )
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "groq"
    assert env["HINDSIGHT_API_LLM_API_KEY"] == "sk-secret"


def test_charm_owned_settings_cannot_be_overridden():
    env = _api_env(extra_env="HINDSIGHT_API_PORT=1234\nHINDSIGHT_API_HOST=127.0.0.1")
    assert env["HINDSIGHT_API_PORT"] == "8888"
    assert env["HINDSIGHT_API_HOST"] == "0.0.0.0"


def test_database_url_from_relation_is_quoted_and_uses_primary():
    creds = hindsight_env.DatabaseCredentials(
        username="relation-7",
        password="p@ss/w:rd",
        endpoints="10.0.0.5:5432,10.0.0.6:5432",
        database="hindsight",
    )
    env = _api_env(database=creds)
    assert (
        env["HINDSIGHT_API_DATABASE_URL"]
        == "postgresql://relation-7:p%40ss%2Fw%3Ard@10.0.0.5:5432/hindsight"
    )
    assert not hindsight_env.uses_embedded_database(env)


def test_relation_wins_over_manual_database_url():
    creds = hindsight_env.DatabaseCredentials("u", "p", "db:5432", "hindsight")
    env = _api_env(secret_content={"database-url": "postgresql://x@y/z"}, database=creds)
    assert env["HINDSIGHT_API_DATABASE_URL"] == "postgresql://u:p@db:5432/hindsight"


def test_manual_database_url_from_secret_is_external():
    env = _api_env(secret_content={"database-url": "postgresql://x:y@rds.example:5432/mem"})
    assert env["HINDSIGHT_API_DATABASE_URL"] == "postgresql://x:y@rds.example:5432/mem"
    assert not hindsight_env.uses_embedded_database(env)


def test_explicit_pg0_url_counts_as_embedded():
    env = _api_env(extra_env="HINDSIGHT_API_DATABASE_URL=pg0://custom")
    assert hindsight_env.uses_embedded_database(env)


def test_tenant_api_key_enables_extension_and_feeds_control_plane():
    secrets = {"tenant-api-key": "hs-key"}
    api = _api_env(secret_content=secrets)
    assert api["HINDSIGHT_API_TENANT_API_KEY"] == "hs-key"
    assert api["HINDSIGHT_API_TENANT_EXTENSION"] == API_KEY_TENANT_EXTENSION

    cp = hindsight_env.control_plane_environment(api, secrets)
    assert cp["HINDSIGHT_CP_DATAPLANE_API_KEY"] == "hs-key"


def test_explicit_tenant_extension_is_respected():
    api = _api_env(
        extra_env="HINDSIGHT_API_TENANT_EXTENSION=my.module:Ext",
        secret_content={"tenant-api-key": "k"},
    )
    assert api["HINDSIGHT_API_TENANT_EXTENSION"] == "my.module:Ext"


def test_control_plane_keys_stay_out_of_the_api():
    secrets = {"hindsight-cp-access-key": "ui-pass", "llm-api-key": "sk"}
    api = _api_env(secret_content=secrets)
    assert not any(k.startswith("HINDSIGHT_CP_") for k in api)

    cp = hindsight_env.control_plane_environment(api, secrets)
    assert cp["HINDSIGHT_CP_ACCESS_KEY"] == "ui-pass"
    assert cp["HINDSIGHT_CP_DATAPLANE_API_URL"] == "http://localhost:8888"
    assert cp["PORT"] == "9999"
    assert "HINDSIGHT_CP_DATAPLANE_API_KEY" not in cp
    assert "HINDSIGHT_API_LLM_API_KEY" not in cp
