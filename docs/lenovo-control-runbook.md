# Runbook — sterowanie komputerem Lenovo (.201) przez urirun

> Stan ponownie zweryfikowany **2026-07-20**. To jest „złoty zapis" CAŁEGO stacku, którym
> dziś steruje się laptopem Lenovo — handlery, trasy, komendy deploy, wymagania
> i ścieżki sesji. Docelowo cały ten stack ma zastąpić jeden connector `kvm`
> (patrz [`ARCHITECTURE-cross-platform-backends.md`](ARCHITECTURE-cross-platform-backends.md)).

## 1. Tożsamość node'a

| co | wartość |
| --- | --- |
| host (orkiestrator) | `nvidia` |
| node | `http://192.168.188.201:8765` |
| nazwa w URI | `laptop` (node serwuje `*://laptop/...`) |
| sesja | Wayland / GNOME (Mutter) |
| `/run` auth | **wyłączone** (`requireRunAuth=false`) — `/run` bez tokenu |
| kanał deploy | tylko mesh urirun (surowy SSH zablokowany polityką) |

Node **nie ma** `WAYLAND_DISPLAY`/`DISPLAY` w env procesu. Zrzut portalowy działa mimo
to (przez DBus); narzędzia wejścia wymagają `WAYLAND_DISPLAY` — handler ustawia
`wayland-0` w `_env()`.

Aktualny deploy jednego connectora wykonuje `scripts/redeploy_node.sh`. Węzeł ma 115 tras
(w tym dokładne aliasy `host` i `laptop`), a sprawdzone backendy to Mutter/PipeWire dla
capture oraz uinput dla klawiatury i myszy. Poniższy opis dwóch historycznych powierzchni
pozostaje przydatny dla diagnostyki i migracji, ale nie jest już zalecaną metodą deployu.

## 2. Dwie powierzchnie tras (muszą współistnieć)

Node serwuje JEDNĄ powierzchnię — `--merge` przy deployu potrafi cicho wyprzeć drugą
(tak zniknęły kiedyś CDP/portal). Po każdym deployu sprawdź `host probe`, że żyją OBIE.

### Powierzchnia A — przeglądarka (CDP) + zrzut ekranu (portal)
Bindings: `.urirun/flows/lenovo-thunderbird/restore_bindings.json`
Kod: `urirun-connector-browser-control/examples/cdp-flat-handler.py` + `examples/39-browser-observe/gillm_capture.py`

| URI | export | rola |
| --- | --- | --- |
| `browser://laptop/cdp/session/command/launch` | `cdp-flat-handler:launch` | start Chrome z portem CDP |
| `browser://laptop/cdp/page/command/navigate` | `:nav` | nawigacja |
| `browser://laptop/cdp/page/query/eval` | `:eval_js` | eval JS w stronie |
| `browser://laptop/cdp/page/query/screenshot` | `:screenshot` | PNG strony przez CDP |
| `browser://laptop/cdp/page/query/tabs` | `:tabs` | lista kart |
| `screen://laptop/portal/query/capture` | `gillm_capture:capture` | historyczny zrzut całego ekranu przez `org.freedesktop.portal.Screenshot` (DBus+GLib, `interactive=False`); aktualny connector ma też sprawdzony capture Mutter/PipeWire |

### Powierzchnia B — wejście klawiatura/mysz + uruchamianie aplikacji
Bindings: `.urirun/flows/lenovo-thunderbird/tb_bindings.json`
Kod: `.urirun/flows/lenovo-thunderbird/tb_handler.py`

| URI | export | rola |
| --- | --- | --- |
| `kvm://laptop/diag/query/which` | `tb_handler:probe` | **introspekcja zdolności**: jakie narzędzia capture/input są, czy `ydotoold` żyje, app_count, thunderbird? |
| `app://laptop/desktop/query/list` | `:list_apps` | katalog aplikacji z wpisów XDG `.desktop` (przestrzeń akcji dla launch) |
| `app://laptop/desktop/command/launch` | `:launch` | uruchom app **przez wpis `.desktop`** (Flatpak/Snap/PATH), opcjonalnie `-compose` |
| `kvm://laptop/input/command/type` | `:type_text` | wpisz tekst (`wtype`, fallback `ydotool type`) |
| `kvm://laptop/input/command/key` | `:key` | klawisz/akord, np. `ctrl+s`, `Tab`, `Return` (`wtype -M/-k/-m`, fallback `ydotool key`) |
| `shell://laptop/command/exec` | `:exec_cmd` | **RCE** (`subprocess shell=True`) — `safe:false`, wymaga jawnego `--allow 'shell://**'` |

> Dlaczego XDG, nie `which`: `which thunderbird` zwraca null (Thunderbird = Flatpak
> `net.thunderbird.Thunderbird`, brak binarki na PATH). Wpis `.desktop` rozwiązuje go
> tak jak systemowa wyszukiwarka aplikacji. Patrz memory `desktop-app-launch-xdg`.

## 3. Deploy (z hosta nvidia, BEZ SSH)

