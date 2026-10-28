# PyInstaller spec: one folder, windowed. Build from the repo root:
#   pyinstaller packaging/windows/anidash.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("uvicorn") + collect_submodules("webview") + collect_submodules("pystray")
          + collect_submodules("keyring.backends") + ["apscheduler.triggers.cron", "apscheduler.triggers.interval"])
datas = [("../../src/anidash/web", "anidash/web"), ("../../src/anidash/assets", "anidash/assets")]
datas += collect_data_files("webview")

a = Analysis(["launcher.py"], pathex=["../../src"], hiddenimports=hidden, datas=datas,
             excludes=["tkinter", "pytest", "PyQt5", "PyQt6", "PySide6"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="anidash", console=False,
          icon="../../src/anidash/assets/icon.ico", version="version_info.txt")
coll = COLLECT(exe, a.binaries, a.datas, name="anidash")
