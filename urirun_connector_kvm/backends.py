"""Decorator-registered, cross-platform desktop-control backends for ``kvm://``.

Each backend wraps **one** helpful library or OS tool and registers itself with a
``@backend(action, name, ...)`` decorator. At call time ``dispatch(action, ...)``
picks the highest-priority backend that is *available* on the live
platform/session and tries it, falling through to the next on failure — so the
same ``kvm://`` routes work on Linux (Wayland *and* X11), Windows and macOS,
using whatever capture/input libraries happen to be installed.

Add a backend for a new tool by writing one decorated function; nothing else in
the connector changes. ``needs_bin``/``needs_mod`` gate availability and double as
install hints surfaced by the ``doctor`` route.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from urirun.connectors.backend_registry import BackendError, have_bin, have_mod  # noqa: F401


# --------------------------------------------------------------------------- #
# platform / session detection
# --------------------------------------------------------------------------- #
def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or (f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else "")


def _wayland_socket() -> str | None:
    """Name of a live ``wayland-*`` socket under the runtime dir, if any."""
    xrd = _runtime_dir()
    if not xrd:
        return None
    try:
        socks = sorted(n for n in os.listdir(xrd)
                       if n.startswith("wayland-") and not n.endswith(".lock"))
    except OSError:
        return None
    return socks[0] if socks else None


def _x_display() -> str | None:
    """First X display socket under ``/tmp/.X11-unix``, as a ``:N`` display string."""
    try:
        socks = sorted(n for n in os.listdir("/tmp/.X11-unix") if n.startswith("X") and n[1:].isdigit())
    except OSError:
        return None
    return f":{socks[0][1:]}" if socks else None


def is_wayland() -> bool:
    if bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    # A node process is usually spawned without the graphical session's env, so the
    # vars above are empty even on a live Wayland seat — which mis-tags the box as
    # linux-x11 and drops grim/portal from the capture candidates. Probe the
    # compositor socket under the runtime dir as ground truth (same discovery the
    # input/capture paths already use via session_env()).
    return _wayland_socket() is not None


def is_x11() -> bool:
    return (bool(os.environ.get("DISPLAY")) or _x_display() is not None) and not is_wayland()


def platform_tag() -> str:
    if sys.platform.startswith("linux"):
        return "linux-wayland" if is_wayland() else "linux-x11"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith(("win", "cygwin")):
        return "windows"
    return "linux-x11"


ALL_PLATFORMS = ("linux-wayland", "linux-x11", "windows", "macos")

_TESSERACT_TSV_COLS = 12    # number of tab-separated columns in tesseract TSV output
_TESSERACT_WORD_LEVEL = "5" # level=5 means word in tesseract's page-hierarchy
_MIN_QUERY_TERM_LEN = 3     # ignore 1-2 char tokens in multi-word query expansion
_MAX_SCREEN_COORD = 100_000 # sanity bound: reject AT-SPI bboxes outside plausible screen area


# --------------------------------------------------------------------------- #
# registry + @backend decorator — have_bin / have_mod / BackendError re-exported
# from urirun.connectors.backend_registry (generic kernel); Backend / @backend /
# dispatch stay kvm-local so test patches on B.have_mod / B.platform_tag reach
# the right lookup in kvm's module globals.
# --------------------------------------------------------------------------- #
@dataclass
class Backend:
    action: str
    name: str
    fn: Callable[..., Any]
    priority: int = 50
    platforms: tuple = ALL_PLATFORMS
    needs_bin: tuple = ()
    needs_mod: tuple = ()
    needs_compositor: tuple = ()   # tags the active compositor must satisfy (e.g. ('wlroots',))
    needs_dev: tuple = ()          # device nodes that must be writable (e.g. ('/dev/uinput',))

    def missing(self) -> dict:
        return {
            "bin": [b for b in self.needs_bin if not have_bin(b)],
            "mod": [m for m in self.needs_mod if not have_mod(m)],
            "dev": [d for d in self.needs_dev if not os.access(d, os.W_OK)],
        }

    def available(self) -> bool:
        if platform_tag() not in self.platforms:
            return False
        m = self.missing()
        if m["bin"] or m["mod"] or m["dev"]:
            return False
        if self.needs_compositor:
            active = compositor_tag()
            if active != "wlroots" and "wlroots" in self.needs_compositor:
                # grim/portal-input depend on wlr-protocols; GNOME (Mutter) and KDE (KWin) ship
                # their own protocols and will always reject them — report unavailable so the
                # ``doctor`` route doesn't advertise a backend that fails at capture time.
                return False
        return True


_REGISTRY: dict[str, list[Backend]] = {}


def backend(action: str, name: str, *, priority: int = 50, platforms: tuple = ALL_PLATFORMS,
            needs_bin: tuple = (), needs_mod: tuple = (),
            needs_compositor: tuple = (), needs_dev: tuple = ()) -> Callable:
    """Register ``fn`` as a backend for ``action``. Highest priority + available wins."""
    def deco(fn: Callable) -> Callable:
        _REGISTRY.setdefault(action, []).append(
            Backend(action, name, fn, priority, platforms, tuple(needs_bin), tuple(needs_mod),
                    tuple(needs_compositor), tuple(needs_dev)))
        _REGISTRY[action].sort(key=lambda b: -b.priority)
        return fn
    return deco


def backends_for(action: str) -> list["Backend"]:
    """Public accessor: registered backends for an action (registration order)."""
    return list(_REGISTRY.get(action, []))


def dispatch(action: str, **kwargs: Any) -> dict:
    """Run ``action`` through the best available backend, returning a result dict
    with ``backend`` set, or raising ``BackendError`` with per-backend diagnostics."""
    candidates = _REGISTRY.get(action, [])
    if not candidates:
        raise BackendError(f"no backends registered for action {action!r}")
    tried, errors = [], []
    for b in candidates:
        if not b.available():
            continue
        tried.append(b.name)
        try:
            result = b.fn(**kwargs) or {}
            result.setdefault("backend", b.name)
            result["platform"] = platform_tag()
            if errors:  # higher-priority backends that failed before this one won —
                result["backendErrors"] = errors  # visible remotely (no node-side logs)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{b.name}: {exc}")
    if not tried:
        hints = []
        for b in candidates:
            if platform_tag() in b.platforms:
                miss = b.missing()
                want = miss["bin"] + miss["mod"] + miss["dev"]
                if want:
                    hints.append(f"{b.name} (install: {', '.join(want)})")
        raise BackendError(f"no available backend for {action!r} on {platform_tag()}; "
                           f"options: {'; '.join(hints) or 'none'}")
    raise BackendError(f"all backends failed for {action!r}: {' | '.join(errors)}")


def registry_report() -> dict:
    """Diagnostics for the ``doctor`` route: which backend serves each action."""
    out = {}
    for action, backs in _REGISTRY.items():
        out[action] = [{"name": b.name, "priority": b.priority, "available": b.available(),
                        "platforms": list(b.platforms), "missing": b.missing()} for b in backs]
    return out


def _run(argv: list[str], *, env: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    # Default to the discovered session env so display-dependent tools (grim, scrot,
    # gnome-screenshot, wtype, xdotool…) can reach the compositor/X server even when the
    # node process was spawned without graphical session vars. Callers that pass an
    # explicit env (ydotool's _yd_env) keep full control.
    p = subprocess.run(argv, capture_output=True, text=True,
                       env=session_env() if env is None else env, timeout=timeout)
    if p.returncode != 0:
        raise BackendError((p.stderr or f"{argv[0]} exit {p.returncode}").strip()[:200])
    return p


# --------------------------------------------------------------------------- #
# CAPTURE backends
# --------------------------------------------------------------------------- #
_PORTAL_SCRIPT = r"""
import sys, dbus, dbus.mainloop.glib
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus(); token = "urisys_portal"
sender = bus.get_unique_name()[1:].replace(".", "_")
rp = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
state = {"uri": None, "error": None}
_CODES = {1: "portal cancelled (code 1)",
          2: "portal denied (code 2) — capture needs a one-time screenshot permission grant "
             "for this app (accept the portal dialog once, or grant it in the desktop's "
             "Privacy/Screenshot settings); on locked-down GNOME the shell may block it entirely"}
def _r(resp, res):
    state["error"] = (_CODES.get(int(resp), f"portal code {resp}") if int(resp)
                      else (None if "uri" in res else "missing uri"))
    state["uri"] = str(res.get("uri")) if "uri" in res else None; loop.quit()
