# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

from __future__ import annotations

import json

import pytest
import urirun
from urirun import v2
from urirun_connector_kvm import (
    capture,
    connector_manifest,
    doctor,
    key,
    main,
    type_text,
    urirun_bindings,
)
import urirun_connector_kvm.backends as B
import urirun_connector_kvm.core as core

ROUTE_KEY = "kvm://host/input/command/key"
ROUTE_MOVE = "kvm://host/input/command/move"
ROUTE_CAPTURE = "kvm://host/screen/query/capture"
ROUTE_TYPE = "kvm://host/input/command/type"
ROUTE_DOCTOR = "kvm://host/doctor/query/report"
EXPECTED_ROUTES = {
    ROUTE_KEY, ROUTE_MOVE, ROUTE_CAPTURE, ROUTE_TYPE, ROUTE_DOCTOR,
    "kvm://host/input/command/click", "kvm://host/input/command/scroll",
    "kvm://host/input/command/double-click", "kvm://host/input/command/triple-click",
    "kvm://host/input/command/right-click", "kvm://host/input/command/middle-click",
    "kvm://host/input/command/hover", "kvm://host/input/command/drag-and-drop",
    "kvm://host/input/command/wait", "kvm://host/proc/command/kill",
    "kvm://host/task/command/run", "kvm://host/window/command/focus",
    "kvm://host/window/command/maximize",
    "kvm://host/window/command/close", "kvm://host/window/command/restore",
    "kvm://host/window/query/list", "kvm://host/a11y/command/act", "kvm://host/abs/command/click",
    "kvm://host/ui/query/locate", "kvm://host/ui/command/click-text",
    "kvm://host/ui/query/find", "kvm://host/ui/command/click", "kvm://host/ui/command/fill",
    "kvm://host/ui/query/wait", "kvm://host/ui/query/verify", "kvm://host/ui/query/strategies",
    "kvm://host/ui/command/type-verified",
    "kvm://host/cdp/session/command/ensure", "kvm://host/cdp/session/query/status",
    "kvm://host/cdp/session/query/ready",
    "kvm://host/cdp/page/command/navigate", "kvm://host/cdp/page/query/ready",
    "kvm://host/cdp/page/query/eval", "kvm://host/cdp/page/command/dom-fill",
    "kvm://host/cdp/page/command/dom-click",
    "kvm://host/ui/command/act", "kvm://host/env/query/profile", "kvm://host/surface/query/current", "kvm://host/display/query/info",
    "kvm://host/ready/query/resolve",
    "kvm://host/browser/query/sessions",
    "kvm://host/vnc/query/status", "kvm://host/vnc/query/capture", "kvm://host/vnc/query/find",
    "kvm://host/vnc/command/click", "kvm://host/vnc/command/type", "kvm://host/vnc/command/key",
    "app://host/desktop/command/launch", "app://host/desktop/query/list",
}


def test_screen_wh_reads_capture_dimensions_without_pillow(monkeypatch, tmp_path) -> None:
    from urirun_connector_kvm import _backends_uinput as U

    cache = tmp_path / "screen-wh"
    monkeypatch.setattr(U, "_SCREEN_WH_CACHE", str(cache))
    monkeypatch.setattr(U.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("URIRUN_KVM_SCREEN", raising=False)

    def capture(_action, *, output, **_kwargs):
        # Enough of a PNG for the stdlib IHDR reader; no Pillow dependency.
        header = b"\x89PNG\r\n\x1a\n" + U._struct.pack(">I", 13) + b"IHDR"
        header += U._struct.pack(">II", 1920, 1080)
        with open(output, "wb") as stream:
            stream.write(header)
        return {"path": output}

    monkeypatch.setattr(B, "dispatch", capture)

    assert U._screen_wh() == (1920, 1080)
    assert cache.read_text() == "1920x1080"


def test_uinput_abs_click_refuses_unknown_screen_geometry(monkeypatch) -> None:
    from urirun_connector_kvm import _backends_uinput as U

    monkeypatch.setattr(U, "uinput_available", lambda: True)
    monkeypatch.setattr(U, "_screen_wh", lambda: (0, 0))

    with pytest.raises(B.BackendError, match="screen geometry unavailable"):
        U.uinput_abs_click(700, 800, 0, 0, do_click=False)


def test_warm_capture_rejects_frame_from_wrong_pipewire_source() -> None:
    meta = {"srcSize": [1920, 1080]}

    assert B._warm_dimensions_valid(meta, (1920, 1080)) is True
    assert B._warm_dimensions_valid(meta, (960, 540), max_width=960) is True
    assert B._warm_dimensions_valid(meta, (640, 480)) is False
    assert B._warm_dimensions_valid(meta, None) is False


def test_key_requires_value() -> None:
    assert key("")["ok"] is False


def test_type_requires_value() -> None:
    assert type_text("")["ok"] is False


def test_no_backend_reports_install_hint(monkeypatch) -> None:
    # With no tools/modules present, dispatch must fail cleanly with an install hint,
    # never raise — so a route stays usable to report what is missing.
    monkeypatch.setattr(B.shutil, "which", lambda _t: None)
    monkeypatch.setattr(B, "have_mod", lambda _m: False)
    monkeypatch.setattr(B.os, "access", lambda *_a, **_k: False)  # no writable /dev/uinput either
    r = key("Return")
    assert r["ok"] is False and "no available backend" in r["error"]


# --- decorator-registered backend registry -------------------------------- #
def test_backend_decorator_registers_and_sorts() -> None:
    calls = []

    @B.backend("unit_probe", "low", priority=10)
    def _low(**_):
        calls.append("low"); return {"hit": "low"}

    @B.backend("unit_probe", "high", priority=90)
    def _high(**_):
        calls.append("high"); return {"hit": "high"}

    # highest priority + available wins
    assert B.dispatch("unit_probe")["hit"] == "high"
    assert calls == ["high"]


def test_dispatch_falls_through_on_failure() -> None:
    @B.backend("unit_fall", "broken", priority=90)
    def _broken(**_):
        raise RuntimeError("boom")

    @B.backend("unit_fall", "works", priority=10)
    def _works(**_):
        return {"ok": True}

    out = B.dispatch("unit_fall")
    assert out["backend"] == "works"


def test_unavailable_backend_skipped_by_needs(monkeypatch) -> None:
    @B.backend("unit_need", "needs_ghost", priority=90, needs_bin=("definitely-not-a-real-binary",))
    def _ghost(**_):
        return {"hit": "ghost"}

    @B.backend("unit_need", "fallback", priority=10)
    def _fb(**_):
        return {"hit": "fallback"}

    assert B.dispatch("unit_need")["hit"] == "fallback"


def test_bindings_are_isolated_handlers() -> None:
    b = urirun_bindings()["bindings"]
    assert set(b) == EXPECTED_ROUTES
    binding = b[ROUTE_CAPTURE]
    # capture is deliberately IN-PROCESS (Tier 3 of PERFORMANCE-REFACTOR): read-only,
    # heavy work delegated to the warm capture-worker subprocess, and the isolated
    # spawn was ~600 ms of the ~730 ms hot perception path on a node.
    assert binding["adapter"] == "local-function"
    assert binding["python"]["module"] == "urirun_connector_kvm.core"
    assert binding["python"]["export"] == "capture"
    assert "argv" not in binding
    # input stays ISOLATED: it touches uinput/ydotool and must not take the node down.
    assert b["kvm://host/input/command/key"]["adapter"] == "local-function-subprocess"
    domains = (binding.get("meta") or {}).get("contract", {}).get("domains") or {}
    assert domains["monitor"]["domain"] == "env:monitors.id"
    json.dumps(urirun_bindings())  # serializable: no live ref leaks


def test_runtime_executes_from_compiled_registry(monkeypatch) -> None:
    # A serialized->compiled registry still runs the route end-to-end. Runs out-of-process
    # (urirun.exec); env propagates, so clear PATH + force have_mod False in the child via
    # an env flag the backends module honours is overkill -- instead assert plumbing ok.
    monkeypatch.setenv("PATH", "")
    registry = urirun.compile_registry(json.loads(json.dumps(urirun_bindings())))
    env = v2.run(
        ROUTE_DOCTOR,
        registry,
        payload={},
        mode="execute",
        policy=urirun.policy(allow=["kvm://*"]),
    )
    assert env["ok"] is True
    data = urirun.result_data(env)
    assert data["ok"] is True and "backends" in data


def test_doctor_reports_backends() -> None:
    r = doctor()
    assert r["ok"] is True
    assert "capture" in r["backends"] and "type" in r["backends"]
    # every registered backend entry carries availability + install hints
    for entry in r["backends"]["capture"]:
        assert set(entry) >= {"name", "priority", "available", "missing"}


def test_compositor_tag_classifies_known_desktops(monkeypatch) -> None:
    # XDG_CURRENT_DESKTOP is the ground truth; map the common compositors to their tag.
    cases = {
        "ubuntu:GNOME": "mutter",
        "GNOME": "mutter",
        "KDE": "kwin",
        "sway": "wlroots",
        "Hyprland": "wlroots",
        "wayfire": "wlroots",
        "weston": "weston",
        "XFCE": "other",      # X11 desktop, not a wlroots family
    }
    for desktop, expected in cases.items():
        monkeypatch.setattr(B, "session_env", lambda **_: {})
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
        assert B.compositor_tag() == expected, f"{desktop} -> expected {expected}"


def test_grim_reports_unavailable_on_mutter_via_doctor(monkeypatch) -> None:
    """The regression: grim needs wlr-screencopy-unstable-v1, which GNOME/Mutter does not ship.
    ``doctor`` must NOT advertise grim as available there, even when the binary is installed and
    the session is wayland — otherwise capture falls through to grim and fails at runtime."""
    monkeypatch.setattr(B, "session_env", lambda **_: {})
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(B, "is_wayland", lambda: True)        # platform linux-wayland passes
    monkeypatch.setattr(B, "have_bin", lambda name: True)     # grim IS installed
    grim = next(b for b in B._REGISTRY["capture"] if b.name == "grim")
    assert grim.available() is False
    assert grim.needs_compositor == ("wlroots",)
    # surfaced through the doctor route too, not just the Backend object
    r = doctor()
    grim_entry = next(e for e in r["backends"]["capture"] if e["name"] == "grim")
    assert grim_entry["available"] is False


def test_grim_remains_available_on_wlroots(monkeypatch) -> None:
    """The flip side: on a wlroots compositor (Sway/Hyprland) where grim actually works, the
    compositor gate must NOT hide it. The needs_compositor fix is a tightener, not a blanket off."""
    monkeypatch.setattr(B, "session_env", lambda **_: {})
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    monkeypatch.setattr(B, "is_wayland", lambda: True)
    monkeypatch.setattr(B, "have_bin", lambda name: True)
    grim = next(b for b in B._REGISTRY["capture"] if b.name == "grim")
    assert grim.available() is True


def test_mutter_capture_backend_not_gated_by_wlroots_requirement(monkeypatch) -> None:
    """The compositor gate must be targeted: GNOME's native ``mutter`` capture backend works ON
    GNOME/Mutter, so it must stay available there — only grim (wlroots-only) is hidden."""
    monkeypatch.setattr(B, "session_env", lambda **_: {})
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(B, "is_wayland", lambda: True)
    monkeypatch.setattr(B, "have_bin", lambda name: True)
    mutter = next((b for b in B._REGISTRY["capture"] if b.name == "mutter"), None)
    assert mutter is not None, "mutter capture backend must exist as the GNOME fallback"
    assert mutter.available() is True
    assert mutter.needs_compositor == ()


def test_grim_unavailable_on_kwin_not_just_mutter(monkeypatch) -> None:
    """KDE/KWin is the other major non-wlroots compositor.grim must also be hidden there; the
    native portal/mutter backends stay available so capture still has a path."""
    monkeypatch.setattr(B, "session_env", lambda **_: {})
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(B, "is_wayland", lambda: True)
    monkeypatch.setattr(B, "have_bin", lambda name: True)
    grim = next(b for b in B._REGISTRY["capture"] if b.name == "grim")
    assert grim.available() is False


def test_capture_tags_screenshot_as_frozen_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: {"backend": "stub", "via": "stub", "path": kw.get("output")},
    )
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 123)
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)
    r = capture(output="/tmp/x.png")
    assert r["ok"] is True and r["kind"] == "screenshot" and r["live"] is False


