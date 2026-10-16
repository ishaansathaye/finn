from app.parser import parse_command


def test_parse_approve_still_parses_for_deprecation_message() -> None:
    parsed = parse_command("approve 1 2")
    assert parsed is not None
    assert parsed.action == "approve"
    assert parsed.targets == ["1", "2"]


def test_parse_portfolio_with_slash() -> None:
    parsed = parse_command("/portfolio")
    assert parsed is not None
    assert parsed.action == "portfolio"
    assert parsed.targets == []


def test_parse_executed_alias_and_pm_prefix() -> None:
    parsed = parse_command("done pm 3")
    assert parsed is not None
    assert parsed.action == "executed"
    assert parsed.targets == ["3"]


def test_parse_g_commands_and_execute_confirm() -> None:
    parsed = parse_command("gs")
    assert parsed is not None
    assert parsed.action == "gamble_small"
    assert parsed.targets == []

    staged_manual = parse_command("g x 2 5")
    assert staged_manual is not None
    assert staged_manual.action == "gamble_stage"
    assert staged_manual.targets == ["2", "5"]

    staged_small = parse_command("gs x 2 5")
    assert staged_small is not None
    assert staged_small.action == "gamblex_select"
    assert staged_small.targets == ["2", "5"]

    legacy = parse_command("gx")
    assert legacy is not None
    assert legacy.action == "gamble_small"
    assert legacy.targets == []

    slate = parse_command("gsl")
    assert slate is not None
    assert slate.action == "slate"
    assert slate.targets == []

    confirm = parse_command("x confirm")
    assert confirm is not None
    assert confirm.action == "execute"
    assert confirm.targets == ["confirm"]

    short_confirm = parse_command("x c")
    assert short_confirm is not None
    assert short_confirm.action == "execute"
    assert short_confirm.targets == ["c"]


def test_parse_latest_alias() -> None:
    parsed = parse_command("latest")
    assert parsed is not None
    assert parsed.action == "latest"
    assert parsed.targets == []




def test_parse_ask_command() -> None:
    parsed = parse_command("ask Rocket Lab")
    assert parsed is not None
    assert parsed.action == "ask"
    assert parsed.targets == ["rocket", "lab"]

    removed = parse_command("ra")
    assert removed is None


def test_parse_agent_command() -> None:
    parsed = parse_command("agent")
    assert parsed is not None
    assert parsed.action == "agent"
    assert parsed.targets == []
