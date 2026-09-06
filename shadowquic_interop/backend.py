from __future__ import annotations

import re
import subprocess
import threading
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
UDP_LOAD_WINDOW_SECONDS = 1.0
MEM_SAMPLE_INTERVAL = 0.4


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
        load_connections: int = 0,
    ) -> None:
        self.commands = command_runner or CommandRunner()
        self.timeout = timeout
        self.readiness_delay = readiness_delay
        self.load_connections = load_connections

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
                    probe = self._probe_udp(
                        network,
                        client_containers[protocol.udp_mode],
                        target_ip,
                        protocol,
                    )
                else:
                    probe = self._probe(
                        network, client_containers["default"], target, protocol
                    )
                if self.load_connections:
                    load_metrics = self._probe_load(
                        network=network,
                        client_name=client_containers[
                            protocol.udp_mode if protocol.is_udp else "default"
                        ],
                        server_name=server_name,
                        target_ip=target_ip,
                        target=target,
                        protocol=protocol,
                    )
                    merged = dict(probe.metrics)
                    merged.update(load_metrics)
                    probe.metrics = merged
                probes.append(probe)
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

    def _read_container_mem_bytes(self, name: str) -> int | None:
        """Return the container's current memory usage in bytes.

        ``docker stats`` is used instead of ``docker inspect`` because the
        inspect ``State.MemoryStats.Usage`` field reads 0 on cgroup v2 hosts
        (GitHub Actions runners included). The inspect value is kept as a
        fallback for older daemons.
        """
        try:
            sampled = self.commands.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.MemUsage}}",
                    name,
                ],
                timeout=20,
                check=False,
            )
            value = parse_mem_usage(sampled.stdout)
            if value is not None:
                return value
        except BackendError:
            pass
        try:
            sampled = self.commands.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.MemoryStats.Usage}}",
                    name,
                ],
                timeout=20,
                check=False,
            )
            return int(sampled.stdout.strip())
        except (BackendError, ValueError):
            return None

    def _probe(
        self, network: str, client_name: str, target: str, protocol: Protocol
    ) -> ProbeResult:
        result = self.commands.run(
            self._http_probe_args(network, client_name, target, protocol),
            timeout=self.timeout + 15,
            check=False,
        )
        return parse_proxypen_output(protocol, result.output, result.returncode)

    def _http_probe_args(
        self, network: str, client_name: str, target: str, protocol: Protocol
    ) -> list[str]:
        return [
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
        ]

    def _probe_udp(
        self,
        network: str,
        client_name: str,
        target_ip: str,
        protocol: Protocol,
    ) -> ProbeResult:
        result = self.commands.run(
            self._udp_probe_args(
                network,
                client_name,
                target_ip,
                protocol,
                seconds=UDP_WINDOW_SECONDS,
            ),
            timeout=self.timeout + 15,
            check=False,
        )
        return parse_udp_probe_output(protocol, result.output, result.returncode)

    def _udp_probe_args(
        self,
        network: str,
        client_name: str,
        target_ip: str,
        protocol: Protocol,
        *,
        seconds: float,
    ) -> list[str]:
        return [
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
            str(seconds),
            "--timeout",
            str(self.timeout),
        ]

    def _probe_load(
        self,
        *,
        network: str,
        client_name: str,
        server_name: str,
        target_ip: str | None,
        target: str,
        protocol: Protocol,
    ) -> dict[str, int]:
        """Run the pressure phase for one protocol.

        ``load_connections`` sessions are launched concurrently against the
        client container while a sampler thread records the peak container
        memory. Returns aggregate load/latency/memory metrics (ms / KiB).
        """
        concurrency = self.load_connections
        if protocol.is_udp:
            assert target_ip is not None
            args_list = [
                self._udp_probe_args(
                    network,
                    client_name,
                    target_ip,
                    protocol,
                    seconds=UDP_LOAD_WINDOW_SECONDS,
                )
                for _ in range(concurrency)
            ]
        else:
            args_list = [
                self._http_probe_args(network, client_name, target, protocol)
                for _ in range(concurrency)
            ]
        results, peak = self._run_concurrent(args_list, [client_name, server_name])

        ok = 0
        http_latencies: list[int] = []
        udp_latency: list[tuple[int, int, int, int]] = []
        totals: dict[str, int] = {}
        for item in results:
            if item is None:
                continue
            if protocol.is_udp:
                probe = parse_udp_probe_output(protocol, item.output, item.returncode)
                if probe.status != Status.PASS:
                    continue
                ok += 1
                metrics = probe.metrics
                for key in ("sent_bytes", "recv_bytes", "sent_packets", "recv_packets"):
                    totals[key] = totals.get(key, 0) + metrics.get(key, 0)
                if "latency_min_ms" in metrics:
                    udp_latency.append(
                        (
                            int(metrics["latency_min_ms"]),
                            int(metrics["latency_avg_ms"]),
                            int(metrics["latency_p95_ms"]),
                            int(metrics["latency_max_ms"]),
                        )
                    )
            else:
                probe = parse_proxypen_output(protocol, item.output, item.returncode)
                if probe.status != Status.PASS:
                    continue
                ok += 1
                http_latencies.append(probe.duration_ms or 0)

        metrics: dict[str, int] = {
            "load_connections": concurrency,
            "load_ok": ok,
        }
        if protocol.is_udp:
            if udp_latency:
                mins = [entry[0] for entry in udp_latency]
                avgs = [entry[1] for entry in udp_latency]
                p95s = [entry[2] for entry in udp_latency]
                maxs = [entry[3] for entry in udp_latency]
                metrics.update(
                    {
                        "load_latency_min_ms": min(mins),
                        "load_latency_avg_ms": sum(avgs) // len(avgs),
                        "load_latency_p95_ms": max(p95s),
                        "load_latency_max_ms": max(maxs),
                    }
                )
            if totals:
                metrics["load_sent_bytes"] = totals.get("sent_bytes", 0)
                metrics["load_recv_bytes"] = totals.get("recv_bytes", 0)
                metrics["load_sent_packets"] = totals.get("sent_packets", 0)
                metrics["load_recv_packets"] = totals.get("recv_packets", 0)
        else:
            stats = _latency_stats(http_latencies)
            if stats is not None:
                metrics.update(
                    {
                        "load_latency_min_ms": stats[0],
                        "load_latency_avg_ms": stats[1],
                        "load_latency_p95_ms": stats[2],
                        "load_latency_max_ms": stats[3],
                    }
                )
        metrics["load_mem_client_max_kb"] = peak.get(client_name, 0) // 1024
        metrics["load_mem_server_max_kb"] = peak.get(server_name, 0) // 1024
        return metrics

    def _run_concurrent(
        self,
        args_list: list[list[str]],
        memory_names: Sequence[str],
    ) -> tuple[list[CommandResult | None], dict[str, int]]:
        """Run docker probe invocations concurrently and sample memory.

        Returns (per-argument results, name -> peak memory usage in bytes).
        """
        results: list[CommandResult | None] = [None] * len(args_list)
        peak: dict[str, int] = {name: 0 for name in memory_names}
        stop = threading.Event()

        def sampler() -> None:
            while not stop.is_set():
                for name in memory_names:
                    value = self._read_container_mem_bytes(name)
                    if value is not None and value > peak[name]:
                        peak[name] = value
                stop.wait(MEM_SAMPLE_INTERVAL)

        def worker(index: int) -> None:
            try:
                results[index] = self.commands.run(
                    args_list[index],
                    timeout=self.timeout + 30,
                    check=False,
                )
            except BackendError as exc:
                results[index] = None

        sampler_thread = threading.Thread(target=sampler, daemon=True)
        sampler_thread.start()
        workers = [
            threading.Thread(target=worker, args=(index,))
            for index in range(len(args_list))
        ]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        stop.set()
        sampler_thread.join()
        return results, peak

    def _capture_logs(self, containers: list[tuple[str, Path]]) -> None:
        for name, destination in containers:
            result = self.commands.run(
                ["docker", "logs", "--timestamps", name], timeout=30, check=False
            )
            if result.output:
                destination.write_text(result.output, encoding="utf-8")

    def _cleanup_container(self, name: str) -> None:
        self.commands.run(["docker", "rm", "--force", name], timeout=30, check=False)


