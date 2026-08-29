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

def test_signal_cancellation_is_inside_marker_cleanup_scope():
 source=MODULE.read_text()
 assert "signal.signal(signal.SIGTERM,cancel_signal)" in source
 assert source.index("try:\n  while time.monotonic()<deadline:") < source.index("finally:\n  if marker_active: _clear_drain_marker")


def test_safe_reload_waits_passively_before_short_drain_cutover():
 source=MODULE.read_text()
 wait=source.index("while time.monotonic()<deadline:")
 first_marker=source.index("_activate_drain_marker(write_drain_request",wait)
 assert wait < first_marker
 assert "refresh_at" not in source
 assert "active work did not drain; reload cancelled without interrupting it" in source


def test_marker_write_exception_cleans_possible_atomic_marker(tmp_path):
 module=load()
 marker=tmp_path/".drain_request.json"
 def write_then_fail(**_kwargs):
  marker.write_text("{}")
  raise RuntimeError("post-write failure")
 def clear_marker(*,home):
  path=home/".drain_request.json"
  if not path.exists(): return False
  path.unlink(); return True
 with pytest.raises(RuntimeError,match="post-write failure"):
  module._activate_drain_marker(write_then_fail,clear_marker,tmp_path)
 assert not marker.exists()


def test_failed_marker_clear_fails_closed(tmp_path):
 module=load()
 marker=tmp_path/".drain_request.json"; marker.write_text("{}")
 def failed_clear(*,home): return False
 with pytest.raises(RuntimeError,match="drain marker remains active"):
  module._clear_drain_marker(failed_clear,tmp_path)
 assert marker.exists()
