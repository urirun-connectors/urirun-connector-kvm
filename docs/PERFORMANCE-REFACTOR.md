# Wydajność sterowania przez mesh — dlaczego wolno i plan refaktoryzacji

Dokument roboczy do refaktoryzacji. Wszystkie liczby **zmierzone na żywo** na węźle
lenovo (`http://192.168.188.201:8765`, GNOME-Wayland, LAN, RTT <5 ms) podczas sesji
automatyzacji testowego portalu. Cel: wiedzieć **gdzie idzie czas** i **co realnie skróci**,
zanim tkniemy kod.

## TL;DR — dlaczego pętla zadania trwa dziesiątki sekund

Każda operacja URI płaci **stały narzut zimnego startu**, a percepcja dokłada **wolny OCR**:

| składnik | koszt | dlaczego |
|---|---|---|
| round-trip HTTP `POST /run` | ~kilka ms | LAN, pomijalne |
| **spawn izolowanego subprocesu** | **~290 ms / wywołanie** | `isolated=True` forkuje świeży interpreter + re-import connectora |
| **grab ekranu (mutter-screencast)** | **~800 ms / zrzut** | teardown+init strumienia przy KAŻDYM zrzucie (brak ciepłego streamu) |
| **OCR (tesseract, pełny 3200×3800)** | **~5–8 s** | CPU, cały ekran; `max_width=1600` → ~4,6 s bez utraty trafności |
| akcja (type/key) w subprocesie | szybka | koszt to głównie spawn powyżej |

Złożenie na typową pętlę **percepcja→akcja**:
- `settle()` = kilka zrzutów dhash → `N × (290 + 800)` ≈ **3,7 s**,
- jedna kotwica `ui/query/verify` = capture + OCR ≈ `290 + 800 + 4600` ≈ **5,7 s**,
- sekwencja akcji osobno = `N × (290 + praca)`; batch = `290 + N × praca`.

Do tego **retry przy flaky kompozytorze** (brak kontroli DOM) → pojedyncze zadanie potrafi
zająć 15–30 s. Zmierzone end-to-end „otwórz kompozytor → zweryfikuj → wpisz": **24,2 s** starym
sposobem vs **8,6 s** nowym (batch + verify_texts) = **2,82×** — przy czym stary nawet nie
otworzył kompozytora, więc realny zysk przy tej samej pracy jest większy.

## Zmierzone liczby (baseline)

```
input/key (trywialny op)            ~290 ms      ← spawn isolated-subprocess dominuje
task_run 3–6 kroków (1 wywołanie)   ~360 ms      ← jeden spawn na całą sekwencję
capture scope=all                   1193 ms, 1,0 MB (3200×3800)
capture max_width=480               ~810 ms, 73 KB   ← podłoga grabu, nie transfer
OCR pełny                           8632 ms, 1049 znaków
OCR max_width=1600                  4604 ms, 1002 znaków   ← sweet spot (bez utraty kotwic)
OCR max_width=900                   2884 ms, 285 znaków    ← za mało (traci trafność)
ui/query/verify (1 kotwica)         5748 ms      ← capture+OCR za KAŻDYM razem
settle()                            ~3679 ms     ← kilka zrzutów × podłoga 800 ms
```

## Cztery korzenie problemu

1. **Wszystko startuje na zimno.** Brak ciepłych procesów: izolowany subprocess na każde
   wywołanie (290 ms), re-init screencast na każdy zrzut (800 ms), tesseract ładowany od zera.
2. **Gadatliwy protokół.** Jedno `POST /run` + jeden subprocess na op. Sekwencja 6 kroków =
   6× narzut, zamiast raz.
3. **Ciężka percepcja.** Pełnoekranowy zrzut + pełnoekranowy OCR CPU. OCR to teraz *dominujący*
   koszt pętli.
4. **Brak kontroli DOM.** Bez CDP/pluginu sterowanie idzie przez wizję/OCR/klawiaturę — wolne
   **i** kruche (fokus pola, otwarcie kompozytora niekonsekwentne) → retry mnożą czas.

## Co już zrobione (po stronie klienta, `vguard.py`) — 3–7×

