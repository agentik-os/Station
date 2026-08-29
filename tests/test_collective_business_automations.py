import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "overlay" / "scripts" / "collective_automation_core.py"
POLLER = ROOT / "overlay" / "scripts" / "collective_composio_poller.py"
NEWS = ROOT / "overlay" / "scripts" / "collective_news_digest.py"
PLUGIN = ROOT / "overlay" / "hermes" / "plugins" / "platforms" / "discord" / "agk_collective_membership.py"


def load(path, name):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signature_requires_explicit_three_steps_and_exact_phrase(tmp_path):
    core = load(CORE, "collective_core_signature")
    store = core.CollectiveStore(tmp_path / "collective.db")
    user = "123456789012345678"
    assert store.signature_progress(user)["signed"] is False
    assert store.protect_legacy_signed([user]) == 1
    assert store.legacy_signed_ids() == [user]
    store.mark_signature_step(user, "house", "step-house")
    store.mark_signature_step(user, "deals", "step-deals")
    assert store.complete_signature(user, "Gareth", "NO", "sign-1")["ok"] is False
    result = store.complete_signature(user, "Gareth", "I ACCEPT", "sign-2")
    assert result["ok"] is True
    assert store.signature_progress(user)["signed"] is True
    assert store.legacy_signed_ids() == []
    assert store.signed_discord_ids() == [user]
    again = store.complete_signature(user, "Gareth", "I ACCEPT", "sign-2")
    assert again == result


def test_reaction_never_completes_signature(tmp_path):
    core = load(CORE, "collective_core_reaction")
    store = core.CollectiveStore(tmp_path / "collective.db")
    user = "123456789012345678"
    store.record_reaction_redirect(user, "reaction-1")
    progress = store.signature_progress(user)
    assert progress["signed"] is False
    assert progress["steps"] == []


def test_typeform_intro_only_maps_public_allowlisted_fields():
    core = load(CORE, "collective_core_intro")
    response = {
        "response_id": "intro-response-1",
        "answers": [
            {"field": {"id": "CzPLfPs3glaR"}, "type": "text", "text": "@builder"},
            {"field": {"id": "S7TU0edWCcBO"}, "type": "text", "text": "Builder"},
            {"field": {"id": "GLzWCSGkkqIw"}, "type": "text", "text": "An agent OS"},
            {"field": {"id": "E36EiKHgYhPf"}, "type": "text", "text": "Madrid"},
            {"field": {"id": "UNKNOWN_PRIVATE"}, "type": "email", "email": "private@example.com"},
        ],
    }
    card = core.map_intro_response(response)
    assert card["event_id"] == "typeform:intro:intro-response-1"
    assert "Builder" in card["content"]
    assert "An agent OS" in card["content"]
    assert "private@example.com" not in card["content"]


def test_typeform_deal_never_posts_private_contact():
    core = load(CORE, "collective_core_deal")
    response = {
        "response_id": "deal-response-1",
        "answers": [
            {"field": {"id": "E7i07POOiF88"}, "type": "text", "text": "Public Co"},
            {"field": {"id": "IMd6gatnSQYN"}, "type": "text", "text": "Needs an AI workflow"},
            {"field": {"id": "e1PGhlvFlDpf"}, "type": "text", "text": "secret@example.com"},
            {"field": {"id": "vO6YB81f3Mw1"}, "type": "boolean", "boolean": True},
        ],
    }
    card = core.map_deal_response(response)
    assert card["accepted_split"] is True
    assert "Public Co" in card["content"]
    assert "secret@example.com" not in card["content"]
    malformed = {**response, "response_id": "deal-response-2", "answers": response["answers"][:-1] + [{"field": {"id": "vO6YB81f3Mw1"}, "type": "boolean", "boolean": "false"}]}
    assert core.map_deal_response(malformed)["accepted_split"] is False