def test_capture_all_scope_reaches_backend_and_result(monkeypatch) -> None:
    seen = {}

    def _dispatch(action, **kw):
        seen.update(kw)
        return {"backend": "stub", "via": "stub", "path": kw.get("output"),
                "scope": "all-monitors", "monitors": [{"index": 1}, {"index": 2}],
                "bbox": [0, 0, 200, 100], "width": 200, "height": 100}

    monkeypatch.setattr(B, "dispatch", _dispatch)
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 123)
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)

    r = capture(output="/tmp/x.png", monitor=-1, scope="all")

    assert seen["scope"] == "all" and seen["monitor"] == -1
    assert r["ok"] is True
    assert r["scope"] == "all-monitors"
    assert r["monitors"] == [{"index": 1}, {"index": 2}]
    assert r["bbox"] == [0, 0, 200, 100]
    assert r["width"] == 200 and r["height"] == 100


def test_capture_single_monitor_connector_does_not_collide_with_connector_id(monkeypatch) -> None:
    # Regression: capturing a specific monitor made the backend return connector="DP-1"
    # (the monitor OUTPUT name); copying it under "connector" collided with the urirun
    # connector id that _ok() injects -> "urirun.ok() got multiple values for keyword
    # argument 'connector'". The output name must land under outputConnector; connector
    # stays "kvm" per the capture contract's golden examples.
    def _dispatch(action, **kw):
        return {"backend": "grim", "via": "grim", "path": kw.get("output"),
                "scope": "monitor", "connector": "DP-1", "monitor": 3,
                "monitors": [{"index": 3, "connector": "DP-1"}]}

    monkeypatch.setattr(B, "dispatch", _dispatch)
    monkeypatch.setattr(B, "_gnome_monitors", lambda: [{"index": 3, "connector": "DP-1"}])
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 204931)
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)

    r = capture(output="/tmp/m3.png", monitor=3)

    assert r["ok"] is True                         # no TypeError
    assert r["connector"] == "kvm"                 # contract: urirun connector id
    assert r["outputConnector"] == "DP-1"          # the captured monitor's output name preserved
    assert r["monitor"] == 3


def test_capture_single_monitor_narrows_bbox_to_that_monitor(monkeypatch) -> None:
    # A single-monitor capture must report THAT monitor's logical rect as bbox, not the full
    # virtual-desktop union the backend emits regardless of scope.
    monitors = [
        {"index": 1, "connector": "HDMI-1", "x": 0, "y": 1609, "logicalWidth": 2048, "logicalHeight": 1280},
        {"index": 3, "connector": "DP-1", "x": 0, "y": 329, "logicalWidth": 2048, "logicalHeight": 1280},
    ]

    def _dispatch(action, **kw):
        return {"backend": "mutter", "via": "mutter-screencast", "path": kw.get("output"),
                "scope": "monitor", "connector": "DP-1", "monitor": 3, "monitors": monitors,
                "bbox": [0, 0, 5888, 2889], "width": 2560, "height": 1600}

    monkeypatch.setattr(B, "dispatch", _dispatch)
    monkeypatch.setattr(B, "_gnome_monitors", lambda: monitors)
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 204931)
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)

    r = capture(output="/tmp/m3.png", monitor=3)

    assert r["ok"] is True
    assert r["scope"] == "monitor" and r["monitor"] == 3
    assert r["bbox"] == [0, 329, 2048, 1280]       # DP-1's region, not [0, 0, 5888, 2889]


def test_capture_rejects_requested_monitor_outside_backend_inventory(monkeypatch) -> None:
    # Regression: the Mutter backend used to fall back to the primary monitor when a requested
    # monitor index was outside the live inventory, producing ok:true with the wrong screenshot.
    def _dispatch(action, **kw):
        return {"backend": "mutter", "via": "mutter-screencast", "path": kw.get("output"),
                "scope": "monitor", "connector": "HDMI-1", "monitor": 3,
                "monitors": [{"index": 1, "connector": "HDMI-1"},
                             {"index": 2, "connector": "DP-2"}],
                "bbox": [0, 0, 4864, 2160], "width": 1024, "height": 768}

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = capture(output="/tmp/m3.png", monitor=3)

    assert r["ok"] is False
    assert "monitor 3 not available" in r["error"]


def test_window_list_passes_app_title_selector(monkeypatch) -> None:
    seen = {}

    def _dispatch(action, **kw):
        seen["action"] = action
        seen.update(kw)
        return {"backend": "stub", "via": "stub",
                "windows": [{"app": "Google Chrome", "title": "LinkedIn", "monitor": 2}],
                "selected": {"app": "Google Chrome", "title": "LinkedIn", "monitor": 2}}

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.window_list(app="chrome", title="linkedin")

    assert r["ok"] is True
    assert seen["action"] == "window_list"
    assert seen["app"] == "chrome"
    assert seen["title"] == "linkedin"
    assert r["selected"]["monitor"] == 2


