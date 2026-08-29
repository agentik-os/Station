import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay" / "scripts" / "install-discord-token.py"


def load_module():
    spec = importlib.util.spec_from_file_location("station_discord_installer_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_distribution_binds_profile_and_os_as_independent_identities(tmp_path):
    module = load_module()
    profile = tmp_path / "agk-architect"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "owner_environment: private\n"
        "profile_id: agk-architect\n"
        "os_id: builder-os\n"
        "version: 0.2.0\n"
    )

    module._validate_private_distribution(profile, "agk-architect", "builder-os", "0.2.0")
    with pytest.raises(ValueError, match="identity mismatch"):
        module._validate_private_distribution(profile, "agk-architect", "research-os", "0.2.0")