def test_stripe_session_requires_paid_complete_and_valid_discord_id():
    core = load(CORE, "collective_core_stripe")
    base = {
        "id": "cs_123",
        "payment_status": "paid",
        "status": "complete",
        "client_reference_id": "123456789012345678",
        "payment_link": "plink_1U7juxHfwsV7ya4QoqJyHjNX",
        "mode": "subscription",
        "currency": "eur",
        "amount_total": 2910,
    }
    lines = [{"price": {"id": "price_1U7juWHfwsV7ya4Qjl7FLMzU"}, "quantity": 1}]
    assert core.map_paid_checkout(base, lines)["discord_id"] == "123456789012345678"
    assert core.map_paid_checkout({**base, "payment_status": "unpaid"}, lines) is None
    assert core.map_paid_checkout({**base, "client_reference_id": "../../etc"}, lines) is None
    assert core.map_paid_checkout({**base, "payment_link": "plink_other"}, lines) is None
    assert core.map_paid_checkout({**base, "amount_total": 0}, lines) is None
    assert core.map_paid_checkout(base, [{"price": {"id": "price_other"}, "quantity": 1}]) is None


def test_terms_version_change_invalidates_old_steps_and_role_reconciliation(tmp_path):
    core = load(CORE, "collective_core_terms_version")
    store = core.CollectiveStore(tmp_path / "collective.db")
    user = "123456789012345678"
    store.mark_signature_step(user, "house", "old-house")
    store.mark_signature_step(user, "deals", "old-deals")
    assert store.complete_signature(user, "Gareth", "I ACCEPT", "old-sign")["ok"] is True
    with store.connect() as db:
        db.execute("UPDATE acceptances SET terms_version='obsolete-v1' WHERE discord_id=?", (user,))
    assert store.signed_discord_ids() == []
    progress = store.mark_signature_step(user, "house", "new-house")
    assert progress["steps"] == ["house"]
    assert progress["signed"] is False


def test_store_claim_and_delivery_are_idempotent(tmp_path):
    core = load(CORE, "collective_core_events")
    store = core.CollectiveStore(tmp_path / "collective.db")
    assert store.claim_event("typeform:intro:1", "intro", "hash") is True
    assert store.claim_event("typeform:intro:1", "intro", "hash") is False
    store.mark_delivered("typeform:intro:1", "154000000000000001", "154000000000000002")
    assert store.event_status("typeform:intro:1")["status"] == "delivered"


def test_dry_run_does_not_claim_or_initialize_events(tmp_path, monkeypatch):
    core = load(CORE, "collective_core_dry_run")
    poller = load(POLLER, "collective_poller_dry_run")
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=fake\n")
    monkeypatch.setattr(poller, "profile_home", lambda: tmp_path)
    monkeypatch.setattr(poller, "typeform_responses", lambda form: [])
    monkeypatch.setattr(poller, "stripe_sessions", lambda: [])
    result = poller.run(dry_run=True)
    assert result["mode"] == "would_initialize"
    assert not (tmp_path / "collective-automation-initialized.json").exists()
    assert not (tmp_path / "collective-automation.db").exists()


def test_signed_reconciliation_continues_after_absent_member(tmp_path):
    poller = load(POLLER, "collective_poller_signed")
    class Store:
        def signed_discord_ids(self): return ["11111111111111111", "22222222222222222"]
        def legacy_signed_ids(self): return ["33333333333333333", "55555555555555555"]
    class Discord:
        def __init__(self): self.seen = []; self.removed = []
        def signed_member_ids(self): return {"22222222222222222", "33333333333333333", "44444444444444444"}
        def grant_signed(self, discord_id):
            self.seen.append(discord_id)
            return discord_id != "11111111111111111"
        def remove_signed(self, discord_id): self.removed.append(discord_id); return True
    discord = Discord()
    result = poller.reconcile_signed(Store(), discord)
    assert discord.seen == ["11111111111111111", "55555555555555555"]
    assert discord.removed == ["44444444444444444"]
    assert result == {"granted": 1, "removed": 1, "absent": 1}


def test_incomplete_stripe_session_is_not_terminally_ignored():
    poller = load(POLLER, "collective_poller_stripe_state")
    assert poller.terminally_ignorable_stripe({"status": "open", "payment_status": "unpaid"}) is False
    assert poller.terminally_ignorable_stripe({"status": "complete", "payment_status": "paid", "payment_link": "wrong"}) is True


def test_unrelated_stripe_session_never_fetches_line_items(monkeypatch):
    poller = load(POLLER, "collective_poller_prefilter")
    monkeypatch.setattr(poller, "stripe_line_items", lambda session_id: (_ for _ in ()).throw(AssertionError("line items fetched")))
    session = {"id": "cs_other", "status": "complete", "payment_status": "paid", "payment_link": "plink_other", "mode": "subscription", "currency": "eur", "amount_total": 9700, "client_reference_id": "123456789012345678"}
    assert poller.mapped_checkout(session) is None


