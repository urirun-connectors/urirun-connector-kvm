# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# Direct RFB (VNC) surface — the reliable way to drive a noVNC-hosted desktop. Instead of
# synthesizing browser events against the scaled noVNC <canvas> (lossy for OCR, fragile for
# focus/keymaps), this speaks the RFB protocol to the VNC server itself: capture reads the
# NATIVE framebuffer (pixel-perfect, so OCR locate actually works) and input injects RFB
# pointer/key events at exact remote coordinates — no canvas-scale mapping, no keyboard-focus
# races. Requires the [vnc] extra (vncdotool). The web noVNC client and this surface can be
# used side by side: a human watches through noVNC while URIs act through RFB.
#
# PROCESS MODEL: vncdotool runs a twisted reactor in a NON-daemon thread; a process that
# connected must call api.shutdown() or it never exits (proven: handler processes hung
# forever). Reactors don't restart, so the rule is ONE ``session()`` PER PROCESS — which is
# exactly what isolated=True route handlers get (subprocess per call). Batch every RFB op
# of one handler inside one ``with session(...)`` block using the client-level helpers.
#
# Target resolution order: explicit ``target=`` on the route, else URIRUN_KVM_VNC
# (vncdotool syntax: 'host::5900' for a raw port, 'host:1' for display :1).
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


class VncError(RuntimeError):
    pass


def resolve_target(target: str = "") -> str:
    t = (target or os.environ.get("URIRUN_KVM_VNC", "")).strip()
    if not t:
        raise VncError("no VNC target: pass target='host::5900' or set URIRUN_KVM_VNC")
    return t


@contextmanager
def session(target: str = "", password: str | None = None, timeout: float = 12) -> Iterator[Any]:
    """One RFB session per process (see PROCESS MODEL above): connect, yield the client,
    then disconnect AND stop the reactor so the process can exit."""
    try:
        from vncdotool import api
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise VncError("vncdotool not installed — pip install 'urirun-connector-kvm[vnc]'") from exc
    client = api.connect(resolve_target(target), password=password, timeout=timeout)
    try:
        yield client
    finally:
        for step in (client.disconnect, api.shutdown):
            try:
                step()
            except Exception:  # noqa: BLE001 - teardown is best-effort, exit must proceed
                pass


# ---- client-level ops: compose several inside ONE session ----------------------------

def grab(client: Any, out: str = "") -> dict:
    """Native-resolution framebuffer grab. Returned size doubles as the coordinate
    space for click/move (RFB coords == image px, always)."""
    out = out or os.path.join(_shots_dir(), "vnc_capture.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    client.captureScreen(out)
    w = h = None
    try:
        from PIL import Image
        with Image.open(out) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001 - Pillow optional; size is informative only
        pass
    return {"path": out, "width": w, "height": h, "via": "rfb", "coord_space": "framebuffer-px"}


def click_at(client: Any, x: int, y: int, button: int = 1, double: bool = False) -> dict:
    """Pointer press+release at EXACT framebuffer coords (1=left 2=middle 3=right)."""
    client.mouseMove(int(x), int(y))
    client.mousePress(int(button))
    if double:
        client.pause(0.08)
        client.mousePress(int(button))
    return {"clicked": [int(x), int(y)], "button": int(button), "double": bool(double), "via": "rfb"}


_CHAR_KEYS = {" ": "space", "\n": "enter", "\t": "tab"}


def type_on(client: Any, text: str, enter: bool = False) -> dict:
    """Type text char-by-char as RFB key events (keysyms carry case/symbols natively —
    no host keyboard-layout dependency, unlike ydotool/xdotool)."""
    for ch in text:
        client.keyPress(_CHAR_KEYS.get(ch, ch))
    if enter:
        client.keyPress("enter")
    return {"typed": len(text), "enter": bool(enter), "via": "rfb"}


def combo_on(client: Any, combo: str) -> dict:
    """Press a chord like 'ctrl-alt-t', 'alt-F2', 'enter': modifiers held, final key
    pressed, modifiers released in reverse order."""
    # vncdotool's named-key map is lowercase.  Route callers commonly use X11
    # spellings such as ``Return`` or ``CTRL+L``; passing those through makes
    # vncdotool fall back to ``ord(value)`` and raises for multi-character
    # names.  Chord notation is case-insensitive, unlike literal text typing.
    parts = [
        part.strip().lower()
        for part in combo.replace("+", "-").split("-")
        if part.strip()
    ]
    if not parts:
        raise VncError("empty key combo")
    mods, last = parts[:-1], parts[-1]
    for m in mods:
        client.keyDown(m)
    client.keyPress(_CHAR_KEYS.get(last, last))
    for m in reversed(mods):
        client.keyUp(m)
    return {"combo": combo, "via": "rfb"}


# ---- single-op conveniences (each opens THE process's one session) --------------------

def capture(target: str = "", out: str = "", password: str | None = None) -> dict:
    with session(target, password) as c:
        return grab(c, out)


def click(x: int, y: int, button: int = 1, double: bool = False,
          target: str = "", password: str | None = None) -> dict:
    with session(target, password) as c:
        return click_at(c, x, y, button=button, double=double)


def move(x: int, y: int, target: str = "", password: str | None = None) -> dict:
    with session(target, password) as c:
        c.mouseMove(int(x), int(y))
    return {"moved": [int(x), int(y)], "via": "rfb"}


def type_text(text: str, enter: bool = False, target: str = "", password: str | None = None) -> dict:
    with session(target, password) as c:
        return type_on(c, text, enter=enter)


def key_combo(combo: str, target: str = "", password: str | None = None) -> dict:
    with session(target, password) as c:
        return combo_on(c, combo)


def _shots_dir() -> str:
    base = os.environ.get("URIRUN_ARTIFACTS_DIR",
                          os.path.join(os.path.expanduser("~"), ".urirun", "artifacts"))
    return os.path.join(base, "screenshots")
