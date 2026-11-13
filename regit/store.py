"""Shadow-git snapshot store.

Every watched folder gets its own hidden git repository under ``$REGIT_HOME``
(default ``~/.local/share/regit``). The repo's work tree is the watched folder,
but its ``.git`` dir, index and history live elsewhere, so the project's real
git (if any) is never touched. The user's global/system git config is ignored
on purpose: no hooks, no gpg signing, no fsmonitor surprises.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from .timeparse import Spec, parse_spec

BRANCH = "snapshots"
REF = f"refs/heads/{BRANCH}"

DEFAULT_EXCLUDES = """\
# regit defaults (project .gitignore is honoured too; add your own in .regitignore)
.git/
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.tox/
.next/
.nuxt/
.cache/
.gradle/
target/
dist/
build/
*.o
*.so
*.class
.DS_Store
Thumbs.db
*.swp
*.swo
*~
.#*
"""


class RegitError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    sha: str
    ts: int
    message: str
    index: int = 0  # 1-based position in the list it came from (newest = 1)

    @property
    def short(self) -> str:
        return self.sha[:8]


def regit_home() -> Path:
    env = os.environ.get("REGIT_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "regit"


# --------------------------------------------------------------------------- registry


def _registry_path(home: Path) -> Path:
    return home / "projects.json"


def load_projects(home: Path | None = None) -> list[Path]:
    home = home or regit_home()
    p = _registry_path(home)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    return [Path(x) for x in data.get("projects", [])]


def _save_projects(projects: list[Path], home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    p = _registry_path(home)
    tmp = p.with_suffix(".tmp")
    uniq = sorted({str(x) for x in projects})
    tmp.write_text(json.dumps({"projects": uniq}, indent=2) + "\n")
    os.replace(tmp, p)


def register(workdir: Path, home: Path | None = None) -> None:
    home = home or regit_home()
    projects = load_projects(home)
    if workdir not in projects:
        projects.append(workdir)
    _save_projects(projects, home)


def unregister(workdir: Path, home: Path | None = None) -> bool:
    home = home or regit_home()
    projects = load_projects(home)
    if workdir not in projects:
        return False
    _save_projects([p for p in projects if p != workdir], home)
    return True


def find_project(path: Path, home: Path | None = None) -> Path | None:
    """Watched folder that contains ``path`` (deepest match wins)."""
    path = Path(os.path.abspath(path))
    best = None
    for proj in load_projects(home):
        if path == proj or proj in path.parents:
            if best is None or len(proj.parts) > len(best.parts):
                best = proj
    return best


# --------------------------------------------------------------------------- shadow repo


class Shadow:
    def __init__(self, workdir: Path | str, home: Path | None = None):
        self.workdir = Path(os.path.abspath(workdir))
        self.home = home or regit_home()
        digest = hashlib.sha1(str(self.workdir).encode()).hexdigest()[:12]
        self.gitdir = self.home / "repos" / f"{self.workdir.name or 'root'}-{digest}"

    # ---- plumbing -----------------------------------------------------------

    def _env(self, extra: dict | None = None) -> dict:
        keep = {"GIT_PAGER"}
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in keep}
        env.update(
            GIT_DIR=str(self.gitdir),
            GIT_WORK_TREE=str(self.workdir),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_LITERAL_PATHSPECS="1",
        )
        if extra:
            env.update(extra)
        return env

    def git(self, *args: str, check: bool = True, input: bytes | None = None,
            env: dict | None = None, capture: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.workdir,
            env=self._env(env),
            input=input,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
        if check and proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            raise RegitError(f"git {' '.join(args)} failed: {err}")
        return proc

    def out(self, *args: str, **kw) -> str:
        return self.git(*args, **kw).stdout.decode(errors="replace").strip()

    @contextlib.contextmanager
    def lock(self):
        """Serialise writers (daemon vs. CLI). Holding it means any index.lock is stale."""
        self.gitdir.mkdir(parents=True, exist_ok=True)
        with open(self.gitdir / "regit.lock", "w") as fh:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            stale = self.gitdir / "index.lock"
            if stale.exists():
                stale.unlink()
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    # ---- lifecycle ----------------------------------------------------------

    @property
    def exists(self) -> bool:
        return (self.gitdir / "HEAD").exists()

    def init(self) -> bool:
        """Create the shadow repo. Returns False if it already existed."""
        if not self.workdir.is_dir():
            raise RegitError(f"not a folder: {self.workdir}")
        if self.exists:
            return False
        self.gitdir.parent.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in self._env().items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
        subprocess.run(["git", "init", "-q", "--bare", "-b", BRANCH, str(self.gitdir)],
                       env=env, check=True, capture_output=True)
        for key, val in [
            ("core.bare", "false"),
            ("core.worktree", str(self.workdir)),
            ("core.excludesFile", str(self.workdir / ".regitignore")),
            ("core.quotePath", "false"),
            ("core.autocrlf", "false"),
            ("core.hooksPath", os.devnull),
            ("commit.gpgSign", "false"),
            ("advice.addEmbeddedRepo", "false"),
            ("user.name", "regit"),
            ("user.email", "regit@localhost"),
            ("gc.auto", "1000"),
        ]:
            self.git("config", key, val)
        (self.gitdir / "info").mkdir(exist_ok=True)
        (self.gitdir / "info" / "exclude").write_text(DEFAULT_EXCLUDES)
        (self.gitdir / "regit.json").write_text(
            json.dumps({"workdir": str(self.workdir), "created": int(time.time())}, indent=2) + "\n"
        )
        return True

    def destroy(self) -> None:
        if self.gitdir.exists():
            shutil.rmtree(self.gitdir)

    def require(self) -> None:
        if not self.exists:
            raise RegitError(f"{self.workdir} is not watched. Run: regit watch {self.workdir}")

    # ---- snapshots ----------------------------------------------------------

    def head(self) -> str | None:
        p = self.git("rev-parse", "-q", "--verify", f"{REF}^{{commit}}", check=False)
        if p.returncode != 0:
            return None
        return p.stdout.decode().strip() or None

    def snapshot(self, message: str | None = None, now: float | None = None) -> Snapshot | None:
        """Take a snapshot if anything changed since the last one. Returns it, or None."""
        self.require()
        with self.lock():
            # --ignore-errors: an unreadable file must not block saving the rest
            self.git("add", "-A", "--ignore-errors", check=False)
            tree = self.out("write-tree")
            head = self.head()
            if head:
                if self.out("rev-parse", f"{head}^{{tree}}") == tree:
                    return None
                changed = self.out("diff-tree", "-r", "--name-only", "-z", head, tree).split("\0")
            else:
                changed = self.out("ls-tree", "-r", "--name-only", "-z", tree).split("\0")
                if not changed or changed == [""]:
                    return None  # empty folder, nothing to remember yet
            changed = [c for c in changed if c]
            subject = message or _auto_message(changed)
            body = "\n".join(changed[:200])
            ts = int(now if now is not None else time.time())
            date = f"{ts} +0000"
            args = ["commit-tree", tree, "-m", subject, "-m", body or "(none)"]
            if head:
                args[2:2] = ["-p", head]
            sha = self.out(*args, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
            self.git("update-ref", "-m", subject, REF, sha, *( [head] if head else [] ))
            return Snapshot(sha, ts, subject, 1)

    def snapshots(self, path: str | None = None, limit: int | None = None) -> list[Snapshot]:
        """Newest first. With ``path``: only snapshots where that path changed."""
        if not self.head():
            return []
        args = ["log", "--format=%H%x1f%ct%x1f%s%x1e", REF]
        if limit:
            args.insert(1, f"-n{limit}")
        if path:
            args += ["--", path]
        raw = self.out(*args)
        snaps = []
        for i, rec in enumerate(r for r in raw.split("\x1e") if r.strip()):
            sha, ct, subj = rec.strip().split("\x1f", 2)
            snaps.append(Snapshot(sha, int(ct), subj, i + 1))
        return snaps

    def object_at(self, sha: str, path: str) -> str | None:
        p = self.git("rev-parse", "-q", "--verify", f"{sha}:{path}", check=False)
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def relpath(self, path: Path | str) -> str:
        """Path (relative to cwd or absolute) -> path relative to the watched folder."""
        ap = Path(os.path.abspath(path))
        if ap != self.workdir and self.workdir not in ap.parents:
            raise RegitError(f"{path} is outside the watched folder {self.workdir}")
        rel = os.path.relpath(ap, self.workdir)
        return "." if rel == "." else rel.replace(os.sep, "/")

    def resolve(self, spec: str | None, path: str | None = None,
                now: float | None = None) -> Snapshot:
        """Pick a snapshot.

        * ``None``  -> newest snapshot whose version of ``path`` differs from now
                       (i.e. "the previous version"); takes a fresh snapshot first.
        * ``14m`` / ``14:30`` / ISO date -> newest snapshot at or before that moment.
        * ``@N``    -> N-th entry of ``regit log [path]``.
        * sha prefix.
        """
        self.require()
        scoped = path if path not in (None, ".") else None

        if spec is None:
            self.snapshot()
            head = self.head()
            if not head:
                raise RegitError("no snapshots yet, nothing to go back to")
            if scoped is None:
                snaps = self.snapshots(limit=2)
                if len(snaps) < 2:
                    raise RegitError("there is only one snapshot, no earlier version exists")
                return snaps[1]
            current = self.object_at(head, scoped)
            for s in self.snapshots(scoped):
                obj = self.object_at(s.sha, scoped)
                if obj is not None and obj != current:
                    return s
            raise RegitError(f"no other version of '{scoped}' than the current one")

        try:
            parsed: Spec = parse_spec(spec, now=now)
        except ValueError as e:
            raise RegitError(str(e)) from None
        if parsed.kind == "index":
            snaps = self.snapshots(scoped)
            n = int(parsed.value)
            if n > len(snaps):
                raise RegitError(f"there are only {len(snaps)} snapshots, @{n} does not exist")
            return snaps[n - 1]
        if parsed.kind == "time":
            target = float(parsed.value)
            for s in self.snapshots():
                if s.ts <= target:
                    return s
            raise RegitError("no snapshot is that old")
        # rev
        p = self.git("rev-parse", "-q", "--verify", f"{parsed.value}^{{commit}}", check=False)
        sha = p.stdout.decode().strip()
        if p.returncode != 0 or not sha:
            raise RegitError(f"unknown snapshot '{spec}'")
        for s in self.snapshots():
            if s.sha == sha:
                return s
        raise RegitError(f"'{spec}' is not a regit snapshot")

    def restore(self, snap: Snapshot, paths: list[str]) -> Snapshot | None:
        """Bring ``paths`` back to how they were in ``snap``.

        Takes a safety snapshot first, so a restore is itself undoable.
        Returns that safety snapshot (or None if nothing had changed).
        """
        self.require()
        for p in paths:
            if p != "." and self.object_at(snap.sha, p) is None:
                raise RegitError(f"'{p}' did not exist in snapshot {snap.short}")
        safety = self.snapshot(message=f"before restoring {', '.join(paths)} to {snap.short}")
        with self.lock():
            self.git("restore", f"--source={snap.sha}", "--worktree", "--", *paths)
        return safety

    def prune(self, older_than: int, now: float | None = None) -> int:
        """Forget snapshots older than ``older_than`` seconds (always keeps the newest)."""
        self.require()
        now = time.time() if now is None else now
        cutoff = now - older_than
        with self.lock():
            snaps = self.snapshots()
            if not snaps:
                return 0
            keep = [s for s in snaps if s.ts >= cutoff] or snaps[:1]
            if len(keep) == len(snaps):
                return 0
            parent = None
            for s in reversed(keep):
                tree = self.out("rev-parse", f"{s.sha}^{{tree}}")
                body = self.out("log", "-1", "--format=%b", s.sha) or "(none)"
                args = ["commit-tree", tree, "-m", s.message, "-m", body]
                if parent:
                    args[2:2] = ["-p", parent]
                date = f"{s.ts} +0000"
                parent = self.out(*args, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
            self.git("update-ref", REF, parent)
            self.git("reflog", "expire", "--expire=now", "--all")
            self.git("gc", "-q", "--prune=now")
            return len(snaps) - len(keep)

    def size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.gitdir):
            for f in files:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(root, f))
        return total


def _auto_message(changed: list[str]) -> str:
    n = len(changed)
    if n == 0:
        return "auto"
    names = ", ".join(changed[:3])
    more = f" (+{n - 3})" if n > 3 else ""
    word = "file" if n == 1 else "files"
    return f"auto: {n} {word}: {names}{more}"
