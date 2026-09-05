import json
import tempfile
import unittest
from pathlib import Path

from shadowquic_interop.adapters import IMPLEMENTATIONS
from shadowquic_interop.models import CellResult, ProbeResult, Protocol, Status
from shadowquic_interop.runner import InteropRunner, read_result, write_result


class FakeBackend:
    def __init__(self) -> None:
        self.prepared = None
        self.calls = []

    def prepare(self, *, build: bool = True) -> None:
        self.prepared = build

    def run_cell(self, **kwargs) -> CellResult:
        self.calls.append(kwargs)
        probes = [
            ProbeResult(protocol=item, status=Status.PASS, http_status=200)
            for item in kwargs["protocols"]
        ]
        return CellResult(
            client=kwargs["client"].key,
            server=kwargs["server"].key,
            status=Status.PASS,
            probes=probes,
            duration_ms=12,
        )


class RunnerTests(unittest.TestCase):
    def test_matrix_runs_client_only_implementations_against_supported_servers(self) -> None:
        backend = FakeBackend()
        implementations = list(IMPLEMENTATIONS.values())
        result = InteropRunner(backend).run(
            clients=implementations,
            servers=implementations,
            protocols=[Protocol.HTTP2, Protocol.HTTP3],
            target="https://example.com/",
            work_dir=Path("work"),
            build=False,
        )
        self.assertFalse(backend.prepared)
        self.assertEqual(len(result.results), 16)
        self.assertEqual(len(backend.calls), 12)
        unsupported = [
            cell for cell in result.results if cell.status == Status.UNSUPPORTED
        ]
        self.assertEqual(len(unsupported), 4)
        self.assertTrue(all(cell.server == "clash-rs" for cell in unsupported))
        self.assertEqual(
            {call["server"].key for call in backend.calls},
            {"shadowquic", "quicproxy", "mihomo"},
        )
        self.assertEqual(
            {call["client"].key for call in backend.calls},
            {"shadowquic", "quicproxy", "mihomo", "clash-rs"},
        )

    def test_result_round_trip(self) -> None:
        backend = FakeBackend()
        shadowquic = IMPLEMENTATIONS["shadowquic"]
        result = InteropRunner(backend).run(
            clients=[shadowquic],
            servers=[shadowquic],
            protocols=[Protocol.HTTP2, Protocol.UDP_DATAGRAM],
            target="https://example.com/",
            work_dir=Path("work"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_result(result, Path(directory))
            loaded = read_result(path)
            self.assertEqual(loaded.run_id, result.run_id)
            self.assertEqual(loaded.protocols, [Protocol.HTTP2, Protocol.UDP_DATAGRAM])
            self.assertEqual(loaded.results[0].probes[0].protocol, Protocol.HTTP2)
            self.assertEqual(loaded.results[0].probes[1].protocol, Protocol.UDP_DATAGRAM)
            latest = json.loads((Path(directory) / "latest.json").read_text())
            self.assertEqual(latest["schema_version"], 1)

    def test_missing_udp_mode_only_marks_that_probe_unsupported(self) -> None:
        from dataclasses import replace

        backend = FakeBackend()
        client = replace(
            IMPLEMENTATIONS["shadowquic"], udp_modes=frozenset({"stream"})
        )
        server = IMPLEMENTATIONS["shadowquic"]
        result = InteropRunner(backend).run(
            clients=[client],
            servers=[server],
            protocols=[
                Protocol.HTTP2,
                Protocol.UDP_STREAM,
                Protocol.UDP_DATAGRAM,
            ],
            target="https://example.com/",
            work_dir=Path("work"),
            build=False,
        )
        cell = result.results[0]
        self.assertEqual(cell.status, Status.PASS)
        by_protocol = {probe.protocol: probe for probe in cell.probes}
        self.assertEqual(by_protocol[Protocol.HTTP2].status, Status.PASS)
        self.assertEqual(by_protocol[Protocol.UDP_STREAM].status, Status.PASS)
        self.assertEqual(by_protocol[Protocol.UDP_DATAGRAM].status, Status.UNSUPPORTED)
        # The unsupported probe never reaches the backend.
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(
            backend.calls[0]["protocols"], [Protocol.HTTP2, Protocol.UDP_STREAM]
        )


if __name__ == "__main__":
    unittest.main()
