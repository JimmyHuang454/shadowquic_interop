from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import IMPLEMENTATIONS, select_implementations
from .backend import BackendError, DockerBackend
from .models import Protocol, Status
from .report import generate_site
from .runner import InteropRunner, write_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadowquic-interop",
        description="Run and publish the ShadowQUIC interoperability matrix.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="execute the interoperability matrix")
    run.add_argument(
        "--clients",
        type=_csv,
        default=None,
        metavar="LIST",
        help="comma-separated client keys (default: all)",
    )
    run.add_argument(
        "--servers",
        type=_csv,
        default=None,
        metavar="LIST",
        help="comma-separated server keys (default: all)",
    )
    run.add_argument(
        "--protocols",
        type=_protocols,
        default=[
            Protocol.HTTP2,
            Protocol.HTTP3,
            Protocol.UDP_STREAM,
            Protocol.UDP_DATAGRAM,
        ],
        metavar="LIST",
        help=(
            "comma-separated probes: http2, http3, udp-over-stream, "
            "udp-over-datagram"
        ),
    )
    run.add_argument("--target", default="https://cloudflare.com/")
    run.add_argument("--results-dir", type=Path, default=Path("results"))
    run.add_argument("--work-dir", type=Path, default=Path("work"))
    run.add_argument("--timeout", type=int, default=30)
    run.add_argument("--no-build", action="store_true", help="reuse local images")
    run.add_argument(
        "--fail-on-test-failure",
        action="store_true",
        help="return a nonzero status if a runnable cell fails",
    )

    generate = subparsers.add_parser("generate", help="build the static report")
    generate.add_argument("--results-dir", type=Path, default=Path("results"))
    generate.add_argument("--output-dir", type=Path, default=Path("site"))

    subparsers.add_parser("implementations", help="list endpoint capabilities")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "generate":
            count = generate_site(
                results_dir=args.results_dir, output_dir=args.output_dir
            )
            print(f"Generated {args.output_dir / 'index.html'} with {count} run(s)")
            return 0
        if args.command == "implementations":
            _print_implementations()
            return 0
    except (BackendError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _run(args: argparse.Namespace) -> int:
    clients = select_implementations(args.clients)
    servers = select_implementations(args.servers)
    backend = DockerBackend(timeout=args.timeout)
    result = InteropRunner(backend).run(
        clients=clients,
        servers=servers,
        protocols=args.protocols,
        target=args.target,
        work_dir=args.work_dir,
        build=not args.no_build,
    )
    output = write_result(result, args.results_dir)
    counts = {status: 0 for status in Status}
    for cell in result.results:
        counts[cell.status] += 1
    print(f"Run: {result.run_id}")
    print(f"Result: {output}")
    print(" ".join(f"{status.value}={counts[status]}" for status in Status))
    if args.fail_on_test_failure and any(
        cell.status in {Status.FAIL, Status.ERROR} for cell in result.results
    ):
        return 1
    return 0


def _print_implementations() -> None:
    print(f"{'KEY':<14} {'CLIENT':<8} {'SERVER':<8} SOURCE")
    for implementation in IMPLEMENTATIONS.values():
        print(
            f"{implementation.key:<14} "
            f"{('yes' if implementation.client else 'no'):<8} "
            f"{('yes' if implementation.server else 'no'):<8} "
            f"{implementation.source}"
        )
        if implementation.note:
            print(f"  {implementation.note}")


def _csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("list cannot be empty")
    return items


def _protocols(value: str) -> list[Protocol]:
    try:
        protocols = [Protocol(item) for item in _csv(value)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "protocols must be http2, http3, udp-over-stream and/or "
            "udp-over-datagram"
        ) from exc
    return list(dict.fromkeys(protocols))

