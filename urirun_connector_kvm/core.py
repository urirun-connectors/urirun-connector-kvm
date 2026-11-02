# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

"""KVM (keyboard / video / mouse) routes for urirun — cross-platform.

One ``kvm://`` surface for capturing the screen and driving keyboard/mouse on
Linux (Wayland *and* X11), Windows and macOS. Every capability is served by a
**decorator-registered backend** (see ``backends.py``): the connector auto-selects
the best available tool/library for the live session and falls through on failure,
so the same routes work everywhere a suitable helper is installed.

Routes (each a typed ``@conn.handler``, ``isolated=True`` so it stays
registry-portable through ``python -m urirun.exec``):

* ``kvm://{host}/screen/query/capture``     — screenshot (portal/grim/mss/scrot/…)
* ``kvm://{host}/input/command/type``        — type a whole string
* ``kvm://{host}/input/command/key``         — a key / hotkey combo
* ``kvm://{host}/input/command/click``       — mouse click (optionally at x,y)
* ``kvm://{host}/input/command/move``        — move the pointer (absolute)
* ``kvm://{host}/input/command/scroll``      — scroll wheel
* ``kvm://{host}/task/command/run``          — a bounded sequence of the above
* ``kvm://{host}/window/command/focus``      — activate a window by title
* ``kvm://{host}/window/command/maximize``   — maximize a window so it fills the screen
* ``kvm://{host}/window/query/list``         — list windows
* ``kvm://{host}/doctor/query/report``       — which backend serves each action

Backend chains (auto, highest-priority available wins):
  capture: portal(Wayland) → grim → mss → pillow → scrot → imagemagick → gnome-screenshot → screencapture(macOS)
           → CDP page (browser-page fallback when the OS portal is blocked but a debug Chrome is reachable)
  input:   ydotool(Wayland) → wtype/xdotool(X11) → pynput(any)
Helpful optional libraries (install for more platforms): ``mss``, ``pynput``,
``Pillow``, ``pytesseract`` + system tools ``ydotool``/``ydotoold``, ``wmctrl``,
``grim``/``scrot``, ``python3-gobject``+``python3-dbus`` (Wayland portal capture).
"""

import base64 as _b64
import json as _json
import os
import shutil
import tempfile
import time
from typing import Any

import urirun


