import tempfile
import unittest
from pathlib import Path

from shadowquic_interop.adapters import IMPLEMENTATIONS
from shadowquic_interop.backend import (
    BackendError,
    CommandResult,
    DockerBackend,
    PROXYPEN_IMAGE,
    UDP_IMAGE,
    parse_proxypen_output,
    parse_udp_probe_output,
)
from shadowquic_interop.models import Protocol, Status


class UDPParserTests(unittest.TestCase):
    def test_udp_success_records_byte_and_packet_counts(self) -> None:
        output = (
            "udp: testing ...\n"
            "[UDP]   OK (2009ms) sent:472080000B recv:210534800B "
            "sent_packets:337200 recv_packets:150382 window:2000ms\n"
        )
        result = parse_udp_probe_output(Protocol.UDP_STREAM, output, 0)
        self.assertEqual(result.status, Status.PASS)
        self.assertEqual(result.duration_ms, 2009)
        self.assertEqual(result.metrics["sent_bytes"], 472080000)
        self.assertEqual(result.metrics["recv_bytes"], 210534800)
        self.assertEqual(result.metrics["sent_packets"], 337200)
        self.assertEqual(result.metrics["recv_packets"], 150382)
        self.assertEqual(result.metrics["window_ms"], 2000)

    def test_udp_failure(self) -> None:
        output = "[UDP]   FAILED: no echo datagrams returned during the throughput test\n"
        result = parse_udp_probe_output(Protocol.UDP_DATAGRAM, output, 1)
        self.assertEqual(result.status, Status.FAIL)
        self.assertEqual(
            result.message, "no echo datagrams returned during the throughput test"
        )

    def test_udp_unrecognized_output_is_infrastructure_error(self) -> None:
        result = parse_udp_probe_output(Protocol.UDP_STREAM, "crash: oom", 101)
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("crash: oom", result.message or "")


class ProxyPenParserTests(unittest.TestCase):
    def test_success(self) -> None:
        result = parse_proxypen_output(
            Protocol.HTTP2,
            "Testing proxy ...\n\n[HTTP/2]   OK 200 (493ms) socks:4ms tls:88ms ttfb:251ms size:1400B\n",
            0,
        )
        self.assertEqual(result.status, Status.PASS)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.duration_ms, 493)
        self.assertEqual(result.metrics["socks"], 4)
        self.assertEqual(result.metrics["size"], 1400)

    def test_failure(self) -> None:
        result = parse_proxypen_output(
            Protocol.HTTP3,
            "[HTTP/3]   FAILED: SOCKS UDP associate rejected\n",
            1,
        )
        self.assertEqual(result.status, Status.FAIL)
        self.assertEqual(result.message, "SOCKS UDP associate rejected")

    def test_unrecognized_output_is_infrastructure_error(self) -> None:
        result = parse_proxypen_output(Protocol.HTTP3, "panic: unavailable", 101)
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("panic: unavailable", result.message or "")


class UDPCellTests(unittest.TestCase):
    """Orchestration: each UDP mode gets its own client container, and the
    UDP echo target runs on the cell network with an inspected IP."""

    @staticmethod
    def _fake_commands() -> "RecordingCommands":
        return RecordingCommands()

    def test_udp_probes_start_target_and_one_client_per_mode(self) -> None:
        commands = self._fake_commands()
        backend = DockerBackend(command_runner=commands, readiness_delay=0)
        implementation = IMPLEMENTATIONS["quicproxy"]
        with tempfile.TemporaryDirectory() as directory:
            result = backend.run_cell(
                client=implementation,
                server=implementation,
                protocols=[
                    Protocol.HTTP2,
                    Protocol.UDP_STREAM,
                    Protocol.UDP_DATAGRAM,
                ],
                target="https://example.com/",
                work_dir=Path(directory),
            )
        self.assertEqual(result.status, Status.PASS)
        self.assertEqual(
            [probe.protocol for probe in result.probes],
            [Protocol.HTTP2, Protocol.UDP_STREAM, Protocol.UDP_DATAGRAM],
        )

        flattened = [" ".join(call) for call in commands.calls]
        udp_runs = [
            call for call in flattened if UDP_IMAGE in call and " probe " in call
        ]
        self.assertEqual(len(udp_runs), 2)
        echo_starts = [
            call for call in flattened if UDP_IMAGE in call and " echo " in call
        ]
        self.assertEqual(len(echo_starts), 1)
        self.assertTrue(
            any("--detach" in call and "config.json" in call for call in flattened),
            "default client must start for the HTTP probe",
        )
        self.assertTrue(
            any("config-stream.json" in call for call in flattened),
            "stream client must mount the stream config",
        )
        self.assertTrue(
            any("config-datagram.json" in call for call in flattened),
            "datagram client must mount the datagram config",
        )
        self.assertTrue(
            any("172.30.0.9:9000" in call for call in flattened),
            "UDP probes must target the inspected container IP",
        )

    def test_http_only_run_skips_udp_infrastructure(self) -> None:
        commands = self._fake_commands()
        backend = DockerBackend(command_runner=commands, readiness_delay=0)
        implementation = IMPLEMENTATIONS["quicproxy"]
        with tempfile.TemporaryDirectory() as directory:
            result = backend.run_cell(
                client=implementation,
                server=implementation,
                protocols=[Protocol.HTTP2],
                target="https://example.com/",
                work_dir=Path(directory),
            )
        self.assertEqual(result.status, Status.PASS)
        flattened = [" ".join(call) for call in commands.calls]
        self.assertFalse(
            any(UDP_IMAGE in call for call in flattened),
            "no UDP infrastructure may start for HTTP-only runs",
        )
        self.assertFalse(
            any("config-stream" in call or "config-datagram" in call for call in flattened)
        )


