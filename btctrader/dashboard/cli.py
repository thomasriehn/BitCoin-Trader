"""``btctrader-dashboard``: serve the read-only dashboard with uvicorn on ``DASHBOARD_BIND``."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Sequence

from btctrader.common.config import ConfigError, load_settings
from btctrader.common.log import setup_logging

log = logging.getLogger("btctrader.dashboard")


def parse_bind(bind: str) -> tuple[str, int]:
    """Split ``host:port`` (IPv6 hosts may be written as ``[::1]:8090``)."""
    host, sep, port = bind.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ConfigError(f"DASHBOARD_BIND must look like host:port, got {bind!r}")
    host = host.strip("[]")
    port_num = int(port)
    if not 0 < port_num < 65536:
        raise ConfigError(f"DASHBOARD_BIND port out of range: {port_num}")
    return host, port_num


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IPs; False for hostnames and any other address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btctrader-dashboard", description="Read-only dashboard")
    parser.add_argument("--bind", help="override DASHBOARD_BIND (host:port)")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("btctrader.dashboard", args.log_level)
    try:
        settings = load_settings()
        host, port = parse_bind(args.bind or settings.dashboard_bind)
    except ConfigError as exc:
        log.error("error=%s", exc)
        return 2
    import uvicorn  # imported late so tests do not need the server package loaded

    if not is_loopback(host):
        log.warning(
            "DASHBOARD_BIND %s:%d is not loopback: the page has no login and shows balances, trades and "
            "guard state. Bind to 127.0.0.1 and publish it with 'tailscale serve' instead.",
            host,
            port,
        )
    log.info("dashboard listening on http://%s:%d (ledger=%s)", host, port, settings.ledger_db_path)
    uvicorn.run(
        "btctrader.dashboard.app:app",
        host=host,
        port=port,
        log_level=args.log_level.lower(),
        access_log=False,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