def _adopt_flat_siblings() -> None:
    """Flat-module deploy ONLY (``__package__`` empty): make the flat sibling files
    authoritative for ``urirun_connector_kvm.*`` imports. Without this, a node whose
    urirun BUNDLE ships an (older) installed ``urirun_connector_kvm`` silently wins
    every ``from urirun_connector_kvm import X`` inside the deployed modules — the
    node then runs new core.py with OLD backends.py (observed on lenovo: crop worked,
    new capture backends never appeared)."""
    import importlib
    import sys as _sys
    import types as _types
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    pkg = _types.ModuleType("urirun_connector_kvm")
    pkg.__path__ = [here]  # type: ignore[attr-defined]
    _sys.modules["urirun_connector_kvm"] = pkg
    for name in ("_urirun_compat", "backends", "_backends_uinput", "_backends_surface", "launch_backends",
                 "cdp", "_cdp_impl", "control", "environment", "strategies", "surface",
                 "vnc", "contracts", "capture_worker", "readiness"):
        if not os.path.exists(os.path.join(here, name + ".py")):
            continue
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - optional sibling (capture_worker needs dbus,
            continue      # contracts may need a newer toolkit) — core must still load
        _sys.modules["urirun_connector_kvm." + name] = mod
        setattr(pkg, name, mod)


if not __package__:  # running as a flat module pushed by `host deploy --code`
    _adopt_flat_siblings()

try:  # normal package import
    from urirun_connector_kvm import _urirun_compat
except ImportError as _e:  # flat-module deploy
    _missing = getattr(_e, "name", None) or ""
    if _missing and not _missing.startswith("urirun_connector_kvm"):
        raise
    import _urirun_compat  # type: ignore

try:  # normal package import
    from urirun_connector_kvm import backends as B
except ImportError as _e:  # flat-module deploy (host `deploy --code core.py backends.py`)
    # Only use the flat-file fallback when the kvm PACKAGE itself is absent.
    # If backends.py was found but has a missing dependency inside it, re-raise so
    # the real error ("No module named X") is visible instead of "No module named backends".
    _missing = getattr(_e, "name", None) or ""
    if _missing and not _missing.startswith("urirun_connector_kvm"):
        raise
    import sys as _sys, os as _os
    _kvm_dir = _os.path.dirname(_os.path.abspath(__file__))
    if _kvm_dir not in _sys.path:
        _sys.path.insert(0, _kvm_dir)
    import backends as B  # type: ignore

try:  # universal control-tool router (cdp -> atspi -> vision)
    from urirun_connector_kvm import control as C
except ImportError as _e:  # flat-module deploy (push control.py + cdp.py too)
    _missing = getattr(_e, "name", None) or ""
    if _missing and not _missing.startswith("urirun_connector_kvm"):
        raise
    import control as C  # type: ignore

try:  # CDP surface — used as a capture fallback when OS-level capture is blocked
    from urirun_connector_kvm import cdp as _cdp
except ImportError as _e:  # flat-module deploy
    _missing = getattr(_e, "name", None) or ""
    if _missing and not _missing.startswith("urirun_connector_kvm"):
        raise
    try:
        import cdp as _cdp  # type: ignore
    except ImportError:
        _cdp = None  # type: ignore

CONNECTOR_ID = "kvm"
conn = _urirun_compat.connector(CONNECTOR_ID, scheme="kvm")

try:  # wrap conn.handler BEFORE the decorators below so every contracted route is guarded
    from urirun_connectors_toolkit.contract_gate import enforce as _enforce
    from urirun_connector_kvm.contracts import CONTRACTS as _CONTRACTS_EARLY
    _enforce(conn, _CONTRACTS_EARLY,
             validate=os.environ.get("URIRUN_CONTRACT_CHECK") == "1")
    del _CONTRACTS_EARLY
except Exception:  # noqa: BLE001 - contracts are enrichment; absent toolkit must not break imports
    pass

# A real screenshot is hundreds of KB; the GNOME-Wayland xdg-portal empty/blocked placeholder is ~3.8 KB.
# Below this, a portal capture is treated as a degraded non-capture, not a false success.
_MIN_REAL_SHOT_BYTES = 20_000

_CDP_SESSION_TIMEOUT = 12.0

try:
    from urirun.node.preconditions import need_from_backend_error as _backend_need
except ImportError:
    def _backend_need(msg: str) -> dict | None:  # type: ignore[misc]
        return None
_ACT_BUDGET_SECS = 25.0
_LOCATE_MIN_CONF = 40


def _ok(**kw: Any) -> dict[str, Any]:
    return urirun.ok(connector=CONNECTOR_ID, **kw)


def _fail_from(action: str, exc: Exception) -> dict[str, Any]:
    return urirun.fail(str(exc), connector=CONNECTOR_ID, action=action, platform=B.platform_tag())


def _spread(d: dict | None, *also_exclude: str) -> dict[str, Any]:
    """Strip the envelope-reserved keys from an inner result dict BEFORE spreading it into
    ``urirun.ok``/``urirun.fail`` — passing the inner ``error``/``ok``/``connector``/``action``
    (or a key the caller sets explicitly) collides with the envelope's own and raises
    TypeError, which used to mask the real result behind a generic handler crash."""
    reserved = {"ok", "error", "connector", "action", *also_exclude}
    return {k: v for k, v in (d or {}).items() if k not in reserved}


def _positioned_click(button: str, x: int | None, y: int | None, clicks: int = 1) -> dict[str, Any]:
    """Position+click as ONE uinput absolute device when /dev/uinput is writable.
    On a GNOME/Wayland node that lacks WAYLAND_DISPLAY, ydotool's ``mousemove -a``
    mismaps and dumps the cursor at the top-left hot-corner (opens Activities); and a
    split move(uinput-abs)+click(ydotool) clicks at the wrong place because the abs
    device is destroyed before the click. ``uinput_abs_click`` does both atomically on
    one device (auto-detecting screen size), so the click lands where we positioned.
    Falls back to the move+click chain only when /dev/uinput is not writable."""
    if x is not None and y is not None and B.uinput_available():
        return B.uinput_abs_click(int(x), int(y), 0, 0, button=button, do_click=True, clicks=clicks)
    moved = None
    if x is not None and y is not None:
        moved = B.dispatch("move", x=int(x), y=int(y))
        time.sleep(0.15)
    last: dict[str, Any] = {}
    for _ in range(max(1, clicks)):
        last = B.dispatch("click", button=button)
        time.sleep(0.05)
    return {**last, "moved": moved} if moved else last


def _apply_capture_postprocessing(out: str, cx: int, cy: int, zoom: int,
                                   crop_w: int, crop_h: int, max_width: int) -> tuple:
    """Apply PIL post-processing to a captured PNG.
    Returns (full_size, crop_info) — both may be None when PIL is absent."""
    from PIL import Image
    with Image.open(out) as im:
        full = list(im.size)
        wpx, hpx = im.size
        want_crop = int(cx) >= 0 and int(cy) >= 0 and (int(zoom) > 1 or (crop_w and crop_h))
        if want_crop:
            cw = int(crop_w) if crop_w else max(64, wpx // int(zoom))
            ch = int(crop_h) if crop_h else max(64, hpx // int(zoom))
            x0 = max(0, min(int(cx) - cw // 2, wpx - cw))
            y0 = max(0, min(int(cy) - ch // 2, hpx - ch))
            im = im.crop((x0, y0, x0 + cw, y0 + ch))
            crop = {"x": x0, "y": y0, "w": cw, "h": ch, "cx": int(cx), "cy": int(cy)}
        else:
            crop = None
            if max_width and im.width > int(max_width):
                ratio = int(max_width) / im.width
                im = im.resize((int(max_width), int(im.height * ratio)))
            else:
                # Nothing changed — do NOT re-encode. The unconditional save used to
                # PNG-re-encode (optimize=True, slow) EVERY frame, silently converting
                # the warm worker's jpeg back to a bigger PNG and taxing every capture.
                return full, None
        im.convert("RGB").save(out, format="PNG", optimize=True)
    return full, crop


# --------------------------------------------------------------------------- #
# screen capture
# --------------------------------------------------------------------------- #
def _cdp_capture(out: str) -> "dict[str, Any] | None":
    """Fallback capture via the active CDP page (``Page.captureScreenshot``) when OS-level
    capture is blocked — e.g. a GNOME-Wayland portal that returns an empty placeholder while
    a real Chrome is reachable on the debug port. Captures the browser viewport (the meaningful
    content for web automation), NOT the whole desktop. Returns a payload dict with ``via='cdp'``
    on success, or ``None`` when CDP is unreachable / fails so the caller keeps its degraded path."""
    if _cdp is None:
        return None
    try:
        if not _cdp.reachable():
            return None
        res = _cdp.command("Page.captureScreenshot", {"format": "png"})
        data = _b64.b64decode(res.get("data") or "")
    except Exception:  # noqa: BLE001 — any CDP failure just means no fallback is available
        return None
    if len(data) < _MIN_REAL_SHOT_BYTES:  # a real page frame is hundreds of KB; tiny = not useful
        return None
    try:
        with open(out, "wb") as fh:
            fh.write(data)
    except OSError:
        return None
    return {"kind": "screenshot", "path": out, "via": "cdp", "backend": "cdp-page",
            "bytes": len(data), "scope": "browser-page"}


def _cdp_fallback_or(out: str, base64: bool, degraded: dict[str, Any]) -> dict[str, Any]:
    """Return a real CDP page capture if one is available, else the degraded envelope."""
    shot = _cdp_capture(out)
    if shot is None:
        return degraded
    if base64:
        with open(out, "rb") as fh:
            shot["pngBase64"] = _b64.b64encode(fh.read()).decode()
    return urirun.tag(_ok(**shot), "screenshot")


def _browser_capture_requested(scope: str = "") -> bool:
    return str(scope or "").strip().lower() in {"browser", "browser-page", "page", "tab", "viewport", "cdp"}


def _single_monitor_bbox(payload: dict[str, Any]) -> list | None:
    """The captured monitor's ``[x, y, w, h]`` logical rect when a single monitor was captured.

    Backends emit the full virtual-desktop union ``bbox`` even for a single-monitor capture, so a
    ``scope: monitor`` result claimed the whole desktop while the image was just one monitor.
    Narrow it to the selected monitor (matched by index or output connector) so ``bbox`` matches
    the frame actually produced. Logical geometry, consistent with the desktop bbox union and
    ``_monitor_for_bbox``. Returns None (leave bbox untouched) when geometry is unavailable."""
    if str(payload.get("scope") or "").strip().lower() != "monitor":
        return None
    monitors = payload.get("monitors")
    if not isinstance(monitors, list):
        return None
    idx, conn = payload.get("monitor"), payload.get("outputConnector")
    for m in monitors:
        if not isinstance(m, dict):
            continue
        if (idx is not None and m.get("index") == idx) or (conn and m.get("connector") == conn):
            w, h = m.get("logicalWidth"), m.get("logicalHeight")
            if w and h:
                return [int(m.get("x") or 0), int(m.get("y") or 0), int(w), int(h)]
    return None


def _missing_requested_monitor(monitor: int, backend_result: dict[str, Any]) -> str | None:
    if int(monitor or 0) <= 0:
        return None
    if str(backend_result.get("scope") or "").strip().lower() not in {"", "monitor"}:
        return None
    monitors = backend_result.get("monitors")
    if not isinstance(monitors, list) or not monitors:
        return None
    ids = sorted(
        int(m.get("index"))
        for m in monitors
        if isinstance(m, dict) and isinstance(m.get("index"), int)
    )
    if int(monitor) in ids:
        return None
    available = ", ".join(str(i) for i in ids) or "none"
    return f"monitor {int(monitor)} not available; active monitors: {available}"


def _missing_requested_monitor_from_inventory(monitor: int, scope: str = "") -> str | None:
    if int(monitor or 0) <= 0:
        return None
    if str(scope or "").strip().lower() in {"all", "all-monitors", "desktop"}:
        return None
    try:
        monitors = B._gnome_monitors()
    except Exception:  # noqa: BLE001 - inventory is best-effort; backend guard still applies
        monitors = []
    if not monitors:
        return None
    ids = sorted(
        int(m.get("index"))
        for m in monitors
        if isinstance(m, dict) and isinstance(m.get("index"), int)
    )
    if int(monitor) in ids:
        return None
    available = ", ".join(str(i) for i in ids) or "none"
    return f"monitor {int(monitor)} not available; active monitors: {available}"


def _resolve_output_path(output: str) -> str:
    """Return the absolute path where the screenshot will be written."""
    _art_root = os.path.expanduser(os.environ.get("URIRUN_ARTIFACT_DIR", "~/.urirun/artifacts"))
    _shot_dir = os.path.join(_art_root, "screenshots")
    if output and os.path.isabs(output):
        return output
    os.makedirs(_shot_dir, exist_ok=True)
    name = os.path.basename(output) if output else f"urirun-kvm-shot-{os.getpid()}.png"
    return os.path.join(_shot_dir, name)


def _build_capture_payload(
    out: str, res: dict, cx: int, cy: int,
    zoom: int, crop_w: int, crop_h: int, max_width: int,
) -> dict[str, Any]:
    """Assemble and post-process the capture result payload."""
    full = crop = None
    try:
        full, crop = _apply_capture_postprocessing(out, cx, cy, zoom, crop_w, crop_h, max_width)
    except Exception:  # noqa: BLE001 - PIL optional; keep raw capture
        pass
    payload: dict[str, Any] = {
        "kind": "screenshot", "path": out, "monitor": res.get("monitor"),
        "via": res.get("via"), "backend": res.get("backend"),
        "fullSize": full, "crop": crop,
        "bytes": os.path.getsize(out) if os.path.exists(out) else 0,
    }
    for key in ("scope", "monitors", "bbox", "width", "height", "grabMs", "backendErrors", "format"):
        if res.get(key) not in (None, "", []):
            payload[key] = res.get(key)
    if res.get("connector"):
        payload["outputConnector"] = res["connector"]
    mon_bbox = _single_monitor_bbox(payload)
    if mon_bbox is not None:
        payload["bbox"] = mon_bbox
    return payload


def _placeholder_guard(
    payload: dict[str, Any], res: dict, out: str, base64: bool
) -> dict[str, Any] | None:
    """Return a degraded/CDP result if the file looks like a blocked-session placeholder, else None."""
    if payload["bytes"] == 0 or (
        payload["bytes"] < _MIN_REAL_SHOT_BYTES
        and res.get("via") in {"xdg-portal", "mutter-screencast"}
    ):
        _via = res.get("via") or "unknown"
        return _cdp_fallback_or(out, base64, urirun.ok(
            connector=CONNECTOR_ID, action="capture", degraded=True, kind="screenshot",
            degradedReason=(
                f"{_via} returned a {payload['bytes']}-byte placeholder (empty/blocked) — not "
                "a real screenshot; needs a GUI session or the grim/mutter/CDP backend"
            ),
            via=_via, backend=res.get("backend"), bytes=payload["bytes"], path=out,
            platform=B.platform_tag(),
        ))
    return None


@conn.handler("screen/query/capture", isolated=False,  # in-process: read-only, and the
              # isolated spawn was ~600 ms of the ~730 ms hot perception path (Tier 3);
              # heavy lifting is in the warm capture worker subprocess anyway
              meta={"label": "Capture the screen (auto backend)"})
def capture(output: str = "", monitor: int = 0, max_width: int = 0, base64: bool = False,
            cx: int = -1, cy: int = -1, zoom: int = 0, crop_w: int = 0, crop_h: int = 0,
            scope: str = "", fmt: str = "") -> dict[str, Any]:
    """Capture the live screen via the best available backend. ``max_width`` downscales
    (so coords map 1:1 to a logical screen on HiDPI); ``base64`` returns the PNG inline.
    Focus crop: pass ``cx``/``cy`` (+ ``zoom`` N -> a full/N window, or explicit
    ``crop_w``/``crop_h``) to return ONLY a zoomed tile around that point — so a
    remote caller transfers a small region where the action is, not the whole screen.
    ``crop`` in the result gives the tile's origin/size for mapping coords back.
    ``scope='browser'`` means a prior CDP browser step owns the visual surface, so prefer
    a viewport screenshot over an arbitrary OS monitor on multi-monitor sessions."""
    out = _resolve_output_path(output)
    if _browser_capture_requested(scope):
        shot = _cdp_capture(out)
        if shot is not None:
            if base64:
                with open(out, "rb") as fh:
                    shot["pngBase64"] = _b64.b64encode(fh.read()).decode()
            return urirun.tag(_ok(**shot), "screenshot")
    if missing := _missing_requested_monitor_from_inventory(monitor, scope):
        return _fail_from("capture", B.BackendError(missing))
    try:
        # max_width reaches the warm worker (gst videoscale: 4x fewer pixels to
        # png-encode + no PIL resize here); other backends ignore it via **_.
        # fmt='jpeg' (warm worker only): ~4-6x smaller frame for perception loops;
        # cold backends ignore it and still produce PNG — check `format` in the result.
        res = B.dispatch("capture", output=out, monitor=monitor, scope=scope,
                         max_width=max_width if int(cx) < 0 else 0, fmt=fmt)
    except B.BackendError as exc:
        msg = str(exc)
        _need = _backend_need(msg)
        _portal_blocked = any(k in msg for k in (
            "portal denied", "portal cancelled", "portal blocked", "placeholder",
        ))
        if _portal_blocked or _need:
            return _cdp_fallback_or(out, base64, urirun.ok(
                connector=CONNECTOR_ID, action="capture",
                degraded=True, degradedReason=msg, platform=B.platform_tag(),
                **({"need": _need} if _need else {})))
        return _fail_from("capture", exc)
    if missing := _missing_requested_monitor(monitor, res):
        return _fail_from("capture", B.BackendError(missing))
    payload = _build_capture_payload(out, res, cx, cy, zoom, crop_w, crop_h, max_width)
    if guarded := _placeholder_guard(payload, res, out, base64):
        return guarded
    if base64:
        with open(out, "rb") as fh:
            payload["pngBase64"] = _b64.b64encode(fh.read()).decode()
    return urirun.tag(_ok(**payload), "screenshot")


# --------------------------------------------------------------------------- #
# display geometry — a first-class query (callers hit a missing display/query/info
# 374x to get screen size; capture only returned it as a side effect)
# --------------------------------------------------------------------------- #
@conn.handler("display/query/info", isolated=False,  # read-only, in-process (Tier 3)
              meta={"label": "Screen size, monitors, scale (the capture/action coordinate space)"})
def display_info() -> dict[str, Any]:
    """The display geometry callers need without taking a screenshot: full pixel size (the
    space capture and click coordinates live in), per-monitor geometry+scale, and whether
    OS-level pixel input is trustworthy here. Cheap — no capture unless size is uncached."""
    try:
        w, h = B._screen_wh()
        surf = B.surface_report()
        mons = B._gnome_monitors() or surf.get("monitors") or []
        return _ok(action="display-info", fullSize=[w, h], width=w, height=h,
                   monitors=mons, monitorCount=len(mons), platform=B.platform_tag(),
                   wayland=surf.get("wayland"), multiMonitor=surf.get("multiMonitor"),
                   fractionalHiDPI=surf.get("fractionalHiDPI"),
                   osLevelReliable=surf.get("osLevelReliable"),
                   recommendedSurfaces=surf.get("recommendedSurfaces"))
    except Exception as exc:  # noqa: BLE001
        return _fail_from("display-info", exc)


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
def _verify_focus(expect: str) -> dict[str, Any]:
    """Foreground-window title probe compared against ``expect`` (case-insensitive substring),
    so a caller can refuse to type into the wrong window BEFORE the keys go out instead of
    only noticing afterward via a screenshot OCR check. ``trusted:false`` means the OS/session
    gave no answer at all (pure Wayland has no public active-window query, see
    ``surface._active_window``) — that is neither a match nor a mismatch, so callers must treat
    it as unverifiable rather than as a pass."""
    win = _surface_mod()._active_window()
    title = win.get("title", "")
    if not title:
        return {"trusted": False, "verified": None, "title": "", "expect": expect}
    return {"trusted": True, "verified": expect.lower() in title.lower(),
            "title": title, "expect": expect, "via": win.get("via")}


def _window_geometry(title: str) -> "dict[str, Any] | None":
    try:
        return B.dispatch("window_geometry", title=title)
    except B.BackendError:
        return None


def _verify_fullscreen(title: str) -> dict[str, Any]:
    """Does the window matching ``title`` fill the screen? Compares its ``wmctrl`` geometry
    against the display size (X11/XWayland only — same trust boundary as ``_verify_focus``:
    wmctrl can't see Wayland-native windows). A tolerance accounts for panels/title bars, so a
    plain maximize (not literal edge-to-edge fullscreen) counts. This matters because absolute
    click/type coordinates are computed against a full-screen frame — a partially-visible or
    tiled window silently breaks that mapping without ever raising an error."""
    geo = _window_geometry(title)
    if not geo:
        return {"trusted": False, "fullscreen": None, "title": title}
    try:
        sw, sh = B._screen_wh()
    except Exception:  # noqa: BLE001
        return {"trusted": False, "fullscreen": None, "title": title}
    covers = geo["w"] >= sw * 0.92 and geo["h"] >= sh * 0.85
    return {"trusted": True, "fullscreen": covers, "title": geo.get("title", title),
            "geometry": {"x": geo["x"], "y": geo["y"], "w": geo["w"], "h": geo["h"]},
            "screen": [sw, sh]}


def _ensure_fullscreen(title: str) -> dict[str, Any]:
    """check→satisfy→retry: verify the window fills the screen; if a TRUSTED probe says it
    doesn't, maximize it once and verify again. Never raises — degrades honestly (unchanged
    ``trusted:false``) when the geometry probe can't answer at all."""
    check = _verify_fullscreen(title)
    if check["trusted"] is False or check["fullscreen"] is True:
        return check
    try:
        B.dispatch("maximize", title=title)
        time.sleep(0.3)
    except B.BackendError:
        pass
    return _verify_fullscreen(title)


def _grounding_checks(action: str, expect_window: str, require_fullscreen: bool
                       ) -> "tuple[dict | None, dict | None, dict | None]":
    """Shared pre-flight for type_text/key: ``(focus_check, fullscreen_check, fail_envelope)``.
    A non-``None`` ``fail_envelope`` means the caller must return it immediately without
    dispatching the input event."""
    focus_check = _verify_focus(expect_window) if expect_window else None
    if focus_check is not None and focus_check["verified"] is False:
        return focus_check, None, urirun.fail(
            f"active window {focus_check['title']!r} does not match "
            f"expect_window={expect_window!r}; refused to {action} blind",
            connector=CONNECTOR_ID, action=action, focus=focus_check)
    fullscreen_check = _verify_fullscreen(expect_window) if (expect_window and require_fullscreen) else None
    if fullscreen_check is not None and fullscreen_check["trusted"] and fullscreen_check["fullscreen"] is False:
        return focus_check, fullscreen_check, urirun.fail(
            f"window {expect_window!r} is not maximized/fullscreen; refused to {action} blind",
            connector=CONNECTOR_ID, action=action, fullscreen=fullscreen_check)
    return focus_check, fullscreen_check, None


@conn.handler("input/command/type", isolated=True, meta={"label": "Type a whole string"})
def type_text(text: str = "", expect_window: str = "", require_fullscreen: bool = False) -> dict[str, Any]:
    """Type ``text`` into whatever currently has focus. Pass ``expect_window`` (a substring of
    the expected window title) to ground the target BEFORE typing — a trusted probe that shows
    a different window refuses the call instead of typing blind. Add ``require_fullscreen=True``
    to also refuse when a trusted probe shows that window isn't maximized. Leave both empty to
    keep today's blind behaviour (e.g. when the caller already grounded via
    ``task/command/run``'s ``focus`` step, or verifies after the fact via
    ``ui/command/type-verified``)."""
    if not text:
        return urirun.fail("text is required", connector=CONNECTOR_ID)
    focus_check, fullscreen_check, fail = _grounding_checks("type", expect_window, require_fullscreen)
    if fail is not None:
        return fail
    try:
        r = _ok(action="type", **B.dispatch("type", text=text))
        if focus_check is not None:
            r["focus"] = focus_check
        if fullscreen_check is not None:
            r["fullscreen"] = fullscreen_check
        return r
    except B.BackendError as exc:
        return _fail_from("type", exc)


@conn.handler("input/command/key", isolated=True, meta={"label": "Send a key or hotkey combo"})
def key(key: str = "", keys: str = "", expect_window: str = "", require_fullscreen: bool = False) -> dict[str, Any]:
    """Send ``keys``/``key``. Pass ``expect_window``/``require_fullscreen`` to refuse the call
    (same grounding as ``type_text``) when a trusted probe shows the wrong window, or a window
    that isn't maximized."""
    combo = keys or key
    if not combo:
        return urirun.fail("key/keys is required", connector=CONNECTOR_ID)
    focus_check, fullscreen_check, fail = _grounding_checks("key", expect_window, require_fullscreen)
    if fail is not None:
        return fail
    try:
        r = _ok(action="key", **B.dispatch("key", keys=combo))
        if focus_check is not None:
            r["focus"] = focus_check
        if fullscreen_check is not None:
            r["fullscreen"] = fullscreen_check
        return r
    except B.BackendError as exc:
        return _fail_from("key", exc)


@conn.handler("input/command/click", isolated=True, meta={"label": "Mouse click (optionally at x,y)"})
def click(button: str = "left", x: int | None = None, y: int | None = None) -> dict[str, Any]:
    if button not in ("left", "middle", "right"):
        return urirun.fail("button must be left|middle|right", connector=CONNECTOR_ID)
    try:
        return _ok(action="click", **_positioned_click(button, x, y))
    except B.BackendError as exc:
        return _fail_from("click", exc)


@conn.handler("input/command/move", isolated=True, meta={"label": "Move the mouse pointer (absolute)"})
def move(x: int = 0, y: int = 0) -> dict[str, Any]:
    try:
        return _ok(action="move", **B.dispatch("move", x=int(x), y=int(y)))
    except B.BackendError as exc:
        return _fail_from("move", exc)


@conn.handler("input/command/wait", isolated=True, meta={"label": "Pause for N seconds (let the UI settle)"})
def wait(seconds: float = 1.0, ms: int = 0) -> dict[str, Any]:
    """Sleep so a UI can settle between actions (page load, animation, focus change).
    A primitive the NL planner reaches for between type/click steps — pass ``seconds``
    (float) or ``ms``. Capped at 30s so a bad plan can't hang the node."""
    try:
        secs = min(30.0, max(0.0, float(ms) / 1000.0 if ms else float(seconds)))
        time.sleep(secs)
        return _ok(action="wait", seconds=secs)
    except Exception as exc:  # noqa: BLE001
        return _fail_from("wait", exc)



@conn.handler("input/command/scroll", isolated=True, meta={"label": "Scroll the wheel (dy<0 = down)"})
def scroll(dy: int = -3) -> dict[str, Any]:
    try:
        return _ok(action="scroll", **B.dispatch("scroll", dy=int(dy)))
    except B.BackendError as exc:
        return _fail_from("scroll", exc)


@conn.handler("input/command/double-click", isolated=True, meta={"label": "Double click (optionally at x,y)"})
def double_click(x: int | None = None, y: int | None = None) -> dict[str, Any]:
    try:
        return _ok(action="double-click", **_positioned_click("left", x, y, clicks=2))
    except B.BackendError as exc:
        return _fail_from("double-click", exc)


@conn.handler("input/command/triple-click", isolated=True, meta={"label": "Triple click (optionally at x,y)"})
def triple_click(x: int | None = None, y: int | None = None) -> dict[str, Any]:
    try:
        return _ok(action="triple-click", **_positioned_click("left", x, y, clicks=3))
    except B.BackendError as exc:
        return _fail_from("triple-click", exc)


@conn.handler("input/command/right-click", isolated=True, meta={"label": "Right click (optionally at x,y)"})
def right_click(x: int | None = None, y: int | None = None) -> dict[str, Any]:
    try:
        return _ok(action="right-click", **_positioned_click("right", x, y))
    except B.BackendError as exc:
        return _fail_from("right-click", exc)


@conn.handler("input/command/middle-click", isolated=True, meta={"label": "Middle click (optionally at x,y)"})
def middle_click(x: int | None = None, y: int | None = None) -> dict[str, Any]:
    try:
        return _ok(action="middle-click", **_positioned_click("middle", x, y))
    except B.BackendError as exc:
        return _fail_from("middle-click", exc)


@conn.handler("input/command/hover", isolated=True, meta={"label": "Hover/move the mouse pointer to x,y"})
def hover(x: int = 0, y: int = 0) -> dict[str, Any]:
    try:
        return _ok(action="hover", **B.dispatch("move", x=int(x), y=int(y)))
    except B.BackendError as exc:
        return _fail_from("hover", exc)


@conn.handler("input/command/drag-and-drop", isolated=True, meta={"label": "Drag and drop (approximate move->click->move)"})
def drag_and_drop(x: int, y: int, destination_x: int, destination_y: int) -> dict[str, Any]:
    try:
        B.dispatch("move", x=int(x), y=int(y))
        time.sleep(0.1)
        res = B.dispatch("move", x=int(destination_x), y=int(destination_y))
        return _ok(action="drag-and-drop", **_spread(res))
    except B.BackendError as exc:
        return _fail_from("drag-and-drop", exc)





@conn.handler("abs/command/click", isolated=True, meta={"label": "Pixel-accurate click via a uinput absolute device"})
def click_abs(x: int = 0, y: int = 0, sw: int = 0, sh: int = 0, button: str = "left",
              do_click: bool = True, expect: str = "", monitor: int = 0) -> dict[str, Any]:
    """Click pixel (x,y) of a screenshot sized (sw,sh) using a raw uinput ABSOLUTE
    device. Coordinates map by FRACTION onto the desktop, so HiDPI/multi-monitor scaling
    and pointer acceleration (which break ydotool here) are bypassed — a screenshot pixel
    maps straight to a click point. Linux only.

    Honest ``effect`` reporting (anti ``runtime-lies-ok``): a bare ``ok:true`` only ever
    meant "the event was dispatched", never "the UI reacted". So the envelope now always
    carries an explicit ``effect``:
      • ``do_click=False`` (pure positioning) → ``effect:true`` — the move IS the effect.
      • ``expect=""`` (default click) → ``effect:null, verified:false`` — dispatched but the
        UI outcome is unverified AT THIS LAYER; pass ``expect`` or follow with a capture.
      • ``expect="<text>"`` → capture after the click and confirm ``<text>`` is on screen;
        the envelope is ``ok/effect:true`` only when verified, else ``ok:false/effect:false``.
    """
    try:
        did = B.uinput_abs_click(int(x), int(y), int(sw), int(sh), button, bool(do_click))
    except B.BackendError as exc:
        return _fail_from("click-abs", exc)
    base = dict(action="click-abs", screen=[sw, sh], **_spread(did))
    if not do_click:  # pure positioning — the move is the whole effect
        return _ok(effect=True, verified=None, **base)
    if not expect:  # dispatched, but this layer cannot confirm the UI reacted
        return _ok(effect=None, verified=False,
                   note="click dispatched; UI effect unverified at this layer — "
                        "pass expect=<text> or follow with a capture to confirm",
                   **base)
    try:
        shot = _fresh_capture("abs-click-verify", monitor)
    except Exception as exc:  # noqa: BLE001
        return _ok(effect=None, verified=False,
                   note=f"click dispatched; post-click capture failed: {str(exc)[:120]}",
                   **base)
    check = _verify_text_in_image(shot["path"], expect)
    if check.get("verified"):
        return _ok(effect=True, verified=True, expect=expect, verify=check, **base)
    return urirun.fail(f"click dispatched but expected text not found: {expect!r}",
                       connector=CONNECTOR_ID, action="click-abs", effect=False,
                       verified=False, expect=expect, verify=check, screen=[sw, sh])


def _shot_tag(label: str) -> str:
    return f"urirun-kvm-{label}-{os.getpid()}-{int(time.time() * 1000)}.png"


def _fresh_capture(label: str, monitor: int = 0) -> dict[str, Any]:
    path = _capture_native(monitor, _shot_tag(label))
    return {"label": label, "path": path, "screen": _image_wh(path)}


def _verify_text_in_image(image: str, text: str, min_conf: int = 35) -> dict[str, Any]:
    if not text:
        return {"required": False, "verified": None}
    try:
        hit = B.dispatch("locate", image=image, query=text, min_conf=int(min_conf))
    except B.BackendError as exc:
        return {"required": True, "verified": False, "error": str(exc)[:200]}
    found = bool(hit.get("found") or hit.get("matches"))
    return {"required": True, "verified": found, "via": hit.get("source") or hit.get("via"),
            "matches": len(hit.get("matches") or [])}


def _abs_click_from_frame(x: int, y: int, screen: list[int] | None, button: str = "left") -> dict[str, Any]:
    sw = int((screen or [0, 0])[0] or 0)
    sh = int((screen or [0, 0])[1] or 0)
    return B.uinput_abs_click(int(x), int(y), sw, sh, button=button, do_click=True)


@conn.handler("ui/command/type-verified", isolated=True,
              meta={"label": "Grounded click→type→verify draft→optional submit"})
def ui_type_verified(
    text: str = "",
    x: int = -1,
    y: int = -1,
    screen_w: int = 0,
    screen_h: int = 0,
    submit: bool = False,
    submit_key: str = "Enter",
    draft_expect: str = "",
    sent_expect: str = "",
    require_draft_verify: bool = True,
    require_sent_verify: bool = False,
    stabilize_key: str = "",
    settle: float = 0.8,
    monitor: int = 0,
) -> dict[str, Any]:
    """Type into a visible field with explicit grounding and postconditions.

    This route is the reusable version of the successful Signal workflow:
    stabilize state if needed, capture the frame, click coordinates in that exact
    frame's geometry, type, capture again, verify the draft, and only then optionally
    submit. ``ok`` means the requested postcondition passed; primitive command
    success is reported separately in ``steps``.
    """
    if not text:
        return urirun.fail("text is required", connector=CONNECTOR_ID, action="type-verified")
    if x < 0 or y < 0:
        return urirun.fail("x/y are required", connector=CONNECTOR_ID, action="type-verified")

    started = time.time()
    steps: list[dict[str, Any]] = []
    commentary: list[str] = []
    try:
        if stabilize_key:
            key_res = B.dispatch("key", keys=stabilize_key)
            steps.append({"op": "stabilize", "key": stabilize_key, "via": key_res.get("via"), "ok": True})
            commentary.append(f"stabilized with {stabilize_key}")
            time.sleep(min(max(float(settle), 0.1), 5.0))

        before = _fresh_capture("before-type-verified", monitor)
        screen = [int(screen_w), int(screen_h)] if screen_w and screen_h else before.get("screen")
        clicked = _abs_click_from_frame(x, y, screen)
        steps.append({"op": "click", "at": [int(x), int(y)], "screen": screen,
                      "via": clicked.get("via"), "ok": True})
        commentary.append(f"clicked target at {[int(x), int(y)]} in frame {screen}")
        time.sleep(min(max(float(settle), 0.1), 5.0))

        typed = B.dispatch("type", text=text)
        steps.append({"op": "type", "chars": len(text), "via": typed.get("via"), "ok": True})
        commentary.append(f"typed {len(text)} chars")
        time.sleep(min(max(float(settle), 0.1), 5.0))

        draft = _fresh_capture("draft-type-verified", monitor)
        draft_check = _verify_text_in_image(draft["path"], draft_expect or text)
        steps.append({"op": "verify-draft", **draft_check})
        if require_draft_verify and draft_check.get("verified") is not True:
            commentary.append("draft verification failed; stopped before submit")
            return urirun.fail("draft verification failed; stopped before submit",
                               connector=CONNECTOR_ID, action="type-verified",
                               verified=False, submitted=False, stopped="draft-not-verified",
                               elapsedMs=round((time.time() - started) * 1000),
                               screenshots={"before": before, "draft": draft},
                               steps=steps, commentary=commentary)

        sent = None
        sent_check = {"required": False, "verified": None}
        if submit:
            key_res = B.dispatch("key", keys=submit_key)
            steps.append({"op": "submit", "key": submit_key, "via": key_res.get("via"), "ok": True})
            commentary.append(f"submitted with {submit_key}")
            time.sleep(min(max(float(settle), 0.1), 5.0))
            sent = _fresh_capture("sent-type-verified", monitor)
            if sent_expect or require_sent_verify:
                sent_check = _verify_text_in_image(sent["path"], sent_expect or text)
                steps.append({"op": "verify-sent", **sent_check})
                if require_sent_verify and sent_check.get("verified") is not True:
                    commentary.append("sent verification failed after submit")
                    return urirun.fail("sent verification failed after submit",
                                       connector=CONNECTOR_ID, action="type-verified",
                                       verified=False, submitted=True, stopped="sent-not-verified",
                                       elapsedMs=round((time.time() - started) * 1000),
                                       screenshots={"before": before, "draft": draft, "sent": sent},
                                       steps=steps, commentary=commentary)

        verified = (draft_check.get("verified") is True) and (
            not submit or not require_sent_verify or sent_check.get("verified") is True
        )
        return _ok(action="type-verified", verified=verified, submitted=bool(submit),
                   elapsedMs=round((time.time() - started) * 1000),
                   screenshots={"before": before, "draft": draft, **({"sent": sent} if sent else {})},
                   steps=steps, commentary=commentary)
    except B.BackendError as exc:
        return _fail_from("type-verified", exc)


# --------------------------------------------------------------------------- #
# batched task — one bounded sequence (move/click/type/key/scroll/focus/sleep)
# --------------------------------------------------------------------------- #
def _refuse_if_wrong_window(op: str, expected_title: str, log: list[dict]) -> "dict[str, Any] | None":
    """task_run guard: before a type/key step, refuse the WHOLE task (nothing typed) if a
    TRUSTED foreground-window probe shows a window other than the one the batch's own
    ``focus`` step targeted. Returns a fail envelope to return immediately, or ``None`` to
    let the step proceed (including when the probe is untrusted — see ``_verify_focus``)."""
    if op not in ("type", "key") or not expected_title:
        return None
    check = _verify_focus(expected_title)
    if check["verified"] is False:
        log.append({"op": op, "ok": False, "focus": check,
                    "error": f"active window {check['title']!r} != expected {expected_title!r}"})
        return urirun.fail(
            f"step {op!r} refused: wrong window focused ({check['title']!r} != {expected_title!r})",
            connector=CONNECTOR_ID, steps=log)
    return None


def _refuse_if_not_fullscreen(op: str, expected_title: str, require_fullscreen: bool,
                               log: list[dict]) -> "dict[str, Any] | None":
    """task_run guard: before any action step, refuse the WHOLE task if a TRUSTED geometry
    probe shows the batch's targeted window no longer fills the screen (resized, un-maximized,
    tiled by the WM after focus). Absolute click/type coordinates are computed against a
    full-screen frame, so this catches the same class of silent-wrong-target mistake as
    ``_refuse_if_wrong_window``, just for window STATE instead of window IDENTITY."""
    if op not in ("type", "key", "click", "move", "scroll") or not expected_title or not require_fullscreen:
        return None
    check = _verify_fullscreen(expected_title)
    if check["trusted"] and check["fullscreen"] is False:
        log.append({"op": op, "ok": False, "fullscreen": check,
                    "error": f"window {expected_title!r} is not maximized/fullscreen"})
        return urirun.fail(
            f"step {op!r} refused: {expected_title!r} is not maximized/fullscreen",
            connector=CONNECTOR_ID, steps=log)
    return None


def _guard_step(op: str, expected_title: str, require_fullscreen: bool,
                 log: list[dict]) -> "dict[str, Any] | None":
    """Combined task_run pre-flight: refuse the step (and the whole task) if a TRUSTED probe
    shows the wrong window focused OR the right window no longer filling the screen."""
    return (_refuse_if_wrong_window(op, expected_title, log)
            or _refuse_if_not_fullscreen(op, expected_title, require_fullscreen, log))


def _task_focus_step(st: dict) -> "tuple[dict, str, bool, dict]":
    """Runs a task_run ``focus`` step: activate the window, and — unless the step passes
    ``fullscreen: false`` — ensure (check→satisfy→retry) it fills the screen. Returns
    ``(dispatch result, new expected_title, new require_fullscreen, extra log fields)``."""
    title = str(st.get("title", ""))
    r = B.dispatch("focus", title=title)
    require_fullscreen = bool(st.get("fullscreen", True))
    extra = {"fullscreen": _ensure_fullscreen(title)} if require_fullscreen else {}
    return r, title, require_fullscreen, extra


@conn.handler("task/command/run", isolated=True, meta={"label": "Run a bounded input sequence"})
def task_run(steps: list | None = None) -> dict[str, Any]:
    """Execute an ordered list of ``{op, ...}`` steps in one call (so a focus→click→
    type→submit flow shares the same ydotoold session). ops: focus, move, click, type,
    key, scroll, sleep. Input is structured data — never arbitrary code.

    A ``focus`` step records the window this task targets and — unless it passes
    ``fullscreen: false`` — maximizes it (check→satisfy→retry: verify, and only maximize if a
    trusted probe says it isn't already filling the screen). Every action step that follows a
    ``focus`` step re-probes BOTH the foreground window and its geometry FIRST and refuses to
    fire (the task fails, nothing is sent) if a TRUSTED probe shows the wrong window active or
    the right window no longer filling the screen — grounding before injection instead of
    discovering a wrong-window/wrong-layout mistake only after input already landed. When a
    probe can't answer at all (pure Wayland has no public active-window/geometry query), that
    is honestly unverifiable, not a pass, and the step proceeds best-effort exactly as before —
    callers on those sessions still want ``ui/command/type-verified`` for a post-hoc check."""
    log: list[dict] = []
    expected_title = ""
    require_fullscreen = True
    for st in (steps or []):
        op = str(st.get("op", ""))
        extra: dict[str, Any] = {}
        try:
            if op == "sleep":
                time.sleep(min(float(st.get("seconds", 0.2)), 5.0))
                log.append({"op": op})
                continue
            guard = _guard_step(op, expected_title, require_fullscreen, log)
            if guard is not None:
                return guard
            if op == "focus":
                r, expected_title, require_fullscreen, extra = _task_focus_step(st)
            elif op == "move":
                r = B.dispatch("move", x=int(st["x"]), y=int(st["y"]))
            elif op == "click":
                if "x" in st and "y" in st:
                    B.dispatch("move", x=int(st["x"]), y=int(st["y"]))
                    time.sleep(0.15)
                r = B.dispatch("click", button=str(st.get("button", "left")))
            elif op == "type":
                r = B.dispatch("type", text=str(st.get("text", "")))
            elif op == "key":
                r = B.dispatch("key", keys=str(st.get("keys", st.get("key", ""))))
            elif op == "scroll":
                r = B.dispatch("scroll", dy=int(st.get("dy", -3)))
            else:
                log.append({"op": op, "skipped": "unknown op"})
                continue
            log.append({"op": op, "via": r.get("via"), "ok": True, **extra})
            time.sleep(float(st.get("after", 0.25)))
        except (B.BackendError, KeyError, ValueError) as exc:
            log.append({"op": op, "ok": False, "error": str(exc)[:160]})
            return urirun.fail(f"step {op!r} failed", connector=CONNECTOR_ID, steps=log)
    return _ok(action="task", steps=log)


# --------------------------------------------------------------------------- #
# windows + diagnostics
# --------------------------------------------------------------------------- #
@conn.handler("window/command/focus", isolated=True, meta={"label": "Activate a window by title"})
def focus(title: str = "") -> dict[str, Any]:
    """Activate a window by title. ``wmctrl -a``/atspi are fire-and-forget — they don't error
    when the title matches nothing — so the response includes ``verify``: a foreground-window
    re-probe against ``title``, honest about whether the activation actually landed
    (``verify.verified``) or couldn't be checked at all (``verify.trusted:false`` on pure
    Wayland). Callers who need to act on the result, not just log it, should use
    ``task/command/run`` with a ``focus`` step, which enforces this before any ``type``/``key``
    step that follows."""
    if not title:
        return urirun.fail("title is required", connector=CONNECTOR_ID)
    try:
        r = _ok(action="focus", **B.dispatch("focus", title=title))
        r["verify"] = _verify_focus(title)
        return r
    except B.BackendError as exc:
        # Degrade instead of fail — focus is best-effort; CDP flows don't need it,
        # and Wayland wmctrl/atspi are unreliable on pre-navigation pages.
        return urirun.ok(
            connector=CONNECTOR_ID, action="focus",
            degraded=True, degradedReason=str(exc),
            platform=B.platform_tag(),
        )


@conn.handler("window/command/maximize", isolated=True,
              meta={"label": "Maximize a window by title so it fills the screen"})
def window_maximize(title: str = "") -> dict[str, Any]:
    """Maximize a window by title, then re-probe its geometry (same fire-and-forget caveat as
    ``focus``: the WM call doesn't error on a non-matching title, so ``verify`` is the honest
    signal). ``task/command/run``'s ``focus`` step calls this automatically before any
    subsequent action step — this route is for a standalone check/fix."""
    if not title:
        return urirun.fail("title is required", connector=CONNECTOR_ID)
    try:
        B.dispatch("maximize", title=title)
    except B.BackendError as exc:
        return _fail_from("maximize", exc)
    return _ok(action="maximize", verify=_verify_fullscreen(title))


@conn.handler("window/query/list", isolated=True, meta={"label": "List open windows"})
def window_list(app: str = "", title: str = "") -> dict[str, Any]:
    try:
        return _ok(**B.dispatch("window_list", app=app, title=title))
    except B.BackendError as exc:
        return _fail_from("window_list", exc)


@conn.handler("window/command/close", isolated=True,
              meta={"label": "Snapshot the active page, then close it (reversible: restore)"})
def window_close(id: str = "") -> dict[str, Any]:
    """Checkpoint-before-mutate: capture the page's serializable state (URL, scroll, form
    values, sessionStorage) in one CDP round-trip, return it as ``snapshot``, then close the
    tab. The reversible engine pairs this with window/command/restore as the inverse. Fidelity
    is bounded to what the snapshot serialized; ephemeral in-memory state (live sockets, JS-only
    vars) is outside the edge and is not claimed restorable."""
    cdp = _cdp_mod()
    snapshot_js = """(() => ({
        url: location.href, scrollX: scrollX, scrollY: scrollY,
        forms: [...document.querySelectorAll('input,textarea,[contenteditable]')].map(
            (el, i) => ({i: i, ce: !!el.isContentEditable, v: el.isContentEditable ? el.textContent : el.value})),
        session: (() => { try { return Object.fromEntries(Object.entries(sessionStorage)); } catch (e) { return {}; } })()
    }))()"""
    try:
        snap = cdp.evaluate(snapshot_js)
    except (B.BackendError, cdp.CdpError) as exc:
        return _fail_from("window-close", exc)
    if isinstance(snap, dict):
        snap["id"] = id or "active"
    try:
        cdp.evaluate("window.close()")
    except (B.BackendError, cdp.CdpError):
        # some pages refuse window.close(); the tab may persist, the snapshot stays valid
        pass
    return _ok(action="window-close", did=f"close({id or 'active'})", reversible=True, snapshot=snap,
               inverse={"path": "window/command/restore", "args": {"snapshot": snap}})


@conn.handler("window/command/restore", isolated=True,
              meta={"label": "Reopen and rehydrate a window from a close snapshot"})
def window_restore(snapshot: dict | None = None) -> dict[str, Any]:
    """Inverse of window/command/close: navigate to the snapshot URL, then rehydrate scroll and
    form values (dispatching input/change so React/contenteditable register them). Honest
    caveats: sessionStorage set post-load is missed by on-load scripts and back/forward history
    is not reconstructable by a single navigate; fidelity is bounded to the snapshot."""
    s = snapshot or {}
    if not s.get("url"):
        return urirun.fail("snapshot.url is required to restore", connector=CONNECTOR_ID, action="window-restore")
    rehydrate_js = """(() => {
        scrollTo(__SX__, __SY__);
        const f = __FORMS__;
        const els = document.querySelectorAll('input,textarea,[contenteditable]');
        f.forEach(x => {
            const el = els[x.i];
            if (!el) return;
            if (x.ce) el.textContent = x.v; else el.value = x.v;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        });
    })()"""
    expr = (rehydrate_js.replace("__SX__", str(int(s.get("scrollX", 0))))
            .replace("__SY__", str(int(s.get("scrollY", 0))))
            .replace("__FORMS__", _json.dumps(s.get("forms") or [])))
    cdp = _cdp_mod()
    try:
        cdp.navigate(s["url"])
        cdp.page_ready()
        cdp.evaluate(expr)
    except (B.BackendError, cdp.CdpError) as exc:
        return _fail_from("window-restore", exc)
    return _ok(action="window-restore", did=f"restore({s.get('id', '?')})", reversible=True,
               inverse={"path": "window/command/close", "args": {"id": s.get("id")}})


@conn.handler("proc/command/kill", isolated=True, meta={"label": "Terminate a process by PID or name (node lifecycle control)"})
def proc_kill(pid: int = 0, name: str = "", signal: str = "TERM") -> dict[str, Any]:
    """Send a signal to a process so process lifecycle is controllable *via a URI*, not a
    side-channel shell — e.g. close a stray CDP/headless browser or restart Chrome with a
    debug port. Target by ``pid`` OR by ``name`` (pgrep -f pattern → signals every match).
    Defaults to SIGTERM (graceful); pass ``signal="KILL"`` to force. Refuses pid<=1 and a
    name shorter than 3 chars (avoid over-broad kills)."""
    import os as _os
    import signal as _sig
    import subprocess as _sp
    sig = getattr(_sig, signal if signal.startswith("SIG") else f"SIG{signal.upper()}", _sig.SIGTERM)
    targets: list[int] = []
    if name:
        if len(name) < 3:  # noqa: PLR2004
            return urirun.fail("name must be >= 3 chars", connector=CONNECTOR_ID, action="kill")
        try:
            out = _sp.run(["pgrep", "-f", name], capture_output=True, text=True, timeout=8).stdout
            targets = [int(x) for x in out.split() if x.isdigit() and int(x) != _os.getpid()]
        except Exception as exc:  # noqa: BLE001
            return urirun.fail(f"pgrep failed: {exc}", connector=CONNECTOR_ID, action="kill")
    elif int(pid) > 1:
        targets = [int(pid)]
    else:
        return urirun.fail("pass pid>1 or name>=3 chars", connector=CONNECTOR_ID, action="kill")
    killed, errs = [], []
    for p in targets:
        try:
            _os.kill(p, sig)
            killed.append(p)
        except ProcessLookupError:
            pass
        except PermissionError:
            errs.append(p)
    return _ok(action="kill", signal=sig.name, matched=len(targets), killed=killed, denied=errs)


@conn.handler("a11y/command/act", isolated=True, meta={"label": "Find a UI element by role/name and focus/click/set-text it (AT-SPI)"})
def a11y_act(app: str = "", role: str = "", name: str = "", action: str = "focus",
             text: str = "", nth: int = 0) -> dict[str, Any]:
    """Resolution-independent UI control via the accessibility tree: locate an element
    by ``app``/``role``/``name`` and ``focus``/``click``/``settext`` it — no coordinates.
    Web content (Chrome/Firefox) must have a11y enabled. Returns the element's screen
    ``bbox`` so a caller can fall back to a ``kvm`` click when no action is exposed."""
    if action not in ("focus", "click", "settext", "gettext"):
        return urirun.fail("action must be focus|click|settext|gettext", connector=CONNECTOR_ID)
    try:
        res = B.dispatch("a11y", app=app, role=role, name=name, op=action, text=text, nth=int(nth))
        return _ok(action="a11y", request={"app": app, "role": role, "name": name, "op": action}, **_spread(res))
    except B.BackendError as exc:
        return _fail_from("a11y", exc)


# --------------------------------------------------------------------------- #
# ui/* — semantic perceive → act → verify layer over locate (AT-SPI → imgl → vql)
# + input. Targets are named by text/role/app, not coordinates.
# --------------------------------------------------------------------------- #
def _click_hit(hit: dict, app: str, role: str, text: str) -> dict:
    """Act on a located hit: AT-SPI native click when actionable (no coords), else a
    kvm click at the element's centre. Prefers the locate ``center`` (OCR backends emit
    it directly), falls back to the bbox centre, and raises a clean error when the target
    was not located — instead of ``KeyError: 'bbox'`` on a tesseract miss, which returns
    ``found: false`` with no bbox."""
    if hit.get("source") == "atspi" and hit.get("actionable"):
        return {"how": "atspi-action", **B.dispatch("a11y", app=app, role=role, name=text, op="click")}
    if not hit.get("found"):
        raise B.BackendError(f"ui-click: target not located (text={text!r} role={role!r})")
    center = hit.get("center")
    if center and len(center) == 2:  # noqa: PLR2004
        cx, cy = int(center[0]), int(center[1])
    elif hit.get("bbox"):
        cx, cy = B.bbox_center(hit["bbox"])
    else:
        raise B.BackendError(f"ui-click: located hit has no center/bbox (source={hit.get('source')!r})")
    return {"how": "kvm-click", "at": [cx, cy], **_positioned_click("left", cx, cy)}


def _router_return(action: str, r: dict[str, Any]) -> dict[str, Any]:
    """Serialise a control-router result safely: drop the router's own ``ok``/``error``
    (urirun.ok/fail set those) so spreading the rest never collides, and surface the
    ``attempts`` trail either way so a miss explains which strategies were tried."""
    body = {k: v for k, v in r.items() if k not in ("ok", "error")}
    if r.get("ok") or r.get("found"):
        return _ok(action=action, **body)
    return urirun.fail(r.get("error", "no control strategy succeeded"),
                       connector=CONNECTOR_ID, action=action, **body)


@conn.handler("ui/query/find", isolated=True,
              meta={"label": "Locate a UI element (router: cdp→atspi→vision)"})
def ui_find(text: str = "", role: str = "", app: str = "", nth: int = 0, name: str = "") -> dict[str, Any]:
    return _router_return("find", C.route("locate", text=text, role=role, app=app, name=name))


@conn.handler("ui/command/click", isolated=True,
              meta={"label": "Find a target and click it (router: cdp→atspi→vision)"})
def ui_click(text: str = "", role: str = "", app: str = "", name: str = "") -> dict[str, Any]:
    return _router_return("ui-click", C.route("click", text=text, role=role, app=app, name=name))


@conn.handler("ui/command/fill", isolated=True,
              meta={"label": "Find a field, focus it and type a value (router; verifies)"})
def ui_fill(text: str = "", role: str = "entry", app: str = "", value: str = "",
            verify: bool = True, name: str = "") -> dict[str, Any]:
    """Route a fill to the best control tool (cdp DOM → atspi → vision OCR+uinput): locate
    the field by ``text``/``role``/``name``, focus it, type ``value``. The vision fallback
    VERIFIES the value actually appeared and FAILS honestly if it focused the wrong
    element; the cdp/atspi strategies act on the real element so they don't need it."""
    if not value:
        return urirun.fail("value is required", connector=CONNECTOR_ID)
    prev = None  # read the field's current value (same finder fill uses) so we can register an inverse
    try:
        loc = C.route("locate", text=text, role=role, app=app, name=name, cheap=True)
        if loc.get("ok") or loc.get("found"):
            prev = loc.get("value")
    except Exception:  # noqa: BLE001 - locate is best-effort; no prev value -> no inverse
        prev = None
    out = _router_return("ui-fill", C.route("fill", text=text, role=role, app=app,
                                            name=name, value=value, verify=verify))
    if out.get("ok") and isinstance(prev, str) and prev != value:
        # fill(new) reverse fill(old): restore the value we overwrote
        out["inverse"] = {"path": "ui/command/fill",
                          "args": {"text": text, "role": role, "name": name, "value": prev}}
    return out


@conn.handler("ui/query/strategies", isolated=True,
              meta={"label": "Which control strategies are available right now"})
def ui_strategies() -> dict[str, Any]:
    return _ok(action="strategies", **C.report())


@conn.handler("env/query/profile", isolated=True,
              meta={"label": "Live capability profile of this session (cdp/atspi/ocr/input/display)"})
def env_profile() -> dict[str, Any]:
    """What UI control ACTUALLY works here — so the router fits the machine and the
    diagnostics layer recommends only feasible remediation. ``controlStrategies`` says which
    of cdp/atspi/vision can run; ``best`` is the preferred one; ``controllable`` is the
    honest top-line (false ⇒ install tesseract / grant /dev/uinput / launch a CDP Chrome)."""
    try:
        from urirun_connector_kvm import environment as _env
    except ImportError:
        import environment as _env  # type: ignore
    return _ok(action="env-profile", **_env.profile())


def _surface_mod() -> Any:
    try:
        from urirun_connector_kvm import surface as _s
    except ImportError:
        import surface as _s  # type: ignore
    return _s


@conn.handler("browser/query/sessions", isolated=True,
              meta={"label": "Scan running browsers and installed profiles for active service sessions (LinkedIn, Google…)"})
def browser_sessions(services: str = "") -> dict[str, Any]:
    """Task-aware session discovery: which browser/profile is logged in to which service.
    ``services`` is a comma-separated list of service names to check (default: all known services).
    Returns a list of browser entries with ``sessions`` dict so the planner can choose the right
    browser/profile for the task instead of opening a throwaway profile without a session."""
    try:
        from urirun_connector_kvm import environment as _env
    except ImportError:
        import environment as _env  # type: ignore
    svc_list = [s.strip() for s in services.split(",") if s.strip()] if services else None
    entries = _env.browser_sessions(svc_list)
    return _ok(action="browser-sessions", browsers=entries)


def _readiness_mod() -> Any:
    try:
        from urirun_connector_kvm import readiness as _r
    except ImportError:
        import readiness as _r  # type: ignore
    return _r


@conn.handler("ready/query/resolve", isolated=True,
              meta={"label": "Resolve the execution surface for a task and gate readiness (resolve-first)"})
def ready_resolve(task: str = "", service: str = "") -> dict[str, Any]:
    """READINESS KERNEL: before any input is sent, answer 'on which surface may this task
    run, and is it safe?'. Composes the LIVE signals (browser sessions/auth per profile,
    CDP reachability, window-enumeration health, input availability) into a policy-gated
    ready:// decision — recommended_surface, forbidden, blockers — so a plan never opens a
    throwaway browser when a logged-in profile exists. See readiness.py for the policy."""
    if not service:
        return urirun.fail("service is required (e.g. 'linkedin')", connector=CONNECTOR_ID,
                           action="ready-resolve")
    try:
        from urirun_connector_kvm import environment as _env
    except ImportError:
        import environment as _env  # type: ignore
    sessions = _env.browser_sessions([service])
    running_cdp = [b for b in sessions if b.get("running") and b.get("cdp_port")]
    cdp_reachable = False
    try:
        cdp_reachable = bool(_cdp_mod().reachable())
    except Exception:  # noqa: BLE001
        cdp_reachable = False
    # Window enumeration: PREFER vdisplay (multi-backend, sees Chrome on Wayland where atspi
    # cannot); fall back to the local atspi list. Degraded means the enumeration BACKEND could
    # not list windows at all — NOT merely that Chrome isn't running — so the resolver trusts
    # the window signal whenever a backend works.
    enum_ok, win_backend = _enumerate_windows()
    input_ok = B.uinput_available() or bool(shutil.which("ydotool"))
    signals = {
        "browser_sessions": sessions,
        "cdp_reachable": cdp_reachable,
        "cdp_auth_known": any((b.get("sessions") or {}).get(service) for b in running_cdp),
        "input_available": input_ok,
        "window_list_degraded": not enum_ok,
        "window_backend": win_backend,
        "vision_available": input_ok and _vision_available(),
        "api_connector_available": _api_connector_available(service),
    }
    return _ok(action="ready-resolve", **_readiness_mod().resolve(task or f"{service}.action", service, signals))


def _vision_available() -> bool:
    """Is the VISION grounding path usable — capture + a vql analyser served on this node? It
    needs no window list, so it is the desktop surface on GNOME-Wayland. Checked via in-node
    route composition (the vql:// diagnose route), so it reflects what is actually deployed."""
    r = _call_node_route("vql://host/image/query/diagnose", {"image": "__probe__"})
    return r is not None            # route reachable (envelope even on the probe path) = vql present


def _call_node_route(uri: str, payload: dict, timeout: float = 20) -> "dict | None":
    """In-node route composition: call a SIBLING connector's route served on THIS node, over
    the node's own loopback (URIRUN_NODE_SELF_URL, set at deploy via --env). The right way for
    one connector to consume another — the deployed module name (flat vs package) is irrelevant,
    only the served URI matters. Returns None when self-URL is unset or the call fails, so the
    caller degrades. Safe: the node is a ThreadingHTTPServer, so a self-call runs on a new
    thread (no deadlock)."""
    base = os.environ.get("URIRUN_NODE_SELF_URL")
    if not base:
        return None
    try:
        import json as _j
        import urllib.request as _u
        body = _j.dumps({"uri": uri, "mode": "execute", "payload": payload}).encode()
        req = _u.Request(base.rstrip("/") + "/run", data=body,
                         headers={"Content-Type": "application/json"})
        env = _j.load(_u.urlopen(req, timeout=timeout))
        val = (env.get("result") or {}).get("value")
        return val if isinstance(val, dict) else (env.get("result") or {})
    except Exception:  # noqa: BLE001
        return None


def _enumerate_windows() -> tuple[bool, str]:
    """(can_ground_app_focus, backend). Whether we can TRUST the window list to identify an
    app's focus owner — the precondition for safe blind HID.

    Empirically (2026-07-05): on GNOME-Wayland NO tool enumerates Wayland-native app windows
    unless org.gnome.Shell.Eval is on (usually disabled) — x11/xdotool sees only XWayland,
    atspi only gnome-shell. So a window list that came back is NOT automatically trustworthy:
    vdisplay reports ``wayland_native_visible`` and we honour it. When Wayland-native windows
    are invisible we return degraded, so the resolver won't recommend blind typing.

    Composition order: the vdisplay ROUTE via in-node self-call (works regardless of how
    vdisplay is deployed) → the vdisplay package import (dev/local) → the atspi fallback."""
    r = _call_node_route("vdisplay://host/windows/query/list", {"apps_only": False})
    if r is not None:                       # the ROUTE was reached (composition layer worked)
        if r.get("ok"):
            return bool(r.get("wayland_native_visible")), "vdisplay:route"
        # route reached but enumeration unavailable (e.g. Wayland: xdotool missing) — trustworthy
        # window grounding is impossible here, so degraded; but record that vdisplay answered.
        return False, "vdisplay:route(unavailable)"
    try:
        from urirun_connector_vdisplay.core import windows_list as _vd_windows
        r = _vd_windows(apps_only=False)
        if r.get("ok"):
            # trustworthy only if Wayland-native windows are actually enumerable (or X11 session)
            return bool(r.get("wayland_native_visible")), "vdisplay:import"
    except Exception:  # noqa: BLE001 - vdisplay not installed on this node
        pass
    try:
        wins = _window_mod_list()
        # atspi here only ever surfaces gnome-shell/Terminal (never app/Chrome windows) — treat
        # that as degraded: it cannot ground an unambiguous app-window focus owner.
        app_like = [w for w in wins if str(w.get("app", "")).lower()
                    not in ("gnome-shell", "", "org.gnome.terminal")]
        return bool(app_like), "atspi"
    except Exception:  # noqa: BLE001
        return False, "none"


def _window_mod_list() -> list:
    try:
        r = window_list()
    except Exception:  # noqa: BLE001
        return []
    return r.get("windows") or []


def _api_connector_available(service: str) -> bool:
    """Is a purpose-built API connector for this service importable on the node?"""
    import importlib.util
    return importlib.util.find_spec(f"urirun_connector_{service}") is not None


@conn.handler("surface/query/current", isolated=True,
              meta={"label": "What UI surface is in the foreground (browser via CDP, or desktop)"})
def surface_current() -> dict[str, Any]:
    """Foreground surface so a flow need not name ``app``: a reachable CDP page ⇒ browser
    (router uses the DOM path); otherwise a desktop surface + the env's best strategy."""
    return _ok(action="surface", **_surface_mod().current())


@conn.handler("cdp/session/command/ensure", isolated=True,
              meta={"label": "Reuse or launch a dedicated-profile CDP Chrome (so the router's cdp strategy is available)"})
def cdp_ensure(url: str = "", user_data_dir: str = "", copy_from: str = "",
               wait: float = 0.0) -> dict[str, Any]:
    """Make the CDP control surface AVAILABLE — LAUNCH/PROBE SPLIT so Chrome's cold-start
    can't blow the node handler's exec cap. Reuses a live endpoint, else FIRES the launch and
    returns ``launching: True`` *without* awaiting the bind (default ``wait=0``). Poll
    ``cdp/session/query/ready`` for readiness — it won't spawn a competing Chrome (re-calling
    *this* would, fighting over the profile lock). ``wait>0`` blocks up to a cap-safe bound
    (min(wait,25)s) for convenience, but a timeout there is ``pending``, NOT a failure — Chrome
    may still be coming up. ``copy_from`` clones auth files for a logged-in session."""
    _cdp = _cdp_mod()
    r = _cdp.start_session(url=url, user_data_dir=user_data_dir, copy_from=copy_from)
    if r.get("ok") and r.get("launching") and float(wait) > 0:
        ready = _cdp.await_ready(timeout=min(float(wait), 25.0))
        r["launching"] = not ready.get("ready")
        if not ready.get("ready"):
            r["pending"] = True                       # not an error: poll cdp/session/query/ready
            r["readyError"] = ready.get("error")
    return _ok(action="cdp-ensure", **_spread(r)) if r.get("ok") else \
        urirun.fail(r.get("error", "cdp ensure failed"), connector=CONNECTOR_ID, action="cdp-ensure", **_spread(r))


@conn.handler("cdp/session/query/ready", isolated=True,
              meta={"label": "Poll until the CDP debug endpoint is reachable (no launch)"})
def cdp_session_ready(timeout: float = _CDP_SESSION_TIMEOUT) -> dict[str, Any]:
    """Readiness half of the launch/probe split: poll the debug endpoint WITHOUT launching
    (distinct from ``cdp/page/query/ready``, which waits on document load). Call after
    ``cdp/session/command/ensure``; repeatable — a pure probe never spawns a competing Chrome.
    ``timeout`` is capped under the node handler exec cap; call again if not ready yet."""
    r = _cdp_mod().await_ready(timeout=min(float(timeout), 25.0))
    return _ok(action="cdp-ready", **_spread(r)) if r.get("ready") else \
        urirun.fail(r.get("error", "debugger not reachable within timeout"),
                    connector=CONNECTOR_ID, action="cdp-ready",
                    **{k: v for k, v in r.items() if k != "error"})


def _cdp_mod() -> Any:
    try:
        from urirun_connector_kvm import cdp as _cdp
    except ImportError:
        import cdp as _cdp  # type: ignore
    return _cdp


@conn.handler("cdp/page/command/navigate", isolated=True,
              meta={"label": "Navigate the CDP page and wait until it finishes loading"})
def cdp_navigate(url: str = "", ready_timeout: float = 8.0) -> dict[str, Any]:
    if not url:
        return urirun.fail("url is required", connector=CONNECTOR_ID)
    cdp = _cdp_mod()
    try:
        prev = None
        try:
            prev = cdp.evaluate("location.href")   # capture before leaving, for the inverse
        except Exception:  # noqa: BLE001 - no page yet -> the inverse is simply unavailable
            prev = None
        nav = cdp.navigate(url)
        ready = cdp.page_ready(timeout=float(ready_timeout))
        out = _ok(action="cdp-navigate", url=url, ready=ready, **_spread(nav, "url"))
        if isinstance(prev, str) and prev and prev != url:
            # navigate(new) reverse navigate(old): return to the URL we left
            out["inverse"] = {"path": "cdp/page/command/navigate", "args": {"url": prev}}
        return out
    except Exception as exc:  # noqa: BLE001
        return urirun.fail(str(exc), connector=CONNECTOR_ID, action="cdp-navigate")


@conn.handler("cdp/page/query/ready", isolated=True,
              meta={"label": "Wait until the CDP page document is fully loaded"})
def cdp_ready(timeout: float = 30.0) -> dict[str, Any]:
    r = _cdp_mod().page_ready(timeout=float(timeout))
    return _ok(action="cdp-ready", **_spread(r)) if r.get("ok") else \
        urirun.fail("page not ready within timeout", connector=CONNECTOR_ID, action="cdp-ready", **_spread(r))


def _evaluate_cdp(expr: str, target_id: str = "", url_filter: str = "") -> Any:
    mod = _cdp_mod()
    if hasattr(mod, "evaluate_target") and (target_id or url_filter or hasattr(mod, "targets")):
        return mod.evaluate_target(expr, target_id=target_id, url_filter=url_filter)
    return mod.evaluate(expr)


@conn.handler("cdp/page/query/eval", isolated=True,
              meta={"label": "Evaluate JS in the CDP page and return the value (DOM read/inspect)"})
def cdp_eval(expr: str = "", target_id: str = "", url_filter: str = "") -> dict[str, Any]:
    """DOM verb: run ``expr`` in the page (returnByValue). The reliable alternative to OCR
    for web flows — read the real DOM instead of guessing from pixels. Runs on the NODE,
    where Chrome's debug port is localhost."""
    if not expr:
        return urirun.fail("expr is required", connector=CONNECTOR_ID, action="cdp-eval")
    try:
        return _ok(action="cdp-eval", value=_evaluate_cdp(expr, target_id=target_id, url_filter=url_filter))
    except Exception as exc:  # noqa: BLE001
        return urirun.fail(str(exc), connector=CONNECTOR_ID, action="cdp-eval")


@conn.handler("cdp/page/command/dom-fill", isolated=True,
              meta={"label": "Fill an input/textarea/contenteditable by CSS selector via the DOM"})
def cdp_dom_fill(selector: str = "", value: str = "", target_id: str = "", url_filter: str = "") -> dict[str, Any]:
    """DOM verb: set the value of the first element matching ``selector`` and fire
    input/change so frameworks (React etc.) notice — no focus race, no keystroke injection.
    contenteditable uses textContent. Returns matched=False when the selector hits nothing."""
    if not selector:
        return urirun.fail("selector is required", connector=CONNECTOR_ID, action="cdp-dom-fill")
    js = (
        "(function(sel,val){var e=document.querySelector(sel);if(!e)return{matched:false};"
        "if(e.isContentEditable){e.focus();e.textContent=val;}"
        "else{var p=Object.getOwnPropertyDescriptor(e.__proto__,'value');"
        "e.focus();if(p&&p.set)p.set.call(e,val);else e.value=val;}"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));"
        "return{matched:true,value:(e.value!==undefined?e.value:e.textContent)};})"
        "(" + _json.dumps(selector) + "," + _json.dumps(value) + ")"
    )
    try:
        r = _evaluate_cdp(js, target_id=target_id, url_filter=url_filter) or {}
        if not r.get("matched"):
            return urirun.fail(f"selector matched nothing: {selector}",
                               connector=CONNECTOR_ID, action="cdp-dom-fill", matched=False)
        return _ok(action="cdp-dom-fill", matched=True, value=r.get("value"))
    except Exception as exc:  # noqa: BLE001
        return urirun.fail(str(exc), connector=CONNECTOR_ID, action="cdp-dom-fill")


@conn.handler("cdp/page/command/dom-click", isolated=True,
              meta={"label": "Click an element by CSS selector (or visible text) via the DOM"})
def cdp_dom_click(selector: str = "", text: str = "", target_id: str = "", url_filter: str = "") -> dict[str, Any]:
    """DOM verb: click the first element matching ``selector``, or (when ``text`` is given)
    the first button/link/[role=button] whose visible text contains it. Reliable where a
    pixel click races a moving layout."""
    if not selector and not text:
        return urirun.fail("selector or text is required", connector=CONNECTOR_ID, action="cdp-dom-click")
    if selector:
        finder = "document.querySelector(" + _json.dumps(selector) + ")"
    else:
        # Prefer an EXACT (trimmed, case-insensitive) text match over a substring one, so
        # "Sign in" does not click "Sign in with Apple"; fall back to substring if no exact hit.
        finder = ("(function(t){var els=Array.from("
                  "document.querySelectorAll('button,a,[role=button]'));"
                  "var norm=function(e){return (e.innerText||'').trim().toLowerCase();};"
                  "return els.find(function(e){return norm(e)===t;})"
                  "||els.find(function(e){return norm(e).includes(t);});})("
                  + _json.dumps(text.strip().lower()) + ")")
    js = "(function(){var e=" + finder + ";if(!e)return{matched:false};" \
         "e.scrollIntoView({block:'center'});e.click();" \
         "return{matched:true,txt:(e.innerText||'').trim().slice(0,60)};})()"
    try:
        r = _evaluate_cdp(js, target_id=target_id, url_filter=url_filter) or {}
        if not r.get("matched"):
            return urirun.fail(f"nothing to click for {selector or text!r}",
                               connector=CONNECTOR_ID, action="cdp-dom-click", matched=False)
        return _ok(action="cdp-dom-click", matched=True, clicked=r.get("txt"))
    except Exception as exc:  # noqa: BLE001
        return urirun.fail(str(exc), connector=CONNECTOR_ID, action="cdp-dom-click")


@conn.handler("cdp/page/query/dom-extract-form", isolated=True,
              meta={"label": "Extract form structure, input fields, labels and options via the DOM"})
def cdp_dom_extract_form(selector: str = "", target_id: str = "", url_filter: str = "") -> dict[str, Any]:
    """DOM query: extract form schema and input fields (inputs, textareas, selects, checkboxes, buttons).
    If selector is given, extracts only within that form/container. Returns list of forms with their
    fields, validation requirements, and values for deterministic form automation."""
    js = r"""
    (function(sel){
      function getLabel(el){
        if(el.labels && el.labels.length > 0){
          return (el.labels[0].innerText || el.labels[0].textContent || '').trim();
        }
        var id = el.id;
        if(id){
          var lbl = document.querySelector('label[for="' + id + '"]');
          if(lbl) return (lbl.innerText || lbl.textContent || '').trim();
        }
        var parentLabel = el.closest('label');
        if(parentLabel){
          return (parentLabel.innerText || parentLabel.textContent || '').trim();
        }
        return el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
      }

      function extractFields(container){
        var els = Array.from(container.querySelectorAll('input, textarea, select, button'));
        return els.map(function(el){
          var tag = el.tagName.toLowerCase();
          var type = (el.type || '').toLowerCase();
          var item = {
            tag: tag,
            type: type,
            name: el.name || '',
            id: el.id || '',
            label: getLabel(el),
            placeholder: el.placeholder || '',
            required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
            value: el.value || ''
          };
          if(tag === 'select'){
            item.options = Array.from(el.options || []).map(function(o){
              return {value: o.value, text: (o.text || '').trim(), selected: o.selected};
            });
          }
          if(type === 'checkbox' || type === 'radio'){
            item.checked = Boolean(el.checked);
          }
          return item;
        });
      }

      var containers = [];
      if(sel){
        var c = document.querySelector(sel);
        if(!c) return {matched: false, forms: []};
        containers = [c];
      } else {
        var forms = Array.from(document.querySelectorAll('form'));
        containers = forms.length > 0 ? forms : [document.body];
      }

      var formsResult = containers.map(function(f){
        return {
          id: f.id || '',
          name: f.name || '',
          action: (typeof f.action === 'string' ? f.action : '') || '',
          method: (f.method || 'get').toLowerCase(),
          fields: extractFields(f)
        };
      });
      return {matched: true, forms: formsResult};
    })(""" + _json.dumps(selector) + ")"

    try:
        r = _evaluate_cdp(js, target_id=target_id, url_filter=url_filter) or {}
        if not r.get("matched", True):
            return urirun.fail(f"selector matched nothing: {selector}",
                               connector=CONNECTOR_ID, action="cdp-dom-extract-form", matched=False)
        return _ok(action="cdp-dom-extract-form", matched=True, forms=r.get("forms", []))
    except Exception as exc:  # noqa: BLE001
        return urirun.fail(str(exc), connector=CONNECTOR_ID, action="cdp-dom-extract-form")


def _resolve_act_app(app: str) -> tuple[str, dict | None]:
    """Detect the foreground surface if app is empty. Returns (resolved_app, surface_or_None)."""
    surface = None
    if not app:
        try:
            surface = _surface_mod().current()
            if surface.get("app"):
                app = surface["app"]
        except Exception:  # noqa: BLE001
            surface = None
    return app, surface


def _act_intent(do: str, text: str, role: str, app: str, name: str, intent: str) -> str:
    """Stable action key for ticketing/debugging. Callers can pass their own NL intent;
    otherwise derive one from the target so repeated failures group together."""
    if intent:
        return intent
    target = text or name or role or "(target)"
    where = f" in {app}" if app else ""
    return f"{do} {target}{where}".strip()


def _act_attempt_signature(result: dict[str, Any]) -> str:
    """A compact, comparable failure signature. If the same signature repeats, retry is
    not progress; it is a blind loop that needs a different surface/strategy."""
    bits: list[str] = [str(result.get("strategy") or "none"), str(result.get("error") or "")]
    for a in result.get("attempts") or []:
        bits.append("|".join(str(a.get(k, "")) for k in ("strategy", "error", "skipped", "found", "verify")))
    pc = result.get("postcondition") or {}
    if pc:
        bits.append(f"post:{pc.get('verified')}:{pc.get('text')}")
    return " ".join(bits).strip()


def _act_verify_expect(expect: str, app: str) -> dict[str, Any]:
    """Semantic postcondition check for ui/command/act. This is the guard against
    treating ok:true from an input primitive as task completion."""
    if not expect:
        return {"required": False, "verified": None}
    hit = C.route("locate", text=expect, app=app, cheap=True)
    present = bool(hit.get("ok") or hit.get("found"))
    return {"required": True, "text": expect, "verified": present,
            "strategy": hit.get("strategy"), "attempts": hit.get("attempts")}


def _act_retry_loop(op: str, text: str, role: str, app: str, name: str, value: str,
                    cheap: bool, retries: int, settle: float, budget: float,
                    start: float, expect: str = "") -> tuple:
    """Run the route/retry loop. Returns (tries, last_result).

    Success means the action route worked AND, when ``expect`` is supplied, the
    postcondition is visible. This prevents ok:true from a click/type primitive from
    masquerading as a completed task.
    """
    tries: list = []
    last: dict = {}
    for attempt in range(max(1, int(retries))):
        # For find/wait, keep the first probe cheap (cdp/atspi) and escalate retries
        # to full locate so vision can recover from cheap-mode false negatives.
        cheap_now = cheap and attempt == 0
        last = C.route(op, text=text, role=role, app=app, name=name, value=value, cheap=cheap_now)
        ok = last.get("ok") or last.get("found")
        post = _act_verify_expect(expect, app) if ok and expect else {"required": False, "verified": None}
        done = bool(ok) and (not expect or bool(post.get("verified")))
        if ok and expect:
            last = {**last, "postcondition": post}
            if not post.get("verified"):
                last["ok"] = False
                last["error"] = f"postcondition not met: {expect!r}"
        last["_act_done"] = done
        sig = _act_attempt_signature(last)
        tries.append({"attempt": attempt + 1, "ok": done, "acted": bool(ok),
                      "verified": post.get("verified"), "strategy": last.get("strategy"),
                      "error": last.get("error"), "signature": sig,
                      "strategyAttempts": last.get("attempts")})
        if done:
            return tries, last
        if time.monotonic() - start + float(settle) >= budget:
            break
        time.sleep(float(settle))
    return tries, last


def _act_stall(tries: list[dict[str, Any]], last: dict[str, Any]) -> dict[str, Any]:
    failed = [t for t in tries if not t.get("ok")]
    sigs = [t.get("signature") for t in failed if t.get("signature")]
    repeated = len(sigs) >= 2 and len(set(sigs)) == 1
    if repeated:
        return {"stalled": "blind-loop", "repeatCount": len(sigs), "signature": sigs[-1]}
    if last.get("postcondition") and not (last.get("postcondition") or {}).get("verified"):
        return {"stalled": "postcondition-missing", "repeatCount": len(failed),
                "signature": sigs[-1] if sigs else ""}
    return {"stalled": None, "repeatCount": len(failed), "signature": sigs[-1] if sigs else ""}


def _act_redefine(stall: dict[str, Any], do: str, text: str, role: str, app: str,
                  name: str, expect: str, last: dict[str, Any]) -> dict[str, Any]:
    reason = stall.get("stalled") or "failed"
    attempts = last.get("attempts") or []
    tried = [a.get("strategy") for a in attempts if a.get("strategy")]
    target = text or name or role
    steps = [
        "stop retrying the same route signature",
        "resolve a higher-level surface first: api/connector, browser CDP, or AT-SPI",
        "only fall back to vision/HID when the target is grounded and the postcondition is verifiable",
    ]
    if expect:
        steps.insert(1, f"verify the goal by finding {expect!r}, not by trusting the input primitive")
    return {"reason": reason, "target": target, "app": app, "tried": tried,
            "next": steps, "suggestedRoute": "ui/command/act",
            "suggestedPayload": {"do": do, "text": text, "role": role, "name": name,
                                 "app": app, "expect": expect, "safe": True}}


def _act_ticket_draft(intent: str, ticket: str, stall: dict[str, Any], redefine: dict[str, Any],
                      tries: list[dict[str, Any]], last: dict[str, Any]) -> dict[str, Any]:
    """Return a planfile-compatible ticket payload. The KVM connector stays dependency-free;
    loop/planfile can create this ticket when policy allows."""
    name = f"[KVM] Redefine stalled UI intent: {intent[:80]}"
    desc = {
        "intent": intent,
        "parent_ticket": ticket,
        "stall": stall,
        "redefine": redefine,
        "tries": tries[-5:],
        "last_error": last.get("error"),
    }
    return {"uri": "task://host/ticket/command/create",
            "payload": {"name": name, "description": _json.dumps(desc, ensure_ascii=False, indent=2),
                        "priority": "high",
                        "labels": "kvm,reliability,blind-loop,self-extension"}}


def _act_reject(do: str, text: str, name: str, value: str, safe: bool) -> dict[str, Any] | None:
    """Reject a malformed/unsafe act request; return a fail dict, else None to proceed.
    Human-in-the-loop gate: refuse an irreversible label (Post/Publish/Send/Delete…) unless
    the caller explicitly drops safe — so an autonomous planner can't publish on its own."""
    if do not in ("click", "fill", "find", "wait"):
        return urirun.fail("do must be click|fill|find|wait", connector=CONNECTOR_ID)
    if do == "fill" and not value:
        return urirun.fail("value is required for fill", connector=CONNECTOR_ID)
    label = (text or name or "").strip().lower()
    if safe and do in ("click", "fill") and any(w in label for w in C._IRREVERSIBLE):
        return urirun.fail(f"refusing to {do} {label!r} with safe=true (pass safe=false to allow)",
                           connector=CONNECTOR_ID, action="act", blocked="irreversible", do=do)
    return None


def _act_ready(ready_timeout: float) -> dict[str, Any] | None:
    """If a CDP page is reachable, wait for it to finish loading; else None."""
    cdp = _cdp_mod()
    if not cdp.reachable():
        return None
    try:
        return cdp.page_ready(timeout=min(float(ready_timeout), 8.0))
    except Exception:  # noqa: BLE001
        return {"ok": False}


@conn.handler("ui/command/act", isolated=True,
              meta={"label": "Self-orchestrating UI action: wait-ready → route → retry → verify"})
def ui_act(do: str = "click", text: str = "", role: str = "", name: str = "", value: str = "",
           app: str = "", retries: int = 3, settle: float = 0.7, ready_timeout: float = 6.0,
           safe: bool = True, expect: str = "", intent: str = "", ticket: str = "") -> dict[str, Any]:
    """ONE high-level URI an LLM planner can target instead of hand-assembling
    wait+find+click+verify (which it gets wrong: dumb sleeps, OCR label guesses, no verify).
    Internally: (1) if a CDP page is reachable, wait for it to finish loading; (2) route the
    action (click|fill|find|wait) through cdp→atspi→vision with up to ``retries`` attempts and
    a ``settle`` pause between (covers spinners / late-rendered elements); (3) return the
    winning strategy + the per-try trail. Acting via the router means it's role/name exact on
    CDP and degrades to OCR only when it must."""
    bad = _act_reject(do, text, name, value, safe)
    if bad is not None:
        return bad
    app, surface = _resolve_act_app(app)
    intent_key = _act_intent(do, text, role, app, name, intent)
    budget = _ACT_BUDGET_SECS
    start = time.monotonic()
    ready = _act_ready(ready_timeout)
    op = "locate" if do in ("find", "wait") else do
    cheap = do in ("find", "wait")
    tries, last = _act_retry_loop(op, text, role, app, name, value, cheap, retries, settle,
                                  budget, start, expect=expect)
    if last.get("_act_done"):
        body = {k: v for k, v in last.items()
                if k not in ("ok", "error", "attempts", "postcondition", "_act_done")}
        return _ok(action="act", do=do, app=app, surface=surface, ready=ready, tries=tries,
                   intent=intent_key, ticket=ticket or None, postcondition=last.get("postcondition"),
                   strategyAttempts=last.get("attempts"), **body)
    stall = _act_stall(tries, last)
    redefine = _act_redefine(stall, do, text, role, app, name, expect, last)
    draft = _act_ticket_draft(intent_key, ticket, stall, redefine, tries, last)
    return urirun.fail(last.get("error", f"act:{do} failed after {len(tries)} tries"),
                       connector=CONNECTOR_ID, action="act", do=do, app=app, surface=surface,
                       intent=intent_key, ticket=ticket or None, postcondition=last.get("postcondition"),
                       ready=ready, tries=tries, waited=round(time.monotonic() - start, 1),
                       strategyAttempts=last.get("attempts"), stalled=stall.get("stalled"),
                       stall=stall, redefine=redefine, ticketDraft=draft)


@conn.handler("cdp/session/query/status", isolated=True,
              meta={"label": "Is a CDP debug endpoint reachable, and on what page"})
def cdp_status() -> dict[str, Any]:
    try:
        from urirun_connector_kvm import cdp as _cdp
    except ImportError:
        import cdp as _cdp  # type: ignore
    reachable = _cdp.reachable()
    targets = []
    if reachable and hasattr(_cdp, "targets"):
        try:
            tlist = _cdp.targets()
            targets = [{"id": t.get("id"), "url": t.get("url"), "title": t.get("title")} for t in tlist]
        except Exception:
            pass
    return _ok(action="cdp-status", reachable=reachable, endpoint=_cdp.endpoint(), targets=targets)


@conn.handler("ui/query/wait", isolated=True, meta={"label": "Poll until a target appears (router)"})
def ui_wait(text: str = "", role: str = "", app: str = "", timeout: float = 10.0,
            interval: float = 0.7, name: str = "") -> dict[str, Any]:
    # Bound by REAL elapsed time, capped below the node's ~30s subprocess cap: counting
    # `interval` increments let a slow CDP/OCR poll run far past `timeout` and the node
    # killed core:ui_wait with TimeoutExpired. Poll the CHEAP strategies only (cdp/atspi
    # DOM-presence) so a single poll can't stall — OCR is reserved for the click itself.
    budget = max(0.0, min(float(timeout), 22.0))
    start = time.monotonic()
    last: dict[str, Any] = {}
    while True:
        last = C.route("locate", text=text, role=role, app=app, name=name, cheap=True)
        elapsed = time.monotonic() - start
        if last.get("found"):
            # _spread also drops `found`/`waited`/`action`/`strategy` — keys this _ok sets
            # explicitly — so the route hit doesn't collide ("_ok() got multiple values for 'found'").
            return _ok(action="wait", found=True, waited=round(elapsed, 1),
                       **_spread(last, "found", "waited"))
        if elapsed >= budget:
            break
        time.sleep(min(float(interval), max(0.0, budget - elapsed)))
    return urirun.fail(f"target not found within {timeout}s", connector=CONNECTOR_ID,
                       action="wait", waited=round(time.monotonic() - start, 1),
                       attempts=last.get("attempts"))


@conn.handler("ui/query/verify", isolated=True, meta={"label": "Assert a string is present on screen"})
def ui_verify(expect: str = "", text: str = "", app: str = "", required: bool = False) -> dict[str, Any]:
    expect = expect or text  # accept 'text' as alias (LLM planner uses both names)
    if not expect:
        return urirun.fail("expect is required", connector=CONNECTOR_ID)
    hit: dict[str, Any] = {}
    try:
        hit = B.dispatch("locate", text=expect, app=app)
        present = bool(hit.get("found"))
    except B.BackendError:
        present = False
    if required and not present:
        return urirun.fail(f"required text not found on screen: '{expect}'",
                           connector=CONNECTOR_ID, action="verify", present=False)
    return _ok(action="verify", present=present, via=hit.get("source"))


# --------------------------------------------------------------------------- #
# composite UI verbs — capture + locate (VQL/OCR) + KVM act, in one route
# The "specialized commands wired to libraries" layer: a caller asks for a label,
# not pixels. Coordinates from locate map 1:1 to a native-resolution capture.
# --------------------------------------------------------------------------- #
def _capture_native(monitor: int = 0, output: str = "") -> str:
    """Capture at native resolution (no downscale) so OCR boxes are screen pixels."""
    out = _resolve_output_path(output) if output else os.path.join(tempfile.gettempdir(), f"urirun-kvm-ui-{os.getpid()}.png")
    B.dispatch("capture", output=out, monitor=monitor)
    return out


def _image_wh(path: str) -> list[int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return [int(im.size[0]), int(im.size[1])]
    except Exception:  # noqa: BLE001 - PIL is optional and screenshots can be stale
        return None


def _input_wh() -> list[int] | None:
    try:
        sw, sh = B._screen_wh()
        return [int(sw), int(sh)] if sw and sh else None
    except Exception:  # noqa: BLE001
        return None


def _map_image_to_input(center: list | tuple, image: str) -> tuple[int, int, dict[str, Any]]:
    """Map OCR/capture coordinates into the input coordinate space.

    On remote/Wayland paths the rendered capture can be 1280x720 while the input surface is
    1920x1080. Treating image pixels as input pixels lands in the wrong app. Scaling by the
    observed frame size and the configured/detected input size makes the mismatch explicit.
    """
    x, y = float(center[0]), float(center[1])
    img = _image_wh(image)
    inp = _input_wh()
    mapping: dict[str, Any] = {"from": "image-px", "to": "input-px",
                               "image": img, "input": inp, "scale": [1.0, 1.0]}
    if img and inp and img[0] and img[1] and (img[0] != inp[0] or img[1] != inp[1]):
        sx, sy = inp[0] / img[0], inp[1] / img[1]
        x, y = x * sx, y * sy
        mapping["scale"] = [round(sx, 6), round(sy, 6)]
        mapping["scaled"] = True
    else:
        mapping["scaled"] = False
    mapped = [int(round(x)), int(round(y))]
    mapping["point"] = mapped
    return mapped[0], mapped[1], mapping


@conn.handler("ui/query/locate", isolated=True,
              meta={"label": "Locate on-screen elements by text (capture + OCR/VQL) → coordinates"})
def ui_locate(query: str = "", min_conf: int = _LOCATE_MIN_CONF, monitor: int = 0) -> dict[str, Any]:
    """Screenshot the screen and return elements whose text matches ``query`` (empty =
    all), each with a pixel ``box`` and click ``center`` — the perceive+locate half of
    the autonomous loop. Feed a ``center`` straight into ui/command/click-text or a
    kvm click."""
    try:
        shot = _capture_native(monitor)
        loc = B.dispatch("locate", image=shot, query=query, min_conf=int(min_conf))
    except B.BackendError as exc:
        return _fail_from("locate", exc)
    screen = None
    try:
        from PIL import Image
        with Image.open(shot) as im:
            screen = list(im.size)
    except Exception:  # noqa: BLE001 - PIL optional
        pass
    return _ok(action="locate", screenshot=shot, screen=screen, **_spread(loc, "screenshot", "screen"))


@conn.handler("ui/command/click-text", isolated=True,
              meta={"label": "Find on-screen text and click it via KVM (optionally type + submit)"})
def ui_click_text(text: str = "", button: str = "left", nth: int = 0, min_conf: int = 40,
                  then_type: str = "", then_key: str = "", monitor: int = 0) -> dict[str, Any]:
    """Close the perceive→locate→act loop in one call: screenshot, OCR-locate ``text``,
    move+click its center via KVM, then optionally type ``then_type`` and press
    ``then_key`` (e.g. ctrl+enter to submit). ``nth`` picks among multiple matches
    (sorted by confidence)."""
    if not text:
        return urirun.fail("text is required", connector=CONNECTOR_ID)
    try:
        shot = _capture_native(monitor)
        loc = B.dispatch("locate", image=shot, query=text, min_conf=int(min_conf))
    except B.BackendError as exc:
        return _fail_from("locate", exc)
    matches = loc.get("matches") or []
    if not matches:
        return urirun.fail(f"no on-screen text matches {text!r}", connector=CONNECTOR_ID,
                           action="click-text", screenshot=shot, candidates=0)
    target = matches[min(int(nth), len(matches) - 1)]
    cx, cy, coord = _map_image_to_input(target["center"], shot)
    try:
        clicked = _positioned_click(button, cx, cy)
        result: dict[str, Any] = {"clicked": target, "via": clicked.get("via"),
                                  "screenshot": shot, "matchCount": len(matches),
                                  "clickedInput": [cx, cy], "coordinateMapping": coord}
        if then_type:
            time.sleep(0.2)
            B.dispatch("type", text=then_type)
            result["typed"] = len(then_type)
        if then_key:
            time.sleep(0.2)
            B.dispatch("key", keys=then_key)
            result["submitted"] = then_key
    except B.BackendError as exc:
        return _fail_from("click-text", exc)
    return _ok(action="click-text", text=text, **result)


@conn.handler("doctor/query/report", isolated=True, meta={"label": "Report available backends + which surface to trust"})
def doctor() -> dict[str, Any]:
    """Backends per action AND a surface report: whether OS-level pixel input is actually
    reliable on this session (it is not on Wayland multi-monitor / fractional-HiDPI), with
    the recommended surface (browser-cdp / remotedesktop-portal / vdisplay) and warnings."""
    return _ok(platform=B.platform_tag(), wayland=B.is_wayland(),
               surfaces=B.surface_report(), backends=B.registry_report())


# --------------------------------------------------------------------------- #
# vnc/* — direct RFB surface for noVNC-hosted desktops (vnc.py). Deterministic by
# design: explicit target, native-resolution perceive (framebuffer grab) → OCR/vision
# locate → act at exact remote coords. No canvas scaling, no host-focus races, and no
# AT-SPI (which reads the LOCAL a11y tree — meaningless for a remote framebuffer).
# --------------------------------------------------------------------------- #
try:
    from urirun_connector_kvm import vnc as _vnc
except ImportError:  # pragma: no cover - flat single-file deploy
    try:
        import vnc as _vnc  # type: ignore
    except ImportError:
        _vnc = None  # type: ignore

_VNC_LOCATE_ORDER = ("easyocr", "tesseract", "imgl", "vql")


def _vnc_surface():
    if _vnc is None:
        raise RuntimeError("vnc surface unavailable — pip install 'urirun-connector-kvm[vnc]'")
    return _vnc


def _vnc_find_on(path: str, query: str, role: str = "") -> dict[str, Any]:
    """Locate on a pre-captured remote frame with OCR/vision backends only, in fixed
    reliability order; first genuine hit wins, misses are accumulated for the caller."""
    misses: list[str] = []
    backends = {b.name: b for b in B.backends_for("locate")}
    for nm in _VNC_LOCATE_ORDER:
        b = backends.get(nm)
        if b is None or not b.available():
            continue
        try:
            hit = b.fn(image=path, query=query, role=role)
            if hit.get("found"):
                return hit
            misses.append(f"{nm}: found=false ({hit.get('candidates', 0)} candidates)")
        except Exception as exc:  # noqa: BLE001 - fall through the chain per backend
            misses.append(f"{nm}: {exc}")
    return {"found": False, "misses": misses}


@conn.handler("vnc/query/status", isolated=True,
              meta={"label": "RFB reachability + native framebuffer size"})
def vnc_status(target: str = "") -> dict[str, Any]:
    try:
        v = _vnc_surface()
        shot = v.capture(target=target, out=os.path.join(tempfile.gettempdir(), "kvm_vnc_status.png"))
        return _ok(target=v.resolve_target(target), width=shot["width"], height=shot["height"], via="rfb")
    except Exception as exc:  # noqa: BLE001 - connectivity errors become an honest fail
        return _fail_from("vnc-status", exc)


@conn.handler("vnc/query/capture", isolated=True,
              meta={"label": "Native-resolution framebuffer screenshot over RFB"})
def vnc_capture(target: str = "", out: str = "", base64: bool = False) -> dict[str, Any]:
    try:
        shot = _vnc_surface().capture(target=target, out=out)
        res = _ok(action="capture", **shot)
        if base64:
            res["base64"] = _b64.b64encode(open(shot["path"], "rb").read()).decode()
        return res
    except Exception as exc:  # noqa: BLE001
        return _fail_from("vnc-capture", exc)


@conn.handler("vnc/query/find", isolated=True,
              meta={"label": "Locate text on the remote framebuffer (OCR/vision chain)"})
def vnc_find(text: str = "", role: str = "", target: str = "") -> dict[str, Any]:
    if not text and not role:
        return urirun.fail("text (or role) is required", connector=CONNECTOR_ID)
    try:
        v = _vnc_surface()
        shot = v.capture(target=target)
        hit = _vnc_find_on(shot["path"], text, role)
        # locate backends emit their own coord_space=image-px; on an RFB frame that IS
        # framebuffer-px, so override rather than collide on the kwarg
        return _ok(**{**hit, "action": "find", "frame": shot["path"],
                      "coord_space": "framebuffer-px"})
    except Exception as exc:  # noqa: BLE001
        return _fail_from("vnc-find", exc)


@conn.handler("vnc/command/click", isolated=True,
              meta={"label": "Click the remote desktop: by located text or exact coords"})
def vnc_click(text: str = "", x: int = -1, y: int = -1, button: int = 1, double: bool = False,
              verify: str = "", settle: float = 1.0, target: str = "") -> dict[str, Any]:
    """Perceive → act → verify in one route: locate ``text`` on a fresh native frame
    (or take exact ``x``/``y``), click over RFB, and when ``verify`` is given re-capture
    after ``settle`` seconds and require that text on screen — the flow layer gets an
    honest ``verified`` instead of fire-and-forget."""
    try:
        v = _vnc_surface()
        hit: dict[str, Any] = {}
        with v.session(target) as c:      # ONE RFB session per process (see vnc.py)
            if text:
                hit = _vnc_find_on(v.grab(c)["path"], text)
                if not hit.get("found"):
                    return urirun.fail(f"vnc-click: {text!r} not located on remote frame",
                                       connector=CONNECTOR_ID, misses=hit.get("misses"))
                x, y = hit["center"]
            if x < 0 or y < 0:
                return urirun.fail("vnc-click: give text or x/y", connector=CONNECTOR_ID)
            res = v.click_at(c, x, y, button=button, double=double)
            out = _ok(action="click", located=hit.get("center"), source=hit.get("source"), **res)
            if verify:
                time.sleep(min(float(settle), 15.0))
                check = _vnc_find_on(v.grab(c)["path"], verify)
                if not check.get("found"):
                    # one re-look: a single frame can catch mid-render UI or the pointer
                    # glyph sitting ON the verify text (seen live: the cursor over a fresh
                    # fluxbox menu row mangled its OCR) — frame noise, not a real failure
                    time.sleep(0.6)
                    check = _vnc_find_on(v.grab(c)["path"], verify)
                out["verified"] = bool(check.get("found"))
                out["verify"] = {"text": verify, **({"center": check["center"]} if check.get("found") else
                                                    {"misses": check.get("misses")})}
        return out
    except Exception as exc:  # noqa: BLE001
        return _fail_from("vnc-click", exc)


@conn.handler("vnc/command/type", isolated=True,
              meta={"label": "Type text into the remote desktop over RFB keysyms"})
def vnc_type(text: str = "", enter: bool = False, target: str = "") -> dict[str, Any]:
    if not text and not enter:
        return urirun.fail("text is required", connector=CONNECTOR_ID)
    try:
        return _ok(action="type", **_vnc_surface().type_text(text, enter=enter, target=target))
    except Exception as exc:  # noqa: BLE001
        return _fail_from("vnc-type", exc)


@conn.handler("vnc/command/key", isolated=True,
              meta={"label": "Press a key chord (e.g. ctrl-alt-t) over RFB"})
def vnc_key(combo: str = "", target: str = "") -> dict[str, Any]:
    if not combo:
        return urirun.fail("combo is required", connector=CONNECTOR_ID)
    try:
        return _ok(action="key", **_vnc_surface().key_combo(combo, target=target))
    except Exception as exc:  # noqa: BLE001
        return _fail_from("vnc-key", exc)


# --------------------------------------------------------------------------- #
# desktop apps — launch / list the way the system app search does (XDG/open/start)
# --------------------------------------------------------------------------- #
@conn.handler("app://host/desktop/command/launch", isolated=True,
              meta={"label": "Launch a desktop app (XDG .desktop / open / startfile)"})
def launch(app: str = "", compose: str = "", args: list | None = None, settle: float = 0) -> dict[str, Any]:
    """Launch a desktop app by resolving it the way the system app search does —
    XDG ``.desktop`` entries on Linux (covers Flatpak/Snap/PATH), ``open -a`` on macOS,
    ``startfile`` on Windows. ``compose`` appends Thunderbird's ``-compose`` draft;
    ``settle`` seconds (<=30) are slept so a follow-up capture/keypress sees the window."""
    if not app:
        return urirun.fail("app is required", connector=CONNECTOR_ID)
    try:
        res = B.dispatch("launch", app=app, compose=compose, args=args or [], settle=settle)
        out = _ok(action="launch", **res)
        # reversibility contract: launch ⟂ kill(pid). Returning the concrete inverse lets the host
        # transition ledger undo this launch (close the app we opened) — create⟂delete, no CDP.
        pid = res.get("pid")
        if pid:
            node = os.environ.get("URIRUN_NODE_NAME", "host")
            out["inverse"] = {"uri": f"kvm://{node}/proc/command/kill", "args": {"pid": int(pid)}}
        return out
    except B.BackendError as exc:
        return _fail_from("launch", exc)


@conn.handler("app://host/desktop/query/list", isolated=True,
              meta={"label": "List launchable desktop apps"})
def list_apps(filter: str = "") -> dict[str, Any]:  # noqa: A002 - route input field name
    """The action space for launch: installed apps (id + name), so an LLM picks by name
    instead of guessing a PATH binary."""
    try:
        return _ok(action="list_apps", **B.dispatch("launch_list", filter=filter))
    except B.BackendError as exc:
        return _fail_from("list_apps", exc)



# Join the route contracts (output shape + golden examples) onto the live bindings BY ROUTE KEY, so
# conn.bindings()/the manifest carry the output model an LLM planner needs to chain steps and to know
# a result may come back degraded. Enrichment only: in a flat-file push the toolkit/contracts may be
# absent, so a missing import must never break core import (mirrors the _backend_need fallback above).
try:
    from urirun_connectors_toolkit.contract_gate import attach_contracts as _attach_contracts
    from urirun_connector_kvm.contracts import CONTRACTS as _CONTRACTS

    _attach_contracts(conn, _CONTRACTS)
except Exception:  # noqa: BLE001 - contracts are an enrichment, never a hard dependency
    pass


def urirun_bindings() -> dict[str, Any]:
    """Serializable v2 bindings for this connector (entry point: urirun.bindings)."""
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    m = conn.manifest(_urirun_compat.load_manifest(__package__))
    try:  # GENERATED per-URI capability list (URI_COMMAND_STANDARD.md §6)
        from urirun_connectors_toolkit.connector_sdk import manifest_routes
        m["routes"] = manifest_routes(urirun_bindings())
    except Exception:  # noqa: BLE001 - enrichment; never break the manifest
        pass
    return m


def main(argv: list[str] | None = None) -> int:
    return conn.cli(argv, manifest_prose=_urirun_compat.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())