| technika | zysk | mechanizm |
|---|---|---|
| `batch()` → `task_run` | **6,9×** na sekwencjach | N kroków w jednym subprocesie/round-tripie |
| `verify_texts()` | **3,8×** na percepcji | 1 capture + 1 OCR sprawdza WIELE kotwic (zamiast N×) |
| OCR `max_width=1600` | **1,9×** | mniej pikseli, bez utraty trafności |
| małe zrzuty dhash/settle | 14× mniej danych | `max_width=480` do detekcji zmiany |
| `guarded_batch()` | pewność | batch + postcond `verify_texts` + retry |
| **OCR na HOŚCIE przez base64** (2026-07-05) | **OCR 4,6 s → 0,45 s (10×)**; kotwica 4,6–5,7 s → **0,85–2,6 s** | zrzut wraca base64 po LAN (~270 KB @1600, grosze), OCR liczy 16-rdzeniowy host zamiast laptopa |
| cache OCR po dhash (2026-07-05) | ekran statyczny: percepcja ≈ sam capture (~0,9 s) | `verify_texts` pomija OCR, gdy dhash klatki ≈ poprzedni |
| `user_active()` + `respect_user` (2026-07-05) | bezpieczeństwo | `guarded_batch` NIE przejmuje klawiatury, gdy ekran żyje (ktoś pracuje na maszynie) |

**Zmierzone 2026-07-05 (żywy węzeł lenovo):** `verify_texts` 3 kotwice: host-OCR zimny **2,6 s**,
z cache **0,87 s**, `anchor()` **0,85 s**, region-crop **0,87 s** — vs node-side `ui/query/verify`
**4,6 s za JEDNĄ kotwicę**. Wniosek architektoniczny: **transport base64 + moc obliczeniowa hosta
bije OCR na węźle o rząd wielkości** — węzeł ma tylko TANIO patrzeć (capture), liczyć ma host.
To de-priorytetyzuje easyocr NA WĘŹLE (Tier 2): host-tesseract przez base64 daje już 0,45 s;
paddle/easyocr na HOŚCIE (RTX 4060) to dalsza rezerwa — uwaga: paddle PP-OCRv6_medium na CPU
z `enable_mkldnn=False` = **14,5 s** (zmierzone), do tej pętli się NIE nadaje; tylko mobile-modele
albo GPU.

**Potwierdzone 2026-07-05:** crop DZIAŁA po redeployu connectora na .201 (`crop:{x,y,w,h}`,
22 KB zamiast 270 KB) — poprzedni negatywny wynik to była stara wersja na węźle, zgodnie z diagnozą.
Redeploy po restarcie węzła (merge-deploy nie przeżywa restartu; routeCount 7 → 55):
bindings z `urirun_bindings()` z refami spłaszczonymi (`urirun_connector_kvm.core` → `core`), potem
`urirun host deploy http://…:8765 --bindings b.json --allow 'kvm://**' --allow 'app://**'
--code <wszystkie moduły .py> --merge --persist --identity ~/.ssh/id_ed25519`.

**Wyczerpane po stronie klienta.** Dalsze skoki wymagają zmian w connectorze/runtime.

## Plan refaktoryzacji (connector kvm + runtime) — kolejność wg zysk/koszt

