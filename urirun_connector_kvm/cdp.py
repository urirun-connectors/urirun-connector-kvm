# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# KVM CDP shim: re-exports the generic protocol surface from urirun.connectors.surfaces.cdp
# and adds only what is kvm-specific — endpoint wired to URIRUN_KVM_CDP_URL, launch that
# uses kvm's session_env(), and the DOM find/act UI contract (text/role/name semantics).
#
# Protocol, transport, discovery, snapshot primitives and launch helper live in the generic
# surface so browser-debug / webpage / chrome-plugin connectors share a single CDP client.
import json
import os
import subprocess
from typing import Any

try:
    from urirun_connector_kvm.backends import BackendError
except ImportError:  # pragma: no cover - flat deploy
    from backends import BackendError  # type: ignore

# Resolve the CDP surface. Prefer the installed `urirun_cdp` (the extracted package). If it is
# ABSENT — e.g. on a node provisioned without it — `urirun.connectors.surfaces.cdp` silently
# degrades to a stub whose reachable() returns False even when Chrome IS listening, breaking every
# CDP flow. So fall back to the kvm-bundled vendored copy (_cdp_impl) instead, keeping the connector
# CDP-self-sufficient on any node. (Host with urirun_cdp installed gets the canonical module.)
try:
    from urirun_cdp import cdp as _surface  # canonical, when installed
except ImportError:
    try:
        from urirun_connector_kvm import _cdp_impl as _surface  # bundled fallback (node lacks urirun-cdp)
    except ImportError:  # pragma: no cover - flat deploy
        import _cdp_impl as _surface  # type: ignore

# wire kvm-specific endpoint resolver (reads URIRUN_KVM_CDP_URL / URIRUN_KVM_CDP_PORT)
# and session env (display-aware env for the Chrome subprocess)
def _kvm_endpoint() -> str:
    return (os.environ.get("URIRUN_KVM_CDP_URL") or "http://127.0.0.1:9222").rstrip("/")

try:
    from urirun_connector_kvm.backends import session_env as _session_env
except ImportError:  # pragma: no cover - flat deploy
    from backends import session_env as _session_env  # type: ignore

_surface.configure(endpoint=_kvm_endpoint, env=_session_env)

# --------------------------------------------------------------------------- #
# re-exports — same function objects as the generic surface; monkeypatch on this
# module's namespace (cdp.reachable, cdp.navigate, …) reaches the router because
# control.py / core.py call these qualified (cdp.reachable()) not as bare imports.
# --------------------------------------------------------------------------- #
endpoint = _surface.endpoint
reachable = _surface.reachable
navigate = _surface.navigate
page_ready = _surface.page_ready
evaluate = _surface.evaluate          # raw JS eval (snapshot/restore/navigate-inverse capture)
command = _surface.command            # raw CDP method call (e.g. Page.captureScreenshot)
CdpError = _surface.CdpError          # raised by evaluate when no page / JS throws

# --------------------------------------------------------------------------- #
# launch — kept here (not re-exported) because tests patch cdp._find_chrome and
# cdp.os.makedirs through this module's namespace, and because start_session calls
# reachable() / navigate() / session_env() all kvm-qualified.
# --------------------------------------------------------------------------- #
_CDP_AWAIT_TIMEOUT = 12.0
_CDP_LAUNCH_TIMEOUT = 14.0

_CHROME_CANDIDATES = ("google-chrome-stable", "google-chrome", "chromium-browser",
                      "chromium", "brave-browser", "microsoft-edge")
_AUTH_FILES = ("Local State", "Default/Cookies", "Default/Network/Cookies",
               "Default/Login Data", "Default/Preferences", "Default/Web Data")


def _find_chrome() -> str:
    import shutil
    for c in (os.environ.get("URIRUN_KVM_CHROME"), *_CHROME_CANDIDATES):
        if c and shutil.which(c):
            return shutil.which(c)
    raise BackendError("no chrome/chromium binary found")


def _copy_auth(src: str, dst: str) -> list:
    import shutil
    copied = []
    src = os.path.expanduser(src)
    for rel in _AUTH_FILES:
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        if os.path.exists(s):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            try:
                shutil.copy2(s, d)
                copied.append(rel)
            except Exception:  # noqa: BLE001
                pass
    return copied


