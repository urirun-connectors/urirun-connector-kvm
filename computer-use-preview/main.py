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
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from agent import BrowserAgent
from computers import BrowserbaseComputer, PlaywrightComputer, KvmComputer

PLAYWRIGHT_SCREEN_SIZE = (1440, 900)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the browser agent with a query or plan.")
    parser.add_argument(
        "--query",
        type=str,
        default="",
        help="The query for the browser agent to execute.",
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=None,
        help="Path to JSON plan file containing sequential steps.",
    )
    default_env = "playwright" if os.environ.get("GEMINI_API_KEY") else "local"
    parser.add_argument(
        "--env",
        type=str,
        choices=("playwright", "browserbase", "kvm", "local"),
        default=default_env,
        help="The computer use environment to use.",
    )
    parser.add_argument(
        "--node",
        type=str,
        default="http://localhost:8766",
        help="urirun node URL (for --env kvm): drives a real desktop via kvm:// routes.",
    )
    parser.add_argument(
        "--node-name",
        type=str,
        default="laptop",
        help="urirun node URI name (for --env kvm), e.g. 'laptop'.",
    )
    parser.add_argument(
        "--initial_url",
        type=str,
        default="https://www.google.com",
        help="The inital URL loaded for the computer.",
    )
    parser.add_argument(
        "--highlight_mouse",
        action="store_true",
        default=False,
        help="If possible, highlight the location of the mouse.",
    )
    default_model = "gemini-3.5-flash" if os.environ.get("GEMINI_API_KEY") else "local"
    parser.add_argument(
        "--model",
        default=default_model,
        help="Set which main model to use (use 'local' for cloud-free execution).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    query = getattr(args, "query", "") or ""
    plan_arg = getattr(args, "plan", None)
    if isinstance(plan_arg, str) and plan_arg:
        plan_data = json.loads(Path(plan_arg).read_text(encoding="utf-8"))
        if not query and isinstance(plan_data, dict):
            query = plan_data.get("query", "")
        if not query and isinstance(plan_data, list):
            query = f"execute {len(plan_data)} plan step(s)"

    if args.env == "playwright":
        if PlaywrightComputer is None:
            raise RuntimeError("playwright is not installed in this environment")
        env = PlaywrightComputer(
            screen_size=PLAYWRIGHT_SCREEN_SIZE,
            initial_url=args.initial_url,
            highlight_mouse=args.highlight_mouse,
        )
    elif args.env == "browserbase":
        if BrowserbaseComputer is None:
            raise RuntimeError("browserbase is not installed in this environment")
        env = BrowserbaseComputer(
            screen_size=PLAYWRIGHT_SCREEN_SIZE,
            initial_url=args.initial_url,
        )
    elif args.env == "kvm":
        env = KvmComputer(node_url=args.node, node_name=args.node_name)
    elif args.env == "local":
        env = KvmComputer(node_url="local", node_name=args.node_name)
    else:
        raise ValueError(f"Unknown environment: {args.env}")

    with env as browser_computer:
        agent = BrowserAgent(
            browser_computer=browser_computer,
            query=query,
            model_name=args.model,
        )
        agent.agent_loop()
    return 0


if __name__ == "__main__":
    main()
