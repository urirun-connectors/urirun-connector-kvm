# urirun-connector-kvm


## AI Cost Tracking

![PyPI](https://img.shields.io/badge/pypi-costs-blue) ![Version](https://img.shields.io/badge/version-0.3.1-blue) ![Python](https://img.shields.io/badge/python-3.9+-blue) ![License](https://img.shields.io/badge/license-Apache--2.0-green)
![AI Cost](https://img.shields.io/badge/AI%20Cost-$0.64-orange) ![Human Time](https://img.shields.io/badge/Human%20Time-59.8h-blue) ![Model](https://img.shields.io/badge/Model-openrouter%2Fqwen%2Fqwen3--coder--next-lightgrey)

- 🤖 **LLM usage:** $0.6379 (90 commits)
- 👤 **Human dev:** ~$5980 (59.8h @ $100/h, 30min dedup)

Generated on 2026-10-04 using [openrouter/qwen/qwen3-coder-next](https://openrouter.ai/qwen/qwen3-coder-next)

---

Cross-platform KVM (keyboard / video / mouse) connector for [ifURI](https://ifuri.com) /
urirun. Capture the screen and drive keyboard & mouse through `kvm://` routes — on
Linux (Wayland **and** X11), Windows and macOS.

Catalog: <https://connect.ifuri.com/connectors/kvm>

## Routes

| URI | Operation |
| --- | --- |
| `kvm://host/screen/query/capture` | screenshot → file (and optional inline base64) |
| `kvm://host/input/command/type` | type a whole string |
| `kvm://host/input/command/key` | a key or hotkey combo (e.g. `ctrl+a`) |
| `kvm://host/input/command/click` | mouse click (optionally at `x,y`) |
| `kvm://host/input/command/move` | move the pointer (absolute) |
| `kvm://host/input/command/scroll` | scroll wheel |
| `kvm://host/task/command/run` | a bounded sequence of the above (one session) |
| `kvm://host/window/command/focus` | activate a window by title |
| `kvm://host/window/query/list` | list windows |
| `kvm://host/doctor/query/report` | which backend serves each action + install hints |
| `kvm://host/abs/command/click` | pixel-accurate click via a uinput absolute device (`sw`/`sh` = the coord space to scale from) |
| `app://host/desktop/command/launch` | launch a desktop app (XDG `.desktop` / `open` / `startfile`) |
| `app://host/desktop/query/list` | list launchable desktop apps |

### UI grounding & accessibility (locate → act → verify)

| URI | Operation |
| --- | --- |
| `kvm://host/ui/query/find` | locate a UI element by text/role → bbox + coordinates |
| `kvm://host/ui/query/locate` | OCR/vision-locate on a screenshot → matches with click centers |
| `kvm://host/ui/command/click-text` | find on-screen text and click it (optionally type + submit) |
| `kvm://host/ui/command/click` | find a target and click it (a11y action or centre click) |
| `kvm://host/ui/command/fill` | find a field, focus it, type a value (+verify) |
| `kvm://host/ui/command/type-verified` | click a visible field in screenshot coordinates, type a draft, verify it on screen, then optionally submit |
| `kvm://host/ui/query/wait` | poll until a target appears (or timeout) — closed-loop |
| `kvm://host/ui/query/verify` | assert a string is present on screen |
| `kvm://host/a11y/command/act` | find a UI element by role/name and focus/click/set-text it (AT-SPI, resolution-independent) |

## Decorator-driven, multi-backend

Every capability is served by a **`@backend(action, name, …)`-registered** function in
[`backends.py`](urirun_connector_kvm/backends.py). At call time the connector picks the
highest-priority backend that is *available* on the live platform/session and falls
through on failure — so the same routes work everywhere a suitable helper is installed.
Add support for a new tool by writing one decorated function; nothing else changes.

```
capture: portal(Wayland) → grim → mss → Pillow → scrot → ImageMagick → gnome-screenshot → screencapture(macOS)
input:   ydotool(Wayland) → wtype / xdotool(X11) → pynput(any)
focus:   wmctrl(X11/Xwayland) → pygetwindow(Win/macOS)
launch:  XDG .desktop / gtk-launch(Linux, covers Flatpak/Snap) → open -a(macOS) → startfile(Windows)
locate:  AT-SPI(a11y tree, exact) → EasyOCR → tesseract → imgl/vql(vision) — genuine text match, honest miss
```

`screen/query/capture` also takes a focus crop (`cx`/`cy`/`zoom` or `crop_w`/`crop_h`)
to return only a zoomed tile around a point — so a remote caller ships a small region
where the action is, not the whole screen.
For browser flows that already use CDP, pass `scope=browser` to capture the active page
viewport first; this avoids grabbing the wrong OS monitor on multi-monitor Wayland sessions.

Run `kvm://host/doctor/query/report` to see what is available and what to install.

## Install

```bash
pip install urirun-connector-kvm                 # core (degrades to whatever is present)
pip install "urirun-connector-kvm[full]"         # + mss, Pillow, pynput, pytesseract
```

Optional extras per platform/capability: `capture` (mss+Pillow), `portal`
(PyGObject+dbus-python, Wayland), `input` (pynput), `ocr` (pytesseract), `windows`
(pygetwindow). System tools used when present: `ydotool`+`ydotoold`, `wmctrl`,
`grim`/`scrot`, ImageMagick, and on Wayland `python3-gobject`+`python3-dbus` for portal
capture.

## Choosing a surface (read this first)

The hardest possible way to automate a GUI is **driving pixels of a live GNOME/Wayland
desktop** — and on multi-monitor + fractional-HiDPI it does **not** work reliably. Three
independent walls hit at once: (1) screenshot pixels don't map to a fixed input coordinate
(multi-output + fractional scaling + fluctuating resolution); (2) Wayland forbids
focus-stealing, so synthetic keys go to whatever window is *active*, not your target; (3)
selecting the right element needs a real grounding model, not OCR. The fix is not to fight
these — it's to **execute in the same space you observe in**, so coordinates and focus stop
being a problem *by construction*:

| Target | Recommended surface | Why coords + focus are solved |
| --- | --- | --- |
| **Web app** (LinkedIn, forms, e-commerce) | **CDP / Playwright** — drive the browser, not the screen | viewport coords are 1:1, focus is the browser's, selection is the DOM (often no vision at all) |
| **Native app, live & visible** | **RemoteDesktop portal** (`org.freedesktop.portal.RemoteDesktop` + ScreenCast) | `NotifyPointerMotionAbsolute` uses the *same stream space* as capture → screenshot pixel == click point, by construction; can take focus |
| **Native app, headless/CI** | **virtual display** (Xvfb / headless wlroots, e.g. `vdisplay`) | one fixed-resolution "monitor", no accel, full focus, 1:1 clicks |
| **X11 / single-monitor / no fractional HiDPI** | the OS-level `kvm://` routes below | here pixels *do* map 1:1, so the raw input routes are fine |

### Web (recommended): CDP/Playwright with a persistent profile

Proven end-to-end (LinkedIn post as logged-in user, no coordinates, no vision):

```python
from playwright.sync_api import sync_playwright
prof = "/path/to/copied-chrome-auth"   # copy Local State + Default/{Cookies,Network/Cookies,Login Data,Preferences,Web Data}
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        prof, channel="chrome",
        args=["--password-store=gnome-libsecret"])   # ← decrypts keyring-encrypted cookies (li_at)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.linkedin.com/feed/")
    page.get_by_text("Start a post").first.click()                 # DOM selector, not pixels
    page.locator("div.ql-editor[contenteditable]").first.click()
    page.keyboard.type("…post text…")
    # HITL: screenshot for approval, then page.get_by_role("button", name="Post").click()
```

Key gotcha: a **headless** Chrome or `--password-store=basic` uses a throwaway key and the
copied cookies won't decrypt → you land on `/login`. Use `--password-store=gnome-libsecret`
(needs the session keyring). See `examples/47-nl-desktop-control/`.

## OS-level pixel input on Wayland — known limitations (best-effort fallback)

The `screen/query/capture` + `input/command/*` + `abs/command/click` routes are correct on
**X11, single-monitor, and virtual displays**, and are the cross-platform fallback. On
**live GNOME/Wayland multi-monitor / fractional HiDPI they are unreliable** and were
*disproven* in practice: `ydotool` absolute is miscalibrated, relative is acceleration-
distorted, the uinput absolute device maps to one output (not the screenshot), the portal
resolution can change mid-session, `wmctrl` focus can't reach native-Wayland windows, and
synthetic keys land in the active window. Prefer a surface from the table above.

`capture` is the one piece that *does* work on Wayland: the `portal` backend calls
`org.freedesktop.portal.Screenshot` via a system python with `dbus`+`gi` (one-time grant).
Run `kvm://host/doctor/query/report` — it now detects Wayland/multi-monitor and warns when
OS-level pixel input is unreliable on the current session.

## License

Released under the terms in [LICENSE](LICENSE).

## Development

```bash
make test     # registry/dispatch unit tests + bindings validate
```
