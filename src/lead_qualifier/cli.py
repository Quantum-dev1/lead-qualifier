"""Command line interface: serve, qualify, eval, sign/send."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx

from . import __version__
from .config import Settings
from .icp import load_icp
from .models import LeadIn
from .signature import sign


def _settings_with(args: argparse.Namespace) -> Settings:
    overrides = {}
    if getattr(args, "icp", None):
        overrides["icp_path"] = args.icp
    return Settings(**overrides)


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s:     %(name)s: %(message)s")
    from .app import create_app

    uvicorn.run(create_app(_settings_with(args)), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_qualify(args: argparse.Namespace) -> int:
    from .app import build_llm
    from .qualifier import Qualifier

    settings = _settings_with(args)
    lead = LeadIn.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
    llm = None if args.no_llm else build_llm(settings)
    result = asyncio.run(Qualifier(load_icp(settings.icp_path), llm).qualify(lead))
    print(result.model_dump_json(indent=2))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .app import build_llm
    from .eval import format_report, load_cases, run_eval

    settings = _settings_with(args)
    llm = None
    if args.llm == "live":
        llm = build_llm(settings)
        if llm is None:
            print(
                "--llm live needs LLM_MODEL (and usually LLM_BASE_URL / LLM_API_KEY) in the environment.",
                file=sys.stderr,
            )
            return 2
    cases = load_cases(args.dataset)
    today = date.fromisoformat(args.today) if args.today else None
    result = asyncio.run(run_eval(cases, load_icp(settings.icp_path), llm, today))
    mode = f"live model {llm.model}" if llm else "rules only (no LLM)"
    print(f"Dataset: {args.dataset} ({len(cases)} leads) | mode: {mode}\n")
    print(format_report(result))
    if args.min_accuracy is not None and result.accuracy < args.min_accuracy:
        print(
            f"\nAccuracy {result.accuracy:.1%} is below the required {args.min_accuracy:.0%}", file=sys.stderr
        )
        return 1
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    settings = _settings_with(args)
    body = Path(args.file).read_bytes()
    LeadIn.model_validate_json(body)  # fail fast on a malformed lead
    headers = {"content-type": "application/json"}
    secret_list = settings.webhook_secrets()
    if secret_list:
        headers.update(sign(secret_list[0], body))
    response = httpx.post(args.url, content=body, headers=headers, timeout=60)
    print(f"HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
    return 0 if response.is_success else 1


def cmd_sign(args: argparse.Namespace) -> int:
    settings = _settings_with(args)
    secret_list = settings.webhook_secrets()
    if not secret_list:
        print("WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2
    for name, value in sign(secret_list[0], Path(args.file).read_bytes()).items():
        print(f"{name}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lead-qualifier", description="Qualify inbound leads against your ICP and route them."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--icp", help="path to the ICP YAML (default: $ICP_PATH or config/icp.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)

    qualify = sub.add_parser("qualify", help="qualify one lead from a JSON file and print the decision")
    qualify.add_argument("file")
    qualify.add_argument("--no-llm", action="store_true", help="rules only, even if LLM_MODEL is set")
    qualify.set_defaults(func=cmd_qualify)

    ev = sub.add_parser("eval", help="measure accuracy on a labelled JSONL dataset")
    ev.add_argument("--dataset", default="evals/leads.jsonl")
    ev.add_argument(
        "--llm",
        choices=["off", "live"],
        default="off",
        help="off = deterministic rules engine; live = model from LLM_* env vars",
    )
    ev.add_argument(
        "--today", help="reference date for relative timelines (YYYY-MM-DD), for reproducible runs"
    )
    ev.add_argument("--min-accuracy", type=float, help="exit with status 1 below this accuracy (0-1), for CI")
    ev.set_defaults(func=cmd_eval)

    send = sub.add_parser(
        "send", help="POST a lead JSON file to a running server, signed with WEBHOOK_SECRET"
    )
    send.add_argument("file")
    send.add_argument("--url", default="http://127.0.0.1:8000/leads")
    send.set_defaults(func=cmd_send)

    sig = sub.add_parser("sign", help="print signature headers for a request body file")
    sig.add_argument("file")
    sig.set_defaults(func=cmd_sign)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
