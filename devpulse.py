#!/usr/bin/env python3
"""
DevPulse — health check for your development environment.

  - Health     : project summary (stack, git, disk, file types)
  - Cleanup    : node_modules, venv, target/, caches... remove them in one go
  - Secrets    : hardcoded API keys (OpenAI, AWS, Stripe, GitHub...) in tracked files
  - Processes  : local servers listening on ports (3000, 8080...) and instant kill

Usage:
    python devpulse.py [path]            # TUI
    python devpulse.py [path] --check    # secrets scan only, non-zero exit code if found (pre-push)

Dependencies: pip install textual rich
Works on Windows, macOS and Linux.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
ROOT = Path(ARGS[0] if ARGS else os.getcwd()).expanduser().resolve()
CHECK_ONLY = "--check" in sys.argv
IS_WINDOWS = os.name == "nt"

SKIP_DIRS = {".git", ".hg", ".svn"}
MAX_SCAN_BYTES = 1_000_000
DEV_PORTS = {3000, 3001, 4000, 4200, 5000, 5173, 5500, 8000, 8080, 8081, 8888, 9000}


# --------------------------------------------------------------------------- #
# Cleanup targets: regenerable folders.
# A target is only offered if its parent folder proves it belongs to a project
# (e.g. node_modules only when package.json exists).
# --------------------------------------------------------------------------- #
def _has(*names: str):
    def check(parent: Path) -> bool:
        return any((parent / n).exists() for n in names)
    return check


def _always(_parent: Path) -> bool:
    return True


CLEAN_TARGETS = {
    "node_modules": _has("package.json"),
    ".venv": _has("pyvenv.cfg"),
    "venv": _has("pyvenv.cfg"),
    "target": _has("Cargo.toml", "pom.xml"),
    ".next": _has("package.json"),
    "__pycache__": _always,
    ".pytest_cache": _always,
    ".mypy_cache": _always,
    ".ruff_cache": _always,
    ".turbo": _always,
    ".parcel-cache": _always,
}


# --------------------------------------------------------------------------- #
# Secret detection (specific patterns to limit false positives)
# --------------------------------------------------------------------------- #
SECRET_PATTERNS = {
    "OpenAI": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    "AWS Access Key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS Secret": re.compile(
        r"(?i)aws[_\-\w]{0,20}(?:secret|key)[\w\-]{0,20}['\"]?\s*[:=]\s*['\"][A-Za-z0-9/+=]{40}['\"]"
    ),
    "Stripe": re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    "GitHub Token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    "Google API": re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "Private Key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}

STACK_MARKERS = {
    "package.json": "Node/JS",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "Gemfile": "Ruby",
    "pom.xml": "Java",
    "docker-compose.yml": "Docker",
    "Dockerfile": "Docker",
}


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bar(pct: float, width: int = 30) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def mask(secret: str) -> str:
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 14 else "***"


def walk_files(root: Path, prune: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in prune]
        for f in filenames:
            yield Path(dirpath, f)


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def find_reclaimable(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        keep = []
        for d in dirnames:
            if d in SKIP_DIRS:
                continue
            path = Path(dirpath, d)
            if d in CLEAN_TARGETS:
                if CLEAN_TARGETS[d](path.parent):
                    found.append(path)
                continue  # never descend into a cleanup target
            keep.append(d)
        dirnames[:] = keep
    return found


def git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def candidate_files(root: Path) -> list[str]:
    """Tracked files plus files not ignored by git (what would go out on push)."""
    out = git(root, "ls-files", "-co", "--exclude-standard", "-z")
    if out is not None:
        return [f for f in out.split("\0") if f]
    return [str(p.relative_to(root)) for p in walk_files(root, SKIP_DIRS | set(CLEAN_TARGETS))]


def scan_secrets(root: Path) -> list[tuple[str, int, str, str]]:
    findings = []
    for rel in candidate_files(root):
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:  # binary file
            continue
        text = data.decode("utf-8", errors="ignore")
        for kind, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                findings.append((rel, line, kind, mask(m.group(0))))
    return findings


def _listening_macos_linux() -> list[tuple[int, int, str, str]]:
    try:
        r = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("lsof is not installed on this system") from exc

    rows: dict[tuple[int, int], tuple[int, int, str, str]] = {}
    pid: int | None = None
    cmd = ""
    for line in r.stdout.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid, cmd = int(val), ""
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            addr, _, port = val.rpartition(":")
            if port.isdigit():
                rows.setdefault((pid, int(port)), (int(port), pid, cmd, addr))
    return sorted(rows.values())


def _listening_windows() -> list[tuple[int, int, str, str]]:
    try:
        # errors="replace": Windows console output uses OEM code pages (accents in French).
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        ).stdout or ""
        tasks = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        ).stdout or ""
    except FileNotFoundError as exc:
        raise RuntimeError("netstat/tasklist not available") from exc

    names: dict[int, str] = {}
    for row in csv.reader(tasks.splitlines()):
        if len(row) >= 2 and row[1].isdigit():
            names[int(row[1])] = row[0]

    rows: dict[tuple[int, int], tuple[int, int, str, str]] = {}
    for line in netstat.splitlines():
        parts = line.split()
        # Expected: TCP  <local addr>  <foreign addr>  <state>  <pid>
        # A listening socket has no real peer (foreign port 0 or *). The state word is
        # localized (LISTENING / ÉCOUTE), so we don't rely on it.
        if (len(parts) == 5 and parts[0] == "TCP" and parts[4].isdigit()
                and parts[2].rpartition(":")[2] in ("0", "*")):
            addr, _, port = parts[1].rpartition(":")
            if port.isdigit():
                pid = int(parts[4])
                rows.setdefault((pid, int(port)), (int(port), pid, names.get(pid, "?"), addr))
    return sorted(rows.values())


def listening_processes() -> list[tuple[int, int, str, str]]:
    return _listening_windows() if IS_WINDOWS else _listening_macos_linux()


def stop_pid(pid: int) -> str:
    try:
        if IS_WINDOWS:
            # On Windows, os.kill terminates the process directly (TerminateProcess).
            os.kill(pid, signal.SIGTERM)
            return "terminated"
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return "stopped (SIGTERM)"
        os.kill(pid, signal.SIGKILL)
        return "force-stopped (SIGKILL)"
    except ProcessLookupError:
        return "already stopped"
    except PermissionError:
        return "permission denied"
    except OSError as exc:
        return f"failed: {exc}"


def _clear_readonly(func, path, _exc_info) -> None:
    """Windows: node_modules and .git contain read-only files that rmtree can't delete."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def health_snapshot(root: Path) -> dict:
    files, size, exts = 0, 0, Counter()
    for p in walk_files(root, SKIP_DIRS | set(CLEAN_TARGETS)):
        try:
            st = p.lstat()
        except OSError:
            continue
        files += 1
        size += st.st_size
        exts[p.suffix.lower() or "(no ext)"] += 1

    du = shutil.disk_usage(root)
    branch = (git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    status = git(root, "status", "--porcelain")
    dirty = len([line for line in status.splitlines() if line]) if status is not None else None
    stacks = sorted({v for k, v in STACK_MARKERS.items() if (root / k).exists()})
    return {
        "files": files, "size": size, "exts": exts,
        "total": du.total, "used": du.used, "free": du.free,
        "branch": branch, "dirty": dirty, "stacks": stacks,
    }


def render_health(s: dict) -> Panel:
    pct = s["used"] / s["total"] * 100 if s["total"] else 0
    if s["branch"] is None:
        git_line = "not a git repository"
    else:
        git_line = f"{s['branch']} · {s['dirty']} modified file(s)"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column()
    grid.add_row("Project", Text(str(ROOT)))
    grid.add_row("Stack", Text(", ".join(s["stacks"]) or "not detected"))
    grid.add_row("Files", Text(f"{s['files']:,} · {human(s['size'])} (caches excluded)"))
    grid.add_row("Git", Text(git_line))
    grid.add_row("Disk", Text(f"{bar(pct)} {pct:.0f} % · {human(s['free'])} free"))
    grid.add_row("Types", Text(", ".join(f"{e} ({n})" for e, n in s["exts"].most_common(6)) or "—"))
    return Panel(grid, title="🩺 DevPulse · project health", border_style="magenta", padding=(1, 2))


# --------------------------------------------------------------------------- #
# Confirmation modal
# --------------------------------------------------------------------------- #
class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #dialog {
        width: 70; height: auto; padding: 1 2;
        border: thick $error; background: $panel;
    }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.message))
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="no")
                yield Button("Confirm", variant="error", id="yes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
class DevPulse(App):
    TITLE = "DevPulse"
    SUB_TITLE = str(ROOT)
    CSS = """
    #health-view { padding: 1 2; }
    #clean-summary, #secret-summary, #proc-summary { padding: 0 2; height: auto; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Refresh"),
        Binding("space", "toggle", "Select"),
        Binding("d", "clean", "Clean"),
        Binding("s", "secrets", "Scan secrets"),
        Binding("k", "kill", "Kill"),
        Binding("1", "tab('health')", "Health", show=False),
        Binding("2", "tab('clean')", "Cleanup", show=False),
        Binding("3", "tab('secrets')", "Secrets", show=False),
        Binding("4", "tab('procs')", "Processes", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.clean_items: list[tuple[Path, int]] = []
        self.clean_selected: set[int] = set()
        self.procs: list[tuple[int, int, str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="health"):
            with TabPane("🩺 Health", id="health"):
                yield Static("Analyzing…", id="health-view")
            with TabPane("🧹 Cleanup", id="clean"):
                yield Static("", id="clean-summary")
                yield DataTable(id="clean-table", cursor_type="row")
            with TabPane("🔐 Secrets", id="secrets"):
                yield Static("Press [s] to scan files before pushing.", id="secret-summary")
                yield DataTable(id="secret-table", cursor_type="row")
            with TabPane("⚡ Processes", id="procs"):
                yield Static("", id="proc-summary")
                yield DataTable(id="proc-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#clean-table", DataTable).add_columns("Sel", "Folder", "Size", "Path")
        self.query_one("#secret-table", DataTable).add_columns("File", "Line", "Type", "Snippet")
        self.query_one("#proc-table", DataTable).add_columns("Port", "PID", "Command", "Address")
        self.action_rescan()

    # ---- UI helpers ------------------------------------------------------- #
    def active_tab(self) -> str:
        return self.query_one(TabbedContent).active

    def action_tab(self, name: str) -> None:
        self.query_one(TabbedContent).active = name

    def action_rescan(self) -> None:
        self.load_health()
        self.load_clean()
        self.load_procs()

    # ---- Health ----------------------------------------------------------- #
    @work(thread=True, exclusive=True, group="health")
    def load_health(self) -> None:
        snap = health_snapshot(ROOT)
        self.call_from_thread(self._show_health, snap)

    def _show_health(self, snap: dict) -> None:
        self.query_one("#health-view", Static).update(render_health(snap))

    # ---- Cleanup ---------------------------------------------------------- #
    @work(thread=True, exclusive=True, group="clean")
    def load_clean(self) -> None:
        self.call_from_thread(
            self.query_one("#clean-summary", Static).update,
            Text("Scanning for heavy folders…"),
        )
        items = [(p, dir_size(p)) for p in find_reclaimable(ROOT)]
        items.sort(key=lambda x: -x[1])
        self.call_from_thread(self._show_clean, items)

    def _show_clean(self, items: list[tuple[Path, int]]) -> None:
        self.clean_items = items
        self.clean_selected.clear()
        table = self.query_one("#clean-table", DataTable)
        table.clear()
        for path, size in items:
            table.add_row("☐", path.name, human(size), str(path.relative_to(ROOT)))
        self._update_clean_summary()

    def _update_clean_summary(self) -> None:
        total = sum(s for _, s in self.clean_items)
        chosen = [self.clean_items[i][1] for i in self.clean_selected]
        self.query_one("#clean-summary", Static).update(Text(
            f"Reclaimable: {human(total)} · Selected: {len(chosen)} folder(s), {human(sum(chosen))}\n"
            "[space] select · [d] delete selection · [r] refresh"
        ))

    def action_toggle(self) -> None:
        if self.active_tab() != "clean" or not self.clean_items:
            return
        table = self.query_one("#clean-table", DataTable)
        row = table.cursor_row
        if row in self.clean_selected:
            self.clean_selected.discard(row)
            mark = "☐"
        else:
            self.clean_selected.add(row)
            mark = "☑"
        table.update_cell_at(Coordinate(row, 0), mark)
        self._update_clean_summary()

    def action_clean(self) -> None:
        if self.active_tab() != "clean":
            return
        if not self.clean_selected:
            self.notify("Select folders first with [space].", severity="warning")
            return
        targets = [self.clean_items[i][0] for i in sorted(self.clean_selected)]
        size = sum(self.clean_items[i][1] for i in self.clean_selected)
        msg = (
            f"Delete {len(targets)} folder(s) to free {human(size)}?\n"
            "These are dependencies or caches and can be regenerated (npm i, pip install, cargo build)."
        )
        self.push_screen(Confirm(msg), lambda ok: self._after_clean_confirm(ok, targets))

    def _after_clean_confirm(self, ok: bool, targets: list[Path]) -> None:
        if ok:
            self.delete_paths(targets)

    @work(thread=True, group="delete")
    def delete_paths(self, targets: list[Path]) -> None:
        root = ROOT.resolve()
        freed, errors = 0, []
        for p in targets:
            try:
                if p.is_symlink():
                    raise ValueError("symbolic links are refused")
                real = p.resolve()
                if real == root or root not in real.parents:
                    raise ValueError("outside the project, refused")
                size = dir_size(real)
                shutil.rmtree(real, onerror=_clear_readonly)
                freed += size
            except Exception as exc:  # keep going with the other folders
                errors.append(f"{p.name}: {exc}")
        if errors:
            self.call_from_thread(
                self.notify,
                f"Freed {human(freed)} · {len(errors)} error(s): {errors[0]}",
                severity="warning",
            )
        else:
            self.call_from_thread(self.notify, f"✔ Freed {human(freed)}")
        self.call_from_thread(self.load_clean)

    # ---- Secrets ---------------------------------------------------------- #
    def action_secrets(self) -> None:
        self.run_secrets()

    @work(thread=True, exclusive=True, group="secrets")
    def run_secrets(self) -> None:
        self.call_from_thread(
            self.query_one("#secret-summary", Static).update,
            Text("Scanning git-tracked files…"),
        )
        findings = scan_secrets(ROOT)
        self.call_from_thread(self._show_secrets, findings)

    def _show_secrets(self, findings: list[tuple[str, int, str, str]]) -> None:
        table = self.query_one("#secret-table", DataTable)
        table.clear()
        for rel, line, kind, snip in findings:
            table.add_row(rel, str(line), kind, snip)
        summary = self.query_one("#secret-summary", Static)
        if findings:
            summary.update(Text(
                f"⚠ {len(findings)} potential secret(s) — revoke them and remove them before git push",
                style="bold red",
            ))
        else:
            summary.update(Text("✔ No secrets detected in git-tracked files", style="bold green"))

    # ---- Processes -------------------------------------------------------- #
    @work(thread=True, exclusive=True, group="procs")
    def load_procs(self) -> None:
        try:
            procs, err = listening_processes(), None
        except Exception as exc:  # show the error in the UI rather than crash
            procs, err = [], f"{type(exc).__name__}: {exc}"
        self.call_from_thread(self._show_procs, procs, err)

    def _show_procs(self, procs: list[tuple[int, int, str, str]], err: str | None) -> None:
        self.procs = procs
        table = self.query_one("#proc-table", DataTable)
        table.clear()
        for port, pid, cmd, addr in procs:
            style = "bold yellow" if port in DEV_PORTS else ""
            table.add_row(Text(str(port), style=style), str(pid), cmd, addr)
        summary = self.query_one("#proc-summary", Static)
        if err:
            summary.update(Text(err, style="red"))
        else:
            dev = sum(1 for p in procs if p[0] in DEV_PORTS)
            summary.update(Text(
                f"{len(procs)} listening port(s) · {dev} dev port(s) (in yellow)\n"
                "[k] stop process · [r] refresh"
            ))

    def action_kill(self) -> None:
        if self.active_tab() != "procs":
            return
        row = self.query_one("#proc-table", DataTable).cursor_row
        if row is None or row >= len(self.procs):
            return
        port, pid, cmd, _addr = self.procs[row]
        if pid == os.getpid():
            self.notify("Refused: this is DevPulse itself.", severity="warning")
            return
        self.push_screen(
            Confirm(f"Stop '{cmd}' (PID {pid}) listening on port {port}?"),
            lambda ok: self._after_kill_confirm(ok, pid, cmd),
        )

    def _after_kill_confirm(self, ok: bool, pid: int, cmd: str) -> None:
        if ok:
            self.kill_process(pid, cmd)

    @work(thread=True, group="kill")
    def kill_process(self, pid: int, cmd: str) -> None:
        result = stop_pid(pid)
        self.call_from_thread(self.notify, f"{cmd} (PID {pid}): {result}")
        self.call_from_thread(self.load_procs)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    if not ROOT.is_dir():
        print(f"Folder not found: {ROOT}", file=sys.stderr)
        return 2

    if CHECK_ONLY:
        findings = scan_secrets(ROOT)
        for rel, line, kind, snip in findings:
            print(f"{rel}:{line}  {kind}  {snip}")
        if findings:
            print(f"\n✘ {len(findings)} secret(s) detected — push blocked.", file=sys.stderr)
            return 1
        print("✔ No secrets detected.")
        return 0

    DevPulse().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