```bash
cd /home/tom/github/if-uri/urirun

# --- Powierzchnia B (input + launch) ---
.venv/bin/urirun host deploy http://192.168.188.201:8765 \
  --bindings .urirun/flows/lenovo-thunderbird/tb_bindings.json \
  --code     .urirun/flows/lenovo-thunderbird/tb_handler.py \
  --allow 'app://**' --allow 'kvm://**' --allow 'browser://**' --allow 'screen://**' \
  --merge --identity ~/.ssh/id_ed25519

# --- Powierzchnia A (CDP + portal) — odtworzenie, gdy --merge ją wyprze ---
.venv/bin/urirun host deploy http://192.168.188.201:8765 \
  --bindings .urirun/flows/lenovo-thunderbird/restore_bindings.json \
  --code ../urirun-connector-browser-control/examples/cdp-flat-handler.py \
  --code ../examples/39-browser-observe/gillm_capture.py \
  --allow 'browser://**' --allow 'screen://**' --identity ~/.ssh/id_ed25519
```

**Auth (raz):** `urirun host copy-id http://192.168.188.201:8765 --identity ~/.ssh/id_ed25519`
— idempotentne (`ok=true`, `keyCount=1`). Wymaga `cryptography` w venv.
Deploy NIE może czyścić `authorized_keys` (kiedyś replace-deploy wyzerował klucz i
zablokował node). Auth ⟂ rejestr.

## 4. Wymagania NA lenovo (.201)

| pakiet | po co | status (2026-06-23) |
| --- | --- | --- |
| `python3-dbus`, `python3-gobject` | zrzut portalowy | ✅ obecne (GNOME) |
| uinput / `ydotool` | klawiatura/mysz (Wayland) | ✅ uinput i ydotool dostępne; sprawdzono ruch absolutny i klawisz Shift |
| Thunderbird | flow draftu | ✅ jako Flatpak `net.thunderbird.Thunderbird` |

Przed akcją absolutną connector odczytuje geometrię z wyniku capture lub nagłówka PNG.
Jeżeli geometria jest nieznana, akcja kończy się błędem zamiast wysłać surowe współrzędne
uinput.

## 5. Weryfikacja

```bash
# obie powierzchnie żyją?
.venv/bin/urirun host probe http://192.168.188.201:8765

# zdolności wejścia/capture na node:
curl -s -X POST http://192.168.188.201:8765/run -H 'Content-Type: application/json' \
  -d '{"uri":"kvm://laptop/diag/query/which","payload":{}}' | python3 -m json.tool
# oczekiwane: wtype lub ydotool != null, ydotoold_running=true (jeśli ydotool)
```

## 6. Flow / planowanie

| plik | rola |
| --- | --- |
| `.urirun/flows/lenovo-thunderbird-draft.yaml` | statyczny: otwórz Thunderbird → wpisz → zapisz draft, z weryfikacją zrzutami |
| `.urirun/flows/lenovo-autonomous-daily-work.yaml` | autonomiczna praca dzienna |
| `.urirun/flows/lenovo-ai-closed-loop.yaml` | pętla zamknięta capture→LLM→akcja |

```bash
# wykonanie flow + zapis zrzutów do artefaktów:
.venv/bin/urirun host flow run .urirun/flows/lenovo-thunderbird-draft.yaml \
  --config ~/.urirun/mesh.json --execute \
  --artifact-dir ~/.urirun/artifacts/thunderbird-draft

# wariant NL (Gemini sam układa plan nad żywą przestrzenią akcji):
URIRUN_DOTENV=1 .venv/bin/urirun host ask \
  "otwórz thunderbird na laptop, napisz draft do zespol@example.com i zapisz" \
  --config ~/.urirun/mesh.json --env-file .env
```

## 7. Gdzie urirun trzyma stan (do inspekcji)

| co | ścieżka |
| --- | --- |
| konfig hosta | `~/.urirun/mesh.json` |
| stan hosta | `~/.urirun/` — `errors.jsonl`, `host.db`, `<node>/session/`, `artifacts/` |
| node-local (repo) | `<repo>/.urirun/` — `flows/`, `reports/`, `scheme-index.json` |
| artefakty flow | katalog z `--artifact-dir` |

## 8. Mapa: ten stack → docelowy connector `kvm`

| dzisiaj (stack) | docelowo (connector kvm, jeden pakiet, każdy OS) |
| --- | --- |
| `gillm_capture:capture` (portal) | backend `portal` trasy `kvm://…/screen/query/capture` |
| connector kvm `capture` (grim/scrot) | backendy `grim`/`scrot`/`mss` tej samej trasy |
| `tb_handler:type_text` | `kvm://…/input/command/type` (backendy `wtype`/`ydotool`/`pynput`) |
| `tb_handler:key` (akordy) | `kvm://…/input/command/key` (akordy w backendach) |
| connector kvm `move` | `kvm://…/input/command/move` (+ `click`) |
| `tb_handler:list_apps` / `launch` | `app://…/desktop/query/list` + `command/launch` (backendy XDG/Win/mac) |
| `tb_handler:probe` (which) | `kvm://…/host/query/facts` (introspekcja zdolności) |
| `cdp-flat-handler:*` | zostaje w `urirun-connector-browser-control` (osobna domena) |

Szczegóły konsolidacji i wzorzec dekoratorów: [`ARCHITECTURE-cross-platform-backends.md`](ARCHITECTURE-cross-platform-backends.md).
