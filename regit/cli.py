"""regit: a local time machine for your files, independent of your real git history."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, daemon
from .store import (RegitError, Shadow, find_project, load_projects, register,
                    regit_home, unregister)
from .timeparse import ago, fmt_ts, parse_duration

SYSTEMD_UNIT = """\
[Unit]
Description=regit - automatic file snapshots (local time machine)

[Service]
ExecStart={python} -m regit daemon --interval {interval} --keep {keep}
Environment=PYTHONPATH={pythonpath}
Restart=on-failure
RestartSec=10
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=default.target
"""


def _shadow_for(path: str | None) -> tuple[Shadow, str]:
    """Find watched project for ``path`` (or cwd) -> (shadow, path relative to it)."""
    target = Path(os.path.abspath(path or "."))
    proj = find_project(target)
    if proj is None:
        raise RegitError(f"{target} is not inside a watched folder. Run: regit watch <folder>")
    sh = Shadow(proj)
    sh.require()
    return sh, sh.relpath(target)


def _duration(text: str) -> int:
    d = parse_duration(text)
    if d is None or d <= 0:
        raise argparse.ArgumentTypeError(f"not a duration: {text} (e.g. 30s, 2m, 14d)")
    return d


def _print_snaps(snaps, current_marker: str | None = None, sh: Shadow | None = None,
                 rel: str | None = None) -> None:
    for s in snaps:
        mark = ""
        if current_marker and sh and rel:
            if sh.object_at(s.sha, rel) == current_marker:
                mark = "  <- current"
            elif sh.object_at(s.sha, rel) is None:
                mark = "  (deleted)"
        print(f"@{s.index:<4} {ago(s.ts):>16}  {fmt_ts(s.ts)}  {s.short}  {s.message}{mark}")


# ------------------------------------------------------------------ commands


def cmd_watch(a) -> int:
    for p in a.paths or ["."]:
        sh = Shadow(p)
        created = sh.init()
        register(sh.workdir)
        snap = sh.snapshot(message="first snapshot" if created else None)
        state = "new" if created else "already watched"
        print(f"watching {sh.workdir} ({state})" + (f", snapshot {snap.short}" if snap else ""))
    if not daemon.running_pid():
        print("note: the daemon is not running. Start it with `regit start` (or `regit install-systemd`).")
    return 0


def cmd_unwatch(a) -> int:
    sh = Shadow(a.path)
    if not unregister(sh.workdir) and not sh.exists:
        raise RegitError(f"{sh.workdir} was not being watched")
    if a.purge:
        sh.destroy()
        print(f"stopped watching {sh.workdir} and deleted its history")
    else:
        print(f"stopped watching {sh.workdir} (history kept in {sh.gitdir}; use --purge to delete it)")
    return 0


def cmd_snap(a) -> int:
    sh, _ = _shadow_for(a.path)
    snap = sh.snapshot(message=a.message)
    print(f"snapshot {snap.short}  {snap.message}" if snap else "nothing changed since the last snapshot")
    return 0


def cmd_log(a) -> int:
    sh, rel = _shadow_for(a.path)
    scoped = None if rel == "." else rel
    snaps = sh.snapshots(scoped, limit=a.n)
    if not snaps:
        print("no snapshots" + (f" for '{rel}'" if scoped else ""))
        return 0
    marker = None
    if scoped:
        # snapshot first so "current" compares against what is on disk right now
        sh.snapshot()
        head = sh.head()
        marker = sh.object_at(head, scoped) if head else None
        snaps = sh.snapshots(scoped, limit=a.n)
    _print_snaps(snaps, marker, sh, scoped)
    return 0


def cmd_diff(a) -> int:
    sh, rel = _shadow_for(a.path)
    snap = sh.resolve(a.at, rel)
    print(f"# {snap.short} ({ago(snap.ts)}, {fmt_ts(snap.ts)}) -> now", file=sys.stderr)
    args = ["diff", "--stat" if a.stat else "--patch", snap.sha, "--", rel]
    sh.git(*args, check=False, capture=False)
    return 0


def cmd_show(a) -> int:
    sh, rel = _shadow_for(a.path)
    snap = sh.resolve(a.at, rel)
    sh.git("show", "--stat" if a.stat else "--patch", "--format=%h  %cd%n%s%n", "--date=local",
           snap.sha, "--", rel, check=False, capture=False)
    return 0


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_restore(a) -> int:
    # all paths must live in one watched folder
    sh, first = _shadow_for(a.paths[0])
    rels = [first] + [sh.relpath(p) for p in a.paths[1:]]
    snap = sh.resolve(a.at, rels[0] if len(rels) == 1 else None)
    what = ", ".join(rels)
    print(f"restoring {what} to {snap.short} ({ago(snap.ts)}, {fmt_ts(snap.ts)})")
    if not a.yes:
        sh.git("diff", "--stat", "-R", snap.sha, "--", *rels, check=False, capture=False)
        if not _confirm("Proceed?"):
            print("aborted, nothing changed")
            return 1
    safety = sh.restore(snap, rels)
    print("restored." + (f" The previous state was saved as {safety.short} "
                         f"(undo with: regit restore {what} --at {safety.short})"
                         if safety else " The previous state was already snapshotted (see regit log)."))
    return 0


def cmd_tm(a) -> int:
    """Interactive: browse versions of one file, peek at diff, restore."""
    sh, rel = _shadow_for(a.path)
    if rel == ".":
        raise RegitError("give a file or folder, e.g. regit tm src/app.py")
    sh.snapshot()
    while True:
        head = sh.head()
        current = sh.object_at(head, rel) if head else None
        snaps = sh.snapshots(rel, limit=a.n)
        if not snaps:
            print(f"no history for '{rel}'")
            return 0
        print(f"\n{rel}: versions (newest first)")
        _print_snaps(snaps, current, sh, rel)
        try:
            choice = input("\nnumber = preview diff, q = quit: ").strip().lstrip("@")
        except EOFError:
            return 0
        if choice.lower() in ("q", ""):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(snaps):
            print("no such number")
            continue
        snap = snaps[int(choice) - 1]
        if sh.object_at(snap.sha, rel) is None:
            print("the file did not exist in that snapshot; pick another one")
            continue
        sh.git("diff", "-R", snap.sha, "--", rel, check=False, capture=False)
        print(f"(diff: now -> version @{choice}, i.e. what a restore would change)")
        if _confirm(f"Restore {rel} to @{choice} ({ago(snap.ts)})?"):
            safety = sh.restore(snap, [rel])
            print("restored." + (f" The previous state was saved as {safety.short}." if safety
                                 else " The previous state was already snapshotted."))
            return 0


def cmd_prune(a) -> int:
    targets = load_projects() if a.all else [_shadow_for(a.path)[0].workdir]
    for proj in targets:
        sh = Shadow(proj)
        if not sh.exists:
            continue
        n = sh.prune(a.older_than)
        print(f"{proj}: removed {n} snapshot(s)")
    return 0


def cmd_status(a) -> int:
    pid = daemon.running_pid()
    print(f"daemon: {f'running (pid {pid})' if pid else 'not running'}")
    print(f"data:   {regit_home()}")
    projects = load_projects()
    if not projects:
        print("nothing is being watched. Run: regit watch <folder>")
        return 0
    for proj in projects:
        sh = Shadow(proj)
        if not sh.exists:
            print(f"  {proj} (no snapshot repo yet; the daemon will create it)")
            continue
        snaps = sh.snapshots()
        last = f"last {ago(snaps[0].ts)}" if snaps else "no snapshots"
        size = _fmt_size(sh.size_bytes())
        gone = "" if proj.is_dir() else "  (warning: folder is missing)"
        print(f"  {proj}: {len(snaps)} snapshots, {last}, {size}{gone}")
    return 0


def _fmt_size(n: int) -> str:
    return f"{max(1, round(n / 1024))} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB"


def _daemon_args(a) -> list[str]:
    return ["--interval", str(a.interval), "--keep", a.keep]


def cmd_daemon(a) -> int:
    keep = None if a.keep in ("0", "never") else _duration(a.keep)
    return daemon.run(interval=a.interval, keep=keep, once=a.once)


def cmd_start(a) -> int:
    pid = daemon.running_pid()
    if pid:
        print(f"the daemon is already running (pid {pid})")
        return 0
    pid = daemon.spawn(_daemon_args(a))
    print(f"daemon started (pid {pid}), snapshot every {a.interval}s. Log: {daemon.log_path(regit_home())}")
    return 0


def cmd_stop(a) -> int:
    import signal
    pid = daemon.running_pid()
    if not pid:
        print("the daemon is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"stopping the daemon (pid {pid})")
    return 0


def cmd_gui(a) -> int:
    from . import gui
    gui.serve(port=a.port, open_browser=not a.no_browser)
    return 0


def cmd_install_systemd(a) -> int:
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "regit.service"
    unit.write_text(SYSTEMD_UNIT.format(
        python=sys.executable, interval=a.interval, keep=a.keep,
        pythonpath=Path(__file__).resolve().parent.parent,
    ))
    print(f"wrote {unit}\nnow run:\n  systemctl --user daemon-reload\n"
          f"  systemctl --user enable --now regit.service")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="regit", description=__doc__)
    p.add_argument("--version", action="version", version=f"regit {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    AT_HELP = "when: 14m, 2h, 1d, 14:30, '2026-11-13 14:30', @3 (from regit log) or a hash. " \
              "Default: the previous version that differs from the current one"

    sp = add("watch", cmd_watch, "start watching one or more folders")
    sp.add_argument("paths", nargs="*")

    sp = add("unwatch", cmd_unwatch, "stop watching a folder")
    sp.add_argument("path", nargs="?", default=".")
    sp.add_argument("--purge", action="store_true", help="also delete its snapshot history")

    sp = add("snap", cmd_snap, "take a snapshot now")
    sp.add_argument("path", nargs="?")
    sp.add_argument("-m", "--message")

    sp = add("log", cmd_log, "list snapshots (of the whole folder or one file)")
    sp.add_argument("path", nargs="?")
    sp.add_argument("-n", type=int, default=30, help="how many to show (default 30)")

    sp = add("diff", cmd_diff, "diff a snapshot against the current state")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--at", "-a", help=AT_HELP)
    sp.add_argument("--stat", action="store_true")

    sp = add("show", cmd_show, "show what a snapshot changed")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--at", "-a", help=AT_HELP)
    sp.add_argument("--stat", action="store_true")

    sp = add("restore", cmd_restore, "restore files or folders from a snapshot")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--at", "-a", help=AT_HELP)
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    sp = add("tm", cmd_tm, "interactive time machine for one file")
    sp.add_argument("path")
    sp.add_argument("-n", type=int, default=20)

    sp = add("prune", cmd_prune, "delete old snapshots")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--older-than", type=_duration, default=_duration("14d"), metavar="DURATION")
    sp.add_argument("--all", action="store_true", help="in all watched folders")

    add("status", cmd_status, "show watched folders and daemon state")

    for name, fn, help_ in [
        ("daemon", cmd_daemon, "run the daemon in the foreground"),
        ("start", cmd_start, "start the daemon in the background"),
        ("install-systemd", cmd_install_systemd, "write a systemd --user service"),
    ]:
        sp = add(name, fn, help_)
        sp.add_argument("--interval", type=int, default=120, help="seconds between snapshots (default 120)")
        sp.add_argument("--keep", default="14d", help="how long to keep snapshots (default 14d, 0 = forever)")
        if name == "daemon":
            sp.add_argument("--once", action="store_true", help="run a single pass and exit")

    add("stop", cmd_stop, "stop the daemon")

    sp = add("gui", cmd_gui, "open the web GUI in your browser (served on 127.0.0.1)")
    sp.add_argument("--port", type=int, default=0, help="port (default: a random free port)")
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser, just print the link")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except RegitError as e:
        print(f"regit: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
