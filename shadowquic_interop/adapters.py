from __future__ import annotations

import json
from dataclasses import dataclass

from .models import ImplementationRecord


USERNAME = "interop"
PASSWORD = "shadowquic-interop"
SNI = "cloudflare.com"
SERVER_PORT = 1443
SOCKS_PORT = 1080


@dataclass(slots=True, frozen=True)
class Implementation:
    key: str
    name: str
    source: str
    image: str
    config_format: str
    client: bool = True
    server: bool = True
    note: str | None = None
    udp_modes: frozenset[str] = frozenset({"stream", "datagram"})

    @property
    def config_name(self) -> str:
        return f"config.{self.config_format}"

    def record(self) -> ImplementationRecord:
        return ImplementationRecord(
            key=self.key,
            name=self.name,
            source=self.source,
            image=self.image,
            client=self.client,
            server=self.server,
            note=self.note,
        )

    def command(self) -> list[str]:
        if self.key == "mihomo":
            return ["-f", f"/config/{self.config_name}"]
        return ["-c", f"/config/{self.config_name}"]

    def render_server(self) -> str:
        if self.key == "shadowquic":
            return _shadowquic_server()
        if self.key == "quicproxy":
            return _quicproxy_server()
        if self.key == "mihomo":
            return _mihomo_server()
        raise ValueError(f"{self.name} has no ShadowQUIC server adapter")

    def render_client(self, server_host: str, *, udp_mode: str | None = None) -> str:
        """Render the client config for ``udp_mode``.

        ``udp_mode`` selects how UDP sessions are carried over the QUIC
        tunnel: ``"stream"`` (reliable QUIC streams) or ``"datagram"``
        (RFC 9221 QUIC datagrams, the default). Servers support both modes
        without configuration, so the mode is a purely client-side choice.
        """
        mode = udp_mode or "datagram"
        if mode not in self.udp_modes:
            raise ValueError(
                f"{self.name} client does not support UDP-over-{mode} mode"
            )
        if self.key == "shadowquic":
            return _shadowquic_client(server_host, mode)
        if self.key == "quicproxy":
            return _quicproxy_client(server_host, mode)
        if self.key == "mihomo":
            return _mihomo_client(server_host, mode)
        if self.key == "clash-rs":
            return _clash_rs_client(server_host, mode)
        raise ValueError(f"{self.name} has no ShadowQUIC client adapter")


IMPLEMENTATIONS: dict[str, Implementation] = {
    "shadowquic": Implementation(
        key="shadowquic",
        name="shadowquic",
        source="https://github.com/spongebob888/shadowquic",
        image="ghcr.io/spongebob888/shadowquic:latest",
        config_format="yaml",
    ),
    "quicproxy": Implementation(
        key="quicproxy",
        name="QuicProxy",
        source="https://github.com/RealBikiniBottom/QuicProxy",
        image="shadowquic-interop/quicproxy:latest",
        config_format="json",
    ),
    "mihomo": Implementation(
        key="mihomo",
        name="mihomo Meta",
        source="https://github.com/MetaCubeX/mihomo/tree/Meta",
        image="shadowquic-interop/mihomo-meta:latest",
        config_format="yaml",
        note="Built from the upstream Meta branch, which includes ShadowQUIC outbound and listener support.",
    ),
    "clash-rs": Implementation(
        key="clash-rs",
        name="clash-rs",
        source="https://github.com/Watfaq/clash-rs/releases/latest",
        image="shadowquic-interop/clash-rs:latest",
        config_format="yaml",
        server=False,
        note="Built locally from the latest GitHub release. ShadowQUIC is supported as an outbound only.",
    ),
}


def select_implementations(values: list[str] | None) -> list[Implementation]:
    if not values:
        return list(IMPLEMENTATIONS.values())
    unknown = sorted(set(values) - IMPLEMENTATIONS.keys())
    if unknown:
        raise ValueError(f"unknown implementations: {', '.join(unknown)}")
    return [IMPLEMENTATIONS[key] for key in dict.fromkeys(values)]


def _shadowquic_server() -> str:
    return f"""inbound:
  type: shadowquic
  bind-addr: \"0.0.0.0:{SERVER_PORT}\"
  users:
    - username: \"{USERNAME}\"
      password: \"{PASSWORD}\"
  jls-upstream:
    addr: \"{SNI}:443\"
  alpn: [\"h3\"]
  congestion-control: cubic
  zero-rtt: true
  gso: false
outbound:
  type: direct
  dns-strategy: prefer-ipv4
log-level: info
"""


def _shadowquic_client(server_host: str, mode: str) -> str:
    return f"""inbound:
  type: socks
  bind-addr: \"0.0.0.0:{SOCKS_PORT}\"
outbound:
  type: shadowquic
  addr: \"{server_host}:{SERVER_PORT}\"
  username: \"{USERNAME}\"
  password: \"{PASSWORD}\"
  server-name: \"{SNI}\"
  alpn: [\"h3\"]
  initial-mtu: 1300
  congestion-control: cubic
  zero-rtt: true
  gso: false
  over-stream: {str(mode == 'stream').lower()}
log-level: info
"""