def test_composio_file_output_is_bounded_parsed_and_deleted(tmp_path, monkeypatch):
    poller = load(POLLER, "collective_poller_file_output")
    monkeypatch.setattr(poller, "COMPOSIO_ARTIFACT_ROOTS", (tmp_path,))
    inline = {"successful": True, "data": {"inline": True}}
    assert poller.decode_composio_result(inline) is inline

    artifact = tmp_path / "result.json"
    artifact.write_text(json.dumps({"successful": True, "data": {"items": []}}))
    decoded = poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(artifact)})
    assert decoded == {"successful": True, "data": {"items": []}}
    assert not artifact.exists()

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json")
    try:
        poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(invalid)})
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid Composio JSON accepted")
    assert not invalid.exists()

    monkeypatch.setattr(poller, "COMPOSIO_ARTIFACT_MAX_BYTES", 8)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"123456789")
    try:
        poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(oversized)})
    except RuntimeError:
        pass
    else:
        raise AssertionError("oversized Composio artifact accepted")
    assert not oversized.exists()

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    try:
        poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(fifo)})
    except RuntimeError:
        pass
    else:
        raise AssertionError("FIFO Composio artifact accepted")
    assert fifo.exists()
    fifo.unlink()

    outside = tmp_path.parent / ("outside-" + tmp_path.name + ".json")
    outside.write_text("{}")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(outside)
    try:
        try:
            poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(symlink)})
        except (OSError, RuntimeError):
            pass
        else:
            raise AssertionError("symlink Composio artifact accepted")
        assert outside.exists()
        assert symlink.is_symlink()
        try:
            poller.decode_composio_result({"storedInFile": True, "outputFilePath": str(outside)})
        except RuntimeError:
            pass
        else:
            raise AssertionError("outside Composio artifact accepted")
    finally:
        symlink.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


def test_news_weekday_and_daily_dedupe(tmp_path):
    news = load(NEWS, "collective_news")
    state = tmp_path / "news-state.json"
    assert news.should_publish("2026-08-31", weekday=0, state_path=state) is True
    news.record_published("2026-08-31", "154000000000000001", state)
    assert news.should_publish("2026-08-31", weekday=0, state_path=state) is False
    assert news.should_publish("2026-08-30", weekday=6, state_path=state) is False


def test_packaging_installs_collective_runtime_and_exact_timers():
    combined = (ROOT / "overlay" / "install.sh").read_text() + (ROOT / "overlay" / "scripts" / "install-shared-hermes.sh").read_text()
    for name in (
        "collective_automation_core.py",
        "collective_composio_poller.py",
        "collective_news_digest.py",
        "collective_discord_reconcile.py",
        "agk-collective-composio.timer",
        "agk-collective-news.timer",
    ):
        assert name in combined
    composio_timer = (ROOT / "overlay" / "systemd" / "agk-collective-composio.timer").read_text()
    assert "OnUnitActiveSec=1min" in composio_timer
    news_timer = (ROOT / "overlay" / "systemd" / "agk-collective-news.timer").read_text()
    assert "OnCalendar=Mon..Fri" in news_timer


def test_adapter_registers_collective_listener_and_panel():
    adapter = (ROOT / "overlay" / "hermes" / "plugins" / "platforms" / "discord" / "adapter.py").read_text()
    assert "register_collective_membership_listener" in adapter
    assert "register_collective_commands" in adapter
    assert '"collective"' in adapter
    assert 'DISCORD_MEMBERS_INTENT' in adapter
    plugin = PLUGIN.read_text()
    assert "sign_house" in plugin
    assert "terms_sign_modal" in plugin
    assert "on_raw_reaction_add" in plugin
    assert plugin.index("store.claim_event,") < plugin.index("await user.send")


def test_command_reconciler_is_exact_and_fail_closed():
    script = (ROOT / "overlay" / "scripts" / "collective_discord_reconcile.py").read_text()
    for name in ("upgrade", "billing", "profile", "opportunities", "learn", "today", "ship", "kudos", "board", "win", "pair", "streak", "deal"):
        assert f'"{name}"' in script
    assert 'REQUIRED_GLOBAL = {"collective", "panel", "clear"}' in script
    assert "Unknown guild commands require owner review" in script
    assert "--apply" in script
    assert "retry_after" in script
    assert "retry budget exhausted" in script
