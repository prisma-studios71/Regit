"""``regit gui`` — local web GUI (stdlib only) served on 127.0.0.1 and opened in a browser.

Same engine as the CLI (``Shadow``), just clickable: pick a project, pick a file,
see its versions, peek at the diff/content, restore with one button.
Protected by a random per-run token and a Host-header check (no DNS rebinding).
"""

from __future__ import annotations

import json
import re
import secrets
import time
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, daemon
from .store import REF, RegitError, Shadow, load_projects, register, regit_home, unregister
from .timeparse import ago, fmt_ts

MAX_PREVIEW = 2 * 1024 * 1024


_AUTO_RE = re.compile(r"^auto: (\d+) (?:plik|pliki|plików): (.*)$")
_BEFORE_RE = re.compile(r"^przed przywróceniem (.*) do (\w+)$")


def msg_en(msg: str) -> str:
    """Translate subjects of snapshots made by regit < 0.2, which wrote them in Polish."""
    m = _AUTO_RE.match(msg)
    if m:
        n = int(m.group(1))
        return f"auto: {n} file{'s' if n != 1 else ''}: {m.group(2)}"
    m = _BEFORE_RE.match(msg)
    if m:
        return f"before restoring {m.group(1)} to {m.group(2)}"
    return {"pierwsza migawka": "first snapshot", "auto": "auto"}.get(msg, msg)


# Tabler Icons — outline set v3.24.0, MIT License (c) Paweł Kuna — https://tabler.io/icons
# Inlined as path data so the GUI works offline and never calls a CDN.
_ICON_PATHS = {
    "history": ['M12 8l0 4l2 2', 'M3.05 11a9 9 0 1 1 .5 4m-.5 5v-5h5'],
    "restore": ['M3.06 13a9 9 0 1 0 .49 -4.087', 'M3 4.001v5h5', 'M12 12m-1 0a1 1 0 1 0 2 0a1 1 0 1 0 -2 0'],
    "eye": ['M10 12a2 2 0 1 0 4 0a2 2 0 0 0 -4 0', 'M21 12c-2.4 4 -5.4 6 -9 6c-3.6 0 -6.6 -2 -9 -6c2.4 -4 5.4 -6 9 -6c3.6 0 6.6 2 9 6'],
    "eye-off": ['M10.585 10.587a2 2 0 0 0 2.829 2.828', 'M16.681 16.673a8.717 8.717 0 0 1 -4.681 1.327c-3.6 0 -6.6 -2 -9 -6c1.272 -2.12 2.712 -3.678 4.32 -4.674m2.86 -1.146a9.055 9.055 0 0 1 1.82 -.18c3.6 0 6.6 2 9 6c-.666 1.11 -1.379 2.067 -2.138 2.87', 'M3 3l18 18'],
    "camera": ['M5 7h1a2 2 0 0 0 2 -2a1 1 0 0 1 1 -1h6a1 1 0 0 1 1 1a2 2 0 0 0 2 2h1a2 2 0 0 1 2 2v9a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-9a2 2 0 0 1 2 -2', 'M9 13a3 3 0 1 0 6 0a3 3 0 0 0 -6 0'],
    "alert-triangle": ['M12 9v4', 'M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0z', 'M12 16h.01'],
    "circle-check": ['M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0 -18 0', 'M9 12l2 2l4 -4'],
    "circle-x": ['M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0 -18 0', 'M10 10l4 4m0 -4l-4 4'],
    "folder-plus": ['M12 19h-7a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2h4l3 3h7a2 2 0 0 1 2 2v3.5', 'M16 19h6', 'M19 16v6'],
    "file": ['M14 3v4a1 1 0 0 0 1 1h4', 'M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2z'],
    "file-x": ['M14 3v4a1 1 0 0 0 1 1h4', 'M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2z', 'M10 12l4 4m0 -4l-4 4'],
    "heartbeat": ['M19.5 13.572l-7.5 7.428l-2.896 -2.868m-6.117 -8.104a5 5 0 0 1 9.013 -3.022a5 5 0 1 1 7.5 6.572', 'M3 13h2l2 3l2 -6l1 3h3'],
    "power": ['M7 6a7.75 7.75 0 1 0 10 0', 'M12 4l0 8'],
}


