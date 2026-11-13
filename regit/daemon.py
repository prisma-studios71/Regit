"""Background loop: every ``interval`` seconds snapshot every watched folder that changed."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .store import RegitError, Shadow, load_projects, regit_home

log = logging.getLogger("regit")

PRUNE_EVERY = 6 * 3600


def pid_path(home: Path) -> Path:
    return home / "daemon.pid"


def log_path(home: Path) -> Path:
    return home / "daemon.log"


def running_pid(home: Path | None = None) -> int | None:
    home = home or regit_home()
    p = pid_path(home)
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def tick(home: Path, keep: int | None, last_prune: dict[Path, float]) -> int:
    """One pass over all projects. Returns number of snapshots taken."""
    taken = 0
    for proj in load_projects(home):
        if not proj.is_dir():
            log.warning("skipping %s (folder is missing)", proj)
            continue
        sh = Shadow(proj, home)
        try:
            if not sh.exists:
                sh.init()
            snap = sh.snapshot()
            if snap:
                taken += 1
                log.info("%s: %s  %s", proj, snap.short, snap.message)
            if keep and time.time() - last_prune.get(proj, 0) > PRUNE_EVERY:
                last_prune[proj] = time.time()
                n = sh.prune(keep)
                if n:
                    log.info("%s: pruned %d old snapshot(s)", proj, n)
        except (RegitError, OSError) as e:
            log.error("%s: %s", proj, e)
    return taken


def run(interval: int = 120, keep: int | None = 14 * 86400, once: bool = False,
        home: Path | None = None) -> int:
    home = home or regit_home()
    home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)

    lock_fh = open(home / "daemon.lock", "w")
    if fcntl:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("the daemon is already running (pid %s)", running_pid(home))
            lock_fh.close()
            return 1
    pid_path(home).write_text(f"{os.getpid()}\n")

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("regit daemon started: every %ds, %d folder(s)", interval, len(load_projects(home)))
    last_prune: dict[Path, float] = {}
    try:
        while not stop:
            tick(home, keep, last_prune)
            if once:
                break
            deadline = time.time() + interval
            while not stop and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))
    finally:
        with contextlib.suppress(OSError):
            pid_path(home).unlink()
        lock_fh.close()
        log.info("regit daemon stopped")
    return 0


def spawn(args: list[str], home: Path | None = None) -> int:
    """Start the daemon detached from the terminal; returns its pid."""
    home = home or regit_home()
    home.mkdir(parents=True, exist_ok=True)
    logf = open(log_path(home), "a")
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [pkg_parent, env.get("PYTHONPATH")]))
    proc = subprocess.Popen(
        [sys.executable, "-m", "regit", "daemon", *args],
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, env=env,
        start_new_session=True, close_fds=True,
    )
    return proc.pid
