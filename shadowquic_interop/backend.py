from __future__ import annotations

import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .adapters import SOCKS_PORT, Implementation
from .models import CellResult, ProbeResult, Protocol, Status, aggregate_status


PROXYPEN_IMAGE = "shadowquic-interop/proxypen:latest"
UDP_IMAGE = "shadowquic-interop/udp:latest"
UDP_TARGET_PORT = 9000
UDP_WINDOW_SECONDS = 2.0


class BackendError(RuntimeError):
    pass


@dataclass(slots=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int,
        check: bool = True,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BackendError(f"command not found: {args[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"command timed out after {timeout}s: {' '.join(args)}") from exc

        result = CommandResult(
            args=list(args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            detail = result.output.strip()[-1000:]
            raise BackendError(
                f"command exited with {result.returncode}: {' '.join(args)}\n{detail}"
            )
        return result


class DockerBackend:
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        timeout: int = 30,
        readiness_delay: float = 2.0,
    ) -> None:
        self.commands = command_runner or CommandRunner()
        self.timeout = timeout
        self.readiness_delay = readiness_delay

    def prepare(self, *, build: bool = True) -> None:
        self.commands.run(["docker", "version"], timeout=30)
        if not build:
            return
        self.commands.run(
            ["docker", "pull", "ghcr.io/spongebob888/shadowquic:latest"],
            timeout=600,
        )
        self.commands.run(
            [
                "docker",
                "build",
                "--pull",
                "--no-cache",
                "-f",
                "docker/clash-rs.Dockerfile",
                "-t",
                "shadowquic-interop/clash-rs:latest",
                ".",
            ],
            timeout=1800,
        )
        self.commands.run(
            [
                "docker",
                "build",
                "--pull",
                "-f",
                "docker/quicproxy.Dockerfile",
                "-t",
                "shadowquic-interop/quicproxy:latest",
                ".",
            ],
            timeout=1800,
        )
        self.commands.run(
            [
                "docker",
                "build",
                "--pull",
                "-f",
                "docker/mihomo-meta.Dockerfile",
                "-t",
                "shadowquic-interop/mihomo-meta:latest",
                ".",
            ],
            timeout=1800,
        )
        self.commands.run(
            [
                "docker",
                "build",
                "--pull",
                "-f",
                "docker/proxypen.Dockerfile",
                "-t",
                PROXYPEN_IMAGE,
                ".",
            ],
            timeout=1800,
        )
        self.commands.run(
            [
                "docker",
                "build",
                "-f",
                "docker/udp.Dockerfile",
                "-t",
                UDP_IMAGE,
                ".",
            ],
            timeout=600,
        )

    def run_cell(
        self,
        *,
        client: Implementation,
        server: Implementation,
        protocols: list[Protocol],
        target: str,
        work_dir: Path,
    ) -> CellResult:
        started = time.monotonic()
        suffix = uuid.uuid4().hex[:10]
        network = f"sq-interop-{suffix}"
        server_name = f"sq-server-{suffix}"
        client_name = f"sq-client-{suffix}"
        udp_target_name = f"sq-udp-{suffix}"
        cell_dir = work_dir / f"{client.key}_{server.key}"
        log_dir = cell_dir / "logs"
        server_config = cell_dir / "server" / server.config_name
        client_configs: dict[str, Path] = {}
        log_dir.mkdir(parents=True, exist_ok=True)
        (cell_dir / "server").mkdir(parents=True, exist_ok=True)
        (cell_dir / "client").mkdir(parents=True, exist_ok=True)

        server_config.write_text(server.render_server(), encoding="utf-8")
        for role, mode in _client_roles(protocols):
            path = cell_dir / "client" / _config_name(client.config_name, role)
            path.write_text(
                client.render_client(server_name, udp_mode=mode), encoding="utf-8"
            )
            client_configs[role] = path

        probes: list[ProbeResult] = []
        message: str | None = None
        created_network = False
        containers: list[tuple[str, Path]] = []
        try:
            self.commands.run(["docker", "network", "create", network], timeout=30)
            created_network = True
            self._start_container(server_name, network, server, server_config)
            containers.append((server_name, log_dir / "server.log"))
            self._assert_running(server_name, "server")

            target_ip: str | None = None
            if any(protocol.is_udp for protocol in protocols):
                self.commands.run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        udp_target_name,
                        "--network",
                        network,
                        UDP_IMAGE,
                        "echo",
                        "--port",
                        str(UDP_TARGET_PORT),
                    ],
                    timeout=60,
                )
                containers.append((udp_target_name, log_dir / "udp.log"))
                self._assert_running(udp_target_name, "udp target")
                target_ip = self._container_ip(udp_target_name)

            client_containers: dict[str, str] = {}
            for role, _ in _client_roles(protocols):
                if role == "default":
                    name = client_name
                else:
                    name = f"{client_name}-{role}"
                self._start_container(
                    name, network, client, client_configs[role]
                )
                containers.append((name, log_dir / ("client.log" if role == "default" else f"client-{role}.log")))
                client_containers[role] = name
                self._assert_running(name, f"{role} client")

            time.sleep(self.readiness_delay)
            for name, _ in containers:
                self._assert_running(name, "container")

            for protocol in protocols:
                if protocol.is_udp:
                    assert target_ip is not None
                    probes.append(
                        self._probe_udp(
                            network,
                            client_containers[protocol.udp_mode],
                            target_ip,
                            protocol,
                        )
                    )
                else:
                    probes.append(
                        self._probe(
                            network, client_containers["default"], target, protocol
                        )
                    )
        except BackendError as exc:
            message = str(exc)
            completed = {probe.protocol for probe in probes}
            probes.extend(
                ProbeResult(protocol=protocol, status=Status.ERROR, message=message)
                for protocol in protocols
                if protocol not in completed
            )
        finally:
            self._capture_logs(containers)
            for name, _ in reversed(containers):
                self._cleanup_container(name)
            if created_network:
                self.commands.run(
                    ["docker", "network", "rm", network], timeout=30, check=False
                )

        return CellResult(
            client=client.key,
            server=server.key,
            status=aggregate_status(probes),
            probes=probes,
            duration_ms=int((time.monotonic() - started) * 1000),
            message=message,
            log_dir=str(log_dir),
        )

    def _start_container(
        self,
        name: str,
        network: str,
        implementation: Implementation,
        config: Path,
    ) -> None:
        self.commands.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                network,
                "--mount",
                f"type=bind,src={config.resolve()},dst=/config/{implementation.config_name},readonly",
                implementation.image,
                *implementation.command(),
            ],
            timeout=60,
        )

    def _assert_running(self, name: str, role: str) -> None:
        result = self.commands.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            logs = self.commands.run(
                ["docker", "logs", name], timeout=30, check=False
            ).output.strip()
            raise BackendError(f"{role} {name} stopped during startup: {logs[-1200:]}")

    def _container_ip(self, name: str) -> str:
        result = self.commands.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                name,
            ],
            timeout=30,
        )
        address = result.stdout.strip()
        if not address:
            raise BackendError(f"container {name} has no network address")
        return address

    def _probe(
        self, network: str, client_name: str, target: str, protocol: Protocol
    ) -> ProbeResult:
        result = self.commands.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                PROXYPEN_IMAGE,
                "test",
                "--proxy",
                f"socks5://{client_name}:{SOCKS_PORT}",
                "--target",
                target,
                "--protocol",
                protocol.value,
                "--timeout",
                str(self.timeout),
            ],
            timeout=self.timeout + 15,
            check=False,
        )
        return parse_proxypen_output(protocol, result.output, result.returncode)

    def _probe_udp(
        self,
        network: str,
        client_name: str,
        target_ip: str,
        protocol: Protocol,
    ) -> ProbeResult:
        result = self.commands.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                UDP_IMAGE,
                "probe",
                "--proxy",
                f"socks5://{client_name}:{SOCKS_PORT}",
                "--target",
                f"{target_ip}:{UDP_TARGET_PORT}",
                "--seconds",
                str(UDP_WINDOW_SECONDS),
                "--timeout",
                str(self.timeout),
            ],
            timeout=self.timeout + 15,
            check=False,
        )
        return parse_udp_probe_output(protocol, result.output, result.returncode)

    def _capture_logs(self, containers: list[tuple[str, Path]]) -> None:
        for name, destination in containers:
            result = self.commands.run(
                ["docker", "logs", "--timestamps", name], timeout=30, check=False
            )
            if result.output:
                destination.write_text(result.output, encoding="utf-8")

    def _cleanup_container(self, name: str) -> None:
        self.commands.run(["docker", "rm", "--force", name], timeout=30, check=False)


