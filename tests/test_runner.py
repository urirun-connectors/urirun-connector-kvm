"""Tests for UniversalRunner and local KVM/CDP executor."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "computer-use-preview"))

from urirun_connector_kvm.runner import UniversalRunner, execute_runner
from urirun_connector_kvm.core import runner_execute, task_run


class TestUniversalRunnerParser(unittest.TestCase):
    def setUp(self):
        self.runner = UniversalRunner()

    def test_parse_navigate(self):
        steps = self.runner.parse_query_to_steps("navigate to https://example.com")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "navigate")
        self.assertEqual(steps[0]["url"], "https://example.com")

    def test_parse_open_polish(self):
        steps = self.runner.parse_query_to_steps("otwórz https://onet.pl")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "navigate")
        self.assertEqual(steps[0]["url"], "https://onet.pl")

    def test_parse_dom_click(self):
        steps = self.runner.parse_query_to_steps("click #submit-btn")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "dom-click")
        self.assertEqual(steps[0]["selector"], "#submit-btn")

    def test_parse_dom_click_polish(self):
        steps = self.runner.parse_query_to_steps("kliknij button.login")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "dom-click")
        self.assertEqual(steps[0]["selector"], "button.login")

    def test_parse_dom_fill(self):
        steps = self.runner.parse_query_to_steps("fill #username with admin")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "dom-fill")
        self.assertEqual(steps[0]["selector"], "#username")
        self.assertEqual(steps[0]["value"], "admin")

    def test_parse_dom_fill_polish(self):
        steps = self.runner.parse_query_to_steps("wpisz secret w #password")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "dom-fill")
        self.assertEqual(steps[0]["selector"], "#password")
        self.assertEqual(steps[0]["value"], "secret")

    def test_parse_click_coords(self):
        steps = self.runner.parse_query_to_steps("click at (250, 450)")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "click")
        self.assertEqual(steps[0]["x"], 250)
        self.assertEqual(steps[0]["y"], 450)

    def test_parse_double_click_coords(self):
        steps = self.runner.parse_query_to_steps("double click at (100, 200)")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "double-click")
        self.assertEqual(steps[0]["x"], 100)
        self.assertEqual(steps[0]["y"], 200)

    def test_parse_type(self):
        steps = self.runner.parse_query_to_steps("type hello world")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "type")
        self.assertEqual(steps[0]["text"], "hello world")

    def test_parse_key_press(self):
        steps = self.runner.parse_query_to_steps("press Enter")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "key")
        self.assertEqual(steps[0]["keys"], "Enter")

    def test_parse_wait(self):
        steps = self.runner.parse_query_to_steps("wait 5 seconds")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "wait")
        self.assertEqual(steps[0]["seconds"], 5.0)

    def test_parse_scroll(self):
        steps = self.runner.parse_query_to_steps("scroll down 300")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["op"], "scroll")
        self.assertEqual(steps[0]["direction"], "down")
        self.assertEqual(steps[0]["amount"], 300)

    def test_parse_multiline(self):
        query = (
            "navigate to https://example.com\n"
            "wait 2 seconds\n"
            "click #main-btn\n"
            "type some query\n"
            "press Enter"
        )
        steps = self.runner.parse_query_to_steps(query)
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[0]["op"], "navigate")
        self.assertEqual(steps[1]["op"], "wait")
        self.assertEqual(steps[2]["op"], "dom-click")
        self.assertEqual(steps[3]["op"], "type")
        self.assertEqual(steps[4]["op"], "key")


class TestUniversalRunnerExecution(unittest.TestCase):
    def setUp(self):
        self.runner = UniversalRunner()

    @patch("urirun_connector_kvm.runner.cdp_dom_click")
    def test_execute_dom_click_cdp(self, mock_click):
        mock_click.return_value = {"ok": True, "clicked": "#btn"}
        res = self.runner.execute_step({"op": "dom-click", "selector": "#btn"})
        self.assertTrue(res["ok"])
        mock_click.assert_called_once_with(selector="#btn", text="", target_id="", url_filter="")

    @patch("urirun_connector_kvm.runner.cdp_dom_fill")
    def test_execute_dom_fill_cdp(self, mock_fill):
        mock_fill.return_value = {"ok": True, "filled": "#inp", "value": "test"}
        res = self.runner.execute_step({"op": "dom-fill", "selector": "#inp", "value": "test"})
        self.assertTrue(res["ok"])
        mock_fill.assert_called_once_with(selector="#inp", value="test", target_id="", url_filter="")

    @patch("urirun_connector_kvm.runner.cdp_navigate")
    def test_execute_navigate_cdp(self, mock_nav):
        mock_nav.return_value = {"ok": True, "navigated": "https://example.com"}
        res = self.runner.execute_step({"op": "navigate", "url": "https://example.com"})
        self.assertTrue(res["ok"])
        mock_nav.assert_called_once_with(url="https://example.com", target_id="", url_filter="")

    @patch("urirun_connector_kvm.runner.cdp_eval")
    def test_execute_eval_cdp(self, mock_eval):
        mock_eval.return_value = {"ok": True, "result": "Title"}
        res = self.runner.execute_step({"op": "eval", "expression": "document.title"})
        self.assertTrue(res["ok"])
        mock_eval.assert_called_once_with(expr="document.title", target_id="", url_filter="")

    @patch("urirun_connector_kvm.runner.click")
    def test_execute_kvm_step(self, mock_click):
        mock_click.return_value = {"ok": True, "clicked": True}
        res = self.runner.execute_step({"op": "click", "x": 100, "y": 200})
        self.assertTrue(res["ok"])
        mock_click.assert_called_once_with(x=100, y=200, button="left")

    @patch.object(UniversalRunner, "execute_step")
    def test_execute_plan(self, mock_exec):
        mock_exec.side_effect = [
            {"ok": True, "navigated": "url"},
            {"ok": True, "clicked": "#btn"},
        ]
        plan = [
            {"op": "navigate", "url": "https://test.com"},
            {"op": "dom-click", "selector": "#btn"},
        ]
        res = self.runner.execute_plan(plan)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["results"]), 2)
        self.assertEqual(res["results"][0]["status"], "completed")
        self.assertEqual(res["results"][1]["status"], "completed")

    @patch.object(UniversalRunner, "execute_plan")
    def test_execute_query(self, mock_plan):
        mock_plan.return_value = {"ok": True, "results": [{"status": "completed"}]}
        res = self.runner.execute_query("navigate to https://example.com")
        self.assertTrue(res["ok"])
        mock_plan.assert_called_once()

    def test_execute_runner_top_level(self):
        with patch.object(UniversalRunner, "execute_plan") as mock_plan:
            mock_plan.return_value = {"ok": True, "results": [{"status": "completed"}]}
            res = execute_runner({"plan": [{"op": "wait", "seconds": 1}]})
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["results"]), 1)


class TestCoreRunnerIntegration(unittest.TestCase):
    @patch("urirun_connector_kvm.core.cdp_dom_click")
    def test_task_run_routes_cdp_dom_click(self, mock_click):
        mock_click.return_value = {"ok": True, "clicked": "#btn"}
        res = task_run([{"op": "dom-click", "selector": "#btn"}])
        self.assertTrue(res["ok"])
        self.assertEqual(res["steps"][0]["op"], "dom-click")
        self.assertEqual(res["steps"][0]["via"], "cdp")

    @patch("urirun_connector_kvm.core.cdp_navigate")
    def test_task_run_routes_cdp_navigate(self, mock_nav):
        mock_nav.return_value = {"ok": True, "navigated": "https://example.com"}
        res = task_run([{"op": "navigate", "url": "https://example.com"}])
        self.assertTrue(res["ok"])

    def test_task_run_unknown_op(self):
        res = task_run([{"op": "unknown_future_op"}])
        self.assertTrue(res["ok"])
        self.assertIn("unknown op", res["steps"][0]["skipped"])

    def test_runner_execute_route(self):
        with patch("urirun_connector_kvm.runner.UniversalRunner.execute_plan") as mock_plan:
            mock_plan.return_value = {"ok": True, "results": [{"status": "completed"}]}
            res = runner_execute({"plan": [{"op": "wait", "seconds": 1}]})
            self.assertTrue(res["ok"])
            self.assertEqual(len(res["results"]), 1)


class TestKvmComputerLocal(unittest.TestCase):
    def test_local_kvm_computer_cdp_methods(self):
        from computers.kvm.kvm import KvmComputer
        comp = KvmComputer(node_url="local", screen_size=(1280, 800))
        self.assertEqual(comp.screen_size(), (1280, 800))

        with patch("urirun_connector_kvm.runner.UniversalRunner.execute_step") as mock_exec:
            mock_exec.return_value = {"ok": True, "pngBase64": "ZmFrZS1zaG90"}

            env_state = comp.click_selector("#submit")
            self.assertTrue(any(
                call.args and call.args[0].get("op") == "dom-click" and call.args[0].get("selector") == "#submit"
                for call in mock_exec.call_args_list
            ))
            self.assertEqual(env_state.screenshot, b"fake-shot")

            env_state = comp.fill_selector("#input", "value123")
            self.assertTrue(any(
                call.args and call.args[0].get("op") == "dom-fill" and call.args[0].get("selector") == "#input"
                for call in mock_exec.call_args_list
            ))

            env_state = comp.navigate("https://example.com")
            self.assertTrue(any(
                call.args and call.args[0].get("op") in ("navigate", "cdp-navigate")
                for call in mock_exec.call_args_list
            ))


if __name__ == "__main__":
    unittest.main()
