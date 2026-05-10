from hivemind.config import Settings
from hivemind.sandbox.settings import build_sandbox_settings


def test_default_query_budget_is_separate_from_operator_token_ceiling(monkeypatch):
    for name in (
        "HIVEMIND_DEFAULT_QUERY_MAX_TOKENS",
        "HIVEMIND_MAX_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.default_query_max_tokens == 1_000_000
    assert settings.max_tokens == 100_000_000
    assert settings.default_query_max_tokens < settings.max_tokens
    assert build_sandbox_settings(settings).global_max_tokens == settings.max_tokens