def icon(name: str, cls: str = "ic") -> str:
    paths = "".join(f'<path d="{d}"/>' for d in _ICON_PATHS[name])
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


# ------------------------------------------------------------------ API logic


def _shadow(project: str) -> Shadow:
    proj = Path(project)
    if proj not in load_projects():
        raise RegitError(f"{project} is not watched")
    sh = Shadow(proj)
    sh.require()
    return sh


def api_projects(_q, _body) -> dict:
    out = []
    for proj in load_projects():
        sh = Shadow(proj)
        snaps = sh.snapshots() if sh.exists else []
        out.append({
            "path": str(proj),
            "name": proj.name or str(proj),
            "count": len(snaps),
            "last": ago(snaps[0].ts) if snaps else None,
            "size": sh.size_bytes() if sh.exists else 0,
            "missing": not proj.is_dir(),
        })
    return {"projects": out, "daemon_pid": daemon.running_pid(), "home": str(regit_home()),
            "version": __version__}


def api_files(q, _body) -> dict:
    sh = _shadow(q["project"])
    sh.snapshot()
    head = sh.head()
    if not head:
        return {"files": []}
    current = {f for f in sh.out("ls-tree", "-r", "--name-only", "-z", head).split("\0") if f}
    ever = {f for f in sh.out("log", "--format=", "--name-only", "-z", REF).split("\0") if f.strip()}
    ever = {f.strip("\n") for f in ever}
    files = [{"path": f, "deleted": False} for f in sorted(current)]
    files += [{"path": f, "deleted": True} for f in sorted(ever - current)]
    return {"files": files}


def api_versions(q, _body) -> dict:
    sh = _shadow(q["project"])
    path = q["path"]
    sh.snapshot()  # so "= now" reflects the disk, e.g. right after a restore
    head = sh.head()
    now_obj = sh.object_at(head, path) if head else None
    out = []
    for s in sh.snapshots(path, limit=200):
        obj = sh.object_at(s.sha, path)
        out.append({"index": s.index, "sha": s.sha, "short": s.short, "ts": s.ts,
                    "ago": ago(s.ts), "when": fmt_ts(s.ts), "message": msg_en(s.message),
                    "current": obj is not None and obj == now_obj, "deleted": obj is None})
    return {"versions": out}


def api_diff(q, _body) -> dict:
    sh = _shadow(q["project"])
    path, sha = q["path"], q["sha"]
    # "now -> that version" = exactly what a restore would change
    diff = sh.git("diff", "--no-color", "-R", sha, "--", path, check=False).stdout
    content, binary = None, False
    if sh.object_at(sha, path) is not None:
        raw = sh.git("cat-file", "-p", f"{sha}:{path}", check=False).stdout[:MAX_PREVIEW]
        binary = b"\0" in raw[:8000]
        content = None if binary else raw.decode(errors="replace")
    return {"diff": diff.decode(errors="replace"), "content": content, "binary": binary}


def api_restore(_q, body) -> dict:
    sh = _shadow(body["project"])
    snap = sh.resolve(body["sha"])
    safety = sh.restore(snap, [body["path"]])
    return {"ok": True, "safety": safety.short if safety else None}


def api_snap(_q, body) -> dict:
    sh = _shadow(body["project"])
    snap = sh.snapshot(message=body.get("message") or None)
    return {"snap": snap.short if snap else None}


def api_watch(_q, body) -> dict:
    path = Path(body["path"]).expanduser()
    sh = Shadow(path)
    sh.init()
    register(sh.workdir)
    sh.snapshot(message="first snapshot")
    return {"ok": True, "path": str(sh.workdir)}