bus.add_signal_receiver(_r, dbus_interface="org.freedesktop.portal.Request", path=rp, signal_name="Response")
proxy = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
dbus.Interface(proxy, "org.freedesktop.portal.Screenshot").Screenshot("", {"handle_token": token, "interactive": False})
loop = GLib.MainLoop(); GLib.timeout_add(12000, lambda: (loop.quit(), False)[1]); loop.run()
if state["error"]: print(state["error"], file=sys.stderr); sys.exit(2)
if not state["uri"]: print("timeout", file=sys.stderr); sys.exit(3)
print(state["uri"])
"""


def _portal_python() -> str | None:
    """A python interpreter that can import dbus+gi (the node venv often can't, but
    the system python can after `dnf/apt install python3-gobject python3-dbus`)."""
    for c in (os.environ.get("URISYS_PORTAL_PYTHON"), "/usr/bin/python3", shutil.which("python3"), sys.executable):
        if not c:
            continue
        try:
            if subprocess.run([c, "-c", "import dbus, gi"], capture_output=True, timeout=5).returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


_MIN_REAL_PORTAL_BYTES = 20_000  # real screenshots are hundreds of KB; ~3.8 KB = empty/denied placeholder


@backend("capture", "portal", priority=95, platforms=("linux-wayland", "linux-x11"))
def _cap_portal(output: str, **_: Any) -> dict:
    """XDG Desktop Portal screenshot — the only sanctioned live capture on GNOME/KDE
    Wayland. Runs via a system python with dbus+gi; needs a one-time permission grant."""
    py = _portal_python()
    if not py:
        raise BackendError("portal needs python3 with dbus+gi (install python3-gobject python3-dbus)")
    import urllib.parse
    from pathlib import Path
    # Needs the session D-Bus address + XDG_RUNTIME_DIR to reach the portal; session_env()
    # fills both in from the runtime dir when the node process was started without them.
    p = _run([py, "-c", _PORTAL_SCRIPT], env=session_env(), timeout=20)
    src = Path(urllib.parse.urlparse(p.stdout.strip()).path)
    data = src.read_bytes()
    if len(data) < _MIN_REAL_PORTAL_BYTES:
        # The portal accepted the call but returned a tiny placeholder — this is the
        # GNOME-Wayland "screenshot permission denied / no active session" failure mode.
        # Raise BackendError so dispatch() falls through to the mutter-screencast backend.
        raise BackendError(
            f"xdg-portal returned a {len(data)}-byte placeholder "
            "(portal blocked or screenshot permission not granted — "
            "grant it in GNOME Settings → Privacy → Screen Sharing, "
            "or install gstreamer pipewiresrc for the mutter-screencast backend)"
        )
    Path(output).write_bytes(data)
    return {"path": output, "bytes": len(data), "via": "xdg-portal"}


_MUTTER_SCRIPT = r"""
import sys, json, dbus, dbus.mainloop.glib, gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
out = sys.argv[1]
selector = sys.argv[2] if len(sys.argv) > 2 else "0"
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()
def _logical_monitors():
    dc = dbus.Interface(bus.get_object("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig"),
                        "org.gnome.Mutter.DisplayConfig")
    _s, physical, logical, _p = dc.GetCurrentState()
    modes = {}
    names = {}
    for mon in physical:
        spec, mode_list, props = mon
        conn = str(spec[0])
        names[conn] = str(props.get("display-name") or "")
        for mode in mode_list:
            mprops = mode[6]
            if bool(mprops.get("is-current")):
                modes[conn] = (int(mode[1]), int(mode[2]))
                break
    out = []
    fb = None
    primary_conn = None
    for idx, lm in enumerate(logical):
        x, y, scale = int(lm[0]), int(lm[1]), float(lm[2])
        primary, mons = lm[4], lm[5]
        if mons:
            conn = str(mons[0][0]); fb = fb or conn
            width, height = modes.get(conn, (0, 0))
            logical_width = int(round(width / scale)) if scale and width else 0
            logical_height = int(round(height / scale)) if scale and height else 0
            if primary: primary_conn = conn
            out.append({"index": idx + 1, "connector": conn, "primary": bool(primary),
                        "x": x, "y": y, "scale": scale,
                        "width": width, "height": height,
                        "logicalWidth": logical_width, "logicalHeight": logical_height,
                        "displayName": names.get(conn, "")})
    bbox = None
    rects = [(m["x"], m["y"], m.get("logicalWidth") or 0, m.get("logicalHeight") or 0)
             for m in out if (m.get("logicalWidth") or 0) > 0 and (m.get("logicalHeight") or 0) > 0]
    if rects:
        minx = min(r[0] for r in rects); miny = min(r[1] for r in rects)
        maxx = max(r[0] + r[2] for r in rects); maxy = max(r[1] + r[3] for r in rects)
        bbox = [int(minx), int(miny), int(maxx - minx), int(maxy - miny)]
    return out, primary_conn or fb, bbox
monitors, primary, bbox = _logical_monitors()
record_all = selector in ("all", "-1")
conn = None
if not record_all:
    try:
        num = int(selector)
    except Exception:
        num = 0
    if num > 0 and num <= len(monitors):
        conn = monitors[num - 1]["connector"]
    elif num > 0:
        print("monitor %s not available; active monitors: %s" %
              (num, ",".join(str(m.get("index")) for m in monitors)), file=sys.stderr)
        sys.exit(4)
    else:
        conn = primary
    if not conn: print("no active monitor", file=sys.stderr); sys.exit(4)
sc = dbus.Interface(bus.get_object("org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast"),
                    "org.gnome.Mutter.ScreenCast")
session = dbus.Interface(bus.get_object("org.gnome.Mutter.ScreenCast", sc.CreateSession({})),
                         "org.gnome.Mutter.ScreenCast.Session")
if record_all:
    if bbox:
        stream = session.RecordArea(bbox[0], bbox[1], bbox[2], bbox[3], {"cursor-mode": dbus.UInt32(1)})
    else:
        stream = session.RecordVirtual({"cursor-mode": dbus.UInt32(1)})
else:
    stream = session.RecordMonitor(conn, {"cursor-mode": dbus.UInt32(1)})
state = {}; loop = GLib.MainLoop()
bus.add_signal_receiver(lambda n: (state.__setitem__("node", int(n)), loop.quit()),
                        dbus_interface="org.gnome.Mutter.ScreenCast.Stream",
                        path=stream, signal_name="PipeWireStreamAdded")
session.Start()
GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
loop.run()
node = state.get("node")
if node is None: session.Stop(); print("no pipewire node (ScreenCast unavailable/restricted)", file=sys.stderr); sys.exit(5)
Gst.init(None)
pipe = Gst.parse_launch("pipewiresrc path=%d num-buffers=1 ! videoconvert ! pngenc snapshot=true ! filesink location=%s" % (node, out))
pipe.set_state(Gst.State.PLAYING)
msg = pipe.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
pipe.set_state(Gst.State.NULL); session.Stop()
if msg and msg.type == Gst.MessageType.ERROR:
    e, _d = msg.parse_error(); print("gstreamer: %s" % e, file=sys.stderr); sys.exit(6)
print(json.dumps({"path": out, "scope": "all-monitors" if record_all else "monitor",
                  "connector": conn or "", "monitor": -1 if record_all else int(selector or "0"),
                  "monitors": monitors, "bbox": bbox or []}))
"""


def _mutter_python() -> "str | None":
    """A python that can import dbus+gi+gstreamer (system python, not the node venv)."""
    chk = "import dbus, gi; gi.require_version('Gst','1.0'); from gi.repository import Gst"
    for c in (os.environ.get("URISYS_PORTAL_PYTHON"), "/usr/bin/python3", shutil.which("python3"), sys.executable):
        if not c:
            continue
        try:
            if subprocess.run([c, "-c", chk], capture_output=True, timeout=5).returncode == 0:
                return c
        except Exception:  # noqa: BLE001
                continue
    return None


def _png_dimensions(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) >= 24 and head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return None


_WARM_PROTO = 3  # keep in lockstep with capture_worker.PROTO


def _warm_selector(monitor: int, scope: str) -> str:
    all_scopes = {"all", "all-monitors", "desktop"}
    return "all" if str(scope or "").strip().lower() in all_scopes or monitor < 0 else str(monitor)


def _warm_socket(selector: str) -> str:
    return os.path.join(_runtime_dir(), "urirun-kvm-warm-%s.sock" % selector)


def _spawn_warm_worker(selector: str, sock: str) -> None:
    """Start the detached warm-capture daemon (capture_worker.py next to this file —
    the same layout packaged AND flat-deployed). It negotiates the ScreenCast session
    once and serves frames on ``sock`` until idle-exit; we do not wait for it here."""
    py = _mutter_python()
    if not py:
        raise BackendError("warm capture needs python3 with dbus+gi+gstreamer")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_worker.py")
    if not os.path.exists(worker):
        raise BackendError("capture_worker.py not deployed next to backends.py")
    subprocess.Popen([py, worker, sock, selector], env=session_env(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _warm_request(sock: str, output: str, max_width: int = 0, fmt: str = "") -> dict:
    """One frame from the warm worker; raises OSError/ValueError on a dead socket."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(15)
    try:
        c.connect(sock)
        c.sendall((json.dumps({"output": output, "max_width": int(max_width or 0),
                               "fmt": fmt or "png"}) + "\n").encode("utf-8"))
        resp = json.loads(c.makefile("r").readline() or "{}")
    finally:
        c.close()
    if not resp.get("ok"):
        raise ValueError(str(resp.get("error") or "warm worker returned no frame"))
    return resp


