from __future__ import annotations

from urirun_connector_kvm.vnc import combo_on, type_on


class RecordingClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def keyDown(self, key: str) -> None:  # noqa: N802 - vncdotool API
        self.events.append(("down", key))

    def keyPress(self, key: str) -> None:  # noqa: N802 - vncdotool API
        self.events.append(("press", key))

    def keyUp(self, key: str) -> None:  # noqa: N802 - vncdotool API
        self.events.append(("up", key))


def test_combo_normalizes_named_key_for_vncdotool() -> None:
    client = RecordingClient()

    result = combo_on(client, "Return")

    assert result == {"combo": "Return", "via": "rfb"}
    assert client.events == [("press", "return")]


def test_combo_normalizes_modifier_and_literal_case() -> None:
    client = RecordingClient()

    combo_on(client, "CTRL+SHIFT+L")

    assert client.events == [
        ("down", "ctrl"),
        ("down", "shift"),
        ("press", "l"),
        ("up", "shift"),
        ("up", "ctrl"),
    ]


def test_text_typing_preserves_literal_case() -> None:
    client = RecordingClient()

    type_on(client, "Ab\n")

    assert client.events == [("press", "A"), ("press", "b"), ("press", "enter")]