def api_unwatch(_q, body) -> dict:
    sh = Shadow(body["project"])
    unregister(sh.workdir)
    return {"ok": True}


ROUTES = {
    ("GET", "/api/projects"): api_projects,
    ("GET", "/api/files"): api_files,
    ("GET", "/api/versions"): api_versions,
    ("GET", "/api/diff"): api_diff,
    ("POST", "/api/restore"): api_restore,
    ("POST", "/api/snap"): api_snap,
    ("POST", "/api/watch"): api_watch,
    ("POST", "/api/unwatch"): api_unwatch,
}


# ------------------------------------------------------------------ HTTP


def make_handler(token: str, port_ref: list):
    class Handler(BaseHTTPRequestHandler):
        server_version = "regit"

        def log_message(self, *_):  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"bad host", "text/plain")
            url = urlparse(self.path)
            if method == "GET" and url.path == "/":
                return self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            fn = ROUTES.get((method, url.path))
            if fn is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Regit-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
            try:
                self._json(HTTPStatus.OK, fn(q, body))
            except (RegitError, KeyError, OSError, ValueError) as e:
                msg = f"missing field {e}" if isinstance(e, KeyError) else str(e)
                self._json(HTTPStatus.BAD_REQUEST, {"error": msg})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def serve(port: int = 0, open_browser: bool = True, ready: threading.Event | None = None,
          info: dict | None = None) -> None:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, port_ref))
    port_ref[0] = httpd.server_address[1]
    url = f"http://127.0.0.1:{port_ref[0]}/#t={token}"
    if info is not None:
        info.update(url=url, token=token, port=port_ref[0], server=httpd)
    print(f"regit gui: {url}\n(press Ctrl+C to quit)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    if ready:
        ready.set()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


# ------------------------------------------------------------------ page

_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>regit time machine</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#1d2330;--muted:#6b7385;--line:#e3e6ec;--accent:#5b5bd6;
--accent-fg:#fff;--add:#e6f6ea;--add-fg:#17692f;--del:#fdecec;--del-fg:#a42121;--hunk:#eef0fb;--sel:#ecebfd;
--warn:#b45309;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--panel:#181b22;--fg:#e4e7ee;--muted:#8d95a7;
--line:#2a2f3a;--accent:#8b8bf5;--accent-fg:#0e0f14;--add:#12301c;--add-fg:#7ee29a;--del:#3a1517;--del-fg:#ff9b9b;
--hunk:#1f2340;--sel:#262650;--warn:#f0a35a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;display:flex;align-items:center;gap:6px}
.ic{width:16px;height:16px;flex:none;vertical-align:-3px}
.logo{width:22px;height:22px;color:var(--accent)}
button,.pill,#toast{display:inline-flex;align-items:center;gap:6px}
.iconbtn{padding:6px 8px}
.item .ic{margin-right:6px;color:var(--muted)}
.warn .ic{color:var(--warn)}
button:disabled{opacity:.5;cursor:default}header .sub{color:var(--muted);font-size:12px}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.pill.on{color:var(--add-fg);border-color:currentColor}.pill.off{color:var(--del-fg);border-color:currentColor}
main{display:grid;grid-template-columns:240px 300px 1fr;height:calc(100vh - 49px)}
section{border-right:1px solid var(--line);overflow:auto;background:var(--panel);min-width:0}
section:last-child{border-right:0;background:var(--bg)}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 14px 6px}
.item{padding:7px 14px;cursor:pointer;border-left:3px solid transparent;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item:hover{background:var(--sel)}.item.sel{background:var(--sel);border-left-color:var(--accent)}
.item small{display:block;color:var(--muted);font-size:12px}
.item.deleted{color:var(--muted);text-decoration:line-through}
input[type=text]{width:calc(100% - 28px);margin:4px 14px;padding:6px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel);
color:var(--fg);cursor:pointer}button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.row{display:flex;gap:8px;margin:6px 14px;flex-wrap:wrap}
.ver{padding:8px 14px;cursor:pointer;border-bottom:1px solid var(--line)}
.ver:hover{background:var(--sel)}.ver.sel{background:var(--sel);box-shadow:inset 3px 0 var(--accent)}
.ver .top{display:flex;gap:4px 8px;align-items:baseline;flex-wrap:wrap}.ver .top>*{white-space:nowrap}
.split{display:grid;grid-template-columns:minmax(220px,300px) 1fr;min-height:100%}
.split>.vlist{border-right:1px solid var(--line);background:var(--panel);overflow:auto}.ver .idx{font-family:var(--mono);color:var(--accent)}
.ver .msg{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tag{font-size:11px;padding:0 6px;border-radius:4px;background:var(--add);color:var(--add-fg)}
.tag.del{background:var(--del);color:var(--del-fg)}
#versions-wrap,.split{height:100%}
.detail{padding:14px 18px;min-width:0}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolbar .grow{flex:1}
.tabs button{border-radius:6px 6px 0 0;border-bottom:0}.tabs button.on{background:var(--sel);border-color:var(--accent)}
pre{margin:0;font:12.5px/1.5 var(--mono);background:var(--panel);border:1px solid var(--line);border-radius:0 6px 6px 6px;
padding:8px 0;overflow:auto;max-height:calc(100vh - 190px)}
pre span{display:block;padding:0 12px;white-space:pre;min-height:1.5em}
.a{background:var(--add);color:var(--add-fg)}.d{background:var(--del);color:var(--del-fg)}
.h{background:var(--hunk);color:var(--muted)}.m{color:var(--muted)}
.empty{color:var(--muted);padding:30px 18px;text-align:center}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);
padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
#toast.show{opacity:1}#toast.err{background:var(--del-fg)}
.warn{color:var(--warn)}
@media (max-width:860px){main{grid-template-columns:1fr;height:auto}section{border-right:0;border-bottom:1px solid var(--line);max-height:45vh}
section:last-child{max-height:none}pre{max-height:60vh}
.split{grid-template-columns:1fr;height:auto}.split>.vlist{border-right:0;border-bottom:1px solid var(--line);max-height:40vh}
#versions-wrap{height:auto}}
</style>
</head>
<body>
<header><h1>{{icon:history:logo}}regit</h1><span class="sub">local time machine for your files</span><span class="grow" style="flex:1"></span>
<span id="daemon" class="pill">daemon…</span></header>
<main>
<section id="col-projects">
  <h2>Projects</h2><div id="projects"></div>
  <h2>Watch a new folder</h2>
  <input type="text" id="newpath" placeholder="/home/you/project">
  <div class="row"><button id="watchbtn">{{icon:folder-plus}}Watch</button></div>
</section>
<section id="col-files">
  <h2>Files</h2>
  <input type="text" id="filter" placeholder="filter…">
  <div class="row"><button id="snapbtn">{{icon:camera}}Snapshot now</button><button id="unwatchbtn" class="iconbtn" title="Stop watching (history is kept)" aria-label="Stop watching">{{icon:eye-off}}</button></div>
  <div id="files"><div class="empty">pick a project</div></div>
</section>
<section id="col-detail">
  <div id="versions-wrap"></div>
</section>
</main>
<div id="toast"></div>
<script>
const ICONS = /*ICONS_JSON*/;
const IC = n => ICONS[n] || '';
const TOKEN = new URLSearchParams(location.hash.slice(1)).get('t') || '';
const S = {project:null, path:null, files:[], versions:[], sha:null, tab:'diff', data:null};
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts={}) {
  const r = await fetch(path, {...opts, headers:{'X-Regit-Token':TOKEN,'Content-Type':'application/json'}});
  const j = await r.json().catch(() => ({error:'bad response'}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
const post = (p, body) => api(p, {method:'POST', body:JSON.stringify(body)});
const fmtSize = b => b < 1048576 ? `${Math.max(1, Math.round(b / 1024))} KB` : `${(b / 1048576).toFixed(1)} MB`;
const qs = o => new URLSearchParams(o).toString();
function toast(msg, err, icon) { const t=$('#toast'); t.innerHTML=IC(icon || (err?'circle-x':'circle-check')) + esc(msg); t.className='show'+(err?' err':''); clearTimeout(t._h); t._h=setTimeout(()=>t.className='', 3500); }

async function loadProjects() {
  const j = await api('/api/projects');
  const d = $('#daemon');
  d.innerHTML = j.daemon_pid ? `${IC('heartbeat')}daemon running · pid ${j.daemon_pid}` : `${IC('power')}daemon stopped · run: regit start`;
  d.className = 'pill ' + (j.daemon_pid ? 'on' : 'off');
  $('#projects').innerHTML = j.projects.length ? j.projects.map(p => `
    <div class="item ${p.path===S.project?'sel':''}" data-p="${esc(p.path)}" title="${esc(p.path)}">
      ${esc(p.name)} ${p.missing?`<span class="warn" title="folder is missing">${IC('alert-triangle')}</span>`:''}
      <small>${p.count} snapshots · ${p.last?esc(p.last):'—'} · ${fmtSize(p.size)}</small></div>`).join('')
    : '<div class="empty">nothing watched yet</div>';
  document.querySelectorAll('#projects .item').forEach(el => el.onclick = () => pickProject(el.dataset.p));
}

async function pickProject(p) {
  S.project = p; S.path = null; S.sha = null;
  loadProjects();
  $('#files').innerHTML = '<div class="empty">loading…</div>';
  $('#versions-wrap').innerHTML = '';
  try { S.files = (await api('/api/files?' + qs({project:p}))).files; renderFiles(); }
  catch (e) { toast(e.message, true); }
}

function renderFiles() {
  const f = $('#filter').value.toLowerCase();
  const list = S.files.filter(x => x.path.toLowerCase().includes(f));
  $('#files').innerHTML = list.length ? list.slice(0, 2000).map(x => `
    <div class="item ${x.deleted?'deleted':''} ${x.path===S.path?'sel':''}" data-f="${esc(x.path)}" title="${esc(x.path)}">${IC(x.deleted?'file-x':'file')}${esc(x.path)}</div>`).join('')
    : '<div class="empty">no files</div>';
  document.querySelectorAll('#files .item').forEach(el => el.onclick = () => pickFile(el.dataset.f));
}

async function pickFile(path) {
  S.path = path; S.sha = null; renderFiles();
  try { S.versions = (await api('/api/versions?' + qs({project:S.project, path}))).versions; }
  catch (e) { return toast(e.message, true); }
  const firstOther = S.versions.find(v => !v.current && !v.deleted);
  renderDetail();
  if (firstOther) pickVersion(firstOther.sha);
}

function renderDetail() {
  const vs = S.versions.map(v => `
    <div class="ver ${v.sha===S.sha?'sel':''}" data-s="${v.sha}">
      <div class="top"><span class="idx">@${v.index}</span><b>${esc(v.ago)}</b>
        <span class="m" style="font-size:12px">${esc(v.when)}</span>
        ${v.current?'<span class="tag">= now</span>':''}${v.deleted?'<span class="tag del">deleted</span>':''}</div>
      <div class="msg">${esc(v.short)} · ${esc(v.message)}</div></div>`).join('');
  let body = '<div class="empty">click a version to see what a restore would change</div>';
  const v = S.versions.find(x => x.sha === S.sha);
  if (v && S.data) {
    const content = S.tab === 'diff' ? renderDiff(S.data.diff)
      : S.data.binary ? '<span class="m">(binary file)</span>'
      : S.data.content == null ? '<span class="m">(the file did not exist in this version)</span>'
      : S.data.content.split('\n').map(l => `<span>${esc(l)}</span>`).join('');
    body = `<div class="toolbar"><b>@${v.index}</b><span class="m">${esc(v.when)} · ${esc(v.ago)}</span><span class="grow"></span>
      <button class="primary" id="restorebtn" ${v.deleted||v.current?'disabled':''}>${IC('restore')}Restore this version</button></div>
      <div class="tabs"><button data-t="diff" class="${S.tab==='diff'?'on':''}">Diff (now → this version)</button>
      <button data-t="content" class="${S.tab==='content'?'on':''}">Content</button></div><pre>${content}</pre>`;
  }
  $('#versions-wrap').innerHTML = `
    <div class="split">
      <div class="vlist">
        <h2>${esc(S.path)}</h2>${vs || '<div class="empty">no versions</div>'}</div>
      <div class="detail">${body}</div></div>`;
  document.querySelectorAll('.ver').forEach(el => el.onclick = () => pickVersion(el.dataset.s));
  document.querySelectorAll('.tabs button').forEach(el => el.onclick = () => { S.tab = el.dataset.t; renderDetail(); });
  const rb = $('#restorebtn'); if (rb) rb.onclick = restore;
}

function renderDiff(d) {
  if (!d.trim()) return '<span class="m">no differences: this version matches the file on disk</span>';
  return d.split('\n').map(l => {
    const c = l.startsWith('@@') ? 'h' : (l.startsWith('+++')||l.startsWith('---')||l.startsWith('diff ')||l.startsWith('index ')) ? 'm'
      : l.startsWith('+') ? 'a' : l.startsWith('-') ? 'd' : '';
    return `<span class="${c}">${esc(l)}</span>`;
  }).join('');
}

async function pickVersion(sha) {
  S.sha = sha; S.data = null; renderDetail();
  try { S.data = await api('/api/diff?' + qs({project:S.project, path:S.path, sha})); renderDetail(); }
  catch (e) { toast(e.message, true); }
}

async function restore() {
  const v = S.versions.find(x => x.sha === S.sha);
  if (!confirm(`Restore ${S.path} to @${v.index} (${v.ago})?\nThe current state is snapshotted first, so you can undo this.`)) return;
  try {
    const j = await post('/api/restore', {project:S.project, path:S.path, sha:S.sha});
    toast('Restored' + (j.safety ? ` · previous state saved as ${j.safety}` : ''));
    pickFile(S.path); loadProjects();
  } catch (e) { toast(e.message, true); }
}

$('#filter').oninput = renderFiles;
$('#snapbtn').onclick = async () => {
  if (!S.project) return toast('pick a project first', true);
  try { const j = await post('/api/snap', {project:S.project}); toast(j.snap ? `Snapshot ${j.snap}` : 'Nothing changed', false, 'camera'); pickProject(S.project); }
  catch (e) { toast(e.message, true); }
};
$('#unwatchbtn').onclick = async () => {
  if (!S.project || !confirm(`Stop watching ${S.project}? (history is kept)`)) return;
  try { await post('/api/unwatch', {project:S.project}); S.project=null; $('#files').innerHTML=''; $('#versions-wrap').innerHTML=''; loadProjects(); }
  catch (e) { toast(e.message, true); }
};
$('#watchbtn').onclick = async () => {
  const path = $('#newpath').value.trim(); if (!path) return;
  try { const j = await post('/api/watch', {path}); $('#newpath').value=''; toast('Watching ' + j.path, false, 'eye'); await loadProjects(); pickProject(j.path); }
  catch (e) { toast(e.message, true); }
};
loadProjects().then(() => { const first = document.querySelector('#projects .item'); if (first) first.click(); })
  .catch(e => toast(e.message, true));
setInterval(() => loadProjects().catch(()=>{}), 15000);
</script>
</body>
</html>
"""


def _render_page() -> str:
    page = re.sub(r"\{\{icon:([\w-]+)(?::([\w-]+))?\}\}",
                  lambda m: icon(m.group(1), f"ic {m.group(2)}" if m.group(2) else "ic"), _PAGE_TEMPLATE)
    return page.replace("/*ICONS_JSON*/", json.dumps({n: icon(n) for n in _ICON_PATHS}))


PAGE = _render_page()
