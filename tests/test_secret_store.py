import os
import stat
import types

import pytest

from app import secret_store


def _app(tmp_path, config=None):
    cfg = {"SECRET_KEY": "template-test-secret"}
    cfg.update(config or {})
    return types.SimpleNamespace(instance_path=str(tmp_path), config=cfg)


def test_set_get_clear_roundtrip(tmp_path):
    store = secret_store.get_store(_app(tmp_path))
    store.set("API_KEY", "sk-template-AAAA")
    assert store.get("API_KEY") == "sk-template-AAAA"
    store.clear("API_KEY")
    assert store.get("API_KEY") is None


def test_set_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        secret_store.get_store(_app(tmp_path)).set("K", "   ")


def test_resolve_env_over_store_then_none(tmp_path):
    app = _app(tmp_path, {"API_KEY": "sk-env-AAAA"})
    secret_store.get_store(app).set("API_KEY", "sk-store-BBBB")
    assert secret_store.resolve(app, "API_KEY") == "sk-env-AAAA"
    assert secret_store.resolve(_app(tmp_path), "MISSING") is None


def test_file_is_0600_and_encrypted_at_rest(tmp_path):
    store = secret_store.get_store(_app(tmp_path))
    store.set("API_KEY", "sk-super-secret-9876")
    path = os.path.join(str(tmp_path), secret_store._STORE_FILENAME)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert b"sk-super-secret-9876" not in open(path, "rb").read()
