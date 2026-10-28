"""Writes version_info.txt (Windows file properties) from anidash.__version__."""
import pathlib
import re
import sys

src = pathlib.Path(__file__).resolve().parents[2] / "src" / "anidash" / "__init__.py"
ver = re.search(r'__version__ = "([^"]+)"', src.read_text())[1]
nums = (list(map(int, re.findall(r"\d+", ver))) + [0, 0, 0, 0])[:4]
t = tuple(nums)
pathlib.Path(__file__).with_name("version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'anidash'), StringStruct('FileDescription', 'anidash'),
    StringStruct('FileVersion', '{ver}'), StringStruct('ProductName', 'anidash'),
    StringStruct('ProductVersion', '{ver}'), StringStruct('LegalCopyright', 'MIT License')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")
if "--print" in sys.argv:
    print(ver)
elif "--print-num" in sys.argv:  # Inno's VersionInfoVersion wants four numbers (2.0.0rc1 -> 2.0.0.1)
    print(".".join(map(str, t)))
