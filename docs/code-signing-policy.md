# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

> Status: anidash has applied (or will apply after v2.0.0) to the SignPath Foundation program. Until a release says it is signed, the Windows installer is **unsigned**, and Windows SmartScreen shows "Windows protected your PC": choose *More info → Run anyway* (see the README's Windows FAQ).

## What gets signed

Only the Windows installer `anidash-<version>-setup.exe` and the program files inside it (`anidash.exe` and its bundled libraries) are signed. They are built from this repository by GitHub Actions ([`.github/workflows/release.yml`](../.github/workflows/release.yml)) when a version tag `v*` is pushed, and nowhere else:

1. PyInstaller builds `anidash.exe` from the tagged source (`packaging/windows/anidash.spec`), and the build is self-tested (`anidash selftest`).
2. Inno Setup builds the installer (`packaging/windows/anidash.iss`). The only third-party binary added at that step is Microsoft's WebView2 bootstrapper, downloaded from Microsoft and checked to carry Microsoft's own Authenticode signature.
3. The installer is submitted to SignPath for signing, and the signed file is attached to the GitHub Release together with `SHA256SUMS.txt`.

Nothing built on a developer's computer is ever signed.

## Team roles

| Role | Members |
|---|---|
| Committers and reviewers | [@rafaefarrukh](https://github.com/rafaefarrukh) |
| Approvers | [@rafaefarrukh](https://github.com/rafaefarrukh) |

- **Committers** may change the source code in this repository.
- **Reviewers:** every change proposed by someone who is not a committer is reviewed by a committer before it is merged.
- **Approvers:** every signing request is approved by an approver, after checking that it was built by the release workflow from a tag on `main`.

All team members use multi-factor authentication for GitHub and for SignPath.

## Privacy policy

anidash runs on your computer and has no server of its own. It sends nothing to the anidash developers, has no telemetry or crash reporting, and shows no ads. It contacts only these services, all to do what you set it up to do:

| Service | When | What it sends |
|---|---|---|
| [AniList](https://anilist.co) (`graphql.anilist.co`) | syncing your list, browsing seasons, after a download | your AniList access token (to read your Watching list and update progress) |
| [Nyaa](https://nyaa.si) (or the base URL you set) | searching for releases | the search terms for the shows you track |
| BitTorrent peers, DHT and the trackers in the magnet links | downloading an episode | what any BitTorrent client sends: your IP address and the torrents you download (bind to a VPN interface in Settings to hide your IP) |
| Your own qBittorrent (`127.0.0.1`), only if you chose it | downloading | the torrents to add |
| [GitHub](https://github.com/rafaefarrukh/anidash/releases) (`api.github.com`) | the update check, monthly (Settings → *Check for updates* can turn it off) and when you press *Check now* or *Update* | a normal HTTPS request with the user agent `anidash/<version>` |
| Microsoft (WebView2 installer) | only during installation on Windows, if WebView2 is missing | Microsoft's installer contacts Microsoft's servers |

Your AniList token and any qBittorrent password are stored in the operating system's credential store (Windows Credential Manager, GNOME Keyring / KWallet), never in anidash's database or logs. The dashboard is served on `127.0.0.1` only and is not reachable from other computers.
