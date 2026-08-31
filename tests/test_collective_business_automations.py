import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "overlay" / "scripts" / "collective_automation_core.py"
POLLER = ROOT / "overlay" / "scripts" / "collective_composio_poller.py"
NEWS = ROOT / "overlay" / "scripts" / "collective_news_digest.py"
RECON = ROOT / "overlay" / "scripts" / "collective_discord_reconcile.py"
BROKER = ROOT / "overlay" / "scripts" / "station_interagent_broker.py"
MIGRATE = ROOT / "overlay" / "scripts" / "migrate_collective_owner.py"
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


def test_composio_requires_explicit_agentik_account_bindings(monkeypatch):
    poller = load(POLLER, "collective_poller_account_binding")
    monkeypatch.delenv("AGK_COMPOSIO_STRIPE_ACCOUNT_ID", raising=False)
    try:
        poller.composio_account("STRIPE_GET_ACCOUNT")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Stripe default Composio account was accepted")
    monkeypatch.setenv("AGK_COMPOSIO_STRIPE_ACCOUNT_ID", "ca_AgentikStripe01")
    monkeypatch.setenv("AGK_COMPOSIO_TYPEFORM_ACCOUNT_ID", "ca_AgentikTypeform01")
    assert poller.composio_account("STRIPE_GET_ACCOUNT") == "ca_AgentikStripe01"
    assert poller.composio_account("TYPEFORM_LIST_FORMS") == "ca_AgentikTypeform01"


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
    assert '"core,web"' in NEWS.read_text()
    state = tmp_path / "news-state.json"
    assert news.should_publish("2026-08-31", weekday=0, state_path=state) is True
    news.record_published("2026-08-31", "154000000000000001", state)
    assert news.should_publish("2026-08-31", weekday=0, state_path=state) is False
    assert news.should_publish("2026-08-30", weekday=6, state_path=state) is False
    assert "No verified material items" in NEWS.read_text()
    assert 'text.find("AGK News —")' in NEWS.read_text()


def test_composio_execution_forces_private_artifact_permissions(monkeypatch):
    poller = load(POLLER, "collective_poller_private_artifacts")
    monkeypatch.setenv("AGK_COMPOSIO_STRIPE_ACCOUNT_ID", "ca_agentik123")
    observed = {}

    class Result:
        returncode = 0
        stdout = '{"data": []}'

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr(poller.subprocess, "run", fake_run)
    assert poller.composio_execute("STRIPE_LIST_CHECKOUT_SESSIONS", {}) == []
    assert observed["preexec_fn"] is poller.private_composio_child_setup
    assert "UMask=0077" in (ROOT / "overlay" / "systemd" / "agk-collective-composio.service").read_text()


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
    news_service = (ROOT / "overlay" / "systemd" / "agk-collective-news.service").read_text()
    assert "WorkingDirectory=/home/agentik" in news_service
    tmpfiles = (ROOT / "overlay" / "tmpfiles.d" / "agk-composio.conf").read_text()
    assert "d /tmp/composio 1777 root root -" in tmpfiles
    installer = (ROOT / "overlay" / "install.sh").read_text()
    tmpfiles_install = 'install -m 0644 "$repo_root/tmpfiles.d/agk-composio.conf" /etc/tmpfiles.d/agk-composio.conf'
    assert tmpfiles_install in installer
    assert installer.rfind('if [ "$system_install" = true ]; then', 0, installer.index(tmpfiles_install)) != -1
    assert 'install -m 0644 "$repo_root/config/discord-channel-state.json" "$install_root/config/discord-channel-state.json"' in installer
    assert "systemd-tmpfiles --create" in installer


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


def test_sign_card_reconciler_removes_reaction_consent_and_versions_v2():
    reconcile = load(RECON, "collective_reconcile_card")
    components = [{"type": 17, "components": [{"type": 10, "content": "Three steps, or react **✅**. **✅ also signs.** partnerships-v1-2026-08-24"}, {"type": 1, "components": [{"type": 2, "custom_id": "sign_house"}, {"type": 2, "custom_id": "sign_deals"}, {"type": 2, "custom_id": "sign_conduct"}]}]}]
    updated = reconcile.canonical_sign_components(components)
    raw = json.dumps(updated, ensure_ascii=False)
    assert "✅ also signs" not in raw
    assert "does **not** sign" in raw
    assert "partnerships-v2-2026-08-29" in raw
    assert components[0]["components"][0]["content"].startswith("Three steps, or react")
    assert reconcile.canonical_sign_components(updated) == updated