### Tier 1 — ciepły screencast — ✅ WDROŻONE 2026-07-05 (`capture_worker.py`)
Długożyjący worker (system-python: dbus+gi+gst) negocjuje sesję ScreenCast RAZ, trzyma węzeł
pipewire otwarty i serwuje klatki po unix-sockecie; żądanie = mini-pipeline
`pipewiresrc→(videoscale)→pngenc` na gotowym strumieniu. Backend `mutter-warm` (prio 99):
pierwszy strzał spawnuje workera i spada do zimnego muttera, kolejne biją w socket; worker
sam się kończy po 120 s bezczynności (wskaźnik „udostępniania ekranu" nie wisi wiecznie).
- **Zmierzone (lenovo, end-to-end przez /run):** capture@1600: **1850 → ~730 ms**; sam grab
  na ciepłym strumieniu **grabMs=108–123 ms** (telemetria w odpowiedzi). Skalowanie do
  max_width robi gst W WORKERZE (4× mniej pikseli do pngenc; PIL na węźle już nie skaluje).
- **Pełna percepcja po Tier 1 + host-OCR:** `verify_texts` zimno **1,9 s**, z cache dhash
  **0,76 s** (bylo 4,6–5,7 s za jedną kotwicę).
- **Stale-worker po redeployu:** żywy worker sprzed deployu serwuje starym kodem, dopóki
  żądania odświeżają mu idle-timer — rozwiązane handshakiem `PROTO` (backend wykrywa
  niezgodność, unlinkuje socket, respawnuje).
- **Audyt live 2026-07-20:** warm path zwrócił klatkę 640×480 przy geometrii negocjowanego
  ScreenCast 1920×1080 (inny/stary PipeWire source). Backend sprawdza teraz rozmiar PNG względem
  `srcSize`, odrzuca taki wynik i przechodzi do zimnego capture. Zmierzone ~55–63 ms nie może być
  traktowane jako poprawne przyspieszenie, dopóki worker nie zachowa deskryptora/remote właściwej
  sesji portalowej i test nie potwierdzi tożsamości źródła, nie tylko poprawnego pliku PNG.
- **Pozostały koszt (~600 ms):** spawn izolowanego subprocessu na węźle + koperta HTTP/base64
  — to jest Tier 3 (de-izolacja tanich handlerów), teraz największy pozostały lever percepcji.

### Tier 2 — szybszy OCR (drugi lever percepcji)
Tesseract CPU pełnoekranowy = 4,6 s. Opcje (najlepiej łącznie):
- **easyocr** (env na .201: `easyocr=false`) → OCR z ~4,6 s na <1 s. **Backend już ISTNIEJE
  w repo**: `backends.py:1372` `_EASYOCR_READER` (ciepły reader cache'owany modułowo,
  `gpu=False`), a `core.py:1166` `_VNC_LOCATE_ORDER` stawia easyocr przed tesseractem.
  Do zrobienia tylko: `pip install easyocr` w venv connectora na węźle (manage-install),
  rozgrzać reader przy starcie node (pierwsze wywołanie ładuje model ~10 s), sprawdzić że
  ścieżka `ui/query/*` też go preferuje. Ryzyko: waga zależności (torch).
- **naprawić crop** — capture na .201 ignoruje `crop_*`/`monitor` (zmierzone: identyczny
  wynik dla wszystkich parametrów). **Kod croppingu JEST w repo**: `core.py:142`
  `_apply_capture_postprocessing` (cx/cy/zoom/crop_w/crop_h/max_width) — czyli na .201 działa
  stara wersja connectora albo trasa gubi parametry. Akcja: redeploy connectora (signed-deploy)
  + **test kontraktowy round-tripu parametrów capture** (payload → pole `crop` w odpowiedzi),
  żeby regresja była łapana. OCR regionu kotwicy (½ pikseli) → ~2×.
- **tuning tesseract** — `--psm 6`, `--oem 1`, grayscale, mniejszy region.
- **Zysk:** percepcja `verify_texts` z ~4,8 s na <1–2 s.
- **Gdzie:** connector ocr (`ocr://…/image/query/text`) + capture crop w kvm.

### Tier 2b — serwerowe `verify_many` / `guarded_task_run` + cache OCR po dhash
Dziś `vguard.verify_texts` = 2 round-tripy (capture → ocr) i zakłada, że plik zrzutu widzi
`ocr://host` na tym samym węźle; `guarded_batch` płaci 2×N round-tripów za retry.
- **Nowa trasa** `kvm://host/ui/query/verify_many` (payload `{"texts": [...], "max_width": 1600,
  "region": {...}}`): capture+OCR+match w JEDNYM wywołaniu po stronie węzła, zwraca
  `{tekst: bool}` — zero PNG po sieci. Analogicznie `input/command/guarded_task_run`
  (steps + expect + tries) — retry bez pełnego cyklu klient↔węzeł.
- **Cache OCR kluczowany dhash:** przed OCR policz dhash klatki; hamming ≤ próg vs poprzednia →
  zwróć zcache'owany tekst. Pętle settle/verify na statycznym ekranie przestają płacić za OCR.
  Naturalne miejsce: ten sam ciepły worker co Tier 1.
- **Zysk:** ~2× na percepcji (round-tripy) + prawie darmowe pętle na statycznym ekranie.

### Tier 3 — de-izolacja tanich handlerów — ✅ WDROŻONE 2026-07-05 (percepcja)
`screen/query/capture` + `display/query/info` przeszły na `isolated=False` (adapter
`local-function`, in-process w węźle — executor jest w v1.EXECUTORS, żadnych zmian
node-side). Obie trasy są read-only, a ciężar i tak niesie subprocess warm-workera.
Wejście (uinput/ydotool) ZOSTAJE izolowane — crash nie może zdjąć węzła; test
`test_bindings_are_isolated_handlers` pinuje OBIE strony kontraktu.
- **Zmierzone (lenovo):** capture@1600 **730 → 462–498 ms**; capture@480 (sonda
  dhash/settle) **184 ms**; kotwica/verify-cached **~520 ms**; settle 3,7–4,9 s → **1,7 s**.
- **Uwaga hot-reload:** in-process moduł jest cache'owany w procesie węzła; deploy
  zdejmuje go z sys.modules (`_write_pushed_code`), więc kolejne wywołanie importuje
  świeży kod — ale ALIASY `urirun_connector_kvm.*` odtwarza dopiero `_adopt_flat_siblings`
  przy imporcie core.
- **Reszta (~350 ms) — ROZWIĄZANE 2026-07-05 (commit 772060a):** to nie była koperta,
  tylko UKRYTY re-encode: `_apply_capture_postprocessing` robił bezwarunkowe
  `im.save(format="PNG", optimize=True)` na KAŻDEJ klatce (wolna ścieżka PIL; po cichu
  konwertował też jpeg workera z powrotem na większy PNG). No-op nie dotyka już pliku.
  Do tego `fmt=jpeg` (gst jpegenc q=85, PROTO 3) przez całą ścieżkę; vguard używa jpeg.
  **Finał (lenovo, end-to-end):** capture@1600 jpeg **91–112 ms** (rano 1850 — **20×**),
  png 161–173 ms, sonda dhash@480 **107 ms**, kotwica z cache **127 ms**; zimna
  weryfikacja = już tylko koszt OCR hosta (~0,5–1,7 s zależnie od gęstości ekranu).
- **Opcja B (pool `--pool`):** node serve ma gotowy HandlerPool (urirun_runtime.worker),
  ale włącza go tylko flaga/config przy starcie — wymaga interwencji na lenovo; po niej
  także trasy izolowane (input) dostaną ciepły start.

### Tier 4 — kontrola DOM (pewność + prędkość dla web)
Bez CDP/pluginu sterowanie przeglądarką jest wolne i kruche (fokus, otwarcie kompozytora).
- **Rozwiązanie:** chrome-plugin (`browser://` po DOM) albo CDP (`--remote-debugging-port`).
- **Zysk:** klik/wpis po selektorze — **niezawodny**, bez OCR/zgadywania; `method_router` już to
  preferuje gdy `cdp.reachable|plugin`.
- **Blokada dziś:** plugin wymaga `Load unpacked` (kopiowanie plików blokuje zepsuty `fs`-write
  — patrz niżej), CDP wymaga restartu przeglądarki.

### Tier 5 — naprawić `fs://` write (odblokowuje transfer/plugin)
`fs://…/write_text`/`write-b64` zwraca `ok:True`, ale plik **znika** (isolated-write nie
utrwala się). Blokuje kopiowanie plików na węzeł (i tym samym instalację pluginu).
- **Zysk:** działający transfer host↔node (`uri-cp` + `archive/unpack-b64` już gotowe po stronie
  klienta) → odblokowuje Tier 4.
- **Gdzie:** handler fs — zapis musi trafiać na realny FS, nie do efemerycznego isolated.

## Higiena kodu przy refaktorze (warunek wejścia w Tier 1/2b)

- `backends.py` ma 1556 linii, `core.py` 1361 — wydzielając ciepły worker i trasy verify_many
  trzymać regułę **CC ≤ 15, extract-method** (gate: `tests/test_cc_gate.py`, `make complexity`).
- Sugerowany podział: `capture_worker.py` (Tier 1), `ocr.py` (tesseract/easyocr + cache dhash),
  `backends.py` zostaje rejestrem backendów + input.
- Kontrakt `navigate` jest lustrzany w 4 polyglot-peerach — zmiany kontraktów ruszać razem.

## Metodologia pomiaru (każdy krok udowodniony, nie zadeklarowany)

- **End-to-end:** to samo zadanie („otwórz kompozytor → zweryfikuj → wpisz → postcond") starą
  i nową ścieżką, `time.perf_counter` wokół całości, na portalu testowym na lenovo.
- **Mikrobenchmark prymitywu:** 3× wywołanie, mediana; zimną pierwszą próbę raportować osobno
  (po Tier 1/2 zimny start będzie jednorazowy, nie per-op).
- **Zysk liczy się tylko przy wykonanej pracy:** stary pomiar 24,2 s zrobił MNIEJ (nie otworzył
  kompozytora) — zawsze potwierdzać postcond (`verify_texts`) przed zapisaniem wyniku.

## Pułapki znane z sesji (nie odkrywać ponownie)

- **Lenovo to żywa maszyna usera** (2026-07-05: batch wpisał tekst w aktywną sesję Chrome).
  Każde przejęcie klawiatury/myszy poprzedzać `user_active()`; `settle()` wybijający timeout
  to często właśnie pracujący człowiek, nie „wolne ładowanie".
- Merge-deploy NIE przeżywa restartu węzła — objaw: `Route not found: kvm.screen.query`,
  routeCount spada do built-inów (7). Potwierdzone 2× (2026-07-05). ZAGADKA: `--persist`
  raportuje sukces (`persisted: ~/.urirun-node/registry.json`, allow w configu, kod w
  `~/.urirun-node/deploy/` — wszystko na dysku), a start węzła i tak ładuje tylko built-iny.
  Root-cause wymaga wglądu NA lenovo (czym startuje node: systemd/skrypt, jakie --registry/
  --config dostaje; podejrzenie: startuje bez configu/z innym cwd, więc nie widzi
  utrwalonego registry). MITYGACJA (wdrożona): `scripts/redeploy_node.sh` (jedna komenda,
  idempotentny merge, ~0,7 s) + samonaprawa w `vguard.Screen._run` — NOT_FOUND → redeploy
  → retry, raz na instancję.
- `import urirun` z cwd `~/github/if-uri` łapie FOLDER `urirun/` zamiast pakietu
  (`urirun has no attribute connector`) — skrypty deployu muszą `cd` do neutralnego katalogu.
- **Flat-deploy vs bundlowany pakiet (PRZYCZYNA „węzeł ma nowy core, stary backends"):**
  bundle urirun z PyPI zawiera własny (stary) `urirun_connector_kvm`, a flat-owe moduły
  preferowały import pakietowy — więc wypchnięty `backends.py` nigdy nie wygrywał. Naprawione
  w `core._adopt_flat_siblings()`: w trybie flat (`__package__` puste) flat-owe sąsiednie pliki
  są rejestrowane w `sys.modules` jako `urirun_connector_kvm.*` i stają się autorytatywne.
  Do tego: relative importy w `backends.py` (`from ._backends_uinput import …`) wysypywały CAŁY
  flat-import („attempted relative import") — każdy import międzymodułowy musi mieć fallback flat.
  Warning `capture_worker: No module named 'dbus'` przy deployu jest OCZEKIWANY (walidacja
  importu idzie w venv węzła; worker działa pod system-pythonem).
- Portal potrafi zwrócić placeholder <20 KB → traktować jako degraded i spaść do
  mutter-screencast/CDP (`_placeholder_guard`, `core.py:314`).
- `settle()` uznaje ekran za stabilny ZANIM pole dostanie fokus — postcond na wpisany tekst
  jest obowiązkowy (stąd `guarded_batch`).
- Nawigacja na URL identyczny z bieżącym to no-op — kompozytor nie otwiera się „świeżo",
  auto-fokus pola nie następuje.
- ydotoold jest transient; współrzędne logiczne 1600 (skalowanie HiDPI).
- vncdotool: jedna sesja na proces albo hang.
- CDP: Chrome startować z `about:blank` + `--remote-allow-origins`, inaczej navigate pada
  mimo działającego debuggera.

## Zasady, których trzymać się w refaktorze (lekcje sesji)

- **Perceive-once → decide/check-many.** Jeden zrzut/OCR karmi wiele sprawdzeń (`verify_texts`),
  nie N× capture+OCR.
- **Batch domyślnie.** Sekwencje jako `task_run`, nie N wywołań.
- **Ciepłe zamiast zimnego.** Stream, worker, model OCR — trzymać rozgrzane.
- **Wybór metody z profilu env, nie hardkod** (`method_router`): OCR/wizja/DOM zależnie od tego,
  co DZIAŁA i jak pewnie — [[verify-before-act]], [[lenovo-browser-control]].
- **Szybko ZAWSZE ze strażnikiem.** Prędkość bez postcond cicho zawodzi (type bez fokusu) —
  `guarded_batch`.

## Szacowany efekt refaktoru

Zakładając Tier 1+2+3 w connectorze:
- zrzut 800 → ~80 ms, OCR 4,6 s → ~1 s, op 290 → ~10 ms.
- Pętla percepcja→akcja z ~15 s (baseline) i ~8,6 s (klient) → **~1,5–2 s** (connector).
- Z Tier 4 (DOM): sterowanie web **niezawodne** i sub-sekundowe, bez retry.
