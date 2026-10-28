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
    lib.mkdir()  # the app creates it in jobs.check_library once it knows the volume is right
    db.set_settings({"library_path": str(lib)})
    yield tmp_path
    db.close()


class FakeEngine:
    """Stands in for the download engine; tests fill `items` (engine.Torrent) and read what was done."""
    name = "builtin"

    def __init__(self):
        self.items, self.added, self.removed, self.stopped, self.renamed = [], [], [], [], []
        self.on_rename = None

    def add(self, infohash, name, save_path, anilist_id):
        self.added.append((infohash, name, save_path))

    def torrents(self):
        return list(self.items)

    def files(self, infohash):
        return next((t.files for t in self.items if t.hash == infohash.lower()), [])

    def rename_file(self, infohash, old, new):
        self.renamed.append(new)
        if self.on_rename:
            self.on_rename(old, new)

    def stop(self, infohash):
        self.stopped.append(infohash)

    def remove(self, hashes):
        self.removed.extend([hashes] if isinstance(hashes, str) else hashes)

    def interfaces(self):
        return {"current": "", "list": [{"name": "wg0 (10.2.0.2)", "value": "wg0"}], "bypass_local_auth": False}

    def bind_interface(self, value):
        self.bound = value

    def health(self):
        return {"ok": True, "version": "fake", "error": None}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    """No test starts a real torrent session (or touches the network) unless it builds one itself."""
    from anidash import engine
    fe = FakeEngine()
    monkeypatch.setattr(engine, "get", lambda: fe)
    return fe