def test_collective_is_owned_by_agentik_never_mission():
    files = [
        ROOT / "bin" / "station",
        ROOT / "overlay" / "scripts" / "install-shared-hermes.sh",
        ROOT / "overlay" / "scripts" / "rotate_discord_token.py",
        ROOT / "overlay" / "scripts" / "recovery_router.py",
        ROOT / "overlay" / "scripts" / "station_interagent_broker.py",
        ROOT / "overlay" / "scripts" / "completion_oracle_gate.py",
        ROOT / "overlay" / "scripts" / "approval_gate.py",
        ROOT / "overlay" / "scripts" / "completion_harness.py",
        ROOT / "overlay" / "scripts" / "fleet_recovery_auditor.py",
        ROOT / "overlay" / "scripts" / "collective_composio_poller.py",
        ROOT / "overlay" / "scripts" / "collective_news_digest.py",
        ROOT / "overlay" / "scripts" / "collective_discord_reconcile.py",
        ROOT / "overlay" / "scripts" / "github_stars_forum_watcher.py",
        ROOT / "overlay" / "scripts" / "migrate_collective_owner.py",
        ROOT / "overlay" / "hermes" / "plugins" / "platforms" / "discord" / "agk_collective_membership.py",
        ROOT / "overlay" / "hermes" / "plugins" / "platforms" / "discord" / "agk_session_control.py",
        ROOT / "overlay" / "systemd" / "agk-github-stars-forum.service",
        ROOT / "overlay" / "systemd" / "agk-collective-composio.service",
        ROOT / "overlay" / "systemd" / "agk-collective-news.service",
        ROOT / "overlay" / "config" / "discord-channel-state.json",
    ]
    combined = "\n".join(path.read_text() for path in files)
    assert "/home/agentik/.hermes/profiles/collective" in combined
    assert "/home/mission/.hermes/profiles/collective" not in combined
    assert 'collective) user=agentik' in (ROOT / "bin" / "station").read_text()
    manifest = __import__("json").loads((ROOT / "overlay" / "config" / "discord-channel-state.json").read_text())
    collective = next(target for target in manifest["targets"] if target["key"] == "collective")
    assert collective["user"] == "agentik"
    assert collective["hermes_home"] == "/home/agentik/.hermes/profiles/collective"


def test_collective_interagent_routes_and_authenticates_as_agentik():
    broker = load(BROKER, "collective_interagent_owner")
    argv, env = broker.notification_command("collective")
    assert argv[argv.index("--reuid") + 1] == "agentik"
    assert env["HOME"] == "/home/agentik"
    assert env["HERMES_HOME"] == "/home/agentik/.hermes/profiles/collective"
    mapping = {1001: "agentik", 1002: "mission"}
    collective_env = lambda _pid: "/home/agentik/.hermes/profiles/collective"
    default_env = lambda _pid: "/home/agentik/.hermes"
    assert broker.source_for_peer(10, 1001, mapping, collective_env) == "collective"
    assert broker.source_for_peer(11, 1001, mapping, default_env) == "agentik"
    assert broker.source_for_peer(12, 1002, mapping, collective_env) == "mission"


