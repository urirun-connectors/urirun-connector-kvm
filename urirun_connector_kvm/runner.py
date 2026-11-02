# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
"""Universal local automation runner integrating KVM and CDP procedures.

Zero cloud/API-key dependencies: operates directly in-process or via local
urirun connector procedures, providing unified execution of DOM (CDP) and
desktop (KVM) automation tasks.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from urirun_connector_kvm import cdp
from urirun_connector_kvm.core import (
    capture,
    cdp_dom_click,
    cdp_dom_extract_form,
    cdp_dom_fill,
    cdp_eval,
    cdp_navigate,
    cdp_status,
    click,
    doctor,
    double_click,
    drag_and_drop,
    focus,
    key,
    middle_click,
    move,
    right_click,
    scroll,
    task_run,
    triple_click,
    type_text,
    wait,
    window_maximize,
)


class UniversalRunner:
    """Unified local task runner combining CDP (DOM) and KVM (HID/OS) capabilities."""

    def __init__(self, node_url: str = "", node_name: str = "laptop") -> None:
        self.node_url = node_url.rstrip("/") if node_url and node_url != "local" else ""
        self.node_name = node_name

    def is_cdp_available(self) -> bool:
        """Check if local or remote CDP endpoint is reachable."""
        try:
            return bool(cdp.reachable())
        except Exception:
            return False

    def is_kvm_available(self) -> bool:
        """Check if local KVM backends are available."""
        try:
            rep = doctor()
            return bool(rep.get("ok", False))
        except Exception:
            return True

    def execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        """Execute a single structured automation step, routing to CDP or KVM."""
        raw_op = str(step.get("op", step.get("action", ""))).lower().strip()
        op = raw_op.replace("_", "-")

        # Explicit or inferred CDP routing
        is_cdp = (
            op.startswith(("cdp-", "dom-"))
            or op in {"navigate", "eval", "find", "act", "extract-form"}
            or ("selector" in step and op not in {"focus", "window-maximize"})
            or "expr" in step
            or "expression" in step
        )

        try:
            if is_cdp:
                res = self._execute_cdp_step(op, step)
                channel = "cdp"
            else:
                res = self._execute_kvm_step(op, step)
                channel = "kvm"

            ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
            out = {
                "ok": ok,
                "op": raw_op,
                "channel": channel,
                "result": res,
                "timestamp": time.time(),
            }
            if isinstance(res, dict):
                for k, v in res.items():
                    if k not in out:
                        out[k] = v
            return out
        except Exception as exc:
            return {
                "ok": False,
                "op": raw_op,
                "channel": "cdp" if is_cdp else "kvm",
                "error": str(exc),
                "timestamp": time.time(),
            }

    def _execute_cdp_step(self, op: str, step: dict[str, Any]) -> dict[str, Any]:
        target_id = str(step.get("target_id", ""))
        url_filter = str(step.get("url_filter", ""))

        if op in {"cdp-click", "dom-click"} or (op == "click" and "selector" in step):
            return cdp_dom_click(
                selector=str(step.get("selector", "")),
                text=str(step.get("text", "")),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-fill", "dom-fill"} or (op == "fill" and "selector" in step):
            return cdp_dom_fill(
                selector=str(step.get("selector", "")),
                value=str(step.get("value", step.get("text", ""))),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-navigate", "navigate", "open", "goto"}:
            return cdp_navigate(
                url=str(step.get("url", step.get("target", ""))),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-eval", "eval", "evaluate"}:
            return cdp_eval(
                expr=str(step.get("expr", step.get("expression", ""))),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-extract-form", "dom-extract-form", "extract-form"}:
            return cdp_dom_extract_form(
                selector=str(step.get("selector", "")),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-find", "find"}:
            return cdp.find(
                text=str(step.get("text", "")),
                role=str(step.get("role", "")),
                name=str(step.get("name", "")),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-act", "act"}:
            return cdp.act(
                op=str(step.get("do", step.get("action", "click"))),
                text=str(step.get("text", "")),
                role=str(step.get("role", "")),
                name=str(step.get("name", "")),
                value=str(step.get("value", "")),
                target_id=target_id,
                url_filter=url_filter,
            )
        if op in {"cdp-status", "status"}:
            return cdp_status()
        raise ValueError(f"Unsupported CDP op: {op!r}")

    def _execute_kvm_step(self, op: str, step: dict[str, Any]) -> dict[str, Any]:
        if op in {"click", "click-at"}:
            return click(
                x=int(step.get("x", 0)),
                y=int(step.get("y", 0)),
                button=str(step.get("button", "left")),
            )
        if op in {"double-click", "double-click-at"}:
            return double_click(x=int(step.get("x", 0)), y=int(step.get("y", 0)))
        if op in {"triple-click", "triple-click-at"}:
            return triple_click(x=int(step.get("x", 0)), y=int(step.get("y", 0)))
        if op in {"middle-click", "middle-click-at"}:
            return middle_click(x=int(step.get("x", 0)), y=int(step.get("y", 0)))
        if op in {"right-click", "right-click-at"}:
            return right_click(x=int(step.get("x", 0)), y=int(step.get("y", 0)))
        if op in {"move", "hover", "hover-at"}:
            return move(x=int(step.get("x", 0)), y=int(step.get("y", 0)))
        if op in {"type", "type-text"}:
            return type_text(text=str(step.get("text", "")))
        if op in {"key", "press-key", "hotkey"}:
            keys = str(step.get("keys", step.get("key", "")))
            return key(keys=keys)
        if op == "scroll":
            dy = int(step.get("dy", step.get("amount", 0)))
            dx = int(step.get("dx", 0))
            return scroll(dx=dx, dy=dy)
        if op in {"wait", "sleep"}:
            sec = float(step.get("seconds", step.get("duration", 1.0)))
            return wait(seconds=sec)
        if op in {"capture", "screenshot"}:
            return capture()
        if op in {"focus", "window-focus"}:
            return focus(app=str(step.get("app", step.get("title", ""))))
        if op in {"maximize", "window-maximize"}:
            return window_maximize(app=str(step.get("app", step.get("title", ""))))
        if op in {"drag", "drag-and-drop"}:
            return drag_and_drop(
                x1=int(step.get("x1", step.get("from_x", 0))),
                y1=int(step.get("y1", step.get("from_y", 0))),
                x2=int(step.get("x2", step.get("to_x", 0))),
                y2=int(step.get("y2", step.get("to_y", 0))),
            )
        if op in {"task", "sequence"}:
            return task_run(steps=step.get("steps", []))
        raise ValueError(f"Unsupported KVM op: {op!r}")

    def execute_plan(
        self, steps: list[dict[str, Any]], stop_on_failure: bool = True
    ) -> dict[str, Any]:
        log: list[dict[str, Any]] = []
        all_ok = True
        for idx, step in enumerate(steps):
            t0 = time.time()
            try:
                res = self.execute_step(step)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            dt = round(time.time() - t0, 4)
            item = dict(res)
            item.setdefault("op", step.get("op", ""))
            item.setdefault("status", "completed" if res.get("ok", True) else "failed")
            item.setdefault("duration_sec", dt)
            log.append(item)
            if not res.get("ok", True):
                all_ok = False
                if stop_on_failure:
                    return {
                        "ok": False,
                        "failed_step_index": idx,
                        "failed_step": step,
                        "results": log,
                        "total": len(steps),
                        "completed": len(log),
                    }
        return {
            "ok": all_ok,
            "results": log,
            "total": len(steps),
            "completed": len(log),
        }

    def parse_query_to_steps(self, query: str) -> list[dict[str, Any]]:
        """Parse natural language queries into deterministic plan steps without LLMs."""
        steps: list[dict[str, Any]] = []
        lines = [line.strip() for line in re.split(r"[\n;]+", query) if line.strip()]

        for line in lines:
            # Navigate / open URL
            nav_m = re.match(
                r"^(?:navigate\s+to|open|go\s+to|otw[oó]rz)\s+(https?://\S+|about:\S+|\S+\.\S+)",
                line,
                re.IGNORECASE,
            )
            if nav_m:
                url = nav_m.group(1)
                if not url.startswith(("http://", "https://", "about:")):
                    url = "https://" + url
                steps.append({"op": "navigate", "url": url})
                continue

            # Polish DOM fill: wpisz <val> w <sel>
            fill_pl_m = re.match(
                r"^wpisz\s+(.+?)\s+w\s+(\S+)$", line, re.IGNORECASE
            )
            if fill_pl_m:
                steps.append(
                    {"op": "dom-fill", "selector": fill_pl_m.group(2).strip(), "value": fill_pl_m.group(1).strip()}
                )
                continue

            # English DOM Fill: fill <sel> with <val>
            fill_en_m = re.match(
                r"^(?:fill|type\s+into)\s+(\S+)\s+(?:with\s+)?(.+)$",
                line,
                re.IGNORECASE,
            )
            if fill_en_m and not line.lower().startswith("type text"):
                steps.append(
                    {"op": "dom-fill", "selector": fill_en_m.group(1), "value": fill_en_m.group(2).strip()}
                )
                continue

            # Double click coords
            dbl_coord_m = re.match(
                r"^(?:double[\s-]*click|podw[oó]jne\s+klikni[eę]cie)\s+(?:at\s+)?\(?\s*(\d+)\s*[,x\s]\s*(\d+)\s*\)?$",
                line,
                re.IGNORECASE,
            )
            if dbl_coord_m:
                steps.append(
                    {
                        "op": "double-click",
                        "x": int(dbl_coord_m.group(1)),
                        "y": int(dbl_coord_m.group(2)),
                    }
                )
                continue

            # Click coords
            click_coord_m = re.match(
                r"^(?:click|kliknij)\s+(?:at\s+)?\(?\s*(\d+)\s*[,x\s]\s*(\d+)\s*\)?$",
                line,
                re.IGNORECASE,
            )
            if click_coord_m:
                steps.append(
                    {
                        "op": "click",
                        "x": int(click_coord_m.group(1)),
                        "y": int(click_coord_m.group(2)),
                    }
                )
                continue

            # DOM Click by selector (e.g. #id, .class, [attr], button.cls, input#id)
            click_sel_m = re.match(
                r"^(?:click|kliknij)\s+((?:[#\.\[]|button|input|a|div|span|select|textarea)\S*)$",
                line,
                re.IGNORECASE,
            )
            if click_sel_m:
                steps.append({"op": "dom-click", "selector": click_sel_m.group(1)})
                continue

            # DOM Click by text
            click_txt_m = re.match(
                r"^(?:click\s+text|kliknij\s+tekst|click\s+button)\s+[\"']?([^\"']+)[\"']?$",
                line,
                re.IGNORECASE,
            )
            if click_txt_m:
                steps.append({"op": "dom-click", "text": click_txt_m.group(1).strip()})
                continue

            # Type text
            type_m = re.match(
                r"^(?:type|wpisz)\s+[\"']?(.+?)[\"']?$", line, re.IGNORECASE
            )
            if type_m:
                steps.append({"op": "type", "text": type_m.group(1)})
                continue

            # Press key
            key_m = re.match(
                r"^(?:press|press\s+key|naci[sś]nij)\s+(.+)$", line, re.IGNORECASE
            )
            if key_m:
                steps.append({"op": "key", "keys": key_m.group(1).strip()})
                continue

            # Wait / sleep
            wait_m = re.match(
                r"^(?:wait|sleep|czekaj)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds|sekundy|sekund)?$",
                line,
                re.IGNORECASE,
            )
            if wait_m:
                steps.append({"op": "wait", "seconds": float(wait_m.group(1))})
                continue

            # Scroll
            scroll_m = re.match(
                r"^scroll\s+(up|down|left|right)\s*(\d+)?$", line, re.IGNORECASE
            )
            if scroll_m:
                dir_val = scroll_m.group(1).lower()
                amount = int(scroll_m.group(2) or 300)
                amount = -amount if dir_val in ("up", "left") else amount
                steps.append({"op": "scroll", "direction": dir_val, "dy": amount, "amount": amount})
                continue

            # Capture screenshot
            if re.match(r"^(?:capture|screenshot|zrzut\s+ekranu)", line, re.IGNORECASE):
                steps.append({"op": "capture"})
                continue

            # Maximize
            if re.match(r"^(?:maximize|maksymalizuj)", line, re.IGNORECASE):
                steps.append({"op": "maximize"})
                continue

            # Generic fallback: execute as type or eval
            steps.append({"op": "eval", "expression": line})

        return steps

    def execute_query(self, query: str, stop_on_failure: bool = True) -> dict[str, Any]:
        """Parse natural language query and execute the resulting sequence of steps."""
        steps = self.parse_query_to_steps(query)
        return self.execute_plan(steps, stop_on_failure=stop_on_failure)


def execute_runner(payload: dict[str, Any]) -> dict[str, Any]:
    """Entry point for executing runner commands via URI dispatch."""
    runner = UniversalRunner(
        node_url=str(payload.get("node_url", "")),
        node_name=str(payload.get("node_name", "laptop")),
    )
    if "plan" in payload and isinstance(payload["plan"], list):
        return runner.execute_plan(
            payload["plan"],
            stop_on_failure=bool(payload.get("stop_on_failure", True)),
        )
    if "query" in payload and isinstance(payload["query"], str):
        return runner.execute_query(
            payload["query"],
            stop_on_failure=bool(payload.get("stop_on_failure", True)),
        )
    raise ValueError("runner payload must contain either 'plan' (list) or 'query' (str)")
