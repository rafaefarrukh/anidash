import os
import tempfile

# Must be set before anidash.paths is imported anywhere.
os.environ.setdefault("ANIDASH_HOME", tempfile.mkdtemp(prefix="anidash-test-"))

import pytest  # noqa: E402

from anidash import db, paths  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh data directory, database and library for each test."""
    for name, sub in (("CONFIG_DIR", "config"), ("DATA_DIR", "data"), ("LOG_DIR", "logs"), ("RUNTIME_DIR", "run")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "data" / "anidash.db")
    monkeypatch.setattr(paths, "RUNTIME_FILE", tmp_path / "run" / "runtime.json")
    monkeypatch.setattr(paths, "SECRETS_FILE", tmp_path / "config" / "secrets.json")
    paths.ensure()
    db.close()
    db.init()
    lib = tmp_path / "Anime"
    db.set_settings({"library_path": str(lib)})
    yield tmp_path
    db.close()
