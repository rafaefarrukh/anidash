import base64
import hashlib
from pathlib import Path

import pytest

from anidash import qbit_setup


def test_pbkdf2_format_and_verify():
    v = qbit_setup.pbkdf2_value("hunter2", salt=b"\x00" * 16)
    assert v.startswith("@ByteArray(") and v.endswith(")")
    salt_b64, dk_b64 = v[len("@ByteArray("):-1].split(":")
    assert base64.b64decode(salt_b64) == b"\x00" * 16
    assert base64.b64decode(dk_b64) == hashlib.pbkdf2_hmac("sha512", b"hunter2", b"\x00" * 16, 100_000)
    assert qbit_setup.verify_pbkdf2("hunter2", f'"{v}"')
    assert not qbit_setup.verify_pbkdf2("wrong", v)
    assert not qbit_setup.verify_pbkdf2("hunter2", "garbage")


def test_known_vector_from_qbittorrent():
    # The default qBittorrent < 4.6.1 "adminadmin" hash shipped in many Docker images.
    v = ('@ByteArray(ARQ77eY1NUZaQsuDHbIMCA==:0WMRkYTUWVT9wVvdDtHAjU9b3b7uB8NR1Gur2hmQCvCDpm39Q+PsJRJPaCU51dEiz+'
         'dTzh8qbPsL8WkFljQYFQ==)')
    assert qbit_setup.verify_pbkdf2("adminadmin", v)


SAMPLE = """[Application]
FileLogger\\Enabled=true

[BitTorrent]
Session\\Port=51413

[Preferences]
General\\Locale=en
WebUI\\Port=9090
WebUI\\Enabled=false

[RSS]
AutoDownloader\\DownloadRepacks=true
"""


def test_set_keys_preserves_everything_else(tmp_path):
    f = tmp_path / "qBittorrent.conf"
    f.write_text(SAMPLE)
    qbit_setup.set_keys(f, "Preferences", {"WebUI\\Enabled": "true", "WebUI\\Username": "anidash"})
    out = f.read_text()
    assert "WebUI\\Enabled=true" in out and "WebUI\\Enabled=false" not in out
    assert "WebUI\\Port=9090" in out and "General\\Locale=en" in out
    assert out.index("WebUI\\Username=anidash") < out.index("[RSS]")   # appended inside its own section
    assert "Session\\Port=51413" in out and "AutoDownloader\\DownloadRepacks=true" in out
    assert qbit_setup.read_section(f, "Preferences")["WebUI\\Username"] == "anidash"


def test_set_keys_new_section_and_file(tmp_path):
    f = tmp_path / "sub" / "qBittorrent.ini"
    qbit_setup.set_keys(f, "LegalNotice", {"Accepted": "true"})
    assert qbit_setup.read_section(f, "LegalNotice") == {"Accepted": "true"}
    qbit_setup.set_keys(f, "Preferences", {"WebUI\\Enabled": "true"})
    assert qbit_setup.read_section(f, "LegalNotice") == {"Accepted": "true"}
    assert qbit_setup.read_section(f, "Preferences") == {"WebUI\\Enabled": "true"}


def test_configure_refuses_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(qbit_setup, "running", lambda: [{"pid": 1, "name": "qbittorrent"}])
    inst = qbit_setup.Install("native", str(tmp_path / "q.conf"))
    with pytest.raises(RuntimeError):
        qbit_setup.configure(inst, "anidash", "pw")


def test_configure_writes_secure_webui(tmp_path, monkeypatch):
    monkeypatch.setattr(qbit_setup, "running", lambda: [])
    cfg = tmp_path / "q.conf"
    cfg.write_text(SAMPLE)
    res = qbit_setup.configure(qbit_setup.Install("native", str(cfg)), "anidash", "s3cret", port=9090)
    sec = qbit_setup.read_section(cfg, "Preferences")
    assert sec["WebUI\\Enabled"] == "true" and sec["WebUI\\Address"] == "127.0.0.1"
    assert sec["WebUI\\LocalHostAuth"] == "true" and sec["WebUI\\Username"] == "anidash"
    assert qbit_setup.verify_pbkdf2("s3cret", sec["WebUI\\Password_PBKDF2"])
    assert res["url"] == "http://127.0.0.1:9090"
    assert res["backup"] and Path(res["backup"]).read_text() == SAMPLE
