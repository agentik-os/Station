from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "overlay/hermes/plugins/agk_discord_ui_policy/__init__.py"


def _policy_prompt() -> str:
    spec = spec_from_file_location("agk_discord_ui_policy", POLICY)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.policy_prompt()


def test_owner_policy_forbids_decorative_full_message_quote_rails():
    prompt = _policy_prompt()

    assert "Do not wrap ordinary replies in full-message Discord blockquotes (`>>>`)" in prompt
    assert "Do not use colored accent rails as decoration" in prompt