import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/scripts/station_loopback_host_proxy.py"


def load():
    spec = importlib.util.spec_from_file_location("station_loopback_host_proxy_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upstream_must_be_loopback_http():
    module = load()
    assert module.validate_upstream("http://127.0.0.1:8463") == "http://127.0.0.1:8463"
    for value in ("https://127.0.0.1:8463", "http://100.64.0.1:8463", "http://example.com"):
        with pytest.raises(ValueError, match="loopback HTTP"):
            module.validate_upstream(value)


def test_headers_rewrite_host_and_drop_hop_by_hop():
    module = load()
    source = {
        "Host": "agk-core.example.ts.net:8443",
        "Authorization": "Bearer preserved",
        "Cookie": "session=preserved",
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
    }
    result = module.upstream_headers(source, "127.0.0.1:8463")
    assert result["Host"] == "127.0.0.1:8463"
    assert result["Authorization"] == "Bearer preserved"
    assert result["Cookie"] == "session=preserved"
    assert "Connection" not in result
    assert "Transfer-Encoding" not in result
