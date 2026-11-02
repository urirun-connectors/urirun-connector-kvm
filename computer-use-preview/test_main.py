# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
import sys
import types
from unittest.mock import patch, MagicMock

if "google" not in sys.modules:
    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    types_mod = types.ModuleType("google.genai.types")

    class _DummyPart:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyContent:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummySchema:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyType:
        OBJECT = "OBJECT"
        STRING = "STRING"
        NUMBER = "NUMBER"
        INTEGER = "INTEGER"
        BOOLEAN = "BOOLEAN"
        ARRAY = "ARRAY"

    class _DummyFunctionDeclaration:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyTool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyGenerateContentConfig:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    types_mod.Part = _DummyPart
    types_mod.Content = _DummyContent
    types_mod.Schema = _DummySchema
    types_mod.Type = _DummyType
    types_mod.FunctionDeclaration = _DummyFunctionDeclaration
    types_mod.Tool = _DummyTool
    types_mod.GenerateContentConfig = _DummyGenerateContentConfig
    genai_mod.types = types_mod
    genai_mod.Client = _DummyClient
    google_mod.genai = genai_mod
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod

import main
from computers.kvm.kvm import KvmComputer

class TestMain(unittest.TestCase):

    @patch('main.argparse.ArgumentParser')
    @patch('main.PlaywrightComputer')
    @patch('main.BrowserAgent')
    def test_main_playwright(self, mock_browser_agent, mock_playwright_computer, mock_arg_parser):
        mock_args = MagicMock()
        mock_args.env = 'playwright'
        mock_args.initial_url = 'test_url'
        mock_args.highlight_mouse = True
        mock_args.query = 'test_query'
        mock_args.model = 'test_model'
        mock_args.api_server = None
        mock_args.api_server_key = None
        mock_arg_parser.return_value.parse_args.return_value = mock_args

        main.main()

        mock_playwright_computer.assert_called_once_with(
            screen_size=main.PLAYWRIGHT_SCREEN_SIZE,
            initial_url='test_url',
            highlight_mouse=True
        )
        mock_browser_agent.assert_called_once()
        mock_browser_agent.return_value.agent_loop.assert_called_once()

    @patch('main.argparse.ArgumentParser')
    @patch('main.BrowserbaseComputer')
    @patch('main.BrowserAgent')
    def test_main_browserbase(self, mock_browser_agent, mock_browserbase_computer, mock_arg_parser):
        mock_args = MagicMock()
        mock_args.env = 'browserbase'
        mock_args.query = 'test_query'
        mock_args.model = 'test_model'
        mock_args.api_server = None
        mock_args.api_server_key = None
        mock_args.initial_url = 'test_url'
        mock_args.highlight_mouse = False
        mock_arg_parser.return_value.parse_args.return_value = mock_args

        main.main()

        mock_browserbase_computer.assert_called_once_with(
            screen_size=main.PLAYWRIGHT_SCREEN_SIZE,
            initial_url='test_url'
        )
        mock_browser_agent.assert_called_once()
        mock_browser_agent.return_value.agent_loop.assert_called_once()

    @patch('main.argparse.ArgumentParser')
    @patch('main.KvmComputer')
    @patch('main.BrowserAgent')
    def test_main_local(self, mock_browser_agent, mock_kvm_computer, mock_arg_parser):
        mock_args = MagicMock()
        mock_args.env = 'local'
        mock_args.query = 'test_query'
        mock_args.model = 'local'
        mock_args.plan = None
        mock_args.node_name = 'laptop'
        mock_arg_parser.return_value.parse_args.return_value = mock_args

        main.main()

        mock_kvm_computer.assert_called_once_with(node_url="local", node_name="laptop")
        mock_browser_agent.assert_called_once()
        mock_browser_agent.return_value.agent_loop.assert_called_once()


class TestKvmComputer(unittest.TestCase):

    def test_click_uses_abs_route_with_full_screen_geometry(self):
        computer = KvmComputer(node_url="http://node", screen_size=(1920, 1080))
        calls = []

        def fake_run(route, payload, scheme="kvm", timeout=60):
            calls.append((route, payload, scheme, timeout))
            if route == "screen/query/capture":
                return {"pngBase64": ""}
            return {"ok": True}

        computer._run = fake_run

        computer.click_at(1254, 400)

        self.assertEqual(calls[0][0], "abs/command/click")
        self.assertEqual(
            calls[0][1],
            {"x": 1254, "y": 400, "sw": 1920, "sh": 1080, "button": "left", "do_click": True},
        )

    def test_hover_uses_abs_route_without_click(self):
        computer = KvmComputer(node_url="http://node", screen_size=(1920, 1080))
        calls = []

        def fake_run(route, payload, scheme="kvm", timeout=60):
            calls.append((route, payload, scheme, timeout))
            if route == "screen/query/capture":
                return {"pngBase64": ""}
            return {"ok": True}

        computer._run = fake_run

        computer.hover_at(700, 300)

        self.assertEqual(calls[0][0], "abs/command/click")
        self.assertEqual(calls[0][1]["do_click"], False)
        self.assertEqual(calls[0][1]["sw"], 1920)
        self.assertEqual(calls[0][1]["sh"], 1080)

if __name__ == '__main__':
    unittest.main()
