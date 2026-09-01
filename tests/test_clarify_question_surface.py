from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _clarify_method(source: str) -> str:
    start = source.index("    async def send_clarify(")
    end = source.index("    async def send_update_prompt(", start)
    return source[start:end]


def test_station_packages_single_adaptive_discord_question_surface():
    adapter = (
        ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"
    ).read_text(encoding="utf-8")
    clarify = _clarify_method(adapter)

    assert "embed=embed" not in clarify
    assert "discord.Embed(" not in clarify
    assert "decision_request_from_clarify(" in clarify
    assert "AdaptiveDecisionView(" in clarify
    assert "return await self.send_decision(" in clarify
    assert "Hermes needs your input" not in clarify
    assert "ClarifyChoiceView(" not in clarify


def test_station_installs_contextual_clarify_contract_into_shared_runtime():
    clarify_path = ROOT / "overlay/hermes/tools/clarify_tool.py"
    assert clarify_path.is_file()
    contract = clarify_path.read_text(encoding="utf-8").lower()
    for phrase in (
        "sole visible question",
        "self-contained",
        "context",
        "decision",
        "target",
        "consequence",
        "do not repeat",
    ):
        assert phrase in contract

    overlay_install = (ROOT / "overlay/install.sh").read_text(encoding="utf-8")
    shared_install = (
        ROOT / "overlay/scripts/install-shared-hermes.sh"
    ).read_text(encoding="utf-8")
    assert '"$install_root/hermes/tools/clarify_tool.py"' in overlay_install
    assert '"$official_dir/tools/clarify_tool.py"' in shared_install
