#!/usr/bin/env python3
"""triage_watcher.py — Phase 1.1 (no-brew variant)
Long-running watcher for 00_Inbox/from-dust/ using Python's `watchdog` library.

Same outcome as fswatch: when a Dust write lands in any agent inbox subdir,
fire triage_dust_writes.py after a 5s debounce. PID-locked so only one
instance runs at a time. Loaded by ~/Library/LaunchAgents/com.tony.ai-brain-triage-watcher.plist.

Graceful no-op if watchdog is missing — logs the install command and sleeps.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Timer

VAULT = Path(os.environ.get("VAULT_ROOT") or (_ for _ in ()).throw(SystemExit("Set VAULT_ROOT to your vault path")))
INBOX = VAULT / "00_Inbox" / "from-dust"
MEETINGS_INBOX = VAULT / "00_Inbox" / "from-meetings"
LOG_DIR = Path.home() / "AI-Brain-build" / "logs"
LOCKFILE = LOG_DIR / "triage-watcher.pid"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with (LOG_DIR / f"triage-watcher-{datetime.now().strftime('%Y-%m-%d')}.log").open("a") as f:
        f.write(line)


def acquire_lock() -> bool:
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            os.kill(pid, 0)  # raises if not running
            log(f"another watcher running (pid {pid}) — exiting")
            return False
        except (OSError, ValueError):
            pass  # stale lock
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    LOCKFILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        LOCKFILE.unlink()
    except OSError:
        pass


def fire_triage() -> None:
    log("debounce settled — firing triage_dust_writes.py")
    try:
        result = subprocess.run(
            ["python3", str(VAULT / "build" / "tools" / "triage_dust_writes.py")],
            capture_output=True, text=True, timeout=120, cwd=str(VAULT),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log(f"  | {line}")
    except subprocess.TimeoutExpired:
        log("⚠ triage timed out after 120s")
    except Exception as e:  # noqa: BLE001
        log(f"⚠ triage failed: {e!r}")


def fire_meetings_ingest() -> None:
    log("debounce settled — firing meeting_ingest.py --scan")
    try:
        result = subprocess.run(
            ["python3", str(VAULT / "build" / "tools" / "meeting_ingest.py"), "--scan"],
            capture_output=True, text=True, timeout=120, cwd=str(VAULT),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log(f"  ⟐ {line}")
        # After ingest, fire triage so the produced SBAP writes get promoted same cycle
        fire_triage()
    except subprocess.TimeoutExpired:
        log("⚠ meeting_ingest timed out after 120s")
    except Exception as e:  # noqa: BLE001
        log(f"⚠ meeting_ingest failed: {e!r}")


def main() -> int:
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log("watchdog not installed — run: python3 -m pip install --user watchdog")
        # Sleep so launchd doesn't respawn-storm us
        time.sleep(3600)
        return 0

    if not acquire_lock():
        return 0

    log(f"START watching {INBOX} (SBAP writes) + {MEETINGS_INBOX} (meeting drop-zone)")

    MEETING_EXTS = (".vtt", ".docx", ".md", ".txt", ".json")

    class Debouncer(FileSystemEventHandler):
        def __init__(self, *, meetings_zone: bool = False):
            self.timer: Timer | None = None
            self.meetings_zone = meetings_zone
            # last-seen content hash per file path; guards against OneDrive
            # metadata-only touches re-firing triage on unchanged content.
            # The triage seen-set is the ultimate backstop, but this avoids
            # the process-spawn entirely when content hasn't changed.
            self._last_hash: dict[str, str] = {}

        def _file_hash(self, path: str) -> str | None:
            try:
                return hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                return None

        def _reset(self):
            if self.timer:
                self.timer.cancel()
            fire_fn = fire_meetings_ingest if self.meetings_zone else fire_triage
            self.timer = Timer(5.0, fire_fn)
            self.timer.daemon = True
            self.timer.start()

        def _should_ignore(self, path: str) -> bool:
            name = os.path.basename(path)
            if name == "README.md" or name.startswith(".") or "/processed/" in path:
                return True
            if "sensitive-quarantine" in path:
                return True
            if self.meetings_zone:
                # accept any of the supported meeting formats
                return not any(path.lower().endswith(ext) for ext in MEETING_EXTS)
            # SBAP zone: only .md
            return not path.endswith(".md")

        def _enqueue_if_changed(self, path: str, event_type: str) -> None:
            """Only reset the debounce timer if the file content has actually changed.
            Suppresses OneDrive mtime-only touches that leave bytes identical."""
            h = self._file_hash(path)
            if h is not None and self._last_hash.get(path) == h:
                log(f"content-unchanged skip ({event_type}): {path}")
                return
            if h is not None:
                self._last_hash[path] = h
            log(f"{'meetings ' if self.meetings_zone else ''}{event_type}: {path}")
            self._reset()

        def on_created(self, event):
            if event.is_directory or self._should_ignore(event.src_path):
                return
            self._enqueue_if_changed(event.src_path, "created")

        def on_modified(self, event):
            if event.is_directory or self._should_ignore(event.src_path):
                return
            self._enqueue_if_changed(event.src_path, "modified")

        def on_moved(self, event):
            target = getattr(event, "dest_path", event.src_path)
            if event.is_directory or self._should_ignore(target):
                return
            self._enqueue_if_changed(target, "moved")

    observer = Observer()
    observer.schedule(Debouncer(meetings_zone=False), str(INBOX), recursive=True)
    if MEETINGS_INBOX.exists():
        observer.schedule(Debouncer(meetings_zone=True), str(MEETINGS_INBOX), recursive=True)
        log(f"  → also watching {MEETINGS_INBOX}")
    observer.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=5)
        release_lock()
        log("STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