def _fake_surface(title: str):
    class _Surface:
        @staticmethod
        def _active_window():
            return {"title": title, "via": "xdotool"} if title else {}
    return _Surface


def test_type_text_refuses_wrong_window(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Mail Client — Inbox"))
    monkeypatch.setattr(B, "dispatch", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not type when the wrong window is focused")))

    r = core.type_text(text="hello", expect_window="Signal")

    assert r["ok"] is False
    assert r["focus"]["verified"] is False
    assert r["focus"]["title"] == "Mail Client — Inbox"


def test_type_text_proceeds_when_window_matches(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "dispatch", lambda action, **kw: {"via": "ydotool"})

    r = core.type_text(text="hello", expect_window="Signal")

    assert r["ok"] is True
    assert r["focus"]["verified"] is True


def test_type_text_stays_blind_without_expect_window(monkeypatch) -> None:
    seen = {}

    def _dispatch(action, **kw):
        seen["called"] = True
        return {"via": "ydotool"}

    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface(""))
    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.type_text(text="hello")

    assert r["ok"] is True
    assert "focus" not in r
    assert seen["called"] is True


def _dispatch_no_geometry(action, **kw):
    """Stand-in B.dispatch for tests exercising the window-IDENTITY guard only: the
    window_geometry/maximize backend is unavailable (BackendError), so the fullscreen guard
    stays honestly untrusted and never interferes."""
    if action in ("window_geometry", "maximize"):
        raise B.BackendError("geometry probe unavailable in test")
    return {"via": "wmctrl" if action == "focus" else "ydotool"}


def test_task_run_refuses_type_after_focus_lands_elsewhere(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Wrong App"))
    monkeypatch.setattr(B, "dispatch", _dispatch_no_geometry)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal"},
        {"op": "type", "text": "should not land"},
    ])

    assert r["ok"] is False
    assert r["steps"][-1]["op"] == "type"
    assert r["steps"][-1]["ok"] is False
    assert r["steps"][-1]["focus"]["title"] == "Wrong App"


def test_task_run_types_when_focused_window_matches(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "dispatch", _dispatch_no_geometry)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal"},
        {"op": "type", "text": "ok to land"},
    ])

    assert r["ok"] is True
    assert [s["op"] for s in r["steps"]] == ["focus", "type"]


def test_task_run_untrusted_probe_does_not_block_type(monkeypatch) -> None:
    # Pure-Wayland sessions where the active window can't be probed at all: honestly
    # unverifiable, not a mismatch, so the step still proceeds best-effort.
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface(""))
    monkeypatch.setattr(B, "dispatch", _dispatch_no_geometry)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal"},
        {"op": "type", "text": "best effort"},
    ])

    assert r["ok"] is True


def test_task_run_focus_step_maximizes_a_small_window(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))
    state = {"small": True}

    def _dispatch(action, **kw):
        if action == "maximize":
            state["small"] = False
            return {"via": "wmctrl"}
        if action == "window_geometry":
            geo = {"x": 100, "y": 100, "w": 800, "h": 600} if state["small"] \
                else {"x": 0, "y": 0, "w": 1920, "h": 1080}
            return {"title": "Signal", **geo}
        return {"via": "wmctrl" if action == "focus" else "ydotool"}

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal"},
        {"op": "type", "text": "hello"},
    ])

    assert r["ok"] is True
    assert r["steps"][0]["fullscreen"]["fullscreen"] is True
    assert state["small"] is False  # the guard's maximize call actually ran


def test_task_run_refuses_action_when_window_never_fills_screen(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))

    def _dispatch(action, **kw):
        if action == "window_geometry":
            return {"title": "Signal", "x": 100, "y": 100, "w": 800, "h": 600}
        return {"via": "wmctrl" if action == "focus" else "ydotool"}

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal"},
        {"op": "type", "text": "should not land"},
    ])

    assert r["ok"] is False
    assert r["steps"][-1]["op"] == "type"
    assert r["steps"][-1]["fullscreen"]["fullscreen"] is False


def test_task_run_fullscreen_false_opts_out(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))

    def _dispatch(action, **kw):
        if action == "maximize":
            raise AssertionError("must not attempt to maximize when fullscreen=False")
        if action == "window_geometry":
            return {"title": "Signal", "x": 100, "y": 100, "w": 800, "h": 600}
        return {"via": "wmctrl" if action == "focus" else "ydotool"}

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.task_run(steps=[
        {"op": "focus", "title": "Signal", "fullscreen": False},
        {"op": "type", "text": "small dialog ok"},
    ])

    assert r["ok"] is True


def test_window_maximize_route(monkeypatch) -> None:
    seen = {}

    def _dispatch(action, **kw):
        seen[action] = kw
        if action == "window_geometry":
            return {"title": "Signal", "x": 0, "y": 0, "w": 1920, "h": 1080}
        return {"via": "wmctrl"}

    monkeypatch.setattr(B, "dispatch", _dispatch)
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))

    r = core.window_maximize(title="Signal")

    assert r["ok"] is True
    assert seen["maximize"]["title"] == "Signal"
    assert r["verify"]["fullscreen"] is True


def test_type_text_refuses_when_window_not_fullscreen(monkeypatch) -> None:
    monkeypatch.setattr(core, "_surface_mod", lambda: _fake_surface("Signal"))
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))

    def _dispatch(action, **kw):
        if action == "window_geometry":
            return {"title": "Signal", "x": 100, "y": 100, "w": 800, "h": 600}
        raise AssertionError("must not type when not fullscreen")

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.type_text(text="hello", expect_window="Signal", require_fullscreen=True)

    assert r["ok"] is False
    assert r["fullscreen"]["fullscreen"] is False


def test_atspi_window_list_uses_long_timeout_and_monitor_inventory(monkeypatch) -> None:
    seen = {}

    class Proc:
        stdout = json.dumps({
            "windows": [{
                "app": "Google Chrome",
                "title": "Chrome",
                "role": "frame",
                "bbox": [2048, 0, 1200, 900],
            }]
        })

    monkeypatch.setattr(B, "_atspi_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(B, "session_env", lambda: {"DISPLAY": ":0"})
    monkeypatch.setattr(B, "_gnome_monitors", lambda: [
        {"index": 1, "connector": "HDMI-1", "x": 0, "y": 1609, "logicalWidth": 2048, "logicalHeight": 1280},
        {"index": 2, "connector": "DP-2", "x": 2048, "y": 0, "logicalWidth": 3840, "logicalHeight": 2160},
    ])

    def _run(argv, *, env=None, timeout=30):
        seen["argv"] = argv
        seen["env"] = env
        seen["timeout"] = timeout
        return Proc()

    monkeypatch.setattr(B, "_run", _run)

    r = B._winlist_atspi(app="chrome")

    assert seen["timeout"] == 25
    assert json.loads(seen["argv"][-1]) == {"app": "chrome", "title": ""}
    assert r["selected"]["monitor"] == 2
    assert r["selected"]["monitorConnector"] == "DP-2"


def test_monitor_for_bbox_uses_logical_monitor_geometry() -> None:
    monitors = [
        {"index": 1, "connector": "HDMI-1", "x": 0, "y": 1609, "logicalWidth": 2048, "logicalHeight": 1280},
        {"index": 2, "connector": "DP-2", "x": 2048, "y": 0, "logicalWidth": 3840, "logicalHeight": 2160},
        {"index": 3, "connector": "DP-1", "x": 0, "y": 329, "logicalWidth": 2048, "logicalHeight": 1280},
    ]

    assert B._monitor_for_bbox([2600, 100, 1200, 900], monitors)["index"] == 2
    assert B._monitor_for_bbox([200, 500, 1000, 800], monitors)["index"] == 3


def test_monitor_for_bbox_handles_atspi_local_origin_on_top_monitor() -> None:
    monitors = [
        {"index": 1, "connector": "HDMI-1", "x": 0, "y": 1609, "logicalWidth": 2048, "logicalHeight": 1280},
        {"index": 2, "connector": "DP-2", "x": 2048, "y": 0, "logicalWidth": 3840, "logicalHeight": 2160},
        {"index": 3, "connector": "DP-1", "x": 0, "y": 329, "logicalWidth": 2048, "logicalHeight": 1280},
    ]

    # Real GNOME/Wayland trace: AT-SPI reported Chrome as [0,0,2113,1592].
    # Treat that as monitor-local top-origin and choose the 4K top monitor,
    # instead of selecting DP-1 by accidental overlap.
    assert B._monitor_for_bbox([0, 0, 2113, 1592], monitors)["index"] == 2


def test_attest_window_monitor_flags_oversized_window_label() -> None:
    # The DP-2/DP-1 data-bug class: a 4K Chrome frame mislabelled onto a smaller monitor.
    # Independent size redundancy must flag it (a window cannot be larger than its display) —
    # downstream captured==selected would NOT, because the bad value propagates.
    dp1 = {"index": 3, "connector": "DP-1", "logicalWidth": 2048, "logicalHeight": 1280}
    dp2 = {"index": 2, "connector": "DP-2", "logicalWidth": 3840, "logicalHeight": 2160}
    bad = B._attest_window_monitor([2048, 0, 2113, 1592], dp1)
    assert bad["ok"] is False and "does NOT fit" in bad["detail"]
    good = B._attest_window_monitor([2048, 0, 2113, 1592], dp2)
    assert good["ok"] is True


def test_capture_xdg_portal_placeholder_is_degraded_not_false_success(monkeypatch) -> None:
    """A tiny xdg-portal capture (~3.8 KB) is the empty/blocked-portal placeholder, not a real
    screenshot. It must come back degraded — never a false ok — so the flow's degraded handling keeps
    it out of known-good and the twin shows 'not a real capture' instead of logging a fake success."""
    monkeypatch.setattr(core, "_cdp", None)  # no CDP fallback here — exercise the degraded guard itself
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: {"backend": "portal", "via": "xdg-portal", "path": kw.get("output")},
    )
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 3848)  # the empty-portal placeholder size
    r = capture(output="/tmp/x.png")
    assert r.get("degraded") is True and "placeholder" in (r.get("degradedReason") or "")

    # a real-sized xdg-portal frame is a genuine success, not degraded
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 200_000)
    r2 = capture(output="/tmp/x.png")
    assert r2["ok"] is True and not r2.get("degraded")

    # the guard is scoped to xdg-portal — a small grim frame must NOT be falsely degraded
    monkeypatch.setattr(B, "dispatch",
                        lambda action, **kw: {"backend": "grim", "via": "grim", "path": kw.get("output")})
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 5000)
    r3 = capture(output="/tmp/x.png")
    assert r3["ok"] is True and not r3.get("degraded")


