import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/hermes/plugins/platforms/discord/agk_message_format.py"
ADAPTER=ROOT/"overlay/hermes/plugins/platforms/discord/adapter.py"
SYNC=ROOT/"overlay/scripts/sync-hermes.sh"


def load():
    spec=importlib.util.spec_from_file_location("agk_message_format_test",MODULE); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_full_message_background_blockquote_is_removed():
    module=load()
    assert module.normalize_station_reply(">>> **Done**\nEverything works.")=="**Done**\nEverything works."


def test_full_message_background_blockquote_is_removed_after_leading_whitespace():
    module=load()
    assert module.normalize_station_reply("\n  >>> **Done**\nEverything works.")=="**Done**\nEverything works."


def test_inline_quote_is_preserved():
    module=load()
    text="Intro\n\n>>> quoted evidence"
    assert module.normalize_station_reply(text)==text


def test_appended_status_preserves_status_and_stays_within_utf16_budget():
    module=load()
    rendered=module.append_station_status(
        "Existing " + "😀" * 1000,
        "RESOLVED",
        "Selected: " + "🚀" * 1000,
        limit=2000,
    )
    assert module.utf16_len(rendered) <= 2000
    assert "\n\nRESOLVED\nSelected:" in rendered


def test_station_text_truncation_is_utf16_safe():
    module=load()
    rendered=module.truncate_station_text("😀" * 2000, 1900)
    assert module.utf16_len(rendered) <= 1900


def test_urls_inside_code_are_duplicated_as_clickable_links_outside_code():
    module=load(); url="https://example.com/path?q=1"
    result=module.normalize_station_reply(f"Run `open {url}` now")
    assert f"<{url}>" in result and result.endswith(f"Links: <{url}>")


def test_existing_markdown_and_bare_links_are_preserved_without_duplicate():
    module=load(); url="https://example.com/docs"
    assert module.normalize_station_reply(f"[Docs]({url})") == f"[Docs]({url})"
    assert module.normalize_station_reply(url) == url


def test_every_conversational_send_and_edit_path_normalizes_before_formatting():
    adapter=ADAPTER.read_text()
    assert "from .agk_message_format import (" in adapter
    assert "normalize_station_reply," in adapter
    assert "def _format_station_message(" in adapter
    assert "self.format_message(normalize_station_reply(content))" in adapter
    assert "self.format_message(content)" not in adapter
    assert 'normalize_station_reply(str(caption or "").strip())' in adapter
    assert 'normalize_station_reply(str(value).strip())' in adapter


def test_profile_sync_installs_reply_normalizer_with_the_adapter():
    sync=SYNC.read_text()
    common_files=sync[sync.index("for common_file in"):sync.index("done",sync.index("for common_file in"))]
    assert "agk_message_format.py" in common_files
