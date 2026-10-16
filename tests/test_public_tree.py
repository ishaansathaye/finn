import pytest

from scripts.check_public_tree import allowed_path


@pytest.mark.parametrize("path", [".env", "finn.db", "finn.db-wal", "docs/finance_snapshot.md", "docs/account_isolation_repair_2026_10_14.md", ".secrets/session.json", "app/token.json", "app/.env", "tests/private/account.py", "../key.py", "/app/config.py", "AGENTS.md"])
def test_private_or_unknown_files_are_rejected(path):
    assert not allowed_path(path)


@pytest.mark.parametrize("path", [".env.example", "README.md", "app/config.py", "scripts/run_telegram_polling.py", "tests/test_parser.py", "docs/architecture.md"])
def test_public_source_is_allowed(path):
    assert allowed_path(path)