def _quicproxy_server() -> str:
    return _json_config(
        {
            "inbounds": {
                "shadowquic": {
                    "type": "shadowquic",
                    "address": "0.0.0.0",
                    "port": SERVER_PORT,
                    "idle_timeout": 60,
                    "mtu_discoveriy": False,
                    "gso": False,
                    "tls": {
                        "enable_jls": True,
                        "jls_username": USERNAME,
                        "jls_password": PASSWORD,
                        "zero_rtt": True,
                        "sni": SNI,
                        "alpn": ["h3"],
                    },
                }
            },
            "outbounds": {
                "default_server": "direct",
                "servers": {"direct": {"type": "direct"}},
            },
            "router": {"default_mode": "proxy"},
            "dns": _quicproxy_dns("direct"),
            "log": {"level": "info", "color": False},
        }
    )


def _quicproxy_client(server_host: str, mode: str) -> str:
    return _json_config(
        {
            "inbounds": {
                "socks": {
                    "type": "socks5",
                    "address": "0.0.0.0",
                    "port": SOCKS_PORT,
                }
            },
            "outbounds": {
                "default_server": "shadowquic",
                "servers": {
                    "shadowquic": {
                        "type": "shadowquic",
                        "address": server_host,
                        "port": SERVER_PORT,
                        "dns": "docker_dns",
                        "idle_timeout": 60,
                        "udp_mod": mode,
                        "mtu_discoveriy": False,
                        "gso": False,
                        "tls": {
                            "enable_jls": True,
                            "jls_username": USERNAME,
                            "jls_password": PASSWORD,
                            "insecure": False,
                            "zero_rtt": True,
                            "sni": SNI,
                            "alpn": ["h3"],
                        },
                    },
                    "direct": {"type": "direct"},
                },
            },
            "router": {"default_mode": "proxy"},
            "dns": _quicproxy_docker_dns(),
            "log": {"level": "info", "color": False},
        }
    )


def _quicproxy_dns(outbound: str) -> dict[str, object]:
    return {
        "default_server": "dns",
        "servers": {
            "dns": {
                "type": "udp",
                "address": "1.1.1.1",
                "port": 53,
                "timeout": 10,
                "outbound": outbound,
                "strategy": "ipv4_only",
            }
        },
    }


def _mihomo_server() -> str:
    return f"""mode: rule
log-level: info
ipv6: false
allow-lan: true
bind-address: \"*\"
listeners:
  - name: shadowquic-interop
    type: shadowquic
    listen: 0.0.0.0
    port: {SERVER_PORT}
    users:
      - username: \"{USERNAME}\"
        password: \"{PASSWORD}\"
    jls-upstream:
      addr: \"{SNI}:443\"
      sni: \"{SNI}\"
    alpn: [\"h3\"]
    quic-versions: [v1]
    zero-rtt: true
    congestion-controller: cubic
    disable-mtu-discovery: false
dns:
  enable: true
  ipv6: false
  nameserver:
    - 1.1.1.1
rules:
  - MATCH,DIRECT
"""


def _mihomo_client(server_host: str, mode: str) -> str:
    return f"""mode: rule
log-level: info
ipv6: false
allow-lan: true
bind-address: \"*\"
socks-port: {SOCKS_PORT}
proxies:
  - name: shadowquic-interop
    type: shadowquic
    server: \"{server_host}\"
    port: {SERVER_PORT}
    username: \"{USERNAME}\"
    password: \"{PASSWORD}\"
    sni: \"{SNI}\"
    alpn: [\"h3\"]
    quic-versions: [v1]
    udp-over-stream: {str(mode == 'stream').lower()}
    zero-rtt: true
    congestion-controller: cubic
    disable-mtu-discovery: false
dns:
  enable: true
  ipv6: false
  nameserver:
    - 127.0.0.11
  proxy-server-nameserver:
    - 127.0.0.11
rules:
  - MATCH,shadowquic-interop
"""


def _clash_rs_client(server_host: str, mode: str) -> str:
    return f"""mode: rule
log-level: info
ipv6: false
allow-lan: true
bind-address: "*"
listeners:
  - name: shadowquic-interop-socks
    type: socks
    listen: 0.0.0.0
    allow-lan: true
    port: {SOCKS_PORT}
    udp: true
proxies:
  - name: shadowquic-interop
    type: shadowquic
    server: "{server_host}"
    port: {SERVER_PORT}
    username: "{USERNAME}"
    password: "{PASSWORD}"
    server-name: "{SNI}"
    alpn: ["h3"]
    congestion-control: cubic
    zero-rtt: true
    over-stream: {str(mode == 'stream').lower()}
    initial-mtu: 1300
    min-mtu: 1290
    keep-alive-interval: 10000
    gso: false
    mtu-discovery: true
dns:
  enable: true
  ipv6: false
  nameserver:
    - 127.0.0.11
rules:
  - MATCH,shadowquic-interop
"""


def _quicproxy_docker_dns() -> dict[str, object]:
    return {
        "default_server": "docker_dns",
        "servers": {
            "docker_dns": {
                "type": "udp",
                "address": "127.0.0.11",
                "port": 53,
                "outbound": "direct",
            }
        },
    }


def _json_config(value: dict[str, object]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