def test_capture_falls_back_to_cdp_when_portal_blocked(monkeypatch) -> None:
    """On a GNOME-Wayland node the OS portal returns an empty placeholder, but a real Chrome is
    reachable on the debug port. capture() must then return the BROWSER page via CDP (real pixels,
    via='cdp') instead of a degraded placeholder — the meaningful content for web automation."""
    from types import SimpleNamespace
    import base64 as _b64
    import os
    import tempfile
    real_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80_000  # >= _MIN_REAL_SHOT_BYTES
    fake_cdp = SimpleNamespace(
        reachable=lambda: True,
        command=lambda method, params=None: {"data": _b64.b64encode(real_png).decode()},
    )
    monkeypatch.setattr(core, "_cdp", fake_cdp)
    # OS capture yields the empty-portal placeholder
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: {"backend": "portal", "via": "xdg-portal", "path": kw.get("output")},
    )
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 3848)
    out = os.path.join(tempfile.mkdtemp(), "shot.png")
    r = capture(output=out)
    assert r["ok"] is True and not r.get("degraded")
    assert r["via"] == "cdp" and r["scope"] == "browser-page"
    assert r["bytes"] >= core._MIN_REAL_SHOT_BYTES


def test_capture_browser_scope_prefers_cdp_before_monitor_backend(monkeypatch) -> None:
    """Browser-scoped screenshots should capture the active CDP page before trying an OS
    monitor. On multi-monitor Wayland the OS monitor can be a valid screenshot of the wrong screen."""
    from types import SimpleNamespace
    import base64 as _b64
    import os
    import tempfile

    real_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80_000
    fake_cdp = SimpleNamespace(
        reachable=lambda: True,
        command=lambda method, params=None: {"data": _b64.b64encode(real_png).decode()},
    )
    monkeypatch.setattr(core, "_cdp", fake_cdp)

    def _unexpected_os_capture(*_args, **_kwargs):
        raise AssertionError("browser-scoped capture should not call the OS monitor backend first")

    monkeypatch.setattr(B, "dispatch", _unexpected_os_capture)
    out = os.path.join(tempfile.mkdtemp(), "shot.png")
    r = capture(output=out, scope="browser", base64=True)

    assert r["ok"] is True
    assert r["via"] == "cdp"
    assert r["scope"] == "browser-page"
    assert r["pngBase64"]


def test_capture_browser_scope_falls_back_to_os_when_cdp_unreachable(monkeypatch) -> None:
    from types import SimpleNamespace

    fake_cdp = SimpleNamespace(reachable=lambda: False)
    monkeypatch.setattr(core, "_cdp", fake_cdp)
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: {"backend": "stub", "via": "stub", "path": kw.get("output")},
    )
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 123_456)
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)

    r = capture(output="/tmp/x.png", scope="browser")

    assert r["ok"] is True
    assert r["via"] == "stub"
    assert r.get("scope") is None


def test_manifest_prose_plus_derived_routes() -> None:
    m = connector_manifest()
    assert m["id"] == "kvm"
    # routes is now the GENERATED per-URI capability list (URI_COMMAND_STANDARD.md §6):
    # each entry self-describes class/verb/summary/mutates/errors.
    assert {r["uri"] for r in m["routes"]} == EXPECTED_ROUTES
    cap = next(r for r in m["routes"] if r["uri"] == ROUTE_CAPTURE)
    assert cap["class"] == "query" and cap["mutates"] is False and cap["summary"]
    typ = next(r for r in m["routes"] if r["uri"] == ROUTE_TYPE)
    assert typ["class"] == "command" and typ["mutates"] is True
    assert "INVALID_ARGUMENT" in cap["errors"]        # from the ONE error catalog
    assert m["uriSchemes"] == ["app", "kvm"]
    assert m["summary"]


