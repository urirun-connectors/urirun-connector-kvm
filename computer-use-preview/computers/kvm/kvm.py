# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
#
# A `Computer` that drives a REAL desktop on a urirun node over kvm:// routes
# (portal screen capture + ydotool input), instead of a Playwright browser. The
# Gemini Computer Use agent loop and safety stay exactly as upstream; only the
# executor changes — so the same agent can operate the live lenovo desktop.
#
# Coordinates: the agent denormalizes the model's 0–1000 grid using
# `screen_size()`, which returns the node's TRUE screen dimensions. We send a
# *downscaled* screenshot to the model (normalized coords are resolution-
# independent) but denormalize back to full pixels, so ydotool clicks land 1:1.
import base64
import json
import time
import urllib.request
from typing import Literal

from computers.computer import Computer, EnvState

API_MAX_WIDTH = 1400  # downscale screenshots sent to the model (coords stay normalized)


class KvmComputer(Computer):
    """Computer Use executor backed by a urirun node's kvm:// surface."""

    def __init__(self, node_url: str = "local", node_name: str = "laptop", screen_size=None):
        self._node = node_url.rstrip("/") if node_url and node_url != "local" else "local"
        self._name = node_name
        self._size = tuple(screen_size) if screen_size else None

    # -- context manager -----------------------------------------------------
    def __enter__(self):
        if self._size is None:
            try:
                res = self._run("screen/query/capture", {})
                full = res.get("fullSize") or [1920, 1080]
                self._size = (int(full[0]), int(full[1]))
            except Exception:
                self._size = (1920, 1080)
        return self

    def __exit__(self, *exc):
        return False

    # -- transport -----------------------------------------------------------
    def _run(self, route: str, payload: dict, scheme: str = "kvm", timeout: int = 60) -> dict:
        if not self._node or self._node == "local":
            from urirun_connector_kvm.runner import UniversalRunner
            runner = UniversalRunner()
            op = route.split("/")[-1]
            step = dict(payload, op=op)
            res = runner.execute_step(step)
            if not res.get("ok"):
                raise RuntimeError(f"{route}: {res.get('error', 'step failed')}")
            result = res.get("result") or res
            return result.get("value") if isinstance(result, dict) and "value" in result else (result or {})

        uri = f"{scheme}://{self._name}/{route}"
        body = json.dumps({"uri": uri, "payload": payload}).encode()
        req = urllib.request.Request(self._node + "/run", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            env = json.loads(resp.read() or "{}")
        if not env.get("ok"):
            dec, err = env.get("decision") or {}, env.get("error") or {}
            raise RuntimeError(f"{uri}: {dec.get('reason') or err.get('message') or err}")
        res = env.get("result")
        val = res.get("value") if isinstance(res, dict) else res
        return val.get("value") if isinstance(val, dict) and "value" in val else (val or {})

    # -- state ---------------------------------------------------------------
    def _state(self) -> EnvState:
        res = self._run("screen/query/capture", {"base64": True, "max_width": API_MAX_WIDTH})
        png = base64.b64decode(res.get("pngBase64", "")) if res.get("pngBase64") else b""
        return EnvState(screenshot=png, url="")

    def _abs_payload(self, x: int, y: int, button: str = "left", do_click: bool = True) -> dict:
        width, height = self.screen_size()
        return {
            "x": int(x),
            "y": int(y),
            "sw": int(width),
            "sh": int(height),
            "button": button,
            "do_click": do_click,
        }

    def screen_size(self) -> tuple[int, int]:
        return self._size

    def current_state(self) -> EnvState:
        return self._state()

    def take_screenshot(self) -> EnvState:
        return self._state()

    # -- mouse ---------------------------------------------------------------
    def _click(self, x, y, button="left") -> EnvState:
        self._run("abs/command/click", self._abs_payload(x, y, button=button))
        return self._state()

    def click_at(self, x, y) -> EnvState:
        return self._click(x, y)

    def double_click_at(self, x, y) -> EnvState:
        self._run("input/command/double-click", {"x": int(x), "y": int(y)})
        return self._state()

    def triple_click_at(self, x, y) -> EnvState:
        self._run("input/command/triple-click", {"x": int(x), "y": int(y)})
        return self._state()

    def middle_click_at(self, x, y) -> EnvState:
        self._run("input/command/middle-click", {"x": int(x), "y": int(y)})
        return self._state()

    def right_click_at(self, x, y) -> EnvState:
        self._run("input/command/right-click", {"x": int(x), "y": int(y)})
        return self._state()

    def hover_at(self, x, y) -> EnvState:
        self._run("abs/command/click", self._abs_payload(x, y, do_click=False))
        return self._state()

    def mouse_down(self, x, y) -> EnvState:
        self._run("input/command/move", {"x": int(x), "y": int(y)})
        return self._state()

    def mouse_up(self, x, y) -> EnvState:
        return self._state()

    def drag_and_drop(self, x, y, destination_x, destination_y) -> EnvState:
        self._run("input/command/drag-and-drop", {
            "x": int(x), "y": int(y),
            "destination_x": int(destination_x), "destination_y": int(destination_y)
        })
        return self._state()

    # -- keyboard ------------------------------------------------------------
    def type_text(self, text, press_enter) -> EnvState:
        self._run("input/command/type", {"text": text})
        if press_enter:
            self._run("input/command/key", {"keys": "Return"})
        return self._state()

    def type_text_at(self, x, y, text, press_enter, clear_before_typing) -> EnvState:
        self._run("abs/command/click", self._abs_payload(x, y)); time.sleep(0.1)
        if clear_before_typing:
            self._run("input/command/key", {"keys": "ctrl+a"})
            self._run("input/command/key", {"keys": "BackSpace"})
        self._run("input/command/type", {"text": text})
        if press_enter:
            self._run("input/command/key", {"keys": "Return"})
        return self._state()

    def key_combination(self, keys) -> EnvState:
        self._run("input/command/key", {"keys": "+".join(keys)})
        return self._state()

    def press_key(self, key) -> EnvState:
        self._run("input/command/key", {"keys": key})
        return self._state()

    def key_down(self, key) -> EnvState:   # kvm has no held key; atomic press
        self._run("input/command/key", {"keys": key})
        return self._state()

    def key_up(self, key) -> EnvState:
        return self._state()

    # -- scrolling -----------------------------------------------------------
    def scroll_document(self, direction) -> EnvState:
        return self.scroll_at(self._size[0] // 2, self._size[1] // 2, direction, 600)

    def scroll_at(self, x, y, direction, magnitude) -> EnvState:
        self._run("abs/command/click", self._abs_payload(x, y, do_click=False))
        dy = magnitude if direction == "down" else -magnitude if direction == "up" else 0
        self._run("input/command/scroll", {"dy": int(dy)})
        return self._state()

    # -- waits ---------------------------------------------------------------
    def wait(self, seconds) -> EnvState:
        self._run("input/command/wait", {"seconds": float(seconds)})
        return self._state()

    def wait_5_seconds(self) -> EnvState:
        return self.wait(5.0)

    # -- browser-style verbs (mapped to desktop keyboard / app launch) -------
    def open_web_browser(self) -> EnvState:
        for app in ("google-chrome", "chromium", "firefox"):
            try:
                self._run("desktop/command/launch", {"app": app, "settle": 4}, scheme="app")
                break
            except Exception:  # noqa: BLE001 - try the next browser
                continue
        return self._state()

    def navigate(self, url) -> EnvState:
        if not self._node or self._node == "local":
            self._run("cdp/page/command/navigate", {"url": url})
            return self._state()
        try:
            from urirun_connector_kvm import cdp
            if cdp.reachable():
                cdp.navigate(url)
                return self._state()
        except Exception:
            pass
        self._run("input/command/key", {"keys": "ctrl+l"}); time.sleep(0.2)
        self._run("input/command/type", {"text": url})
        self._run("input/command/key", {"keys": "Return"})
        return self._state()

    def click_selector(self, selector: str = "", text: str = "") -> EnvState:
        """Click an element by CSS selector or text using DOM/CDP."""
        self._run("cdp/page/command/dom-click", {"selector": selector, "text": text})
        return self._state()

    def fill_selector(self, selector: str = "", value: str = "") -> EnvState:
        """Fill an input element by CSS selector using DOM/CDP."""
        self._run("cdp/page/command/dom-fill", {"selector": selector, "value": value})
        return self._state()

    def extract_form(self, selector: str = "") -> dict:
        """Extract form schema using DOM/CDP."""
        return self._run("cdp/page/query/dom-extract-form", {"selector": selector})

    def search(self) -> EnvState:
        return self.navigate("https://www.google.com")

    def go_back(self) -> EnvState:
        self._run("input/command/key", {"keys": "alt+Left"})
        return self._state()

    def go_forward(self) -> EnvState:
        self._run("input/command/key", {"keys": "alt+Right"})
        return self._state()