def test_collective_has_agentik_community_semantics_and_no_client_paths(tmp_path, monkeypatch):
    plugin_root = ROOT / "overlay" / "hermes" / "plugins"
    sys.path.insert(0, str(plugin_root))
    import types
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_config = types.ModuleType("hermes_cli.config")
    agentik_package = types.ModuleType("agentik_os")
    setattr(agentik_package, "__path__", [str(plugin_root / "agentik_os")])
    setattr(hermes_config, "read_raw_config", lambda: {})
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)
    monkeypatch.setitem(sys.modules, "agentik_os", agentik_package)
    from agentik_os.commands import AgentikCommandService
    from agentik_os.domain import DOMAIN_COMMANDS
    from agentik_os.paths import PathResolver
    from agentik_os.store import ControlStore

    resolver = PathResolver("collective", tmp_path)
    service = AgentikCommandService("collective", ControlStore(tmp_path / "control.db"), resolver)
    assert service.data_environment == "agentik"
    assert service.domain.environment == "agentik"
    assert "client" not in service.command_names
    for command in ("deliverable", "deploy", "report"):
        assert command not in service.command_names
    for command in ("community", "content", "growth", "research"):
        assert command in DOMAIN_COMMANDS["collective"]
    try:
        resolver.client("dentistry")
    except PermissionError:
        pass
    else:
        raise AssertionError("Collective resolved a Mission client path")
    assert resolver.project("community-roadmap") == tmp_path / "workspace" / "projects" / "community-roadmap"


def test_collective_cutover_moves_only_token_and_has_native_rollback():
    migrate = load(MIGRATE, "collective_owner_cutover")
    assert migrate.APP_ID == "1541131574509314209"
    assert migrate.CONTROL_GUILD_ID == "1541131439599386644"
    assert migrate.HOME_CHANNEL_ID == "1541847685680603387"
    assert migrate.COMMUNITY_GUILD_ID == "1350170767366688830"
    assert migrate.FORUM_CHANNEL_ID == "1541222874226888804"
    token = "A" * 40
    source = "KEEP=value\nDISCORD_BOT_TOKEN=" + token + "\nOTHER=value\n"
    extracted, without = migrate.extract_token(source)
    assert extracted == token
    assert "DISCORD_BOT_TOKEN" not in without
    migrate.validate_source_without_discord(without)
    spaced_source = "KEEP=value\nDISCORD_BOT_TOKEN = " + token + "\n"
    spaced_token, spaced_without = migrate.extract_token(spaced_source)
    assert spaced_token == token
    migrate.validate_source_without_discord(spaced_without)
    with __import__("pytest").raises(RuntimeError, match="credential"):
        migrate.extract_token(source + "DISCORD_TOKEN=legacy-value\n")
    with __import__("pytest").raises(RuntimeError, match="duplicate"):
        migrate.extract_token(source + "DISCORD_BOT_TOKEN = " + token + "\n")
    for empty_duplicate in (
        "DISCORD_BOT_TOKEN=\nDISCORD_BOT_TOKEN=" + token + "\n",
        'DISCORD_BOT_TOKEN=""\nDISCORD_BOT_TOKEN = ' + token + "\n",
    ):
        with __import__("pytest").raises(RuntimeError, match="duplicate"):
            migrate.extract_token(empty_duplicate)
    target = migrate.target_env_content("EXISTING=value\n", token)
    assert target.count("DISCORD_BOT_TOKEN=") == 1
    assert "OPENROUTER" not in target
    migrate.validate_target_env(target, token)
    canonical = migrate.target_env_content(
        "KEEP=value\nDISCORD_HOME_CHANNEL=wrong\nDISCORD_ALLOW_ALL_USERS=true\nDISCORD_REQUIRE_MENTION=true\n",
        token,
    )
    assert "KEEP=value" in canonical
    assert canonical.count("DISCORD_HOME_CHANNEL=") == 1
    assert "DISCORD_HOME_CHANNEL=1541847685680603387" in canonical
    assert "DISCORD_ALLOW_ALL_USERS=false" in canonical
    assert "DISCORD_REQUIRE_MENTION=false" in canonical
    migrate.validate_target_env(canonical, token)
    for contaminated in (
        "DISCORD_TOKEN=legacy-value\n",
        "STRIPE_SECRET_KEY=client-value\n",
        "TYPEFORM_TOKEN=client-value\n",
        "COMPOSIO_API_KEY=client-value\n",
        "DENTISTRY_WEBHOOK_SECRET=client-value\n",
        "UNRELATED_PRIVATE_KEY=client-value\n",
    ):
        with __import__("pytest").raises(RuntimeError, match="credential"):
            migrate.target_env_content(contaminated, token)
    script = MIGRATE.read_text()
    assert 'gateway_action(OLD_USER,OLD_ROOT,"stop")' in script
    assert 'gateway_action(NEW_USER,NEW_ROOT,"start")' in script
    assert "shutil.copy" not in script
    assert "copyfile" not in script
    assert "active_agents() != 0" in script
    assert "wait_gateway_state" in script
    assert "writer_pid" in script
    assert "NEW_FRAGMENT" in script
    assert "freeze_old_automations" in script
    for timer in ("agk-github-stars-forum.timer", "agk-collective-composio.timer", "agk-collective-news.timer"):
        assert timer in script