class RecordingCommands:
    """Records docker invocations and answers them as if every container ran."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args, *, timeout, check=True):
        command = list(args)
        self.calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            pattern = command[command.index("-f") + 1] if "-f" in command else ""
            if "Running" in pattern:
                return CommandResult(command, 0, "true\n", "")
            return CommandResult(command, 0, "172.30.0.9\n", "")
        if command[:2] == ["docker", "logs"]:
            return CommandResult(command, 0, "", "")
        if PROXYPEN_IMAGE in command:
            return CommandResult(
                command, 0, "[HTTP/2] OK 200 (20ms) ttfb:10ms\n", ""
            )
        if UDP_IMAGE in command and "probe" in command:
            return CommandResult(
                command,
                0,
                "[UDP]   OK (2009ms) sent:472080000B recv:472080000B "
                "sent_packets:337200 recv_packets:337200 window:2000ms\n",
                "",
            )
        return CommandResult(command, 0, "", "")


class PartialCellTests(unittest.TestCase):
    def test_later_probe_error_cannot_leave_cell_passing(self) -> None:
        class FakeCommands:
            probe_count = 0

            def run(self, args, *, timeout, check=True):
                command = list(args)
                if command[:2] == ["docker", "inspect"]:
                    return CommandResult(command, 0, "true\n", "")
                if PROXYPEN_IMAGE in command:
                    self.probe_count += 1
                    if self.probe_count == 1:
                        return CommandResult(
                            command, 0, "[HTTP/2] OK 200 (20ms) ttfb:10ms\n", ""
                        )
                    raise BackendError("HTTP/3 probe timed out")
                return CommandResult(command, 0, "", "")

        backend = DockerBackend(command_runner=FakeCommands(), readiness_delay=0)
        implementation = IMPLEMENTATIONS["quicproxy"]
        with tempfile.TemporaryDirectory() as directory:
            result = backend.run_cell(
                client=implementation,
                server=implementation,
                protocols=[Protocol.HTTP2, Protocol.HTTP3],
                target="https://example.com/",
                work_dir=Path(directory),
            )
        self.assertEqual(result.status, Status.ERROR)
        self.assertEqual([item.status for item in result.probes], [Status.PASS, Status.ERROR])
        self.assertEqual(result.probes[1].message, "HTTP/3 probe timed out")


class PrepareTests(unittest.TestCase):
    def test_prepares_endpoint_images(self) -> None:
        class RecordingCommands:
            def __init__(self) -> None:
                self.calls = []

            def run(self, args, *, timeout, check=True):
                command = list(args)
                self.calls.append(command)
                return CommandResult(command, 0, "", "")

        commands = RecordingCommands()
        DockerBackend(command_runner=commands).prepare()
        flattened = [" ".join(call) for call in commands.calls]
        self.assertTrue(
            any(
                "docker/mihomo-meta.Dockerfile" in call
                and "shadowquic-interop/mihomo-meta:latest" in call
                for call in flattened
            )
        )
        self.assertTrue(
            any(
                "docker/clash-rs.Dockerfile" in call
                and "shadowquic-interop/clash-rs:latest" in call
                and "--no-cache" in call
                for call in flattened
            )
        )
        self.assertNotIn("docker pull ghcr.io/watfaq/clash-rs:latest", flattened)
        self.assertTrue(
            any(
                "docker/udp.Dockerfile" in call
                and "shadowquic-interop/udp:latest" in call
                for call in flattened
            ),
            "prepare() must build the UDP probe/echo image",
        )


if __name__ == "__main__":
    unittest.main()