def _warm_dimensions_valid(meta: dict, dims: tuple[int, int] | None, max_width: int = 0) -> bool:
    """Reject a PipeWire node that does not match the negotiated ScreenCast geometry.

    PipeWire node ids are only meaningful for the negotiated Mutter stream. A stale or
    wrongly resolved id can otherwise produce a perfectly valid PNG from another source
    (observed live as a 640x480 camera frame for a 1920x1080 monitor). Such a frame must
    never be reported as a successful desktop capture.
    """
    if not dims:
        return False
    src = meta.get("srcSize") or []
    if len(src) < 2:
        return False
    src_w, src_h = int(src[0] or 0), int(src[1] or 0)
    if src_w <= 0 or src_h <= 0:
        return False
    expected_w = min(src_w, int(max_width)) if int(max_width or 0) > 0 else src_w
    expected_h = max(2, int(round(src_h * expected_w / src_w)) // 2 * 2)
    return dims == (expected_w, expected_h)


@backend("capture", "mutter-warm", priority=99, platforms=("linux-wayland",))
def _cap_mutter_warm(output: str, monitor: int = 0, scope: str = "",
                     max_width: int = 0, fmt: str = "", **_: Any) -> dict:
    """WARM mutter capture: a long-lived worker holds the ScreenCast session + pipewire
    node open, so a frame costs a tiny gst pipeline instead of the full dbus negotiation
    (~150-300 ms vs ~700-1200 ms — Tier 1 of PERFORMANCE-REFACTOR). First call spawns the
    worker and falls through (BackendError) to the cold ``mutter`` backend, so callers
    always get a frame; the worker idle-exits after 120 s without requests."""
    selector = _warm_selector(monitor, scope)
    sock = _warm_socket(selector)
    if not os.path.exists(sock):
        _spawn_warm_worker(selector, sock)
        raise BackendError("warm capture worker starting — cold path serves this call")
    try:
        meta = _warm_request(sock, output, max_width, fmt)
        if meta.get("proto") != _WARM_PROTO:  # worker predates the last deploy
            raise ValueError("outdated warm worker (proto %s != %s) — retiring"
                             % (meta.get("proto"), _WARM_PROTO))
        dims = _png_dimensions(output)
        if not _warm_dimensions_valid(meta, dims, max_width):
            raise ValueError(
                "warm PipeWire frame geometry %s does not match ScreenCast source %s"
                % (dims, meta.get("srcSize"))
            )
    except (OSError, ValueError) as exc:
        try:  # stale socket (worker crashed/idle-exited mid-check): clear + respawn next call
            os.unlink(sock)
        except OSError:
            pass
        raise BackendError("warm capture failed (%s) — cold path serves this call" % exc)
    data_len = os.path.getsize(output)
    return {"path": output, "bytes": data_len, "via": "mutter-screencast-warm",
            "format": fmt or "png",
            **({"width": dims[0], "height": dims[1]} if dims else {}),
            **{k: v for k, v in meta.items() if k not in {"path", "ok"}}}


@backend("capture", "mutter", priority=98, platforms=("linux-wayland",))
def _cap_mutter(output: str, monitor: int = 0, scope: str = "", **_: Any) -> dict:
    """GNOME Mutter ScreenCast -> PipeWire capture. Headless and consent-free (the path
    gnome-remote-desktop uses), so it works where the screenshot portal denies non-interactive
    use on GNOME-Wayland. Falls through (BackendError) on non-GNOME (no Mutter bus). Runs via a
    system python with dbus+gi+gstreamer (pipewiresrc)."""
    py = _mutter_python()
    if not py:
        raise BackendError("mutter screencast needs python3 with dbus+gi+gstreamer "
                           "(install python3-gobject python3-dbus gstreamer1.0-plugins-* incl. pipewiresrc)")
    from pathlib import Path
    selector = "all" if str(scope or "").strip().lower() in {"all", "all-monitors", "desktop"} or monitor < 0 else str(monitor)
    proc = _run([py, "-c", _MUTTER_SCRIPT, output, selector], env=session_env(), timeout=30)
    data = Path(output).read_bytes()
    dims = _png_dimensions(output)
    if selector == "all" and dims == (1, 1):
        raise BackendError("mutter RecordVirtual returned a 1x1 placeholder for all monitors")
    try:
        meta = json.loads(proc.stdout.strip() or "{}")
    except Exception:
        meta = {}
    return {"path": output, "bytes": len(data), "via": "mutter-screencast",
            **({"width": dims[0], "height": dims[1]} if dims else {}),
            **{k: v for k, v in meta.items() if k not in {"path"}}}



def compositor_tag() -> str:
    """Coarse classification of the active Wayland compositor, keyed to the protocol families
    a backend needs. ``wlroots`` = supports wlr-screencopy-unstable-v1 / wlr-virtual-pointer
    (Sway, Hyprland, wlroots-based); ``mutter``/``kwin``/``other`` use proprietary equivalents.
    Determined from ``XDG_CURRENT_DESKTOP`` (env or the session D-Bus) — the same ground truth
    ``_is_wlroots_compositor`` already relied on, now surfaced as a tag for availability."""
    desktop = (session_env().get("XDG_CURRENT_DESKTOP") or os.environ.get("XDG_CURRENT_DESKTOP", "")).lower()
    if any(d in desktop for d in ("gnome", "unity", "pantheon", "budgie")):
        return "mutter"      # GNOME family
    if any(d in desktop for d in ("kde", "plasma")):
        return "kwin"
    if "wlroots" in desktop or any(d in desktop for d in ("sway", "hyprland", "wayfire", "river", "labwc")):
        return "wlroots"
    if "weston" in desktop:
        return "weston"
    return "other"


def _is_wlroots_compositor() -> bool:
    """True only on compositors that support wlr-screencopy-unstable-v1 (Sway, Hyprland, etc.).
    GNOME and KDE use their own Wayland protocols and will always reject grim."""
    return compositor_tag() == "wlroots"


def _require_nonempty(output: str, via: str) -> int:
    """Validate a file-producing capture. A tool that exits 0 but writes an empty/missing file did NOT
    capture (gnome-screenshot/scrot/grim on a blocked or permission-denied session). Return the byte
    count, or raise BackendError so dispatch() cascades to the next backend instead of yielding a
    0-byte false success the higher layers must later detect and undo."""
    n = os.path.getsize(output) if os.path.exists(output) else 0
    if n == 0:
        raise BackendError(f"{via} exited 0 but produced an empty file — no capture "
                           f"(blocked session or missing screen-capture permission)")
    return n


@backend("capture", "grim", priority=85, platforms=("linux-wayland",), needs_bin=("grim",),
         needs_compositor=("wlroots",))
def _cap_grim(output: str, **_: Any) -> dict:
    if not _is_wlroots_compositor():
        desktop = os.environ.get("XDG_CURRENT_DESKTOP") or session_env().get("XDG_CURRENT_DESKTOP") or "unknown"
        raise BackendError(
            f"grim requires a wlroots compositor (Sway/Hyprland); "
            f"detected {desktop!r} — use portal backend instead"
        )
    _run(["grim", output])
    return {"path": output, "via": "grim", "bytes": _require_nonempty(output, "grim")}


@backend("capture", "mss", priority=70, platforms=("linux-x11", "windows", "macos"), needs_mod=("mss",))
def _cap_mss(output: str, monitor: int = 0, **_: Any) -> dict:
    import mss as _mss
    import mss.tools as _mss_tools  # `import mss` alone does not expose mss.tools
    with _mss.mss() as sct:
        mons = sct.monitors
        if monitor > 0 and monitor >= len(mons):
            raise BackendError(f"monitor {monitor} not available; active monitors: 1..{max(0, len(mons) - 1)}")
        mon = mons[monitor if 0 <= monitor < len(mons) else 0]
        img = sct.grab(mon)
        _mss_tools.to_png(img.rgb, img.size, output=output)
    return {"path": output, "via": "mss", "size": list(img.size)}


@backend("capture", "pillow", priority=65, platforms=("windows", "macos"), needs_mod=("PIL",))
def _cap_pillow(output: str, **_: Any) -> dict:
    from PIL import ImageGrab
    ImageGrab.grab().save(output)
    return {"path": output, "via": "PIL.ImageGrab"}


@backend("capture", "scrot", priority=60,
         platforms=("linux-x11", "linux-wayland"), needs_bin=("scrot",))
def _cap_scrot(output: str, monitor: int = 0, **_: Any) -> dict:
    if int(monitor or 0) > 0:
        raise BackendError("scrot captures the whole X11 root and cannot target a specific monitor")
    # On XWayland sessions DISPLAY is available even when WAYLAND_DISPLAY is set;
    # scrot uses X11 so it works in that environment.
    env = session_env()
    if not env.get("DISPLAY"):
        raise BackendError("scrot requires an X11 DISPLAY; not available on pure Wayland")
    _run(["scrot", "-o", output], env=env)
    return {"path": output, "via": "scrot", "bytes": _require_nonempty(output, "scrot")}


@backend("capture", "imagemagick", priority=40, platforms=("linux-x11",), needs_bin=("import",))
def _cap_im(output: str, monitor: int = 0, **_: Any) -> dict:
    if int(monitor or 0) > 0:
        raise BackendError("imagemagick root capture cannot target a specific monitor")
    _run(["import", "-window", "root", output])
    return {"path": output, "via": "imagemagick", "bytes": _require_nonempty(output, "imagemagick")}


@backend("capture", "gnome-screenshot", priority=35,
         platforms=("linux-x11", "linux-wayland"), needs_bin=("gnome-screenshot",))
def _cap_gnome(output: str, monitor: int = 0, **_: Any) -> dict:
    if int(monitor or 0) > 0:
        raise BackendError("gnome-screenshot cannot target a specific monitor")
    _run(["gnome-screenshot", "-f", output], timeout=20)
    return {"path": output, "via": "gnome-screenshot", "bytes": _require_nonempty(output, "gnome-screenshot")}


@backend("capture", "screencapture", priority=80, platforms=("macos",), needs_bin=("screencapture",))
def _cap_macos(output: str, **_: Any) -> dict:
    _run(["screencapture", "-x", output])
    return {"path": output, "via": "screencapture", "bytes": _require_nonempty(output, "screencapture")}


# --------------------------------------------------------------------------- #
# INPUT backends — ydotool (Wayland), wtype, xdotool (X11), pynput (cross-platform)
# --------------------------------------------------------------------------- #
def _ydotool_socket() -> str:
    return os.environ.get("YDOTOOL_SOCKET") or f"/run/user/{os.getuid()}/.ydotool_socket"


def ensure_ydotoold() -> str:
    """ydotool needs the ydotoold daemon holding /dev/uinput. Start it (user-owned
    socket; works without root when the user can open /dev/uinput) if not running."""
    sock = _ydotool_socket()
    running = subprocess.run(["pgrep", "-x", "ydotoold"], capture_output=True).returncode == 0
    if not running:
        env = os.environ.copy()
        env["YDOTOOL_SOCKET"] = sock
        subprocess.Popen(["ydotoold", "-p", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
        for _ in range(40):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        time.sleep(0.3)
    return sock


def _yd_env() -> dict:
    env = session_env()
    env["YDOTOOL_SOCKET"] = ensure_ydotoold()
    return env


_YD_BUTTON = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}

# evdev keycodes for ydotool `key` (subset; extend as needed)
_YD_KEY = {"enter": "28", "return": "28", "tab": "15", "esc": "1", "escape": "1", "space": "57",
           "backspace": "14", "ctrl": "29", "shift": "42", "alt": "56", "super": "125", "meta": "125",
           "left": "105", "right": "106", "up": "103", "down": "108", "a": "30", "c": "46", "v": "47",
           "x": "45", "z": "44", "t": "20", "w": "17", "l": "38",
           "f4": "62", "f5": "63", "f6": "64", "f11": "87", "f12": "88"}


def _yd_keyseq(combo: str) -> list[str]:
    keys = [_YD_KEY.get(k.strip().lower()) for k in combo.replace("-", "+").split("+")]
    keys = [k for k in keys if k]
    return [f"{k}:1" for k in keys] + [f"{k}:0" for k in reversed(keys)]


# ---- type ----
def session_env() -> dict:
    """A child-process env that can reach the live graphical session — the Wayland
    compositor, the X display, and the session D-Bus (which the screenshot portal
    needs). A urirun node process is usually spawned without any of these, so
    wl-copy/wtype/grim hang on connect and the portal's SessionBus() fails — point
    every backend at the sockets discovered under XDG_RUNTIME_DIR / /tmp/.X11-unix."""
    env = os.environ.copy()
    xrd = _runtime_dir()
    if xrd:
        env["XDG_RUNTIME_DIR"] = xrd
        if not env.get("WAYLAND_DISPLAY"):
            sock = _wayland_socket()
            if sock:
                env["WAYLAND_DISPLAY"] = sock
        if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(f"{xrd}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={xrd}/bus"
    if not env.get("DISPLAY"):
        disp = _x_display()
        if disp:
            env["DISPLAY"] = disp
    return env


# Back-compat alias: earlier code called this the wayland env.
_wayland_env = session_env


def _clipboard_set(text: str) -> str:
    """Put UTF-8 ``text`` on the system clipboard via the first available tool, so it can be
    pasted (Ctrl+V) verbatim. This is the only reliable way to enter non-ASCII on this stack:
    ydotool types through the kernel keymap and SILENTLY DROPS characters it can't map (Polish
    ł ą ę ż ó, accents, CJK, emoji). Returns the tool name, or raises so the caller fails
    loudly instead of publishing corrupted text."""
    if have_bin("wl-copy"):
        # wl-copy reads stdin, sets the selection, then forks to serve paste requests; the
        # foreground returns once it has the data. If it can't background it stays foreground
        # (selection IS already set), so don't block on it — write, give it a beat, move on.
        proc = subprocess.Popen(["wl-copy", "--type", "text/plain;charset=utf-8"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=_wayland_env())
        try:
            proc.communicate(input=text.encode(), timeout=4)
        except subprocess.TimeoutExpired:
            pass  # still serving the (already-set) selection in the foreground — fine
        return "wl-copy"
    for argv, name in (
        (["xclip", "-selection", "clipboard"], "xclip"),   # X11
        (["xsel", "--clipboard", "--input"], "xsel"),      # X11
    ):
        if have_bin(argv[0]):
            p = subprocess.run(argv, input=text.encode(), capture_output=True, env=_wayland_env(), timeout=10)
            if p.returncode == 0:
                return name
            raise BackendError(f"{name} failed: {(p.stderr or b'').decode(errors='replace')[:120]}")
    raise BackendError("non-ASCII text needs a clipboard tool (wl-clipboard / xclip / xsel) or the "
                       "browser-cdp surface — none installed on this node; ydotool cannot type it")


@backend("type", "uinput", priority=85, platforms=("linux-wayland", "linux-x11"), needs_dev=("/dev/uinput",))
def _type_uinput(text: str, **_: Any) -> dict:
    """Raw-uinput virtual keyboard — the same /dev/uinput path the pointer uses.
    Beats ydotool (prio 80) because ydotoold can report ok while its events never
    reach the session (observed on lenovo GNOME-Wayland); raw uinput lands. ASCII
    only — non-ASCII raises BackendError so dispatch falls to ydotool's clipboard path."""
    if not uinput_available():
        raise BackendError("no write access to /dev/uinput")
    if not text.isascii():
        raise BackendError("uinput keymap is ASCII-only — falling through")
    return {**uinput_type_text(text), "chars": len(text)}


@backend("key", "uinput", priority=85, platforms=("linux-wayland", "linux-x11"), needs_dev=("/dev/uinput",))
def _key_uinput(key: str = "", keys: str = "", **_: Any) -> dict:
    """Key/chord via the raw-uinput virtual keyboard (see _type_uinput)."""
    if not uinput_available():
        raise BackendError("no write access to /dev/uinput")
    return uinput_key_combo(keys or key)


@backend("type", "ydotool", priority=80, platforms=("linux-wayland", "linux-x11"), needs_bin=("ydotool", "ydotoold"))
def _type_ydotool(text: str, **_: Any) -> dict:
    # Non-ASCII: ydotool drops it, so set the clipboard and paste it verbatim instead — or fail
    # loudly (no clipboard tool) rather than silently corrupt the string.
    if not text.isascii():
        tool = _clipboard_set(text)
        _run(["ydotool", "key", *_yd_keyseq("ctrl+v")], env=_yd_env())
        return {"via": f"clipboard:{tool}+ctrl+v", "chars": len(text), "method": "paste"}
    _run(["ydotool", "type", "--", text], env=_yd_env())
    return {"via": "ydotool", "chars": len(text), "method": "keymap"}


@backend("type", "wtype", priority=60, platforms=("linux-wayland",), needs_bin=("wtype",))
def _type_wtype(text: str, **_: Any) -> dict:
    _run(["wtype", "--", text])
    return {"via": "wtype", "chars": len(text)}


@backend("type", "xdotool", priority=70, platforms=("linux-x11",), needs_bin=("xdotool",))
def _type_xdotool(text: str, **_: Any) -> dict:
    _run(["xdotool", "type", "--", text])
    return {"via": "xdotool", "chars": len(text)}


@backend("type", "pynput", priority=40, needs_mod=("pynput",))
def _type_pynput(text: str, **_: Any) -> dict:
    from pynput.keyboard import Controller
    Controller().type(text)
    return {"via": "pynput", "chars": len(text)}


# ---- click ----
@backend("click", "ydotool", priority=80, platforms=("linux-wayland", "linux-x11"), needs_bin=("ydotool", "ydotoold"))
def _click_ydotool(button: str = "left", **_: Any) -> dict:
    _run(["ydotool", "click", _YD_BUTTON.get(button, "0xC0")], env=_yd_env())
    return {"via": "ydotool", "button": button}


@backend("click", "xdotool", priority=70, platforms=("linux-x11",), needs_bin=("xdotool",))
def _click_xdotool(button: str = "left", **_: Any) -> dict:
    _run(["xdotool", "click", {"left": "1", "middle": "2", "right": "3"}.get(button, "1")])
    return {"via": "xdotool", "button": button}


@backend("click", "pynput", priority=40, needs_mod=("pynput",))
def _click_pynput(button: str = "left", **_: Any) -> dict:
    from pynput.mouse import Button, Controller
    Controller().click({"left": Button.left, "right": Button.right, "middle": Button.middle}[button])
    return {"via": "pynput", "button": button}


# ---- move (absolute) ----
# uinput-absolute wins over ydotool: a raw [0,65535] ABS device maps pixel->screen
# deterministically, so capture-space == action-space regardless of the ydotool version's
# coordinate convention. (ydotool `mousemove -a` takes RAW pixels on some builds and a
# [0,65535] range on others; on the latter a raw pixel maps near (0,0) and trips the
# GNOME hot-corner — observed live on the lenovo node.) Falls through to ydotool if
# /dev/uinput is not writable. See `uinput_abs_click` / `_screen_wh` below.
@backend("move", "uinput-abs", priority=90, platforms=("linux-wayland", "linux-x11"))
def _move_uinput_abs(x: int, y: int, **_: Any) -> dict:
    sw, sh = _screen_wh()
    settle = float(os.environ.get("URIRUN_KVM_ABS_SETTLE", "0.6"))
    r = uinput_abs_click(int(x), int(y), sw, sh, do_click=False, settle=settle)
    return {"via": "uinput-absolute", "x": x, "y": y, "abs": r.get("abs"), "screen": [sw, sh]}


@backend("move", "ydotool", priority=80, platforms=("linux-wayland", "linux-x11"), needs_bin=("ydotool", "ydotoold"))
def _move_ydotool(x: int, y: int, **_: Any) -> dict:
    _run(["ydotool", "mousemove", "-a", "-x", str(int(x)), "-y", str(int(y))], env=_yd_env())
    return {"via": "ydotool", "x": x, "y": y}


@backend("move", "xdotool", priority=70, platforms=("linux-x11",), needs_bin=("xdotool",))
def _move_xdotool(x: int, y: int, **_: Any) -> dict:
    _run(["xdotool", "mousemove", str(int(x)), str(int(y))])
    return {"via": "xdotool", "x": x, "y": y}


@backend("move", "pynput", priority=40, needs_mod=("pynput",))
def _move_pynput(x: int, y: int, **_: Any) -> dict:
    from pynput.mouse import Controller
    Controller().position = (int(x), int(y))
    return {"via": "pynput", "x": x, "y": y}


# ---- key / hotkey ----
@backend("key", "ydotool", priority=80, platforms=("linux-wayland", "linux-x11"), needs_bin=("ydotool", "ydotoold"))
def _key_ydotool(keys: str, **_: Any) -> dict:
    seq = _yd_keyseq(keys)
    if not seq:
        raise BackendError(f"unknown key combo {keys!r}")
    _run(["ydotool", "key", *seq], env=_yd_env())
    return {"via": "ydotool", "keys": keys}


@backend("key", "xdotool", priority=70, platforms=("linux-x11",), needs_bin=("xdotool",))
def _key_xdotool(keys: str, **_: Any) -> dict:
    _run(["xdotool", "key", keys.replace("+", "+")])
    return {"via": "xdotool", "keys": keys}


@backend("key", "pynput", priority=40, needs_mod=("pynput",))
def _key_pynput(keys: str, **_: Any) -> dict:
    from pynput.keyboard import Controller, Key
    kb = Controller()
    parts = keys.replace("-", "+").split("+")
    mods = [getattr(Key, p, None) for p in parts[:-1]]
    last = parts[-1]
    for m in mods:
        if m:
            kb.press(m)
    kb.press(last)
    kb.release(last)
    for m in reversed(mods):
        if m:
            kb.release(m)
    return {"via": "pynput", "keys": keys}


# ---- scroll ----
@backend("scroll", "ydotool", priority=80, platforms=("linux-wayland", "linux-x11"), needs_bin=("ydotool", "ydotoold"))
def _scroll_ydotool(dy: int = -3, **_: Any) -> dict:
    _run(["ydotool", "mousemove", "-w", "-x", "0", "-y", str(int(dy))], env=_yd_env())
    return {"via": "ydotool", "dy": dy}


@backend("scroll", "pynput", priority=40, needs_mod=("pynput",))
def _scroll_pynput(dy: int = -3, **_: Any) -> dict:
    from pynput.mouse import Controller
    Controller().scroll(0, int(dy))
    return {"via": "pynput", "dy": dy}


# --------------------------------------------------------------------------- #
# WINDOW focus / list
# --------------------------------------------------------------------------- #
@backend("focus", "wmctrl", priority=70, platforms=("linux-x11", "linux-wayland"), needs_bin=("wmctrl",))
def _focus_wmctrl(title: str, **_: Any) -> dict:
    _run(["wmctrl", "-a", title])
    return {"via": "wmctrl", "title": title}


@backend("focus", "pygetwindow", priority=40, platforms=("windows", "macos"), needs_mod=("pygetwindow",))
def _focus_pgw(title: str, **_: Any) -> dict:
    import pygetwindow as gw
    wins = gw.getWindowsWithTitle(title)
    if not wins:
        raise BackendError(f"no window matching {title!r}")
    wins[0].activate()
    return {"via": "pygetwindow", "title": title}


@backend("maximize", "wmctrl", priority=70, platforms=("linux-x11", "linux-wayland"), needs_bin=("wmctrl",))
def _maximize_wmctrl(title: str, **_: Any) -> dict:
    _run(["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"])
    return {"via": "wmctrl", "title": title}


@backend("window_geometry", "wmctrl", priority=70, platforms=("linux-x11", "linux-wayland"), needs_bin=("wmctrl",))
def _geometry_wmctrl(title: str, **_: Any) -> dict:
    """``wmctrl -lG`` columns: id desktop x y w h client_machine title... — title is whatever
    is left after the first 7 whitespace-separated fields, so split with a maxsplit to keep
    multi-word titles intact. XWayland/X11 only, same visibility limit as focus/window_list:
    NO tool here sees Wayland-native app windows (see ``_enumerate_windows`` in core.py)."""
    p = _run(["wmctrl", "-lG"])
    for line in p.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        _id, _desktop, x, y, w, h, _host, wtitle = parts
        if title.lower() in wtitle.lower():
            return {"via": "wmctrl", "title": wtitle,
                    "x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    raise BackendError(f"no window matching {title!r}")


@backend("window_list", "wmctrl", priority=70, platforms=("linux-x11", "linux-wayland"), needs_bin=("wmctrl",))
def _winlist_wmctrl(app: str = "", title: str = "", **_: Any) -> dict:
    p = _run(["wmctrl", "-l"])
    wins = [" ".join(line.split()[3:]) for line in p.stdout.splitlines() if line.strip()]
    app_q = str(app or "").strip().lower()
    title_q = str(title or "").strip().lower()
    if app_q or title_q:
        wins = [
            title
            for title in wins
            if (not app_q or app_q in title.lower())
            and (not title_q or title_q in title.lower())
        ]
    selected = {"title": wins[0]} if wins else None
    return {"via": "wmctrl", "windows": wins, "selected": selected}


# APP LAUNCH / LIST backends moved to launch_backends.py (own domain; app:// is logically
# a separate connector). Imported at the end of this module so they register.


# --------------------------------------------------------------------------- #
# AT-SPI focus — activate a window by accessible name through the accessibility
# bus. Works on native-Wayland (where wmctrl only sees Xwayland windows). Runs via
# a system python that has gi + the Atspi typelib (the node venv usually doesn't),
# mirroring the portal-capture pattern.
# --------------------------------------------------------------------------- #
def _atspi_python() -> str | None:
    probe = "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi"
    for c in (os.environ.get("URISYS_ATSPI_PYTHON"), os.environ.get("URISYS_PORTAL_PYTHON"),
              "/usr/bin/python3", shutil.which("python3"), sys.executable):
        if not c:
            continue
        try:
            if subprocess.run([c, "-c", probe], capture_output=True, timeout=6).returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return None


_ATSPI_FOCUS_SCRIPT = r"""
import sys, json
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
needle = sys.argv[1].lower()
Atspi.init()
hit = None
desktop = Atspi.get_desktop(0)
for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if app is None:
        continue
    name = (app.get_name() or "")
    for j in range(app.get_child_count()):
        frame = app.get_child_at_index(j)
        if frame is None:
            continue
        fname = (frame.get_name() or "")
        if needle in fname.lower() or needle in name.lower():
            try:
                comp = frame.get_component_iface()
                if comp is not None:
                    comp.grab_focus()
                    hit = {"app": name, "frame": fname}
                    break
            except Exception as exc:  # noqa: BLE001
                hit = {"app": name, "frame": fname, "warn": str(exc)[:80]}
    if hit:
        break
print(json.dumps(hit or {}))
"""


_ATSPI_WINDOW_LIST_SCRIPT = r"""
import sys, json
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
selector = json.loads(sys.argv[1] if len(sys.argv) > 1 else "{}")
app_q = str(selector.get("app") or "").lower()
title_q = str(selector.get("title") or "").lower()
Atspi.init()
desktop = Atspi.get_desktop(0)
windows = []

def bbox_for(node):
    try:
        comp = node.get_component_iface()
        if comp is None:
            return None
        ext = comp.get_extents(Atspi.CoordType.SCREEN)
        return [int(ext.x), int(ext.y), int(ext.width), int(ext.height)]
    except Exception:
        return None

for i in range(desktop.get_child_count()):
    app = desktop.get_child_at_index(i)
    if app is None:
        continue
    try:
        app_name = app.get_name() or ""
    except Exception:
        app_name = ""
    hay_app = app_name.lower()
    if app_q and not title_q and app_q not in hay_app:
        continue
    try:
        child_count = app.get_child_count()
    except Exception:
        child_count = 0
    for j in range(max(0, child_count)):
        frame = app.get_child_at_index(j)
        if frame is None:
            continue
        try:
            title = frame.get_name() or ""
            role = frame.get_role_name() or ""
        except Exception:
            continue
        hay_title = title.lower()
        if app_q and app_q not in hay_app and app_q not in hay_title:
            continue
        if title_q and title_q not in hay_title:
            continue
        windows.append({
            "app": app_name,
            "title": title,
            "role": role,
            "bbox": bbox_for(frame),
        })
print(json.dumps({"windows": windows}))
"""


def _mon_dims(mon: dict) -> tuple[int, int, int, int]:
    """Return (x, y, logicalWidth, logicalHeight) from a monitor dict."""
    mx = int(mon.get("x") or 0)
    my = int(mon.get("y") or 0)
    mw = int(mon.get("logicalWidth") or mon.get("width") or 0)
    mh = int(mon.get("logicalHeight") or mon.get("height") or 0)
    return mx, my, mw, mh


def _monitor_for_bbox(bbox: list | None, monitors: list[dict]) -> dict | None:
    if not bbox or len(bbox) < 4:
        return None
    x, y, w, h = [int(v) for v in bbox[:4]]
    mons = monitors or []
    if len(mons) > 1 and abs(x) <= 8 and abs(y) <= 8:
        # AT-SPI on GNOME/Wayland may report a top-level frame in monitor-local
        # coordinates. In a stacked layout this can otherwise overlap the monitor
        # below and select it by area, even though the local [0,0] frame belongs
        # to the top monitor (the common 4K-at-top case).
        min_y = min(_mon_dims(mon)[1] for mon in mons)
        top = [mon for mon in mons if _mon_dims(mon)[1] == min_y]
        fits = [mon for mon in top if w <= _mon_dims(mon)[2] + 96 and h <= _mon_dims(mon)[3] + 96]
        if fits:
            return max(fits, key=lambda mon: _mon_dims(mon)[2] * _mon_dims(mon)[3])
    cx, cy = x + max(0, w) / 2, y + max(0, h) / 2
    best: tuple[int, dict] | None = None
    for mon in mons:
        mx, my, mw, mh = _mon_dims(mon)
        if mw <= 0 or mh <= 0:
            continue
        if mx <= cx < mx + mw and my <= cy < my + mh:
            return mon
        overlap_w = max(0, min(x + w, mx + mw) - max(x, mx))
        overlap_h = max(0, min(y + h, my + mh) - max(y, my))
        area = overlap_w * overlap_h
        if area and (best is None or area > best[0]):
            best = (area, mon)
    return best[1] if best else None


def _attest_window_monitor(bbox: list | None, mon: dict | None) -> dict | None:
    """Independent geometry redundancy for a window→monitor label (the DP-2/DP-1 data-bug class).

    ``mon`` was chosen by position (``_monitor_for_bbox``). Cross-check it a SECOND, independent
    way: a window cannot be larger than the display it is on, so it must FIT in ``mon``. When a
    window is bigger than its assigned monitor the label is a lie — exactly how DP-2 (4K) got
    mislabelled DP-1 (a 2113x1592 Chrome frame cannot live on a 2048x1280 output). Emitting this
    attestation makes the seam self-report instead of trusting the position heuristic silently;
    ``ok: false`` is the layer-attribution signal a postcondition checker reads."""
    if not (bbox and len(bbox) >= 4 and isinstance(mon, dict)):
        return None
    w, h = int(bbox[2]), int(bbox[3])
    _, _, mw, mh = _mon_dims(mon)
    fits = (mw <= 0 or w <= mw + 96) and (mh <= 0 or h <= mh + 96)
    conn = mon.get("connector")
    return {"ok": bool(fits), "monitor": mon.get("index"), "connector": conn, "fits": bool(fits),
            "detail": (f"window {w}x{h} fits {conn} {mw}x{mh}" if fits
                       else f"window {w}x{h} does NOT fit monitor {conn} {mw}x{mh} — label suspect")}


@backend("window_list", "atspi", priority=85, platforms=("linux-wayland", "linux-x11"))
def _winlist_atspi(app: str = "", title: str = "", **_: Any) -> dict:
    py = _atspi_python()
    if not py:
        raise BackendError("AT-SPI window list needs python3 with gi + Atspi (install python3-gobject + gnome a11y)")
    payload = json.dumps({"app": app, "title": title})
    p = _run([py, "-c", _ATSPI_WINDOW_LIST_SCRIPT, payload], env=session_env(), timeout=25)
    data = json.loads((p.stdout or "{}").strip() or "{}")
    windows = data.get("windows") if isinstance(data, dict) else []
    if not isinstance(windows, list):
        windows = []
    monitors = _gnome_monitors()
    enriched = []
    for win in windows:
        if not isinstance(win, dict):
            continue
        mon = _monitor_for_bbox(win.get("bbox"), monitors)
        item = dict(win)
        if mon:
            item["monitor"] = mon.get("index")
            item["monitorConnector"] = mon.get("connector")
            # L8 self-attestation: independent size-fit redundancy on the monitor label.
            att = _attest_window_monitor(win.get("bbox"), mon)
            if att is not None:
                item["monitorAttestation"] = att
        enriched.append(item)
    selected = enriched[0] if enriched else None
    return {"via": "atspi", "windows": enriched, "selected": selected, "monitors": monitors}


@backend("focus", "atspi", priority=85, platforms=("linux-wayland", "linux-x11"))
def _focus_atspi(title: str, **_: Any) -> dict:
    py = _atspi_python()
    if not py:
        raise BackendError("AT-SPI focus needs python3 with gi + Atspi (install python3-gobject + gnome a11y)")
    # AT-SPI talks over the session bus; session_env() supplies DBUS_SESSION_BUS_ADDRESS
    # (and XDG_RUNTIME_DIR) when the node process was started without them.
    p = _run([py, "-c", _ATSPI_FOCUS_SCRIPT, title], env=session_env(), timeout=12)
    import json as _json
    hit = _json.loads((p.stdout or "{}").strip() or "{}")
    if not hit:
        raise BackendError(f"no accessible window matching {title!r}")
    return {"via": "atspi", "title": title, "matched": hit}


# --------------------------------------------------------------------------- #
# AT-SPI element interaction — find a UI element by (app, role, name) anywhere in
# the accessibility tree and act on it: focus, click (via its Action interface),
# or set text (EditableText). Resolution-independent — no coordinates. Web content
# needs the app's a11y enabled (e.g. Chrome: chrome://accessibility or
# --force-renderer-accessibility). Returns the element's screen bbox so a caller
# can fall back to a kvm click when no Action interface is exposed.
# --------------------------------------------------------------------------- #
_ATSPI_ACT_SCRIPT = r"""
import sys, json
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
cmd = json.loads(sys.argv[1])
app_q = cmd.get("app", "").lower()
role_q = cmd.get("role", "").lower()
name_q = cmd.get("name", "").lower()
action = cmd.get("op", "focus")
text = cmd.get("text", "")
nth = int(cmd.get("nth", 0))
budget = [int(cmd.get("limit", 6000))]
matches = []

def walk(node):
    if node is None or budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        role = node.get_role_name() or ""
        name = node.get_name() or ""
    except Exception:
        return
    if (not role_q or role_q in role.lower()) and (not name_q or name_q in name.lower()):
        if role_q or name_q:
            matches.append(node)
    try:
        n = node.get_child_count()
    except Exception:
        n = 0
    for i in range(min(n, 200)):
        try:
            walk(node.get_child_at_index(i))
        except Exception:
            continue

d = Atspi.get_desktop(0)
for i in range(d.get_child_count()):
    app = d.get_child_at_index(i)
    if app is None:
        continue
    if app_q and app_q not in (app.get_name() or "").lower():
        continue
    walk(app)

if not matches:
    print(json.dumps({"found": False, "candidates": 0})); sys.exit(0)
target = matches[min(nth, len(matches) - 1)]
info = {"found": True, "candidates": len(matches),
        "role": target.get_role_name(), "name": (target.get_name() or "")[:80]}
try:
    comp = target.get_component_iface()
    if comp:
        ext = comp.get_extents(Atspi.CoordType.SCREEN)
        info["bbox"] = [ext.x, ext.y, ext.width, ext.height]
    if action == "focus" and comp:
        info["focused"] = bool(comp.grab_focus())
    elif action == "click":
        ai = target.get_action_iface()
        if ai and ai.get_n_actions() > 0:
            ai.do_action(0); info["clicked"] = True
        elif comp:
            comp.grab_focus(); info["focused_only"] = True
    elif action == "settext":
        et = target.get_editable_text_iface()
        if et:
            et.set_text_contents(text); info["set"] = True
        elif comp:
            comp.grab_focus(); info["focused_for_type"] = True
    elif action == "gettext":
        ti = target.get_text_iface()
        if ti:
            info["text"] = ti.get_text(0, ti.get_character_count())[-4000:]
        else:
            info["text"] = target.get_name() or ""
except Exception as exc:  # noqa: BLE001
    info["warn"] = str(exc)[:120]
print(json.dumps(info))
"""


@backend("a11y", "atspi", priority=90, platforms=("linux-wayland", "linux-x11"))
def _a11y_atspi(app: str = "", role: str = "", name: str = "", op: str = "focus",
                text: str = "", nth: int = 0, **_: Any) -> dict:
    py = _atspi_python()
    if not py:
        raise BackendError("AT-SPI needs python3 with gi + Atspi (install python3-gobject + gnome a11y)")
    import json as _json
    # AT-SPI talks over the session bus — session_env() supplies DBUS_SESSION_BUS_ADDRESS.
    # Bound the tree walk: on a busy desktop a MISS walks the whole a11y tree, and the
    # router runs locate twice (atspi strategy + vision strategy each dispatch locate), so a
    # long timeout stacks past the node's exec cap and hangs. Keep it short → fall through to
    # OCR fast. Tune with URIRUN_KVM_ATSPI_TIMEOUT.
    cmd = _json.dumps({"app": app, "role": role, "name": name, "op": op, "text": text, "nth": nth})
    timeout = float(os.environ.get("URIRUN_KVM_ATSPI_TIMEOUT", "5"))
    try:
        p = _run([py, "-c", _ATSPI_ACT_SCRIPT, cmd], env=session_env(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"atspi: tree walk exceeded {timeout}s (name~{name or text!r}) — falling through") from exc
    res = _json.loads((p.stdout or "{}").strip() or "{}")
    res["via"] = "atspi"
    return res


# --------------------------------------------------------------------------- #
# LOCATE — find on-screen text and return its pixel coordinates (boxes + center)
# The "where is the button?" half of the screenshot→locate→click pipeline. Pixel
# coordinates here map 1:1 to the captured image, so capture at native resolution.
# --------------------------------------------------------------------------- #
def _tsv_lines(tsv: str, min_conf: float) -> list[dict]:
    """Aggregate tesseract word rows (level 5) into line boxes so a multi-word label
    like 'Start a post' is one match, not three."""
    groups: dict[tuple, dict] = {}
    for raw in tsv.splitlines()[1:]:           # skip header row
        c = raw.split("\t")
        if len(c) < _TESSERACT_TSV_COLS or c[0] != _TESSERACT_WORD_LEVEL:
            continue
        try:
            left, top, w, h, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        except ValueError:
            continue
        word = c[11].strip()
        if not word:
            continue
        key = (c[1], c[2], c[3], c[4])         # page, block, par, line
        g = groups.setdefault(key, {"words": [], "confs": [],
                                    "x0": 1 << 30, "y0": 1 << 30, "x1": 0, "y1": 0})
        g["words"].append(word)
        g["confs"].append(conf)
        g["x0"], g["y0"] = min(g["x0"], left), min(g["y0"], top)
        g["x1"], g["y1"] = max(g["x1"], left + w), max(g["y1"], top + h)
    out = []
    for g in groups.values():
        conf = sum(g["confs"]) / len(g["confs"])
        if conf < min_conf:
            continue
        x0, y0, x1, y1 = int(g["x0"]), int(g["y0"]), int(g["x1"]), int(g["y1"])
        out.append({"text": " ".join(g["words"]), "conf": round(conf, 1),
                    "box": [x0, y0, x1 - x0, y1 - y0], "center": [(x0 + x1) // 2, (y0 + y1) // 2]})
    return out


def _tesseract_words_by_line(tsv_stdout: str, min_conf: float) -> dict[tuple, list]:
    """Group tesseract TSV word rows (level 5, conf ≥ min) by (page,block,par,line)."""
    words: dict[tuple, list] = {}
    for raw in tsv_stdout.splitlines()[1:]:
        c = raw.split("\t")
        if len(c) < _TESSERACT_TSV_COLS or c[0] != _TESSERACT_WORD_LEVEL:
            continue
        try:
            left, top, w, h, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        except ValueError:
            continue
        t = c[11].strip()
        if t and conf >= min_conf:
            words.setdefault((c[1], c[2], c[3], c[4]), []).append(
                {"text": t, "conf": conf, "box": [left, top, w, h]})
    return words


def _line_match(ws: list, ql: str, terms: list) -> dict | None:
    """Build a match box for one TSV line if it contains ``ql``; else ``None``."""
    if ql not in " ".join(x["text"] for x in ws).lower():
        return None
    sel = [x for x in ws if any(term in x["text"].lower() for term in terms)] or ws
    x0 = min(x["box"][0] for x in sel)
    y0 = min(x["box"][1] for x in sel)
    x1 = max(x["box"][0] + x["box"][2] for x in sel)
    y1 = max(x["box"][1] + x["box"][3] for x in sel)
    return {"text": " ".join(x["text"] for x in sel),
            "conf": round(sum(x["conf"] for x in sel) / len(sel), 1),
            "box": [x0, y0, x1 - x0, y1 - y0],
            "center": [(x0 + x1) // 2, (y0 + y1) // 2]}


def _tesseract_query_matches(tsv_stdout: str, ql: str, min_conf: float) -> list[dict]:
    """Find word spans in tesseract TSV output whose line contains ``ql``."""
    words = _tesseract_words_by_line(tsv_stdout, float(min_conf))
    terms = [t for t in ql.split() if len(t) >= _MIN_QUERY_TERM_LEN] or ql.split()
    matches = [m for ws in words.values() if (m := _line_match(ws, ql, terms)) is not None]
    matches.sort(key=lambda m: -m["conf"])
    return matches


_OCR_UPSCALE = 2  # desktop UI text is small; 2x LANCZOS + sparse-psm rescues it (see bench)


def _ocr_prep(image: str) -> tuple[str, int]:
    """Preprocess a screenshot for OCR: grayscale + 2x upscale. Tesseract's default page
    segmentation finds ~nothing on a desktop screenshot (sparse small text on dark
    background — e.g. a full-res noVNC frame yields 0 words); prepped + --psm 11 the same
    frame reads every menu label at ~96% conf. Returns (path, scale); scale rescales
    boxes back to original image-px. No Pillow -> original image, scale 1."""
    try:
        from PIL import Image
    except ImportError:
        return image, 1
    try:
        with Image.open(image) as im:
            prepped = im.convert("L").resize(
                (im.width * _OCR_UPSCALE, im.height * _OCR_UPSCALE), Image.LANCZOS)
        out = os.path.join(tempfile.gettempdir(), f"kvm_ocr_prep_{os.getpid()}.png")
        prepped.save(out)
        return out, _OCR_UPSCALE
    except Exception:  # noqa: BLE001 - any decode hiccup: OCR the original
        return image, 1


def _rescale_matches(matches: list[dict], scale: int) -> list[dict]:
    if scale == 1:
        return matches
    for m in matches:
        m["box"] = [v // scale for v in m["box"]]
        m["center"] = [v // scale for v in m["center"]]
    return matches


_FUZZY_MIN = 0.78  # SequenceMatcher ratio floor: 1 flipped glyph in a short label passes


def _fuzzy_line_matches(lines: list[dict], ql: str) -> list[dict]:
    """Fuzzy fallback for OCR noise: score each OCR line against the query on
    alnum-normalized text; best ratio >= _FUZZY_MIN wins. Line-level boxes (the label
    IS the line for menus/buttons); each match carries its 'fuzzy' ratio for audit."""
    import difflib

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    qn = norm(ql)
    if not qn:
        return []
    scored = []
    for m in lines:
        r = difflib.SequenceMatcher(None, qn, norm(m["text"])).ratio()
        if r >= _FUZZY_MIN:
            scored.append((r, dict(m, fuzzy=round(r, 2))))
    return [m for _, m in sorted(scored, key=lambda t: -t[0])]


_EDGE_BAND = 0.18  # top/bottom strips where taskbars/menubars live


def _ocr_passes(prepped: str) -> list[tuple[str, int]]:
    """OCR pass images beyond the plain full frame: inverted full (light-on-dark themes)
    and top/bottom edge bands re-run standalone. Tesseract's segmentation drops small
    text regions on a sparse desktop frame (a taskbar strip OCRs perfectly on its own
    while the full frame yields nothing — proven on real noVNC frames); dedicated passes
    make those regions first-class. Returns (path, y_offset_in_prepped_px)."""
    passes: list[tuple[str, int]] = []
    try:
        from PIL import Image, ImageOps
        with Image.open(prepped) as im:
            base = os.path.splitext(prepped)[0]
            inv = base + "_inv.png"
            ImageOps.invert(im.convert("L")).save(inv)
            passes.append((inv, 0))
            band = int(im.height * _EDGE_BAND)
            for tag, box, dy in (("top", (0, 0, im.width, band), 0),
                                 ("bot", (0, im.height - band, im.width, im.height), im.height - band)):
                p = f"{base}_{tag}.png"
                im.crop(box).save(p)
                passes.append((p, dy))
    except Exception:  # noqa: BLE001 - no Pillow / decode issue: single-pass OCR still works
        pass
    return passes


def _merge_matches(base: list[dict], extra: list[dict], dy: int) -> list[dict]:
    """Merge a pass's matches into the running list: offset band coords back to frame
    space, drop duplicates (same text within 12px)."""
    for m in extra:
        m["box"] = [m["box"][0], m["box"][1] + dy, m["box"][2], m["box"][3]]
        m["center"] = [m["center"][0], m["center"][1] + dy]
        dup = any(d["text"].lower() == m["text"].lower()
                  and abs(d["center"][0] - m["center"][0]) <= 12
                  and abs(d["center"][1] - m["center"][1]) <= 12 for d in base)
        if not dup:
            base.append(m)
    return base


@backend("locate", "tesseract", priority=65, needs_bin=("tesseract",))
def _locate_tesseract(image: str = "", query: str = "", text: str = "", role: str = "",
                      name: str = "", min_conf: float = 40, **_: Any) -> dict:
    """OCR-locate on-screen text. Unlike a saliency detector this GENUINELY matches the
    query against recognised text, so it is preferred (priority 65 > imgl 60) for text
    targets. Returns the unified ``found``/``bbox``/``center`` schema AND the full
    ``matches`` list; ``found: false`` (honestly) when the text is not on screen — never
    a bogus hit. Captures its own screenshot when no ``image`` is supplied. ``name`` (the
    accessibility name an LLM planner emits for textboxes) is accepted as a query alias."""
    q = (query or text or name or "").strip()
    full = None
    if not image or not os.path.exists(image):
        cap = _capture_tmp()           # find/click handlers call us without an image
        image, full = cap["path"], cap.get("fullSize")
    prepped, scale = _ocr_prep(image)
    ql = q.lower()

    def _pass_matches(path: str) -> list[dict]:
        tsv = _run(["tesseract", path, "stdout", "--psm", "11", "tsv"], timeout=60).stdout
        if not ql:
            return sorted(_tsv_lines(tsv, float(min_conf)), key=lambda m: -m["conf"])
        exact = _tesseract_query_matches(tsv, ql, float(min_conf))
        # OCR noise splits/mangles UI labels ('Reconfigure' -> 'Reconfig re', 'Workspaces'
        # -> 'Norkspaces'); when the exact matcher comes up empty, fall back to fuzzy
        # line-level matching so one flipped glyph doesn't sink the whole locate.
        return exact or _fuzzy_line_matches(_tsv_lines(tsv, float(min_conf)), ql)

    matches = _pass_matches(prepped)
    for path, dy in _ocr_passes(prepped):
        matches = _merge_matches(matches, _pass_matches(path), dy)
    matches = _rescale_matches(matches, scale)
    out = {"via": "tesseract", "source": "tesseract", "coord_space": "image-px",
           "query": q, "count": len(matches), "matches": matches, "fullSize": full}
    if matches and ql:
        best = matches[0]
        out.update({"found": True, "bbox": best["box"], "center": best["center"],
                    "text": best["text"], "candidates": len(matches), "actionable": False})
    else:
        # Empty query is NOT a located hit: returning the highest-confidence arbitrary
        # glyph as found:true makes wait/fill/click act on garbage (e.g. "©"). Keep
        # `matches` for the ui_locate text-dump, but report found:false so callers that
        # need a target fail honestly instead of clicking nowhere.
        out["found"] = False           # honest miss — do NOT fall through to a guesser
        out["candidates"] = len(matches)
    return out


_EASYOCR_READER = None


def _easyocr_reader() -> Any:
    """Cache the EasyOCR reader (model load is ~5-10s the first time). Languages from
    URIRUN_KVM_OCR_LANGS (default 'en'); CPU mode for portability."""
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        import easyocr
        langs = [x.strip() for x in os.environ.get("URIRUN_KVM_OCR_LANGS", "en").split(",") if x.strip()]
        _EASYOCR_READER = easyocr.Reader(langs or ["en"], gpu=False)
    return _EASYOCR_READER


@backend("locate", "easyocr", priority=70, needs_mod=("easyocr",))
def _locate_easyocr(image: str = "", query: str = "", text: str = "", role: str = "",
                    min_conf: float = 40, **_: Any) -> dict:
    """OCR-locate via EasyOCR (CRAFT detector + CRNN) — stronger than tesseract on UI
    fonts, low contrast, and non-Latin scripts, with no a11y permissions. Returns the
    same unified ``found``/``bbox``/``center`` + ``matches`` schema; genuine text match,
    honest ``found: false``. NOTE: finds TEXT, not icons — icon targets need the
    computer-use/vision path. Heavy import (torch); the reader is process-cached, but
    isolated route calls reload it per call — for hot loops run locate non-isolated."""
    q = (query or text or "").strip()
    full = None
    if not image or not os.path.exists(image):
        cap = _capture_tmp()
        image, full = cap["path"], cap.get("fullSize")
    reader = _easyocr_reader()
    thr = float(min_conf) / 100.0      # our min_conf is 0-100; EasyOCR confidence is 0-1
    matches = []
    for quad, t, conf in reader.readtext(image):
        if conf < thr or not str(t).strip():
            continue
        xs = [int(p[0]) for p in quad]
        ys = [int(p[1]) for p in quad]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        matches.append({"text": str(t), "conf": round(float(conf) * 100, 1),
                        "box": [x0, y0, x1 - x0, y1 - y0], "center": [(x0 + x1) // 2, (y0 + y1) // 2]})
    ql = q.lower()
    if ql:
        matches = [m for m in matches if ql in m["text"].lower()]
    matches.sort(key=lambda m: -m["conf"])
    out = {"via": "easyocr", "source": "easyocr", "coord_space": "image-px",
           "query": q, "count": len(matches), "matches": matches, "fullSize": full}
    if matches:
        best = matches[0]
        out.update({"found": True, "bbox": best["box"], "center": best["center"],
                    "text": best["text"], "candidates": len(matches), "actionable": False})
    else:
        out["found"] = False
    return out


# --------------------------------------------------------------------------- #
# LOCATE — find a target element and return a normalized hit:
#   {found, bbox:[x,y,w,h], source, coord_space, role?, name?, actionable}
# Backends (decorator fallback): AT-SPI (screen-space bbox, can act directly) →
# imgl vision (OCR/layout) → vql. The `ui/*` routes build perceive→act→verify on
# top of this. `coord_space` is "screen" (compositor coords, click 1:1) for AT-SPI
# and "image-px" (screenshot pixels; HiDPI may need scaling) for vision backends.
# --------------------------------------------------------------------------- #
def bbox_center(bbox: list | tuple) -> tuple:
    x, y, w, h = bbox
    return int(x + w / 2), int(y + h / 2)


@backend("locate", "atspi", priority=90, platforms=("linux-wayland", "linux-x11"))
def _locate_atspi(text: str = "", role: str = "", app: str = "", nth: int = 0, **_: Any) -> dict:
    res = _a11y_atspi(app=app, role=role, name=text, op="find", nth=int(nth))
    if not res.get("found") or not res.get("bbox"):
        raise BackendError(f"atspi: no element role~{role!r} name~{text!r}")
    x, y, w, h = res["bbox"]   # reject off-screen / collapsed elements (a11y tree noise)
    if x < 0 or y < 0 or w < 2 or h < 2 or x > _MAX_SCREEN_COORD or y > _MAX_SCREEN_COORD:  # noqa: PLR2004
        raise BackendError(f"atspi: element name~{text!r} has no on-screen bbox {res['bbox']}")
    return {"found": True, "bbox": res["bbox"], "source": "atspi", "coord_space": "screen",
            "role": res.get("role"), "name": res.get("name"), "actionable": True,
            "candidates": res.get("candidates")}


def _capture_tmp() -> dict:
    import tempfile
    shot = os.path.join(tempfile.gettempdir(), "urirun-kvm-locate.png")
    res = dispatch("capture", output=shot, monitor=0)
    res["path"] = shot
    return res


def _imgl_matching_hits(image: str, q: str, role: str) -> list[dict]:
    """Run ``imgl.cli find`` and keep only TRUSTWORTHY hits: hits whose OWN text doesn't
    contain the query are dropped (belt-and-braces on top of imgl>=0.7.16, where --list
    finally honours --text; older imgl returned every action — measured 20% hit-rate,
    290px median error), or vnc/ui verify-loops get poisoned by a plausible-looking
    wrong element."""
    import json as _json
    args = [sys.executable, "-m", "imgl.cli", "find", image, "--list"]
    if q:
        args += ["--text", q]
    if role:
        args += ["--type", role]
    hits = _json.loads(_run(args, timeout=40).stdout or "[]")
    if q:
        hits = [h for h in hits if q.lower() in str(h.get("text") or "").lower()]
    return hits


@backend("locate", "imgl", priority=60, needs_mod=("imgl",))
def _locate_imgl(image: str = "", query: str = "", text: str = "", role: str = "",
                 name: str = "", **_: Any) -> dict:
    """Vision locate: screenshot → imgl find by text → bbox+center (image-px). Accepts a
    pre-captured ``image`` (a noVNC/RFB frame, a golden fixture) like the OCR backends do;
    captures the local screen only when none is given. Honest miss when the query is empty
    or unmatched — never 'first arbitrary element' (that clicked garbage before)."""
    import json as _json
    q = (query or text or name or "").strip()
    full = None
    if not image or not os.path.exists(image):
        cap = _capture_tmp()
        image, full = cap["path"], cap.get("fullSize")
    if not q and not role:
        raise BackendError("imgl: empty query — refusing to return an arbitrary element")
    hits = _imgl_matching_hits(image, q, role)
    if not hits:
        raise BackendError(f"imgl: no element matching text~{q!r} role~{role!r}")
    h = hits[0]
    bb = h.get("bbox") or {}
    box = [int(bb.get("x", h.get("x", 0))), int(bb.get("y", h.get("y", 0))),
           int(bb.get("w", h.get("w", 0))), int(bb.get("h", h.get("h", 0)))]
    return {"found": True, "bbox": box,
            "center": [box[0] + box[2] // 2, box[1] + box[3] // 2],
            "source": "imgl", "coord_space": "image-px", "text": h.get("text"), "query": q,
            "fullSize": full, "actionable": False, "candidates": len(hits)}


@backend("locate", "vql", priority=50, needs_mod=("vql",))
def _locate_vql(image: str = "", query: str = "", text: str = "", role: str = "", **_: Any) -> dict:
    import json as _json
    full = None
    if not image or not os.path.exists(image):
        cap = _capture_tmp()
        image, full = cap["path"], cap.get("fullSize")
    p = _run([sys.executable, "-m", "imgl.cli", "vql", image], timeout=40)
    doc = _json.loads(p.stdout or "{}")
    needle = (query or text or role).lower()
    hit = _vql_first_match(doc, needle)
    if hit is None:
        raise BackendError(f"vql: no object matching {needle!r}")
    box, label = hit
    return {"found": True, "bbox": box,
            "center": [box[0] + box[2] // 2, box[1] + box[3] // 2],
            "source": "vql", "coord_space": "image-px", "text": label,
            "fullSize": full, "actionable": False}


def _vql_first_match(doc: dict, needle: str) -> "tuple[list[int], Any] | None":
    for layer in (doc.get("scene", {}).get("layers") or []):
        for obj in layer.get("objects", []):
            label = " ".join(str(v) for v in (obj.get("text"), obj.get("label")) if v).lower()
            if needle and needle in label and obj.get("bbox"):
                b = obj["bbox"]
                return ([int(b.get("x") or 0), int(b.get("y") or 0),
                         int(b.get("w") or 0), int(b.get("h") or 0)], obj.get("text"))
    return None



# Pixel-accurate uinput pointer helpers extracted to _backends_uinput.
# NOTE: flat fallback is MANDATORY — a bare relative import here made every flat
# `--code backends.py` deploy fail on the node ("attempted relative import with no
# known parent package"), silently pinning nodes to a stale bundled backends.
try:  # normal package import
    from ._backends_uinput import (  # noqa: E402
        _UI, _UI_DEV_CREATE, _UI_DEV_DESTROY, _UI_SET_EVBIT, _UI_SET_KEYBIT, _UI_SET_ABSBIT,
        _EV_SYN, _EV_KEY, _EV_ABS, _ABS_X, _ABS_Y, _BTN_CODE, _BTN_TOUCH, _ABS_RANGE,
        _SCREEN_WH_CACHE, _ui_io, _ui_iow, uinput_available, _uinput_create_abs,
        _read_text, _png_wh, _screen_wh, _calib, _compute_abs_coords, _uinput_emit_clicks, uinput_abs_click,
        uinput_type_text, uinput_key_combo,
    )
except ImportError:  # flat-module deploy
    from _backends_uinput import (  # type: ignore  # noqa: E402
        _UI, _UI_DEV_CREATE, _UI_DEV_DESTROY, _UI_SET_EVBIT, _UI_SET_KEYBIT, _UI_SET_ABSBIT,
        _EV_SYN, _EV_KEY, _EV_ABS, _ABS_X, _ABS_Y, _BTN_CODE, _BTN_TOUCH, _ABS_RANGE,
        _SCREEN_WH_CACHE, _ui_io, _ui_iow, uinput_available, _uinput_create_abs,
        _read_text, _png_wh, _screen_wh, _calib, _compute_abs_coords, _uinput_emit_clicks, uinput_abs_click,
        uinput_type_text, uinput_key_combo,
    )

# Surface awareness helpers extracted to _backends_surface.
try:  # normal package import
    from ._backends_surface import (  # noqa: E402
        _gnome_monitors, _wayland_present, _surface_warnings,
        _os_level_reliable, _surface_flags, surface_report,
    )
except ImportError:  # flat-module deploy
    from _backends_surface import (  # type: ignore  # noqa: E402
        _gnome_monitors, _wayland_present, _surface_warnings,
        _os_level_reliable, _surface_flags, surface_report,
    )

# Register the launch/launch_list backends (their @backend decorators run on import).
try:  # normal package import
    from urirun_connector_kvm import launch_backends  # noqa: E402,F401
except ImportError:  # flat-module deploy (node pushes backends.py + launch_backends.py as flat modules)
    import launch_backends  # type: ignore  # noqa: F401
