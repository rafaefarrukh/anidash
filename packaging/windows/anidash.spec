# PyInstaller spec: one folder, windowed. Build from the repo root:
#   pyinstaller packaging/windows/anidash.spec --noconfirm
# `anidash.exe selftest out.json` checks the pieces below that only load at runtime.
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

hidden = (collect_submodules("uvicorn") + collect_submodules("webview") + collect_submodules("pystray")
          + collect_submodules("keyring.backends") + collect_submodules("send2trash")
          + ["apscheduler.triggers.cron", "apscheduler.triggers.interval"])
datas = [("../../src/anidash/web", "anidash/web"), ("../../src/anidash/assets", "anidash/assets")]
datas += collect_data_files("webview")  # includes webview/lib: the WebView2 interop DLLs
binaries = []
for pkg in ("pythonnet", "clr_loader", "libtorrent"):  # winforms backend (Python.Runtime.dll + .NET loader); libtorrent's .pyd
    d, b, h = collect_all(pkg)
    datas, binaries, hidden = datas + d, binaries + b, hidden + h

a = Analysis(["launcher.py"], pathex=["../../src"], hiddenimports=hidden, datas=datas, binaries=binaries,
             excludes=["tkinter", "pytest", "PyQt5", "PyQt6", "PySide6"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="anidash", console=False,
          icon="../../src/anidash/assets/icon.ico", version="version_info.txt")
coll = COLLECT(exe, a.binaries, a.datas, name="anidash")