def _running_user_data_dir() -> str | None:
    """Read --user-data-dir from the command line of the Chrome process on our debug port."""
    import glob
    try:
        port = endpoint().rsplit(":", 1)[-1].split("/")[0]
        for cmdline_path in glob.glob("/proc/*/cmdline"):
            try:
                raw = open(cmdline_path, "rb").read().split(b"\x00")
            except OSError:
                continue
            if not any(b"chrome" in a.lower() or b"chromium" in a.lower() for a in raw[:4]):
                continue
            if not any(f"--remote-debugging-port={port}".encode() in a for a in raw):
                continue
            for arg in raw:
                if arg.startswith(b"--user-data-dir="):
                    return arg[len(b"--user-data-dir="):].decode()
    except Exception:  # noqa: BLE001
        pass
    return None


def _kill_chrome_on_port() -> None:
    """Kill the Chrome process currently listening on our debug port."""
    import signal
    import glob
    try:
        port = endpoint().rsplit(":", 1)[-1].split("/")[0]
        for cmdline_path in glob.glob("/proc/*/cmdline"):
            try:
                raw = open(cmdline_path, "rb").read().split(b"\x00")
            except OSError:
                continue
            if not any(b"chrome" in a.lower() or b"chromium" in a.lower() for a in raw[:4]):
                continue
            if not any(f"--remote-debugging-port={port}".encode() in a for a in raw):
                continue
            pid = int(cmdline_path.split("/")[2])
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def start_session(url: str = "", user_data_dir: str = "", copy_from: str = "") -> dict:
    """Reuse a live CDP endpoint, or LAUNCH a dedicated-profile debug Chrome and return
    IMMEDIATELY — does NOT block on the debug port binding.

    Spawns AT MOST one instance: if already reachable it REUSES — unless user_data_dir is
    specified and differs from the running Chrome's profile, in which case the old Chrome is
    terminated and a new one is launched with the correct profile (so auth cookies are available).
    Callers must poll readiness with await_ready rather than re-calling. ``copy_from`` clones
    auth files first so the debug profile opens already logged in."""
    base = endpoint()
    if reachable():
        if user_data_dir:
            requested = os.path.abspath(os.path.expanduser(user_data_dir))
            current = _running_user_data_dir()
            if current and os.path.abspath(os.path.expanduser(current)) != requested:
                _kill_chrome_on_port()
                import time as _t
                _t.sleep(0.5)
                # fall through to launch new Chrome with the correct profile
            else:
                nav = None
                if url:
                    try:
                        nav = navigate(url)
                    except Exception as exc:  # noqa: BLE001
                        nav = {"ok": False, "error": str(exc)}
                return {"ok": True, "reused": True, "launching": False, "endpoint": base, "navigate": nav}
        else:
            nav = None
            if url:
                try:
                    nav = navigate(url)
                except Exception as exc:  # noqa: BLE001
                    nav = {"ok": False, "error": str(exc)}
            return {"ok": True, "reused": True, "launching": False, "endpoint": base, "navigate": nav}
    port = base.rsplit(":", 1)[-1].split("/")[0]
    ddir = user_data_dir or f"/tmp/urirun-kvm-cdp-{port}"
    os.makedirs(ddir, exist_ok=True)
    copied = _copy_auth(copy_from, ddir) if copy_from else []
    argv = [_find_chrome(), f"--remote-debugging-port={port}", f"--user-data-dir={ddir}",
            "--remote-allow-origins=*",  # modern Chrome (>=111) rejects CDP WS without this
            "--no-first-run", "--no-default-browser-check", "--force-renderer-accessibility"]
    # Always open a real page tab. Without a URL arg, Chrome (with a fresh dedicated profile) can come
    # up showing only its browser UI — `/json` then lists a `browser_ui` target and NO `page`, so
    # reachable()/navigate (which need a `page` target) fail even though the debugger is up. `about:blank`
    # guarantees a page tab; the subsequent navigate step drives it to the real URL.
    argv.append(url or "about:blank")
    # Capture Chrome's stderr to a log in the profile dir instead of DEVNULL: when the debug port
    # never binds ("debugger not reachable"), the reason (display unavailable, profile lock, crash,
    # port in use) is in here — await_ready surfaces its tail so the failure is diagnosable.
    log_path = os.path.join(ddir, "chrome-stderr.log")
    try:
        _logf = open(log_path, "wb")
    except OSError:
        _logf = subprocess.DEVNULL
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=_logf,
                            start_new_session=True, env=_session_env())
    return {"ok": True, "reused": False, "launching": True, "endpoint": base, "pid": proc.pid,
            "userDataDir": ddir, "authCopied": copied, "stderrLog": log_path}


