from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Protocol(StrEnum):
    HTTP2 = "http2"
    HTTP3 = "http3"
    UDP_STREAM = "udp-over-stream"
    UDP_DATAGRAM = "udp-over-datagram"

    @property
    def label(self) -> str:
        return {
            self.HTTP2: "HTTP/2",
            self.HTTP3: "HTTP/3",
            self.UDP_STREAM: "UDP over stream",
            self.UDP_DATAGRAM: "UDP over datagram",
        }[self]

    @property
    def is_http(self) -> bool:
        return self in {Protocol.HTTP2, Protocol.HTTP3}

    @property
    def is_udp(self) -> bool:
        return self in {Protocol.UDP_STREAM, Protocol.UDP_DATAGRAM}

    @property
    def udp_mode(self) -> str:
        if self == Protocol.UDP_STREAM:
            return "stream"
        if self == Protocol.UDP_DATAGRAM:
            return "datagram"
        raise ValueError(f"{self.value} has no UDP transport mode")


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


@dataclass(slots=True)
class ProbeResult:
    protocol: Protocol
    status: Status
    http_status: int | None = None
    duration_ms: int | None = None
    metrics: dict[str, int] = field(default_factory=dict)
    message: str | None = None
    output: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProbeResult:
        return cls(
            protocol=Protocol(value["protocol"]),
            status=Status(value["status"]),
            http_status=value.get("http_status"),
            duration_ms=value.get("duration_ms"),
            metrics=dict(value.get("metrics", {})),
            message=value.get("message"),
            output=value.get("output", ""),
        )


@dataclass(slots=True)
class CellResult:
    client: str
    server: str
    status: Status
    probes: list[ProbeResult]
    duration_ms: int
    message: str | None = None
    log_dir: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CellResult:
        return cls(
            client=value["client"],
            server=value["server"],
            status=Status(value["status"]),
            probes=[ProbeResult.from_dict(item) for item in value.get("probes", [])],
            duration_ms=value.get("duration_ms", 0),
            message=value.get("message"),
            log_dir=value.get("log_dir"),
        )


@dataclass(slots=True, frozen=True)
class ImplementationRecord:
    key: str
    name: str
    source: str
    image: str
    client: bool
    server: bool
    note: str | None = None


@dataclass(slots=True)
class RunResult:
    run_id: str
    started_at: str
    finished_at: str
    target: str
    protocols: list[Protocol]
    implementations: list[ImplementationRecord]
    results: list[CellResult]
    schema_version: int = 1
    runner_version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunResult:
        return cls(
            schema_version=value.get("schema_version", 1),
            runner_version=value.get("runner_version", "unknown"),
            run_id=value["run_id"],
            started_at=value["started_at"],
            finished_at=value["finished_at"],
            target=value["target"],
            protocols=[Protocol(item) for item in value["protocols"]],
            implementations=[ImplementationRecord(**item) for item in value["implementations"]],
            results=[CellResult.from_dict(item) for item in value["results"]],
        )


def aggregate_status(probes: list[ProbeResult]) -> Status:
    """Aggregate probe statuses for a cell.

    ``unsupported`` probes only matter when every probe in the cell is
    unsupported; otherwise they are ignored so a cell with one unsupported
    probe and several passing probes stays green.
    """
    statuses = {probe.status for probe in probes}
    if not statuses:
        return Status.ERROR
    statuses.discard(Status.UNSUPPORTED)
    if not statuses:
        return Status.UNSUPPORTED
    if Status.ERROR in statuses:
        return Status.ERROR
    if Status.FAIL in statuses:
        return Status.FAIL
    if statuses == {Status.PASS}:
        return Status.PASS
    return Status.FAIL