def _client_roles(protocols: Sequence[Protocol]) -> list[tuple[str, str | None]]:
    """Map requested protocols to client config roles.

    ``default`` serves the HTTP probes (UDP mode is irrelevant there), while
    each UDP transport mode gets its own client container because the client
    decides at startup how to carry UDP sessions.
    """
    roles: list[tuple[str, str | None]] = []
    if any(protocol.is_http for protocol in protocols):
        roles.append(("default", None))
    for protocol in protocols:
        if protocol.is_udp and protocol.udp_mode not in {role for role, _ in roles}:
            roles.append((protocol.udp_mode, protocol.udp_mode))
    return roles


def _config_name(config_name: str, role: str) -> str:
    if role == "default":
        return config_name
    stem, _, ext = config_name.rpartition(".")
    return f"{stem}-{role}.{ext}"


_SUCCESS = re.compile(
    r"^\[(?P<protocol>HTTP/2|HTTP/3)\]\s+OK\s+(?P<status>\d{3})\s+"
    r"\((?P<duration>\d+)ms\)(?P<metrics>.*)$",
    re.MULTILINE,
)
_FAILURE = re.compile(
    r"^\[(?P<protocol>HTTP/2|HTTP/3)\]\s+FAILED:\s*(?P<message>.+)$",
    re.MULTILINE,
)
_METRIC = re.compile(r"(?P<name>socks|tcp|tls|ttfb|size):(?P<value>\d+)(?:ms|B)")


