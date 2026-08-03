"""Command-line entrypoint: ``capgw serve ...``."""

from __future__ import annotations

import argparse
import logging
import sys

from capgw.config import Config, ConfigError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capgw",
        description=(
            "Capture Gateway: front an OpenAI-compatible LLM endpoint and record "
            "every agent<->model exchange (incl. reasoning) for data/trajectory collection."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the capture gateway server.")
    serve.add_argument("--endpoint", help="Upstream base URL (e.g. https://api.example.com/v1).")
    serve.add_argument("--model", help="Upstream model name to forward every request to.")
    serve.add_argument("--api-key", dest="api_key", help="Bearer API key for the upstream.")
    serve.add_argument(
        "--env-file",
        dest="env_file",
        help="Env file mapping LLM_BASE_URL/LLM_PRO/LLM_API_KEY (e.g. llm.env).",
    )
    serve.add_argument("--host", help="Bind host (default 0.0.0.0).")
    serve.add_argument("--port", type=int, help="Bind port (default 8900).")
    serve.add_argument("--out", dest="out_dir", help="Capture output dir (default ./captures).")
    serve.add_argument(
        "--name",
        help=(
            "Run name. When set, captures are stored as numbered JSON files at "
            "captures/<name>/<name>_<NNNNNN>.json (one file per call)."
        ),
    )
    serve.add_argument(
        "--chat-path",
        dest="chat_completions_path",
        help="Chat completions path appended to endpoint (default /v1/chat/completions).",
    )
    serve.add_argument("--timeout", type=float, help="Upstream request timeout seconds (default 600).")
    serve.add_argument("--log-level", default="info", help="uvicorn log level (default info).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.resolve(
            endpoint=args.endpoint,
            model=args.model,
            api_key=args.api_key,
            env_file=args.env_file,
            host=args.host,
            port=args.port,
            out_dir=args.out_dir,
            name=args.name,
            chat_completions_path=args.chat_completions_path,
            request_timeout=args.timeout,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from capgw.server import create_app

    logging.getLogger("capgw").info("capgw config: %s", config.redacted())
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
