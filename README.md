# 🩺 DevPulse




https://github.com/user-attachments/assets/323ebb0d-98d5-49d6-9cf4-5bf13993fcb2


**A terminal dashboard that keeps your dev environment healthy.**
Scan your projects, reclaim disk space, catch leaked API keys before you push, and shut down forgotten local servers, all from one TUI.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

<!-- Add a screenshot or GIF of the TUI here, e.g. docs/screenshot.png -->
<!-- ![DevPulse screenshot](docs/screenshot.png) -->

---

## ✨ Features

| Module | What it does |
|---|---|
| 🩺 **Health** | Project overview: detected stack, git status, disk usage, file types. |
| 🧹 **Cleanup** | Finds heavy regenerable folders (`node_modules`, `.venv`, `target/`, caches) and deletes the ones you select. |
| 🔐 **Secrets** | Scans git-tracked files for hardcoded API keys (OpenAI, AWS, Stripe, GitHub, Google) and private keys. |
| ⚡ **Processes** | Lists local servers listening on ports (3000, 5173, 8080…) and stops them instantly. |

---

## 📦 Installation

**Requirements:** Python 3.9+ and Git (optional, but needed for the Health git info and the Secrets scan).

```bash
git clone https://github.com/<your-username>/devpulse.git
cd devpulse
pip install -r requirements.txt
```

---

## 🚀 Usage

```bash
# Open the TUI on a folder (defaults to the current directory)
python devpulse.py ~/projects

# Windows example
python devpulse.py "C:\Users\you\Desktop\projects"

# Secrets check only (no UI), for scripts and git hooks
python devpulse.py ~/projects/my-app --check
```

> 💡 Point DevPulse at a parent folder (e.g. `~/projects`) to scan all your projects at once.
> Scanning the whole disk (`/` or `C:\`) works but is slow.

### ⌨️ Keyboard shortcuts

| Key | Action |
|---|---|
| `1` `2` `3` `4` | Switch tab: Health, Cleanup, Secrets, Processes |
| `r` | Refresh the current scan |
| `space` | Select / deselect a folder (Cleanup tab) |
| `d` | Delete the selected folders (asks for confirmation) |
| `s` | Run the secrets scan (Secrets tab) |
| `k` | Stop the selected process (asks for confirmation) |
| `q` | Quit |

---

## 🧹 Cleanup: what gets detected

A folder is only offered for deletion if its parent proves it belongs to a project. This prevents deleting an unrelated `target/` or `venv/` folder by accident.

| Folder | Detected only if… |
|---|---|
| `node_modules/` | a `package.json` exists beside it |
| `.venv/`, `venv/` | a `pyvenv.cfg` exists inside it (real virtual env) |
| `target/` | a `Cargo.toml` or `pom.xml` exists beside it |
| `.next/` | a `package.json` exists beside it |
| `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` | always |
| `.turbo/`, `.parcel-cache/` | always |

**Safety guarantees:**
- Only folders inside the scanned path can be deleted.
- Symbolic links are never followed or deleted.
- DevPulse never descends into a folder it has marked for cleanup.
- A confirmation dialog appears before anything is deleted.

Everything deleted is regenerable (`npm install`, `pip install -r requirements.txt`, `cargo build`…).

---

## 🔐 Secrets: what gets detected

| Type | Pattern |
|---|---|
| OpenAI | `sk-…`, `sk-proj-…`, `sk-svcacct-…` |
| AWS | Access keys `AKIA…` / `ASIA…`, and secret keys assigned next to `aws…secret/key` |
| Stripe | `sk_live_…`, `sk_test_…`, `rk_live_…`, `rk_test_…` |
| GitHub | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` tokens |
| Google | `AIza…` API keys |
| Private keys | `-----BEGIN … PRIVATE KEY-----` |

**What is scanned:** files that git would publish, meaning tracked files plus untracked files that are not ignored. Files ignored through `.gitignore` (such as `.env`) are skipped, so a properly ignored secret is not reported. Binary files and files larger than 1 MB are skipped.

**Secrets are never displayed in full.** Only the first 6 and last 4 characters are shown.

> ⚠️ DevPulse is a fast safety net, not a full audit. For thorough history scanning, use a dedicated tool such as [gitleaks](https://github.com/gitleaks/gitleaks) or [trufflehog](https://github.com/trufflesecurity/trufflehog).
> If a key has already been pushed, **revoke it**. Deleting the commit is not enough.

---

## ⚡ Processes: how it works

- **macOS / Linux:** uses `lsof`, which is installed by default on most systems.
- **Windows:** uses `netstat` and `tasklist`, both built in. Works with localized Windows (for example, French `ÉCOUTE` instead of `LISTENING`).

Stopping a process:
- **macOS / Linux:** sends `SIGTERM`, waits 2 seconds, then sends `SIGKILL` if the process is still running.
- **Windows:** terminates the process directly.

Ports in the common dev range (3000, 3001, 4000, 4200, 5000, 5173, 5500, 8000, 8080, 8081, 8888, 9000) are highlighted in yellow. DevPulse refuses to stop its own process.

---

## 🪝 Use it as a pre-push hook

Block a push if secrets are found:

```sh
# .git/hooks/pre-push   (then: chmod +x .git/hooks/pre-push)
#!/bin/sh
python3 /path/to/devpulse.py "$(git rev-parse --show-toplevel)" --check || exit 1
```

On Windows, Git for Windows runs hooks through its built-in shell, so the same script works. Use the full path to `devpulse.py`.

`--check` prints every finding and exits with code `1` when a secret is detected, which blocks the push.

---

## 🗂️ Project structure

```
devpulse/
├── devpulse.py       # the whole application (single file)
├── requirements.txt  # textual, rich
├── README.md
└── LICENSE
```

---

## 🧩 Built with

- [Textual](https://github.com/Textualize/textual): terminal UI framework
- [Rich](https://github.com/Textualize/rich): rendering

---

## 🛣️ Roadmap

- [ ] Dry-run mode: preview what would be deleted
- [ ] `.devpulseignore` file for custom exclusions
- [ ] Link each listening port to the project it belongs to
- [ ] Configurable secret patterns
- [ ] Packaged install (`pipx install devpulse`)

Ideas and pull requests are welcome.

---

## 🤝 Contributing

1. Fork the repository
2. Create a branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push and open a pull request

---

## ⚠️ Disclaimer

DevPulse deletes folders and stops processes. Review the selection before confirming, and keep your work committed. The authors are not responsible for data loss.

---

## 📄 License

Released under the [MIT License](LICENSE).
<img width="1092" height="582" alt="Capture d&#39;écran 2026-11-01 184603" src="https://github.com/user-attachments/assets/e1d3d8b4-78a5-4d73-9d89-9dcaba6efaf1" />
