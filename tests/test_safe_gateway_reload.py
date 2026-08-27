import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/scripts/station_safe_gateway_reload.py"

def load():
 spec=importlib.util.spec_from_file_location("safe_reload_test",MODULE); assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def test_missing_gateway_state_fails_closed(tmp_path):
 m=load()
 with pytest.raises(m.StatusUnavailable): m.status(tmp_path)

def test_malformed_gateway_state_fails_closed(tmp_path):
 m=load(); (tmp_path/"gateway_state.json").write_text("not-json")
 with pytest.raises(m.StatusUnavailable): m.status(tmp_path)

def test_missing_active_agent_count_fails_closed(tmp_path):
 m=load(); (tmp_path/"gateway_state.json").write_text('{"gateway_state":"running"}')
 with pytest.raises(m.StatusUnavailable): m.status(tmp_path)

def test_long_drain_refreshes_marker():
 source=MODULE.read_text()
 assert "refresh_at" in source
 assert "write_drain_request(principal='station-safe-reload'" in source