_MEM_USAGE = re.compile(r"^\s*([0-9.]+)\s*(B|KiB|MiB|GiB|TiB)?\s*/", re.MULTILINE)
_MEM_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def parse_mem_usage(output: str) -> int | None:
    """Parse the first ``<size> / <limit>`` value from docker stats output."""
    match = _MEM_USAGE.search(output)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "B"
    return int(number * _MEM_UNITS[unit])


def _latency_stats(values: list[int]) -> tuple[int, int, int, int] | None:
    """Return (min, avg, p95, max) over ``values``, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    average = sum(ordered) // count
    p95_position = max(1, (95 * count + 99) // 100)  # nearest-rank
    return (
        ordered[0],
        average,
        ordered[p95_position - 1],
        ordered[-1],
    )


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
    r"window:(?P<window>\d+)ms"
    r"(?:\s+lat_min:(?P<lat_min>\d+)\s+lat_avg:(?P<lat_avg>\d+)\s+"
    r"lat_p95:(?P<lat_p95>\d+)\s+lat_max:(?P<lat_max>\d+)\s+"
    r"lat_samples:(?P<lat_samples>\d+))?$",
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

    The probe reports raw byte and packet counts plus echo round-trip
    latency; rates in MB/s are derived by the report UI from
    ``sent_bytes``/``window_ms`` and ``recv_bytes``/``elapsed``.
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
        latency_keys = {
            "lat_min": "latency_min_ms",
            "lat_avg": "latency_avg_ms",
            "lat_p95": "latency_p95_ms",
            "lat_max": "latency_max_ms",
            "lat_samples": "latency_samples",
        }
        for token, key in latency_keys.items():
            value = success.group(token)
            if value is not None:
                metrics[key] = int(value)
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
