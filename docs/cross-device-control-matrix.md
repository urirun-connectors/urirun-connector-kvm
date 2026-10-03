# Sterowanie urządzeniami i systemami przez urirun

## Wniosek

Nie istnieje jeden uniwersalny kanał programowy, który może kontrolować każde urządzenie,
każdy system, ekran logowania i firmware. Uniwersalne rozwiązanie powstaje przez wspólny
kontrakt urirun nad kilkoma **powierzchniami sterowania**, wybieranymi według możliwości
urządzenia.

Kolejność wyboru powinna być następująca:

1. natywne API urządzenia lub aplikacji (najbardziej deterministyczne),
2. semantyczne API interfejsu użytkownika,
3. agent pulpitu działający w sesji użytkownika,
4. RDP/VNC lub portal zdalnego pulpitu,
5. sprzętowy KVM: obraz HDMI/kamera oraz wejście USB HID.

Ostatnia warstwa jest jedyną ogólną drogą do BIOS/UEFI, instalatora systemu i urządzenia,
na którym nie można uruchomić agenta. Nadal wymaga jednak zgodnego wyjścia obrazu oraz
obsługi klawiatury/myszy HID przez urządzenie docelowe.

## Wspólny kontrakt

Każda powierzchnia powinna implementować ten sam minimalny zestaw operacji:

```text
doctor()                 -> możliwości, uprawnienia, geometria, ograniczenia
capture()                -> klatka + width + height + coordinateSpaceId
click(x, y, spaceId)     -> akcja w tej samej przestrzeni co capture
move(x, y, spaceId)
scroll(dx, dy)
key(combo)
type(text)
verify(expectation)      -> nowa klatka lub stan semantyczny po akcji
```

Najważniejszy warunek bezpieczeństwa i poprawności:

> `capture-space == action-space` — punkt `(x, y)` z przechwyconej klatki musi oznaczać
> dokładnie ten sam punkt dla kliknięcia.

Identyfikator przestrzeni powinien zmieniać się po zmianie rozdzielczości, obrotu,
skalowania, układu monitorów lub sesji. Akcja ze starym identyfikatorem ma zakończyć się
błędem zamiast kliknąć w nieznane miejsce.

## Macierz platform

| Cel | Zalecana powierzchnia | Stan w workspace | Najważniejsze ograniczenie |
| --- | --- | --- | --- |
| Linux X11 | `kvm://` capture + xdotool/uinput | dostępne | agent musi działać w aktywnej sesji |
| Linux Wayland | XDG RemoteDesktop + ScreenCast/libei | częściowo: capture i uinput działają; wspólna sesja portalu do wdrożenia | portal wymaga zgody; niezależne capture/uinput są ryzykowne przy HiDPI i wielu monitorach |
| Windows | DXGI Desktop Duplication + SendInput + UI Automation | podstawowe mss/Pillow/pynput są dostępne; natywny adapter do wdrożenia | UAC/secure desktop i UIPI ograniczają programowe wejście |
| macOS | ScreenCaptureKit + Quartz/Accessibility | podstawowe `screencapture`/pynput są dostępne; natywny adapter do wdrożenia | wymagane Screen Recording i Accessibility |
| przeglądarka | CDP | dostępna powierzchnia CDP | tylko zawartość przeglądarki uruchomionej z debuggingiem |
| pulpit VNC/noVNC | bezpośredni RFB | dostępne `vnc/query/*` i `vnc/command/*` | serwer VNC i dane dostępowe |
| Android | ADB + UI Automator | ADB capture/tap/swipe/key/text/app/fs dostępne; UI Automator do wdrożenia | debugging i autoryzacja hosta; ekran zablokowany/polityki urządzenia |
| iOS/iPadOS | XCUITest/WebDriverAgent na sparowanym Macu | brak connectora | nie jest to ogólny, bezobsługowy kanał sterowania całym urządzeniem |
| urządzenie bez GUI | HTTP/SSH/MQTT/USB/serial | osobne connectory urirun | zależy od API i uwierzytelnienia urządzenia |
| BIOS/UEFI/instalator/lock screen | sprzętowy obraz + USB HID | prototypy CyberMysz/cliKVM są w workspace; adapter urirun do wdrożenia | potrzebny fizyczny tor obrazu i wejścia; brak semantyki UI |
| telefon bez ADB/XCUITest | kamera/capture + HID/touch adapter właściwy dla modelu | rozwiązanie urządzeniowo-specyficzne | mysz USB nie jest równoważna dotykowi na każdym telefonie |

## Co jest obecnie sprawdzone

Na Lenovo z GNOME/Wayland sprawdzono end-to-end:

- `kvm://laptop/screen/query/capture` zwraca rzeczywistą klatkę 1920x1080,
- `kvm://laptop/input/command/move` porusza kursorem do współrzędnych tej klatki,
- trasy `wait`, `key` i `move` są rozwiązywane do właściwych handlerów,
- brak geometrii ekranu zatrzymuje akcję absolutną zamiast wysłać niejednoznaczne
  współrzędne uinput.

Ten wynik potwierdza sterowanie testowanym laptopem. Nie potwierdza jeszcze niezawodności
programowego piksela na dowolnej konfiguracji Wayland. Dla wielu monitorów lub skalowania
ułamkowego należy wdrożyć jedną sesję XDG RemoteDesktop/ScreenCast i związać wejście z tym
samym strumieniem.

## Następne adaptery

### 1. `surface-xdg-remotedesktop`

Jedna sesja portalu powinna utworzyć ScreenCast oraz RemoteDesktop, zachować
`mapping_id`, logiczny rozmiar strumienia i przekazywać wejście przez libei/portal.
Pozwoli to spełnić wspólny kontrakt na GNOME/KDE Wayland bez ręcznej kalibracji.

### 2. `surface-windows-native`

- capture: DXGI Desktop Duplication,
- input: SendInput z raportem integrity level,
- semantyka: UI Automation,
- `doctor`: wykrywanie UAC/secure desktop, wielu monitorów, DPI i sesji RDP.

Akcje muszą kończyć się jawnie jako niedostępne, gdy pulpit ma wyższy poziom integralności.

### 3. `surface-macos-native`

- capture: ScreenCaptureKit,
- input: Quartz events,
- semantyka: Accessibility API,
- `doctor`: stan Screen Recording i Accessibility przed pierwszą akcją.

### 4. rozszerzenie `urirun-connector-adb`

Obok tras pikselowych należy dodać semantyczne:

```text
adb://<node>/ui/query/tree
adb://<node>/ui/query/find
adb://<node>/ui/command/click
adb://<node>/ui/command/set-text
```

Każdy capture i tap powinien nieść bieżący obrót, rozmiar powierzchni i
`coordinateSpaceId`.

### 5. `urirun-connector-ios`

Connector powinien sterować sesją XCUITest/WebDriverAgent na sparowanym Macu i jasno
raportować, że jest to automatyzacja aplikacji/testów, a nie odpowiednik ADB dla całego iOS.

### 6. `surface-hardware-kvm`

Należy opakować istniejący tor CyberMysz/cliKVM stabilnymi trasami:

```text
kvm+hid://<station>/screen/query/capture
kvm+hid://<station>/input/command/key
kvm+hid://<station>/input/command/type
kvm+hid://<station>/input/command/move
kvm+hid://<station>/input/command/click
kvm+hid://<station>/doctor/query/report
```

Stacja musi przechowywać kalibrację obrazu do zakresu absolutnego HID, identyfikator
podłączonego capture i dongla, fizyczny STOP oraz dziennik akcji. To jest docelowa warstwa
awaryjna dla systemów nieznanych, niedostępnych programowo i ekranów przed startem OS.

## Reguły wyboru powierzchni

Router powinien wybierać najwyższą dostępną warstwę, ale nigdy cicho nie przechodzić z
semantycznej operacji do ryzykownego kliknięcia pikselowego. Plan akcji powinien zawierać:

```json
{
  "target": "laptop",
  "surface": "xdg-remotedesktop",
  "coordinateSpaceId": "stream-42:1920x1080:scale-1",
  "action": "click",
  "verify": {"screenChanged": true},
  "fallbackAllowed": ["vnc", "hardware-kvm"]
}
```

Przed wykonaniem wymagane są uwierzytelnienie, minimalna polityka URI, limit czasu,
tryb STOP oraz możliwość odtworzenia logu. Węzeł wystawiony w sieci nie powinien mieć
jednocześnie niechronionego `/run` i polityki `**`.

## Źródła platformowe

- Linux: [XDG ScreenCast](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html),
  [XDG RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- Windows: [Desktop Duplication API](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api),
  [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput),
  [UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiautocore-overview)
- macOS: [ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit/),
  [AXIsProcessTrustedWithOptions](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)
- Android: [ADB](https://developer.android.com/tools/adb),
  [UI Automator](https://developer.android.com/training/testing/other-components/ui-automator)
- iOS: [XCTest](https://developer.apple.com/documentation/xctest),
  [XCUIAutomation](https://developer.apple.com/documentation/xcuiautomation)
- Chrome: [chrome.debugger / CDP](https://developer.chrome.com/docs/extensions/reference/api/debugger)
