import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/hermes/plugins/platforms/discord/agk_message_format.py"


def load():
    spec=importlib.util.spec_from_file_location("agk_message_format_test",MODULE); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_full_message_background_blockquote_is_removed():
    module=load()
    assert module.normalize_station_reply(">>> **Done**\nEverything works.")=="**Done**\nEverything works."


def test_urls_inside_code_are_duplicated_as_clickable_links_outside_code():
    module=load(); url="https://example.com/path?q=1"
    result=module.normalize_station_reply(f"Run `open {url}` now")
    assert f"<{url}>" in result and result.endswith(f"Links: <{url}>")


def test_existing_markdown_and_bare_links_are_preserved_without_duplicate():
    module=load(); url="https://example.com/docs"
    assert module.normalize_station_reply(f"[Docs]({url})") == f"[Docs]({url})"
    assert module.normalize_station_reply(url) == url
