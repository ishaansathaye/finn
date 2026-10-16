import pytest

from app.adapters.options_research_config import OptionsConfigError, load_account_number


@pytest.mark.parametrize("env", [{}, {"AGENTIC_ACCOUNT_NUMBER": ""}, {"AGENTIC_ACCOUNT_NUMBER": " "}])
def test_account_requires_explicit_configuration(env):
    with pytest.raises(OptionsConfigError, match="explicitly configured"):
        load_account_number(env)


@pytest.mark.parametrize("account", ["9107", "main", "123456/", "123456\n654321", "１２３４５６"])
def test_invalid_or_masked_accounts_are_rejected_without_echo(account):
    with pytest.raises(OptionsConfigError) as error:
        load_account_number({"AGENTIC_ACCOUNT_NUMBER": account})
    assert account not in str(error.value)


def test_explicit_mapping_does_not_fall_back_to_process_environment(monkeypatch):
    monkeypatch.setenv("AGENTIC_ACCOUNT_NUMBER", "123456789")
    with pytest.raises(OptionsConfigError):
        load_account_number({})
    assert load_account_number() == "123456789"


def test_full_explicit_account_is_preserved():
    assert load_account_number({"AGENTIC_ACCOUNT_NUMBER": " 001234567 "}) == "001234567"


@pytest.mark.parametrize("key", ["AGENTIC_MAIN_ACCOUNT_DENYLIST", "AGENTIC_SAVINGS_ACCOUNT_DENYLIST"])
def test_existing_trader_denylists_are_respected(key):
    with pytest.raises(OptionsConfigError, match="denylist") as error:
        load_account_number({"AGENTIC_ACCOUNT_NUMBER": "123456789", key: "999999999, 123456789"})
    assert "123456789" not in str(error.value)
