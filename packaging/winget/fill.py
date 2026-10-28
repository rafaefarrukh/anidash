"""Fill the winget manifest templates from a published GitHub Release, for a PR to microsoft/winget-pkgs.

  python packaging/winget/fill.py 2.0.0                  # reads SHA256SUMS.txt from the release
  python packaging/winget/fill.py 2.0.0 --sums dist/SHA256SUMS.txt --date 2026-11-02

Writes packaging/winget/out/manifests/r/RafaeFarrukh/anidash/<version>/ (the layout winget-pkgs uses).
Then: `winget validate --manifest <that folder>` on Windows, and open the PR (docs/PLAN.md, M7)."""
import argparse
import datetime
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASE = "https://github.com/rafaefarrukh/anidash/releases/download/v{version}/{name}"


def sha256_for(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].upper()  # winget-pkgs uses upper case
    raise SystemExit(f"{name} is not in SHA256SUMS.txt")


def fill(version: str, sums: str, date: str, out: Path) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"winget gets final releases only, not {version!r}")
    name = f"anidash-{version}-setup.exe"
    values = {"version": version, "date": date, "installer_url": RELEASE.format(version=version, name=name),
              "sha256": sha256_for(sums, name)}
    dest = out / "manifests" / "r" / "RafaeFarrukh" / "anidash" / version
    dest.mkdir(parents=True, exist_ok=True)
    for tpl in sorted(HERE.glob("*.yaml.in")):
        text = tpl.read_text()
        for k, v in values.items():
            text = text.replace("{" + k + "}", v)
        (dest / tpl.name.removesuffix(".in")).write_text(text)
    return dest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version")
    ap.add_argument("--sums", help="local SHA256SUMS.txt (default: download it from the release)")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="release date, YYYY-MM-DD")
    ap.add_argument("--out", default=str(HERE / "out"))
    a = ap.parse_args(argv)
    if a.sums:
        sums = Path(a.sums).read_text()
    else:
        with urllib.request.urlopen(RELEASE.format(version=a.version, name="SHA256SUMS.txt"), timeout=30) as r:
            sums = r.read().decode()
    print(fill(a.version, sums, a.date, Path(a.out)))


if __name__ == "__main__":
    sys.exit(main())