def parse_proxypen_output(
    protocol: Protocol, output: str, returncode: int
) -> ProbeResult:
    success = _SUCCESS.search(output)
    if success:
        metrics = {
            item.group("name"): int(item.group("value"))
            for item in _METRIC.finditer(success.group("metrics"))
        }
        return ProbeResult(
            protocol=protocol,
            status=Status.PASS,
            http_status=int(success.group("status")),
            duration_ms=int(success.group("duration")),
            metrics=metrics,
            output=output,
        )

    failure = _FAILURE.search(output)
    if failure:
        return ProbeResult(
            protocol=protocol,
            status=Status.FAIL,
            message=failure.group("message").strip(),
            output=output,
        )

    detail = output.strip()[-1200:] or f"ProxyPen exited with status {returncode}"
    return ProbeResult(
        protocol=protocol,
        status=Status.ERROR,
        message=f"unrecognized ProxyPen output: {detail}",
        output=output,
    )


_UDP_OK = re.compile(
    r"^\[UDP\]\s+OK\s+\((?P<duration>\d+)ms\)\s+"
    r"sent:(?P<sent>\d+)B\s+recv:(?P<recv>\d+)B\s+"
    r"sent_packets:(?P<sent_packets>\d+)\s+recv_packets:(?P<recv_packets>\d+)\s+"
    r"window:(?P<window>\d+)ms$",
    re.MULTILINE,
)
_UDP_FAILED = re.compile(
    r"^\[UDP\]\s+FAILED:\s*(?P<message>.+)$",
    re.MULTILINE,
)


def parse_udp_probe_output(
    protocol: Protocol, output: str, returncode: int
) -> ProbeResult:
    """Parse the udp-probe tool summary into a ProbeResult.

    The probe reports raw byte and packet counts; rates in MB/s are derived
    by the report UI from ``sent_bytes``/``window_ms`` and
    ``recv_bytes``/``elapsed``.
    """
    success = _UDP_OK.search(output)
    if success:
        metrics = {
            "sent_bytes": int(success.group("sent")),
            "recv_bytes": int(success.group("recv")),
            "sent_packets": int(success.group("sent_packets")),
            "recv_packets": int(success.group("recv_packets")),
            "window_ms": int(success.group("window")),
        }
        return ProbeResult(
            protocol=protocol,
            status=Status.PASS,
            duration_ms=int(success.group("duration")),
            metrics=metrics,
            output=output,
        )

    failure = _UDP_FAILED.search(output)
    if failure:
        return ProbeResult(
            protocol=protocol,
            status=Status.FAIL,
            message=failure.group("message").strip(),
            output=output,
        )

    detail = output.strip()[-1200:] or f"udp probe exited with status {returncode}"
    return ProbeResult(
        protocol=protocol,
        status=Status.ERROR,
        message=f"unrecognized udp probe output: {detail}",
        output=output,
    )
