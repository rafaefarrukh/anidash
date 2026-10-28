"""The download engine: the built-in libtorrent session (default) or the user's qBittorrent.
Interface: add, torrents, files, rename_file, stop, remove, finalize (data on disk, files released, torrent
gone), interfaces, bind_interface, health, close.
jobs.py and api.py only talk to `get()`. Torrents never seed: jobs removes them once the file is in place."""
import contextlib
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import __version__, db, paths

log = logging.getLogger("anidash.engine")
TRACKERS = ["http://nyaa.tracker.wf:7777/announce", "udp://open.stealth.si:80/announce",
            "udp://tracker.opentrackr.org:1337/announce", "udp://exodus.desync.com:6969/announce"]
ENGINES = ("builtin", "qbittorrent")


@dataclass
class File:
    name: str  # relative to save_path
    size: int


@dataclass
class Torrent:
    hash: str  # v1 infohash, lowercase hex
    name: str
    state: str  # qBittorrent's state names (the dashboard maps them to labels), for both engines
    progress: float
    save_path: str
    amount_left: int
    eta: int | None = None  # seconds
    dlspeed: int = 0  # bytes/s
    files: list[File] = field(default_factory=list)


def magnet(infohash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{infohash}&dn={quote(name)}" + "".join(f"&tr={quote(t)}" for t in TRACKERS)


def _hashes(hashes) -> list[str]:
    return [h.lower() for h in ([hashes] if isinstance(hashes, str) else hashes) if h]


# -- qBittorrent ---------------------------------------------------------------
class QbitEngine:
    """Today's qBittorrent Web API backend (qbit.py), for people who already run qBittorrent."""
    name = "qbittorrent"

    def add(self, infohash: str, name: str, save_path: str, anilist_id: int) -> None:
        from . import qbit
        qbit.add(infohash, name, save_path, anilist_id)

    def torrents(self) -> list[Torrent]:
        from . import qbit
        out = []
        for t in qbit.client().torrents_info(category=qbit.CATEGORY):
            eta = t.eta if 0 <= t.eta < 8640000 else None
            out.append(Torrent(t.hash.lower(), t.name, t.state, t.progress, t.save_path, t.amount_left, eta, t.dlspeed))
        return out

    def files(self, infohash: str) -> list[File]:
        from . import qbit
        return [File(f.name, f.size) for f in qbit.client().torrents_files(torrent_hash=infohash)]

    def rename_file(self, infohash: str, old: str, new: str) -> None:
        from . import qbit
        qbit.client().torrents_rename_file(torrent_hash=infohash, old_path=old, new_path=new)

    def stop(self, infohash: str) -> None:
        from . import qbit
        qbit.client().torrents_stop(torrent_hashes=infohash)

    def remove(self, hashes) -> None:
        from . import qbit
        qbit.remove(_hashes(hashes))

    def finalize(self, infohash: str) -> bool:
        self.remove(infohash)  # qBittorrent writes before it reports a piece done
        return True

    def interfaces(self) -> dict:
        from . import qbit
        return qbit.interfaces()

    def bind_interface(self, value: str) -> None:
        from . import qbit
        qbit.bind_interface(value)

    def health(self) -> dict:
        from . import qbit
        try:
            return {"ok": True, "version": qbit.client().app.version.lstrip("v"), "error": None}
        except Exception as e:
            return {"ok": False, "version": None, "error": str(e) or type(e).__name__}

    def close(self) -> None:
        from . import qbit
        qbit.reset()


# -- built-in (libtorrent) -------------------------------------------------------
def _iface_ip(name: str) -> str | None:
    """The interface's IPv4 address while it is up, else None (VPN down)."""
    import socket

    import psutil
    stats = psutil.net_if_stats().get(name)
    if not stats or not stats.isup:
        return None
    return next((a.address for a in psutil.net_if_addrs().get(name, []) if a.family == socket.AF_INET), None)


def list_interfaces() -> list[dict]:
    import socket

    import psutil
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        ip = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        if ip and not ip.startswith("127.") and stats.get(name) and stats[name].isup:
            out.append({"name": f"{name} ({ip})", "value": name})
    return out


def _listen_port() -> int:
    """A random high port, picked once and kept (stable for port mapping and firewall rules)."""
    port = int(db.setting("bt_port") or 0)
    if not port:
        port = random.randint(49152, 65000)
        db.set_settings({"bt_port": port})
    return port


def _layout(ti):
    return ti.layout() if hasattr(ti, "layout") else ti.files()  # files() is deprecated in libtorrent 2.1


_LT_STATES = {"queued_for_checking": "checkingDL", "checking_files": "checkingDL", "checking_resume_data": "checkingResumeData",
              "downloading_metadata": "metaDL", "allocating": "allocating", "finished": "uploading", "seeding": "uploading"}


class LibtorrentEngine:
    """One libtorrent session owned by the service process. DHT, PEX, LSD and encryption on; resume data
    in DATA_DIR/torrents. With an interface chosen, listen and outgoing sockets bind to its address only;
    if it goes away the session pauses (kill switch) and never falls back to another interface."""
    name = "builtin"
    SAVE_EVERY = 60

    def __init__(self, resume_dir: Path | None = None, interface: str | None = None, port: int | None = None,
                 overrides: dict | None = None):
        import libtorrent as lt
        self.lt = lt
        self.resume_dir = resume_dir or paths.DATA_DIR / "torrents"
        self.resume_dir.mkdir(parents=True, exist_ok=True)
        self.interface = db.setting("bt_interface") or "" if interface is None else interface
        self.port = _listen_port() if port is None else port
        self.blocked: str | None = None  # why transfers are stopped (VPN interface gone)
        self.bound_ip: str | None = None
        self._overrides = overrides or {}
        self._stop = threading.Event()
        self._pending_saves = 0
        self._last_save = time.monotonic()
        self._waits: dict[tuple[str, str], threading.Event] = {}  # (kind, hash) -> set by the alert loop
        # bound to a VPN: start paused on loopback, so nothing leaves before the interface is checked
        self.session = lt.session(self._settings("127.0.0.1" if self.interface else None))
        if self.interface:
            self.session.pause()
        self._apply_binding()
        self._restore()
        self._thread = threading.Thread(target=self._alert_loop, name="libtorrent", daemon=True)
        self._thread.start()

    def _settings(self, ip: str | None) -> dict:
        lt = self.lt
        listen = f"{ip}:{self.port}" if ip else f"0.0.0.0:{self.port},[::]:{self.port}"
        s = {"user_agent": f"anidash/{__version__} libtorrent/{lt.__version__}",
             "alert_mask": int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage),
             "enable_dht": True, "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True,
             "out_enc_policy": int(lt.enc_policy.enabled), "in_enc_policy": int(lt.enc_policy.enabled),
             "allowed_enc_level": int(lt.enc_level.both),
             "listen_interfaces": listen, "outgoing_interfaces": ip or ""}
        if ip:  # port mapping would announce the router's address, outside the VPN
            s.update(enable_upnp=False, enable_natpmp=False, enable_lsd=False)
        s.update(self._overrides)
        return s

    def _apply_binding(self) -> None:
        """Re-checked from the alert loop: follows the VPN's address, pauses everything while it is gone."""
        if not self.interface:
            ip = None
        else:
            ip = _iface_ip(self.interface)
            if ip is None:
                if not self.blocked:
                    self.blocked = f"VPN interface {self.interface} is down; downloads are stopped until it is back"
                    self.session.pause()
                    log.warning(self.blocked)
                return
        if ip != self.bound_ip or self.blocked:
            self.session.apply_settings(self._settings(ip))
            self.bound_ip = ip
            if self.blocked:
                log.info("VPN interface %s is back (%s); resuming downloads", self.interface, ip)
                self.blocked = None
            self.session.resume()

    # -- resume data
    def _resume_file(self, h: str) -> Path:
        return self.resume_dir / f"{h}.fastresume"

    def _restore(self) -> None:
        for f in sorted(self.resume_dir.glob("*.fastresume")):
            try:
                p = self.lt.read_resume_data(f.read_bytes())
                self.session.add_torrent(p)
            except Exception as e:  # a corrupt file must not stop the others
                log.warning("could not restore %s: %s", f.name, e)

    def _save(self, handle, flags=0) -> None:
        handle.save_resume_data(flags | self.lt.torrent_handle.save_info_dict)
        self._pending_saves += 1

    def _alert_loop(self) -> None:
        lt = self.lt
        while not self._stop.is_set():
            self.session.wait_for_alert(1000)
            for a in self.session.pop_alerts():
                try:
                    self._on_alert(a)
                except Exception:
                    log.exception("alert %s", type(a).__name__)
            with contextlib.suppress(Exception):
                self._apply_binding()
            if time.monotonic() - self._last_save > self.SAVE_EVERY:
                self._last_save = time.monotonic()
                for h in self.session.get_torrents():
                    if h.need_save_resume_data():
                        self._save(h, lt.torrent_handle.only_if_modified)

    def _on_alert(self, a) -> None:
        lt = self.lt
        if isinstance(a, lt.save_resume_data_alert):
            self._pending_saves -= 1
            h = str(a.params.info_hashes.v1)
            if self.session.find_torrent(a.params.info_hashes.v1).is_valid():  # not removed meanwhile
                self._resume_file(h).write_bytes(lt.write_resume_data_buf(a.params))
        elif isinstance(a, lt.save_resume_data_failed_alert):
            self._pending_saves -= 1
        elif isinstance(a, lt.torrent_finished_alert):  # no seeding: stop until jobs renames and removes it
            a.handle.unset_flags(lt.torrent_flags.auto_managed)
            a.handle.pause()
            self._save(a.handle)
        elif isinstance(a, lt.metadata_received_alert):
            self._save(a.handle)
        elif isinstance(a, lt.cache_flushed_alert):
            self._signal("flushed", str(a.handle.info_hashes().v1))
        elif isinstance(a, lt.torrent_removed_alert):
            self._signal("removed", str(a.info_hashes.v1))
        elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert, lt.file_rename_failed_alert)):
            log.warning("libtorrent: %s", a.message())

    def _signal(self, kind: str, h: str) -> None:
        ev = self._waits.get((kind, h))
        if ev:
            ev.set()

    def _wait_for(self, kind: str, h: str, start, timeout: float) -> bool:
        ev = self._waits.setdefault((kind, h), threading.Event())
        ev.clear()
        try:
            start()
            return ev.wait(timeout)
        finally:
            self._waits.pop((kind, h), None)

    # -- engine interface
    def _handle(self, infohash: str):
        h = self.session.find_torrent(self.lt.sha1_hash(bytes.fromhex(infohash)))
        return h if h.is_valid() else None

    def add(self, infohash: str, name: str, save_path: str, anilist_id: int) -> None:
        if self._handle(infohash.lower()):
            return
        p = self.lt.parse_magnet_uri(magnet(infohash, name))
        p.save_path = save_path
        p.flags &= ~self.lt.torrent_flags.paused
        self._save(self.session.add_torrent(p))  # remembered even before metadata arrives

    def torrents(self) -> list[Torrent]:
        lt, out = self.lt, []
        for h in self.session.get_torrents():
            st = h.status()
            left = st.total_wanted - st.total_wanted_done if st.has_metadata else 1
            state = _LT_STATES.get(str(st.state).rsplit(".", 1)[-1], "downloading")
            if st.errc.value():
                state = "error"
            elif st.flags & lt.torrent_flags.paused and left > 0:
                state = "stoppedDL"
            elif state == "downloading" and st.num_peers == 0:
                state = "stalledDL"
            eta = int(left / st.download_rate) if st.download_rate > 0 and left > 0 else None
            files = []
            if st.has_metadata:
                fs = _layout(h.torrent_file())
                files = [File(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())]
            out.append(Torrent(str(st.info_hashes.v1), st.name, state, 1.0 if st.has_metadata and left == 0 else st.progress,
                               st.save_path, left, eta, st.download_rate, files))
        return out

    def files(self, infohash: str) -> list[File]:
        return next((t.files for t in self.torrents() if t.hash == infohash.lower()), [])

    def rename_file(self, infohash: str, old: str, new: str) -> None:
        h = self._handle(infohash.lower())
        fs = _layout(h.torrent_file())
        idx = next(i for i in range(fs.num_files()) if fs.file_path(i) == old)
        h.rename_file(idx, new)

    def stop(self, infohash: str) -> None:
        h = self._handle(infohash.lower())
        if h:
            h.unset_flags(self.lt.torrent_flags.auto_managed)
            h.pause()

    def remove(self, hashes) -> None:
        for ih in _hashes(hashes):
            h = self._handle(ih)
            if h:
                self.session.remove_torrent(h)  # files stay; jobs trashes them itself when needed
            self._resume_file(ih).unlink(missing_ok=True)

    def finalize(self, infohash: str, timeout: float = 15) -> bool:
        """Hand a finished torrent's file over: libtorrent can report every piece verified while some are
        still in its write buffer, so flush them (which also closes the file handles), then remove the torrent.
        False if the flush isn't confirmed in time; the torrent stays (paused) and jobs tries again later."""
        ih = infohash.lower()
        h = self._handle(ih)
        if h is None:
            return True
        if not self._wait_for("flushed", ih, h.flush_cache, timeout):
            log.warning("libtorrent did not confirm writing %s yet", ih)
            return False
        self._wait_for("removed", ih, lambda: self.session.remove_torrent(h), 5)
        self._resume_file(ih).unlink(missing_ok=True)
        return True

    def interfaces(self) -> dict:
        return {"current": self.interface, "list": list_interfaces(), "bypass_local_auth": False,
                "version": self.lt.__version__, "blocked": self.blocked}

    def bind_interface(self, value: str) -> None:
        db.set_settings({"bt_interface": value})
        self.interface, self.bound_ip, self.blocked = value, None, None
        if not value:
            self.session.apply_settings(self._settings(None))
            self.session.resume()
        self._apply_binding()

    def health(self) -> dict:
        return {"ok": not self.blocked, "version": self.lt.__version__, "error": self.blocked,
                "port": self.session.listen_port() if self.session.is_listening() else None}

    def close(self, timeout: float = 5) -> None:
        """Save resume data for every torrent, then stop the session."""
        for h in self.session.get_torrents():
            self._save(h)
        deadline = time.monotonic() + timeout
        while self._pending_saves > 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        self.session.post_torrent_updates()  # an alert, so the loop wakes now instead of after its 1 s wait
        self._thread.join(timeout=3)
        self.session.pause()


# -- selection -------------------------------------------------------------------
_current = None
_lock = threading.Lock()


def get():
    """The engine the settings ask for; switching in Settings takes effect on the next call."""
    global _current
    want = db.setting("engine")
    with _lock:
        if _current is None or _current.name != want:
            if _current is not None:
                with contextlib.suppress(Exception):
                    _current.close()
            _current = LibtorrentEngine() if want == "builtin" else QbitEngine()
        return _current


def close() -> None:
    global _current
    with _lock:
        if _current is not None:
            with contextlib.suppress(Exception):
                _current.close()
            _current = None