def test_collective_cutover_restores_both_envs_after_partial_target_write(tmp_path):
    migrate = load(MIGRATE, "collective_owner_partial_restore")
    token = "A" * 40
    old_env = tmp_path / "old.env"
    new_env = tmp_path / "new.env"
    old_original = f"KEEP=old\nDISCORD_BOT_TOKEN={token}\n"
    new_original = "KEEP=new\n"
    old_env.write_text(old_original)
    new_env.write_text(migrate.target_env_content(new_original, token))

    migrate.restore_env_files(
        old_env,
        new_env,
        old_original,
        new_original,
        __import__("os").getuid(),
        __import__("os").getgid(),
        __import__("os").getuid(),
        __import__("os").getgid(),
    )

    assert old_env.read_text() == old_original
    assert new_env.read_text() == new_original


def test_collective_cutover_source_has_verified_success_and_rollback_invariants():
    script = MIGRATE.read_text()
    assert "restore_env_files(" in script
    assert "verify_service_state(" in script
    assert "verify_timer_states(" in script
    assert 'expected_active="inactive"' in script
    assert 'expected_unit_file="disabled"' in script
    assert "rollback incomplete" in script
    assert "moved=False" not in script
    assert "if moved:" not in script


def test_collective_cutover_second_env_write_failure_restores_exact_originals(tmp_path, monkeypatch):
    migrate = load(MIGRATE, "collective_owner_fault_injection")
    token = "A" * 40
    old_env = tmp_path / "old.env"
    new_env = tmp_path / "new.env"
    old_original = f"KEEP=old\nDISCORD_BOT_TOKEN={token}\n"
    new_original = "KEEP=new\n"
    old_without = "KEEP=old\n"
    new_with = migrate.target_env_content(new_original, token)
    old_env.write_text(old_original)
    new_env.write_text(new_original)
    real_atomic_write = migrate.atomic_write
    calls = 0

    def fail_second(path, content, uid, gid):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected source write failure")
        return real_atomic_write(path, content, uid, gid)

    monkeypatch.setattr(migrate, "atomic_write", fail_second)
    import os
    with __import__("pytest").raises(OSError, match="injected source write failure"):
        migrate.transfer_env_files(
            old_env,
            new_env,
            old_original,
            new_original,
            old_without,
            new_with,
            os.getuid(),
            os.getgid(),
            os.getuid(),
            os.getgid(),
        )

    assert old_env.read_text() == old_original
    assert new_env.read_text() == new_original


def test_installer_requires_explicit_collective_provisioning_and_never_restarts_mission_legacy():
    script = (ROOT / "overlay" / "scripts" / "install-shared-hermes.sh").read_text()
    assert "profile create collective" not in script
    assert "gateway install --no-start-now" not in script
    assert "Collective Agentik profile absent; explicit ownership provisioning/cutover required." in script
    assert '[ "$user_name" = mission ] && [ "$unit_name" = hermes-gateway-collective.service ]' in script
    assert "Collective Agentik profile provisioned; credential cutover required before activation." in script
    assert "gateway_ready=true" in script
    assert "composio_ready=$gateway_ready" in script
    assert "AGK_COMPOSIO_STRIPE_ACCOUNT_ID=ca_" in script
    assert "AGK_COMPOSIO_TYPEFORM_ACCOUNT_ID=ca_" in script
    assert "[A-Za-z0-9_-]{8,}" in script