def test_cli_bindings_and_manifest(capsys) -> None:
    assert main(["bindings"]) == 0
    assert ROUTE_KEY in json.loads(capsys.readouterr().out)["bindings"]
    assert main(["manifest"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "kvm"


# --------------------------------------------------------------------------- #
# universal control-tool router
# --------------------------------------------------------------------------- #
import urirun_connector_kvm.control as C  # noqa: E402
import urirun_connector_kvm.cdp as cdp  # noqa: E402


def test_router_prefers_cdp_when_reachable(monkeypatch) -> None:
    monkeypatch.setattr(cdp, "reachable", lambda: True)
    monkeypatch.setattr(cdp, "act", lambda op, text="", role="", name="", value="":
                        {"ok": True, "found": True, "clicked": True, "via": "cdp", "role": role})
    r = C.route("click", text="Post", role="button", app="google-chrome")
    assert r["ok"] is True and r["strategy"] == "cdp" and r["clicked"] is True


def test_router_falls_through_cdp_to_vision(monkeypatch) -> None:
    # cdp reachable but the element is not in the DOM -> raises -> vision takes over
    monkeypatch.setattr(cdp, "reachable", lambda: True)
    monkeypatch.setattr(C._env, "atspi_ready", lambda: False)
    monkeypatch.setattr(C._env, "profile", lambda: {"controlStrategies": {"vision": True}})

    def _boom(*a, **k):
        raise B.BackendError("cdp: element not found")

    monkeypatch.setattr(cdp, "act", _boom)
    monkeypatch.setattr(B, "dispatch", lambda action, **k:
                        {"source": "tesseract", "found": True, "center": [120, 240],
                         "matches": [{"conf": 88.0}]} if action == "locate" else {"via": "stub"})
    monkeypatch.setattr(B, "uinput_abs_click",
                        lambda x, y, *a, **k: {"via": "uinput-absolute", "pixel": [x, y]})
    r = C.route("click", text="Start a post", app="google-chrome")
    assert r["ok"] is True and r["strategy"] == "vision" and r["at"] == [120, 240]
    assert any(a.get("strategy") == "cdp" for a in r["attempts"])


def test_router_empty_target_is_error() -> None:
    r = C.route("click", text="", role="", name="")
    assert r["ok"] is False and "required" in r["error"]


def test_ui_act_retries_then_succeeds(monkeypatch) -> None:
    # orchestrator: page-ready probe + retry loop through the router until ok
    import urirun_connector_kvm.cdp as _cdp
    monkeypatch.setattr(_cdp, "reachable", lambda: True)
    monkeypatch.setattr(_cdp, "page_ready", lambda timeout=8.0: {"ok": True, "readyState": "complete"})
    calls = {"n": 0}
    def _route(op, **k):
        calls["n"] += 1
        if calls["n"] < 2:                       # first try misses, second wins
            return {"ok": False, "error": "not yet", "attempts": []}
        return {"ok": True, "strategy": "cdp", "clicked": True}
    monkeypatch.setattr(core.C, "route", _route)
    monkeypatch.setattr(core.time, "sleep", lambda *_a: None)
    r = core.ui_act(do="click", text="Compose", retries=3)
    assert r["ok"] is True and r["do"] == "click"
    assert len(r["tries"]) == 2 and r["tries"][-1]["ok"] is True


def test_ui_act_requires_postcondition_when_expect_is_given(monkeypatch) -> None:
    import urirun_connector_kvm.cdp as _cdp
    monkeypatch.setattr(_cdp, "reachable", lambda: False)

    def _route(op, **k):
        if op == "click":
            return {"ok": True, "strategy": "vision", "clicked": True,
                    "attempts": [{"strategy": "vision", "ok": True}]}
        if op == "locate" and k.get("text") == "Sent":
            return {"ok": True, "found": True, "strategy": "atspi",
                    "attempts": [{"strategy": "atspi", "ok": True}]}
        return {"ok": False, "error": "not found", "attempts": []}

    monkeypatch.setattr(core.C, "route", _route)
    monkeypatch.setattr(core.time, "sleep", lambda *_a: None)

    r = core.ui_act(do="click", text="Send", app="Signal", expect="Sent",
                    intent="send Signal message", safe=False)

    assert r["ok"] is True
    assert r["intent"] == "send Signal message"
    assert r["postcondition"]["verified"] is True
    assert r["tries"][-1]["verified"] is True


def test_ui_act_detects_blind_loop_and_returns_ticket_draft(monkeypatch) -> None:
    import urirun_connector_kvm.cdp as _cdp
    monkeypatch.setattr(_cdp, "reachable", lambda: False)

    def _route(op, **k):
        if op == "click":
            return {"ok": True, "strategy": "vision", "clicked": True,
                    "attempts": [{"strategy": "vision", "error": "compose not focused"}]}
        if op == "locate":
            return {"ok": False, "error": "expect not visible",
                    "attempts": [{"strategy": "atspi", "found": False}]}
        raise AssertionError(op)

    monkeypatch.setattr(core.C, "route", _route)
    monkeypatch.setattr(core.time, "sleep", lambda *_a: None)

    r = core.ui_act(do="click", text="Message", app="Signal", expect="OK, potwierdzam.",
                    retries=2, intent="reply in Signal", ticket="IFURI-039")

    assert r["ok"] is False
    assert r["stalled"] == "blind-loop"
    assert r["stall"]["repeatCount"] == 2
    assert r["ticket"] == "IFURI-039"
    assert r["ticketDraft"]["uri"] == "task://host/ticket/command/create"
    assert "Redefine stalled UI intent" in r["ticketDraft"]["payload"]["name"]
    assert "stop retrying" in r["redefine"]["next"][0]


def test_ui_act_wait_escalates_from_cheap_to_full_locate(monkeypatch) -> None:
    import urirun_connector_kvm.cdp as _cdp
    monkeypatch.setattr(_cdp, "reachable", lambda: False)

    seen_cheap: list[bool] = []

    def _route(op, **k):
        if op != "locate":
            raise AssertionError(op)
        seen_cheap.append(bool(k.get("cheap")))
        if len(seen_cheap) == 1:
            return {"ok": False, "found": False, "error": "not yet", "attempts": []}
        return {"ok": True, "found": True, "strategy": "vision", "attempts": []}

    monkeypatch.setattr(core.C, "route", _route)
    monkeypatch.setattr(core.time, "sleep", lambda *_a: None)

    r = core.ui_act(do="wait", text="LinkedIn", retries=3)

    assert r["ok"] is True
    assert seen_cheap == [True, False]


def test_click_text_scales_capture_point_to_input_space(monkeypatch, tmp_path) -> None:
    import pytest
    Image = pytest.importorskip("PIL.Image")
    shot = tmp_path / "shot.png"
    Image.new("RGB", (1280, 720), "white").save(shot)

    monkeypatch.setattr(core, "_capture_native", lambda monitor=0: str(shot))
    monkeypatch.setattr(B, "_screen_wh", lambda: (1920, 1080))
    monkeypatch.setattr(B, "uinput_available", lambda: True)
    clicked = {}

    def _dispatch(action, **kw):
        if action == "locate":
            return {"matches": [{"center": [640, 360], "box": [600, 340, 80, 40],
                                  "text": "Message", "conf": 91.0}]}
        raise AssertionError(action)

    def _abs_click(x, y, sw, sh, **kw):
        clicked.update({"x": x, "y": y, "sw": sw, "sh": sh, **kw})
        return {"via": "uinput-absolute", "pixel": [x, y]}

    monkeypatch.setattr(B, "dispatch", _dispatch)
    monkeypatch.setattr(B, "uinput_abs_click", _abs_click)

    r = core.ui_click_text(text="Message")

    assert r["ok"] is True
    assert r["clickedInput"] == [960, 540]
    assert r["coordinateMapping"]["image"] == [1280, 720]
    assert r["coordinateMapping"]["input"] == [1920, 1080]
    assert r["coordinateMapping"]["scale"] == [1.5, 1.5]
    assert clicked["x"] == 960 and clicked["y"] == 540


def test_click_abs_reports_honest_effect(monkeypatch) -> None:
    """IFURI-065 (runtime-lies-ok): abs/command/click must not claim an effect it
    hasn't confirmed. Three branches: pure move → effect True; bare click → effect
    None/unverified; click+expect → verified only when the text appears."""
    monkeypatch.setattr(B, "uinput_abs_click",
                        lambda x, y, sw, sh, button="left", do_click=True:
                        {"via": "uinput-absolute", "abs": [1, 2], "pixel": [x, y],
                         "clicked": bool(do_click), "clicks": 1 if do_click else 0})

    # pure positioning: the move IS the whole effect
    moved = core.click_abs(x=10, y=20, sw=100, sh=200, do_click=False)
    assert moved["ok"] is True and moved["effect"] is True

    # bare click: dispatched but unverified at this layer — must NOT claim effect
    bare = core.click_abs(x=10, y=20, sw=100, sh=200)
    assert bare["ok"] is True
    assert bare["effect"] is None and bare["verified"] is False
    assert "unverified" in bare["note"]

    # click + expect satisfied → verified True / effect True
    monkeypatch.setattr(core, "_fresh_capture",
                        lambda label, monitor=0: {"label": label, "path": "/tmp/v.png", "screen": [100, 200]})
    monkeypatch.setattr(core, "_verify_text_in_image",
                        lambda image, text, min_conf=35: {"required": True, "verified": True, "matches": 1})
    good = core.click_abs(x=10, y=20, sw=100, sh=200, expect="Saved")
    assert good["ok"] is True and good["effect"] is True and good["verified"] is True

    # click + expect NOT found → fail envelope, effect False (no false success)
    monkeypatch.setattr(core, "_verify_text_in_image",
                        lambda image, text, min_conf=35: {"required": True, "verified": False, "matches": 0})
    bad = core.click_abs(x=10, y=20, sw=100, sh=200, expect="Saved")
    assert bad["ok"] is False and bad["effect"] is False and bad["verified"] is False


def test_ui_type_verified_submits_only_after_draft_verify(monkeypatch) -> None:
    shots = iter([
        {"label": "before", "path": "/tmp/before.png", "screen": [1100, 618]},
        {"label": "draft", "path": "/tmp/draft.png", "screen": [1100, 618]},
        {"label": "sent", "path": "/tmp/sent.png", "screen": [1100, 618]},
    ])
    events = []

    monkeypatch.setattr(core, "_fresh_capture", lambda label, monitor=0: next(shots))
    monkeypatch.setattr(core, "_abs_click_from_frame",
                        lambda x, y, screen, button="left": events.append(("click", x, y, screen)) or
                        {"via": "uinput-absolute"})
    monkeypatch.setattr(core, "_verify_text_in_image",
                        lambda image, text, min_conf=35: {"required": True, "verified": True,
                                                          "via": "tesseract", "matches": 1})

    def _dispatch(action, **kw):
        events.append((action, kw))
        if action == "type":
            return {"via": "uinput-keyboard", "chars": len(kw["text"])}
        if action == "key":
            return {"via": "uinput-keyboard", "combo": kw["keys"]}
        raise AssertionError(action)

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.ui_type_verified(text="hello", x=10, y=20, submit=True,
                              sent_expect="hello", require_sent_verify=True)

    assert r["ok"] is True
    assert r["submitted"] is True
    assert r["verified"] is True
    assert [e[0] for e in events] == ["click", "type", "key"]


def test_ui_type_verified_stops_before_submit_when_draft_unverified(monkeypatch) -> None:
    monkeypatch.setattr(core, "_fresh_capture", lambda label, monitor=0: {
        "label": label, "path": f"/tmp/{label}.png", "screen": [1100, 618]
    })
    monkeypatch.setattr(core, "_abs_click_from_frame",
                        lambda x, y, screen, button="left": {"via": "uinput-absolute"})
    monkeypatch.setattr(core, "_verify_text_in_image",
                        lambda image, text, min_conf=35: {"required": True, "verified": False,
                                                          "via": "tesseract", "matches": 0})
    calls = []

    def _dispatch(action, **kw):
        calls.append(action)
        if action == "type":
            return {"via": "uinput-keyboard", "chars": len(kw["text"])}
        if action == "key":
            raise AssertionError("submit key must not be sent when draft verification fails")
        raise AssertionError(action)

    monkeypatch.setattr(B, "dispatch", _dispatch)

    r = core.ui_type_verified(text="hello", x=10, y=20, submit=True)

    assert r["ok"] is False
    assert r["stopped"] == "draft-not-verified"
    assert r["submitted"] is False
    assert calls == ["type"]


def test_ui_act_rejects_bad_verb() -> None:
    assert core.ui_act(do="frobnicate", text="x")["ok"] is False


# --------------------------------------------------------------------------- #
# Wayland/session detection + session env (the linux-x11 misreport fix)
# --------------------------------------------------------------------------- #
def test_is_wayland_detects_via_socket_when_env_absent(monkeypatch) -> None:
    # A node process spawned without graphical session vars must still detect Wayland
    # from the live compositor socket -- else the box mis-tags as linux-x11 and drops
    # grim/portal from the capture candidates.
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setattr(B, "_wayland_socket", lambda: "wayland-1")
    assert B.is_wayland() is True
    monkeypatch.setattr(B, "_wayland_socket", lambda: None)
    assert B.is_wayland() is False


def test_platform_tag_wayland_via_socket(monkeypatch) -> None:
    monkeypatch.setattr(B.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setattr(B, "_wayland_socket", lambda: "wayland-0")
    assert B.platform_tag() == "linux-wayland"
    monkeypatch.setattr(B, "_wayland_socket", lambda: None)
    monkeypatch.setattr(B, "_x_display", lambda: None)
    assert B.platform_tag() == "linux-x11"


def test_session_env_fills_display_and_bus(monkeypatch, tmp_path) -> None:
    (tmp_path / "wayland-0").write_text("")
    (tmp_path / "bus").write_text("")
    monkeypatch.setattr(B, "_runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(B, "_x_display", lambda: ":7")
    for v in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY"):
        monkeypatch.delenv(v, raising=False)
    env = B.session_env()
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={tmp_path}/bus"
    assert env["DISPLAY"] == ":7"


def test_session_env_preserves_existing_vars(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
    monkeypatch.setenv("DISPLAY", ":3")
    env = B.session_env()
    assert env["WAYLAND_DISPLAY"] == "wayland-9" and env["DISPLAY"] == ":3"


# --------------------------------------------------------------------------- #
# chrome launch: dedicated profile + debug port (the "debugger did not come up" fix)
# --------------------------------------------------------------------------- #
import urirun_connector_kvm.launch_backends as LB  # noqa: E402


def test_chrome_launch_injects_dedicated_profile_and_debug_port(monkeypatch) -> None:
    captured = {}

    class _Proc:
        pid = 4321

    monkeypatch.setattr(LB, "_find_app", lambda app: None)
    monkeypatch.setattr(LB.shutil, "which", lambda b: "/usr/bin/google-chrome" if "chrome" in b else None)
    monkeypatch.setattr(LB, "session_env", lambda: {})
    monkeypatch.setattr(LB, "_cdp_wait", lambda port, wait: {"ready": False, "port": port})
    monkeypatch.setattr(LB.subprocess, "Popen",
                        lambda argv, **kw: (captured.__setitem__("argv", argv), _Proc())[1])
    monkeypatch.delenv("URIRUN_KVM_NO_A11Y", raising=False)
    monkeypatch.delenv("URIRUN_KVM_CDP_PROFILE", raising=False)
    monkeypatch.delenv("URIRUN_KVM_CDP_ON_LAUNCH", raising=False)

    # debug=True OPTS IN to the CDP surface: a dedicated --user-data-dir is required for the
    # debug port to bind (Chrome 136+ blocks it on the default profile, and a bare port is
    # swallowed by a running default-profile chrome).
    res = LB._launch_xdg(app="google-chrome", settle=0, debug=True)
    argv = captured["argv"]
    assert any(a.startswith("--remote-debugging-port=") for a in argv)
    assert any(a.startswith("--user-data-dir=") for a in argv)
    assert "--no-first-run" in argv and "--no-default-browser-check" in argv
    assert res["cdp"]["ready"] is False   # readiness is probed + reported, not assumed


def test_chrome_launch_default_keeps_real_profile(monkeypatch) -> None:
    # Without debug, launch must NOT force a dedicated profile — that would open an
    # UNAUTHENTICATED chrome and break logged-in OCR workflows (e.g. a LinkedIn post).
    captured = {}

    class _Proc:
        pid = 99

    monkeypatch.setattr(LB, "_find_app", lambda app: None)
    monkeypatch.setattr(LB.shutil, "which", lambda b: "/usr/bin/google-chrome" if "chrome" in b else None)
    monkeypatch.setattr(LB, "session_env", lambda: {})
    monkeypatch.setattr(LB.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(LB.subprocess, "Popen",
                        lambda argv, **kw: (captured.__setitem__("argv", argv), _Proc())[1])
    monkeypatch.delenv("URIRUN_KVM_NO_A11Y", raising=False)
    monkeypatch.delenv("URIRUN_KVM_CDP_ON_LAUNCH", raising=False)

    res = LB._launch_xdg(app="google-chrome", args=["https://www.linkedin.com"], settle=0)
    argv = captured["argv"]
    assert not any("user-data-dir" in a for a in argv)      # real (logged-in) profile preserved
    assert not any("remote-debugging-port" in a for a in argv)
    assert "--force-renderer-accessibility" in argv          # a11y/OCR help is still applied
    assert "cdp" not in res                                   # no CDP readiness probe


def test_non_chrome_launch_skips_cdp(monkeypatch) -> None:
    captured = {}

    class _Proc:
        pid = 11

    monkeypatch.setattr(LB, "_find_app", lambda app: None)
    monkeypatch.setattr(LB.shutil, "which", lambda b: "/usr/bin/gedit")
    monkeypatch.setattr(LB, "session_env", lambda: {})
    monkeypatch.setattr(LB.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(LB.subprocess, "Popen",
                        lambda argv, **kw: (captured.__setitem__("argv", argv), _Proc())[1])

    res = LB._launch_xdg(app="gedit", settle=0)
    assert not any("remote-debugging-port" in a for a in captured["argv"])
    assert "cdp" not in res


# --------------------------------------------------------------------------- #
# environment-adapted routing (vision is gated on real OCR + input capability)
# --------------------------------------------------------------------------- #
import urirun_connector_kvm.strategies as S  # noqa: E402
import urirun_connector_kvm.environment as E  # noqa: E402


def test_vision_strategy_is_environment_gated(monkeypatch) -> None:
    monkeypatch.setattr(E, "profile", lambda: {"controlStrategies": {"vision": False}})
    assert S.VisionStrategy().available("") is False        # no tesseract / no uinput -> off
    monkeypatch.setattr(E, "profile", lambda: {"controlStrategies": {"vision": True}})
    assert S.VisionStrategy().available("") is True


def test_environment_profile_shape() -> None:
    p = E.profile()
    assert set(p) >= {"platform", "display", "cdp", "ocr", "input", "controlStrategies", "best", "controllable"}
    assert set(p["controlStrategies"]) == {"cdp", "atspi", "vision"}
    assert p["best"] in (None, "cdp", "atspi", "vision")
    assert p["controllable"] is (p["best"] is not None)
    # actionMatrix must be present and structurally complete
    assert "actionMatrix" in p
    m = p["actionMatrix"]
    assert set(m) == {"cdp", "atspi", "uinput", "vision"}
    ACTIONS = {"locate", "click", "type", "navigate", "screenshot"}
    VALID = {"executable", "degraded", "not_executable", "not_applicable", "blocked"}
    for surface, row in m.items():
        assert set(row) == ACTIONS, f"{surface} missing actions"
        for a, v in row.items():
            assert v in VALID, f"{surface}.{a}={v!r} not a valid executability value"


def test_action_matrix_wayland_type_rule() -> None:
    """On Wayland, atspi and uinput CANNOT type — this is a hard platform rule, not detection."""
    wayland_prof = {
        "wayland": True, "controlStrategies": {"cdp": True, "atspi": True, "vision": False},
        "input": {"uinput": True, "ydotool": False, "xdotool": False},
        "ocr": {"tesseract": True, "easyocr": False},
        "osLevelReliable": False,
    }
    m = E.action_matrix(wayland_prof)
    assert m["cdp"]["type"] == "executable",      "CDP must be able to type"
    assert m["atspi"]["type"] == "not_executable", "atspi cannot type on Wayland (compositor focus)"
    assert m["uinput"]["type"] == "not_executable","uinput cannot type on Wayland (compositor focus)"


def test_action_matrix_wayland_screenshot_blocked() -> None:
    """On Wayland with osLevelReliable=False, OS-level screenshot is blocked."""
    prof = {
        "wayland": True, "controlStrategies": {"cdp": False, "atspi": False, "vision": True},
        "input": {"uinput": True, "ydotool": False, "xdotool": False},
        "ocr": {"tesseract": True, "easyocr": False},
        "osLevelReliable": False,
    }
    m = E.action_matrix(prof)
    assert m["uinput"]["screenshot"] == "blocked"
    assert m["vision"]["screenshot"] == "blocked"


def test_action_matrix_x11_type_degraded_not_blocked() -> None:
    """On X11 (not Wayland), atspi type is degraded (works but unreliably) not blocked."""
    x11_prof = {
        "wayland": False, "controlStrategies": {"cdp": False, "atspi": True, "vision": False},
        "input": {"uinput": True, "ydotool": False, "xdotool": False},
        "ocr": {"tesseract": False, "easyocr": False},
        "osLevelReliable": True,
    }
    m = E.action_matrix(x11_prof)
    assert m["atspi"]["type"] == "degraded",  "X11 atspi type should be degraded, not blocked"
    assert m["uinput"]["type"] == "degraded", "X11 uinput type should be degraded, not blocked"


def test_report_includes_environment() -> None:
    import urirun_connector_kvm.control as Ctl
    rep = Ctl.report()
    assert "environment" in rep and "strategies" in rep
    assert {s["name"] for s in rep["strategies"]} == {"cdp", "atspi", "vision"}


def test_cdp_port_prefers_client_url(monkeypatch) -> None:
    # launch must bind the port the cdp client/readiness poll use (URIRUN_KVM_CDP_URL),
    # not a divergent URIRUN_KVM_CDP_PORT — else Chrome binds a port nothing watches.
    monkeypatch.setenv("URIRUN_KVM_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setenv("URIRUN_KVM_CDP_PORT", "9333")
    assert LB._cdp_port() == "9222"
    monkeypatch.delenv("URIRUN_KVM_CDP_URL", raising=False)
    assert LB._cdp_port() == "9333"
    monkeypatch.delenv("URIRUN_KVM_CDP_PORT", raising=False)
    assert LB._cdp_port() == "9222"


# --------------------------------------------------------------------------- #
# Phase 0: envelope-collision regression (inner result with ok/error/url spread
# into urirun.ok/fail used to raise TypeError and mask the real CDP result)
# --------------------------------------------------------------------------- #
def test_spread_strips_envelope_reserved_keys() -> None:
    assert core._spread({"ok": True, "error": "e", "connector": "k", "action": "a", "keep": 1}) == {"keep": 1}
    assert core._spread({"url": "u", "keep": 2}, "url") == {"keep": 2}
    assert core._spread(None) == {}


def test_cdp_navigate_has_no_url_collision(monkeypatch) -> None:
    class FakeCdp:
        @staticmethod
        def navigate(url):
            return {"ok": True, "url": url}            # 'url' would collide with the handler's url=
        @staticmethod
        def page_ready(timeout=8.0):
            return {"ok": True, "readyState": "complete"}
    monkeypatch.setattr(core, "_cdp_mod", lambda: FakeCdp)
    r = core.cdp_navigate(url="https://x")
    assert r["ok"] is True and r["url"] == "https://x"   # no TypeError; url present exactly once


# --------------------------------------------------------------------------- #
# CDP launch/probe split (cold-start must not blow the node handler exec cap)
# --------------------------------------------------------------------------- #
def test_cdp_start_session_reuses_without_spawn(monkeypatch) -> None:
    import subprocess
    monkeypatch.setattr(cdp, "reachable", lambda: True)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or object())
    r = cdp.start_session(url="")
    assert r["reused"] is True and r["launching"] is False
    assert spawned == []                                  # reuse never spawns


def test_cdp_start_session_launches_and_returns_immediately(monkeypatch) -> None:
    import subprocess
    monkeypatch.setattr(cdp, "reachable", lambda: False)  # not up
    monkeypatch.setattr(cdp, "_find_chrome", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(cdp.os, "makedirs", lambda *a, **k: None)

    class _P:
        pid = 555
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _P())
    r = cdp.start_session(url="https://example.com")
    # returns AT ONCE with launching:true — does NOT block polling for the bind
    assert r["ok"] is True and r["launching"] is True and r["reused"] is False and r["pid"] == 555


def test_cdp_await_ready_polls_without_spawn(monkeypatch) -> None:
    import subprocess, time as _t
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("await_ready must not spawn")))
    monkeypatch.setattr(_t, "sleep", lambda *_a: None)
    monkeypatch.setattr(cdp, "reachable", lambda: True)
    assert cdp.await_ready(timeout=1)["ready"] is True
    # times out cleanly when never reachable (bounded; no spawn)
    monkeypatch.setattr(cdp, "reachable", lambda: False)
    out = cdp.await_ready(timeout=0)
    assert out["ready"] is False and "timeout" in out["error"]


def test_cdp_start_session_reuses_matching_profile(monkeypatch) -> None:
    """When user_data_dir matches running Chrome's profile, reuse without respawn."""
    import subprocess
    monkeypatch.setattr(cdp, "reachable", lambda: True)
    monkeypatch.setattr(cdp, "_running_user_data_dir", lambda: "/home/user/.config/google-chrome/Default")
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or object())
    r = cdp.start_session(url="", user_data_dir="/home/user/.config/google-chrome/Default")
    assert r["reused"] is True and r["launching"] is False
    assert spawned == []


def test_cdp_start_session_respawns_on_profile_mismatch(monkeypatch) -> None:
    """When user_data_dir differs from running Chrome's profile, kill and relaunch."""
    import subprocess, time as _t
    killed = []
    reachable_calls = [True, False]  # reachable before kill, not after
    monkeypatch.setattr(cdp, "reachable", lambda: reachable_calls.pop(0) if reachable_calls else False)
    monkeypatch.setattr(cdp, "_running_user_data_dir", lambda: "/tmp/urirun-kvm-cdp-9222")
    monkeypatch.setattr(cdp, "_kill_chrome_on_port", lambda: killed.append(1))
    monkeypatch.setattr(cdp, "_find_chrome", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(cdp.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(_t, "sleep", lambda *_: None)

    class _P:
        pid = 777
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _P())
    r = cdp.start_session(url="", user_data_dir="/home/user/.config/google-chrome/Default")
    assert killed == [1]           # old Chrome was killed
    assert r["reused"] is False    # new Chrome launched
    assert r["launching"] is True


def test_ui_wait_success_has_no_found_collision(monkeypatch) -> None:
    # the route hit carries `found` -> _ok(found=True, **hit) used to raise
    # "_ok() got multiple values for keyword argument 'found'" on the SUCCESS path
    monkeypatch.setattr(core.C, "route",
                        lambda op, **k: {"ok": True, "found": True, "strategy": "cdp", "name": "X"})
    r = core.ui_wait(text="X", timeout=1)
    assert r["ok"] is True and r["found"] is True and r["waited"] is not None and r["strategy"] == "cdp"


# --------------------------------------------------------------------------- #
# compositor detection + degraded capture
# --------------------------------------------------------------------------- #

def test_grim_skipped_on_gnome_wayland(monkeypatch) -> None:
    """_is_wlroots_compositor() returns False for GNOME; grim raises BackendError without running."""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert B._is_wlroots_compositor() is False


def test_grim_allowed_on_sway(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    assert B._is_wlroots_compositor() is True


def test_grim_backend_raises_on_non_wlroots(monkeypatch) -> None:
    import pytest
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    with pytest.raises(B.BackendError, match="wlroots"):
        B._cap_grim(output="/tmp/x.png")


def test_capture_portal_denied_returns_degraded(monkeypatch) -> None:
    """When portal denies permission, capture() returns ok=True, degraded=True — not a hard fail."""
    monkeypatch.setattr(core, "_cdp", None)  # no CDP fallback here — exercise the degraded path itself
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: (_ for _ in ()).throw(
            B.BackendError("portal denied (code 2) — capture needs a one-time screenshot permission grant")
        ),
    )
    r = capture(output="/tmp/x.png")
    assert r["ok"] is True
    assert r.get("degraded") is True
    assert "portal denied" in r.get("degradedReason", "")


def test_capture_other_backend_error_stays_fail(monkeypatch) -> None:
    """User-approved ACTIONABLE-ONLY contract for need_from_backend_error: a backend error becomes a
    degraded acquire-need only when it is actionable — it carries an install hint (``install: grim``)
    or a human grant. A bare "no available backend ... options: none" with nothing to install or grant
    stays an honest hard fail (ok=False), not a need the user cannot act on.
    NOTE: this diverges from a prior agent edit that made EVERY "no available backend" degraded+need."""
    monkeypatch.setattr(core, "_cdp", None)  # no CDP fallback here — exercise the degraded/fail contract
    # actionable: the real backends message carries an install hint -> degraded acquire-need
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: (_ for _ in ()).throw(
            B.BackendError("no available backend for 'capture' on linux-wayland; options: grim (install: grim)")),
    )
    r = capture(output="/tmp/x.png")
    assert r["ok"] is True and r.get("degraded") is True and r.get("need") is not None
    # non-actionable: nothing to install or grant -> honest fail, not a fake acquire
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: (_ for _ in ()).throw(
            B.BackendError("no available backend for 'capture' on headless; options: none")),
    )
    r2 = capture(output="/tmp/x.png")
    assert r2["ok"] is False

    # A truly unrecognised error still hard-fails
    monkeypatch.setattr(
        B, "dispatch",
        lambda action, **kw: (_ for _ in ()).throw(B.BackendError("unexpected internal error xyz")),
    )
    r2 = capture(output="/tmp/x.png")
    assert r2["ok"] is False


def test_capture_zero_byte_any_backend_is_degraded_not_false_success(monkeypatch) -> None:
    """A 0-byte file from ANY backend (e.g. gnome-screenshot exiting 0 but writing nothing on a
    blocked session) is a false success — must come back degraded, not ok with bytes:0."""
    monkeypatch.setattr(core, "_cdp", None)            # no CDP fallback — exercise the degraded guard
    monkeypatch.setattr(B, "dispatch",
                        lambda action, **kw: {"backend": "gnome-screenshot", "via": "gnome-screenshot",
                                              "path": kw.get("output")})
    monkeypatch.setattr(core.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(core.os.path, "getsize", lambda _p: 0)
    r = capture(output="/tmp/x.png")
    assert r.get("degraded") is True and "gnome-screenshot" in (r.get("degradedReason") or "")


def test_capture_backend_zero_byte_raises_so_dispatch_cascades(monkeypatch) -> None:
    """A file-producing backend that exits 0 but writes an empty file must raise BackendError (not
    return a 0-byte success), so dispatch() cascades to the next capture backend instead of letting an
    empty frame win the cascade. This is the backend-level analog of core.py's false-success guard."""
    import pytest
    from urirun_connector_kvm import backends as B
    monkeypatch.setattr(B, "_run", lambda *a, **k: None)        # tool "succeeds" (exit 0)
    monkeypatch.setattr(B.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(B.os.path, "getsize", lambda _p: 0)     # ...but wrote nothing
    for cap in (B._cap_gnome, B._cap_scrot):
        monkeypatch.setattr(B, "session_env", lambda: {"DISPLAY": ":0"})  # let scrot pass its env guard
        with pytest.raises(B.BackendError):
            cap("/tmp/x.png")


def test_cdp_impl_vendor_matches_urirun_cdp() -> None:
    """The kvm-bundled _cdp_impl.py (node CDP fallback when urirun-cdp is absent) must stay
    byte-identical to the canonical urirun_cdp/cdp.py minus its 4-line vendor header — else the
    node's CDP client silently drifts from the host's."""
    import inspect
    try:
        import urirun_cdp.cdp as canonical
    except ImportError:
        import pytest
        pytest.skip("urirun_cdp not installed")
    from urirun_connector_kvm import _cdp_impl
    vendor_body = "\n".join(inspect.getsource(_cdp_impl).splitlines()[4:])  # drop vendor header
    assert vendor_body.strip() == inspect.getsource(canonical).strip(), \
        "vendored _cdp_impl drifted from urirun_cdp/cdp.py — re-vendor it"


def test_cdp_shim_uses_real_surface_when_urirun_cdp_present() -> None:
    """With urirun_cdp installed (host), the shim must resolve the canonical surface, not the stub."""
    from urirun_connector_kvm import cdp
    assert cdp.reachable is not None and cdp.navigate is not None


# ---- vnc/* RFB surface + locate hardening (noVNC reliability work) --------------------

def test_vnc_routes_guard_inputs(monkeypatch) -> None:
    """Every vnc route must fail honestly (ok:false), never raise, on missing inputs/target."""
    monkeypatch.delenv("URIRUN_KVM_VNC", raising=False)
    assert core.vnc_find()["ok"] is False                       # no text
    assert core.vnc_type()["ok"] is False                       # no text
    assert core.vnc_key()["ok"] is False                        # no combo
    assert core.vnc_click()["ok"] is False                      # no text and no x/y
    assert core.vnc_status()["ok"] is False                     # no target anywhere


def test_fuzzy_line_matches_rescues_ocr_noise() -> None:
    """OCR splits/mangles labels ('Reconfigure' -> 'Reconfig re'); fuzzy fallback must
    match them, carry the ratio for audit, and reject unrelated text."""
    lines = [
        {"text": "Reconfig re", "conf": 80.0, "box": [10, 20, 100, 14], "center": [60, 27]},
        {"text": "Norkspaces", "conf": 85.0, "box": [10, 40, 90, 14], "center": [55, 47]},
        {"text": "Exit", "conf": 96.0, "box": [10, 60, 30, 14], "center": [25, 67]},
    ]
    hit = B._fuzzy_line_matches(lines, "reconfigure")
    assert hit and hit[0]["center"] == [60, 27] and hit[0]["fuzzy"] >= 0.78
    assert B._fuzzy_line_matches(lines, "workspaces")[0]["text"] == "Norkspaces"
    assert B._fuzzy_line_matches(lines, "shutdown") == []


def test_merge_matches_offsets_and_dedupes() -> None:
    base = [{"text": "Save", "conf": 90.0, "box": [5, 5, 40, 12], "center": [25, 11]}]
    band = [{"text": "Save", "conf": 88.0, "box": [5, 2, 40, 12], "center": [25, 8]},   # dup after +3
            {"text": "Cancel", "conf": 91.0, "box": [60, 2, 50, 12], "center": [85, 8]}]
    merged = B._merge_matches(base, band, dy=3)
    assert [m["text"] for m in merged] == ["Save", "Cancel"]
    assert merged[1]["center"] == [85, 11]                       # band offset applied


def test_locate_backends_accept_standard_kwargs(tmp_path) -> None:
    """Every registered locate backend must accept the standard call shape
    (image=/query=/role=/name=) without TypeError. Regression: a helper function once
    swallowed the @backend decorator, so the REGISTERED fn rejected query= and the
    backend silently scored 0 on every locate."""
    import base64
    png = tmp_path / "one.png"  # 1x1 PNG: no text anywhere, every backend must miss honestly
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))
    for b in B.backends_for("locate"):
        try:
            b.fn(image=str(png), query="NoSuchLabel", role="", name="")
        except TypeError as exc:
            raise AssertionError(f"locate backend {b.name!r} rejects standard kwargs: {exc}")
        except Exception:
            pass  # honest miss / missing tool is fine — only the call SHAPE is under test