def _chrome_stderr_tail(port: str, limit: int = 700) -> str:
    """Tail of the launched Chrome's stderr log (deterministic default profile dir), for diagnosing a
    debug port that never binds. Empty when the log is absent (e.g. a custom user_data_dir)."""
    try:
        with open(f"/tmp/urirun-kvm-cdp-{port}/chrome-stderr.log", errors="replace") as fh:
            return fh.read()[-limit:].strip()
    except OSError:
        return ""


def _raw_json_probe(base: str) -> str:
    """Probe the debug HTTP endpoint directly so we can tell WHY reachable() is False: a connection
    error (port not bound), an HTTP error (DNS-rebinding Host rejection), or a 200 with N targets but
    no `type==page` (Chrome up but no page tab). Returns a short diagnostic string."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{base}/json", timeout=3) as r:
            data = json.loads(r.read() or b"[]")
        types: dict[str, int] = {}
        for t in data if isinstance(data, list) else []:
            types[t.get("type", "?")] = types.get(t.get("type", "?"), 0) + 1
        pages = sum(1 for t in (data or []) if t.get("type") == "page" and t.get("webSocketDebuggerUrl"))
        return f"/json 200: {len(data)} targets {types}, usable pages={pages}"
    except Exception as exc:  # noqa: BLE001
        return f"/json probe error: {type(exc).__name__}: {exc}"[:160]


def await_ready(timeout: float = _CDP_AWAIT_TIMEOUT) -> dict:
    """Poll until the CDP debug endpoint is reachable, WITHOUT launching anything.
    Idempotent; safe to call repeatedly (never spawns a competing Chrome)."""
    import time as _t
    base = endpoint()
    deadline = _t.monotonic() + max(0.0, float(timeout))
    while True:
        if reachable():
            return {"ok": True, "ready": True, "endpoint": base}
        if _t.monotonic() >= deadline:
            port = base.rsplit(":", 1)[-1].split("/")[0]
            out = {"ok": False, "ready": False, "endpoint": base,
                   "error": "debugger not reachable within timeout"}
            tail = _chrome_stderr_tail(port)
            if tail:
                out["chromeStderr"] = tail  # WHY the port never bound (display/lock/crash)
            out["jsonProbe"] = _raw_json_probe(base)  # connection vs HTTP-reject vs no-page
            return out
        _t.sleep(0.5)


def launch_session(url: str = "", user_data_dir: str = "", copy_from: str = "",
                   wait: float = _CDP_LAUNCH_TIMEOUT) -> dict:
    """Back-compat one-shot: start_session then await_ready. Prefer the split in handlers
    (start returns fast; readiness polls on its own budget) so Chrome's cold-start
    can't blow the node handler's exec cap."""
    r = start_session(url=url, user_data_dir=user_data_dir, copy_from=copy_from)
    if r.get("reused"):
        return r
    ready = await_ready(timeout=wait)
    r["ok"] = bool(ready.get("ready"))
    r["launching"] = not ready.get("ready")
    if not ready.get("ready"):
        r["error"] = ready.get("error", "debugger did not come up within timeout")
    return r


# --------------------------------------------------------------------------- #
# DOM find / act — kvm UI contract (text / role / name semantics over CDP).
# These call _surface.evaluate() so transport lives in the generic surface.
# --------------------------------------------------------------------------- #
_JS = r"""
(function(q){
  function roleOf(el){
    var r=(el.getAttribute&&el.getAttribute('role'))||'';
    if(r) return r.toLowerCase();
    var m={BUTTON:'button',A:'link',INPUT:'textbox',TEXTAREA:'textbox',SELECT:'combobox'};
    if(el.tagName==='INPUT'){var t=(el.type||'').toLowerCase(); if(t==='button'||t==='submit')return'button';}
    if(el.isContentEditable) return 'textbox';
    return (m[el.tagName]||'').toLowerCase();
  }
  function nameOf(el){
    return ((el.getAttribute&&(el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.getAttribute('title')))
            || el.value || el.innerText || el.textContent || '').trim();
  }
  var wantText=(q.text||q.name||'').trim().toLowerCase();
  var wantRole=(q.role||'').trim().toLowerCase();
  var best=null, bestArea=1e18;
  var all=document.querySelectorAll('*');
  for(var i=0;i<all.length;i++){
    var el=all[i], r=el.getBoundingClientRect();
    if(r.width<3||r.height<3) continue;
    if(r.bottom<0||r.top>innerHeight||r.right<0||r.left>innerWidth) continue;
    var rl=roleOf(el);
    if(wantRole && rl!==wantRole) continue;
    var nm=nameOf(el);
    if(wantText && nm.toLowerCase().indexOf(wantText)<0) continue;
    if(!wantText && !wantRole) continue;
    var area=r.width*r.height;
    if(area<bestArea){ best=el; bestArea=area; }    // smallest match = most specific
  }
  if(!best) return {found:false};
  var r=best.getBoundingClientRect();
  var info={found:true, role:roleOf(best), name:nameOf(best).slice(0,80),
            value:(best.value!==undefined?best.value:(best.textContent||'')),
            x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
            w:Math.round(r.width), h:Math.round(r.height)};
  if(q.op==='click'){ best.scrollIntoView({block:'center'}); best.click(); info.clicked=true; }
  else if(q.op==='fill'){
    best.focus();
    if(best.tagName==='INPUT'||best.tagName==='TEXTAREA'){
      best.value=q.value; best.dispatchEvent(new Event('input',{bubbles:true}));
    } else { try{document.execCommand('insertText',false,q.value);}catch(e){ best.textContent=q.value; } }
    info.filled=true;
  }
  return info;
})(__ARG__)
"""


def targets() -> list[dict]:
    """Query available page targets from the CDP debug endpoint."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{endpoint()}/json", timeout=min(_surface._TIMEOUT, 2.5)) as r:
            data = json.loads(r.read() or "[]")
        return [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    except Exception:
        return []


def select_target(target_id: str = "", url_filter: str = "") -> dict:
    """Select a specific page target by ID or URL filter. Fails closed on ambiguity or miss."""
    pages = targets()
    if target_id:
        matched = [p for p in pages if p.get("id") == target_id]
        if not matched:
            raise BackendError(f"cdp: target_id {target_id!r} not found")
        if len(matched) > 1:
            raise BackendError(f"cdp: ambiguous target_id {target_id!r}")
        return matched[0]
    if url_filter:
        matched = [p for p in pages if url_filter in (p.get("url") or "")]
        if not matched:
            raise BackendError(f"cdp: no target matching url_filter {url_filter!r}")
        if len(matched) > 1:
            raise BackendError(f"cdp: ambiguous url_filter {url_filter!r} (matched {len(matched)} targets)")
        return matched[0]
    if len(pages) > 1:
        raise BackendError("cdp: multiple page targets; select one explicitly via target_id or url_filter")
    if not pages:
        raise BackendError("cdp: no page targets available")
    return pages[0]


def evaluate_target(expr: str, target_id: str = "", url_filter: str = "") -> Any:
    """Evaluate a JS expression on an explicitly selected target or the unique active page."""
    if not target_id and not url_filter:
        pages = targets()
        if len(pages) > 1:
            raise BackendError("cdp: multiple page targets; select one explicitly via target_id or url_filter")
        if not pages:
            raise BackendError("cdp: no page targets available")
        target = pages[0]
    else:
        target = select_target(target_id=target_id, url_filter=url_filter)

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BackendError(f"cdp: target {target.get('id')} has no webSocketDebuggerUrl")
    s = _surface._ws_connect(ws_url)
    try:
        msg = _surface._call(s, 1, "Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True
        })
        if msg.get("error"):
            raise BackendError(f"cdp eval error: {msg['error']}")
        r = msg.get("result", {})
        err = r.get("exceptionDetails")
        if err:
            raise BackendError(f"cdp eval error: {err.get('text', err)}")
        return r.get("result", {}).get("value")
    finally:
        try:
            s.close()
        except Exception:
            pass


def _run(op: str, text: str, role: str, name: str, value: str = "", target_id: str = "", url_filter: str = "") -> dict:
    arg = json.dumps({"op": op, "text": text, "role": role, "name": name, "value": value})
    try:
        res = evaluate_target(_JS.replace("__ARG__", arg), target_id=target_id, url_filter=url_filter)
    except (_surface.CdpError, BackendError) as exc:
        raise BackendError(str(exc)) from exc
    if not isinstance(res, dict):
        raise BackendError(f"cdp: unexpected eval result {res!r}")
    res.setdefault("found", False)
    res["via"] = "cdp"
    res["coord_space"] = "viewport-css"
    return res


def find(text: str = "", role: str = "", name: str = "", target_id: str = "", url_filter: str = "") -> dict:
    return _run("locate", text, role, name, target_id=target_id, url_filter=url_filter)


def act(op: str, text: str = "", role: str = "", name: str = "", value: str = "", target_id: str = "", url_filter: str = "") -> dict:
    res = _run(op, text, role, name, value, target_id=target_id, url_filter=url_filter)
    if not res.get("found"):
        raise BackendError(f"cdp: element not found (text={text!r} role={role!r} name={name!r})")
    res["ok"] = True
    return res
