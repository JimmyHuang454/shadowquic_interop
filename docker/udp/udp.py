#!/usr/bin/env python3
"""UDP echo target and SOCKS5 UDP relay probe used by the interop matrix.

The image ships two commands:

  udp.py echo  --port 9000
      Run a UDP echo service. Every datagram it receives is sent back to the
      sender unchanged, which lets the probe measure how much traffic actually
      traverses the ShadowQUIC tunnel in both directions.

  udp.py probe --proxy socks5://HOST:PORT --target HOST:PORT [--seconds N]
      Connect to a proxy's SOCKS5 listener, open a UDP ASSOCIATE, then blast
      UDP datagrams toward the echo target through the relay for N seconds
      while counting the echoes that come back. The resulting figures are
      printed in one machine-readable summary line.

Machine-readable output on stdout (parsed by the runner):

  [UDP]   OK (2012ms) sent:10485760B recv:10485760B sent_packets:7488 recv_packets:7488 window:2000ms lat_min:1 lat_avg:2 lat_p95:4 lat_max:9 lat_samples:30
  [UDP]   FAILED: <reason>

Only the Python standard library is used so the container stays tiny.
"""

from __future__ import annotations

import argparse
import select
import socket
import struct
import sys
import time


DEFAULT_PORT = 9000
# 1400-byte QUIC datagrams are the norm (`max-datagram-frame-size` default);
# leaving headroom for the SOCKS5 UDP header (10B for IPv4) and the 2-byte
# ShadowQUIC datagram id keeps every payload inside a single datagram frame.
DEFAULT_SIZE = 1200
SOCKS_VERSION = 0x05
SOCKS_UDP_ASSOCIATE = 0x03


def log(message: str) -> None:
    print(f"udp: {message}", flush=True)


def parse_socks_proxy(value: str) -> tuple[str, int]:
    if not value.startswith("socks5://"):
        raise SystemExit(f"invalid proxy url (expected socks5://host:port): {value}")
    host_port = value[len("socks5://") :]
    host, _, port = host_port.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid proxy url (expected socks5://host:port): {value}")
    return host, int(port)


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host:
        host, port = value, str(default_port)
    return host, int(port) if port.isdigit() else default_port


def socks_udp_associate(proxy_host: str, proxy_port: int) -> tuple[socket.socket, socket.socket, tuple[str, int]]:
    """Open a SOCKS5 UDP ASSOCIATE and return (tcp, udp, relay_addr).

    The UDP socket is bound to the same local port as the TCP control
    connection, which is what most SOCKS5 relays expect for UDP relay
    sessions (RFC 1928 requires datagrams to come from the TCP peer).
    """
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(10)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(("0.0.0.0", 0))
    local_port = tcp.getsockname()[1]

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", local_port))
    udp.settimeout(0.2)

    tcp.connect((proxy_host, proxy_port))
    tcp.sendall(b"\x05\x01\x00")
    reply = _recv_exact(tcp, 2)
    if reply[0] != SOCKS_VERSION or reply[1] != 0x00:
        raise RuntimeError(f"SOCKS5 no-authentication method rejected: {reply.hex()}")

    # UDP ASSOCIATE with a wildcard destination: many relays accept and
    # answer with the real relay endpoint; others answer the request as-is.
    request = (
        bytes([SOCKS_VERSION, SOCKS_UDP_ASSOCIATE, 0x00])
        + bytes([0x01, 0, 0, 0, 0])
        + struct.pack(">H", 0)
    )
    tcp.sendall(request)
    header = _recv_exact(tcp, 4)
    if header[0] != SOCKS_VERSION or header[1] != 0x00:
        raise RuntimeError(f"SOCKS5 UDP ASSOCIATE rejected with reply code {header[1]}")
    atyp = header[3]
    if atyp == 0x01:
        address = socket.inet_ntoa(_recv_exact(tcp, 4))
    elif atyp == 0x04:
        address = socket.inet_ntop(socket.AF_INET6, _recv_exact(tcp, 16))
    else:  # 0x03 domain name
        length = _recv_exact(tcp, 1)[0]
        address = _recv_exact(tcp, length).decode("idna")
    port = struct.unpack(">H", _recv_exact(tcp, 2))[0]
    if address in ("0.0.0.0", "::"):
        address = proxy_host
    log(f"socks udp associate ready, relay at {address}:{port}")
    return tcp, udp, (address, port)


def _recv_exact(stream: socket.socket, length: int) -> bytes:
    chunks = []
    while length > 0:
        chunk = stream.recv(length)
        if not chunk:
            raise RuntimeError("SOCKS5 connection closed during handshake")
        chunks.append(chunk)
        length -= len(chunk)
    return b"".join(chunks)


def encode_packet(target_host: str, target_port: int, payload: bytes) -> bytes:
    try:
        address = socket.inet_pton(socket.AF_INET, target_host)
        atyp = 0x01
    except OSError:
        address = target_host.encode("idna")
        atyp = 0x03
    return (
        b"\x00\x00\x00"
        + bytes([atyp])
        + address
        + struct.pack(">H", target_port)
        + payload
    )


def decode_packet(datagram: bytes) -> tuple[str, int, bytes]:
    if len(datagram) < 4 or datagram[0] != 0x00 or datagram[1] != 0x00 or datagram[2] != 0x00:
        raise ValueError("not a SOCKS5 UDP relay datagram")
    atyp = datagram[3]
    offset = 4
    if atyp == 0x01:
        host = socket.inet_ntoa(datagram[offset : offset + 4])
        offset += 4
    elif atyp == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, datagram[offset : offset + 16])
        offset += 16
    elif atyp == 0x03:
        length = datagram[offset]
        offset += 1
        host = datagram[offset : offset + length].decode("idna")
        offset += length
    else:
        raise ValueError(f"unsupported address type {atyp}")
    port = struct.unpack(">H", datagram[offset : offset + 2])[0]
    return host, port, datagram[offset + 2 :]


def command_echo(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    log(f"echo listening on udp 0.0.0.0:{args.port}")
    while True:
        data, source = sock.recvfrom(65535)
        sock.sendto(data, source)


def command_probe(args: argparse.Namespace) -> int:
    proxy_host, proxy_port = parse_socks_proxy(args.proxy)
    target_host, target_port = parse_host_port(args.target, DEFAULT_PORT)
    if args.size < 8 or args.size > 65507:
        raise SystemExit("--size must be between 8 and 65507 bytes")

    deadline = time.monotonic() + args.timeout
    tcp: socket.socket | None = None
    udp: socket.socket | None = None
    try:
        log(
            f"testing socks5://{proxy_host}:{proxy_port} -> "
            f"{target_host}:{target_port} udp payload {args.size}B "
            f"for {args.seconds}s"
        )
        tcp, udp, relay = socks_udp_associate(proxy_host, proxy_port)
        udp.sendto(encode_packet(target_host, target_port, b"ping"), relay)

        ready, _, _ = select.select([udp], [], [], 5.0)
        if not ready:
            print("[UDP]   FAILED: no reply from the UDP relay (associate dead?)")
            return 1
        data, _ = udp.recvfrom(65535)
        try:
            _, _, echo = decode_packet(data)
        except ValueError:
            print(f"[UDP]   FAILED: relay sent an unexpected datagram ({len(data)}B)")
            return 1
        if echo != b"ping":
            print("[UDP]   FAILED: echo target did not return the handshake payload")
            return 1
        log("handshake ok, echo target reachable through the relay")

        latency = _run_latency_samples(
            udp,
            relay,
            target_host,
            target_port,
            count=args.latency_samples,
            deadline=deadline,
        )
        result = _run_throughput(
            udp,
            relay,
            target_host,
            target_port,
            seconds=args.seconds,
            size=args.size,
            deadline=deadline,
        )
        if result is None:
            print("[UDP]   FAILED: timed out before the throughput window finished")
            return 1
        sent_bytes, recv_bytes, sent_packets, recv_packets, window_ms, elapsed_ms = result
        if recv_packets == 0:
            print(
                "[UDP]   FAILED: no echo datagrams returned during the throughput test"
            )
            return 1
        if latency is None:
            print("[UDP]   FAILED: no latency samples could complete")
            return 1
        lat_min, lat_avg, lat_p95, lat_max, lat_samples = latency
        print(
            f"[UDP]   OK ({elapsed_ms}ms) "
            f"sent:{sent_bytes}B recv:{recv_bytes}B "
            f"sent_packets:{sent_packets} recv_packets:{recv_packets} "
            f"window:{window_ms}ms "
            f"lat_min:{lat_min} lat_avg:{lat_avg} lat_p95:{lat_p95} "
            f"lat_max:{lat_max} lat_samples:{lat_samples}"
        )
        return 0
    except (RuntimeError, OSError) as exc:
        print(f"[UDP]   FAILED: {exc}")
        return 1
    finally:
        for sock in (tcp, udp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _run_latency_samples(
    udp: socket.socket,
    relay: tuple[str, int],
    target_host: str,
    target_port: int,
    *,
    count: int,
    deadline: float,
) -> tuple[int, int, int, int, int] | None:
    """Measure one-datagram echo round trips back to back.

    Samples run sequentially (one in flight at a time), so the numbers are
    comparable across implementations; when several probes share the tunnel
    at once the sample RTTs also reflect that contention.
    """
    rtts: list[float] = []
    for seq in range(count):
        if time.monotonic() > deadline:
            break
        payload = b"lat" + struct.pack(">H", seq)
        udp.sendto(encode_packet(target_host, target_port, payload), relay)
        started = time.monotonic()
        while True:
            if time.monotonic() - started >= 2.0 or time.monotonic() > deadline:
                break
            readable, _, _ = select.select(
                [udp], [], [], min(0.2, deadline - time.monotonic())
            )
            if not readable:
                continue
            try:
                data, _ = udp.recvfrom(65535)
            except (BlockingIOError, OSError):
                break
            try:
                _, _, echo = decode_packet(data)
            except ValueError:
                continue
            if echo == payload:
                rtts.append((time.monotonic() - started) * 1000)
                break
    if not rtts:
        return None

    ordered = sorted(rtts)
    total = sum(ordered)
    samples = len(ordered)
    p95_index = max(0, int((0.95 * samples + 0.999) - 1))
    return (
        int(ordered[0]),
        int(total / samples),
        int(ordered[min(p95_index, samples - 1)]),
        int(ordered[-1]),
        samples,
    )


def _run_throughput(
    udp: socket.socket,
    relay: tuple[str, int],
    target_host: str,
    target_port: int,
    *,
    seconds: float,
    size: int,
    deadline: float,
) -> tuple[int, int, int, int, int, int] | None:
    """Send payload datagrams for ``seconds``, draining echoes as they come.

    The socket is non-blocking so the probe can push as fast as the relay
    accepts datagrams. Echoes are drained continuously while sending so that
    drops recorded by the probe reflect the tunnel path, not our own receive
    buffer. After the send window the probe keeps draining until every sent
    datagram came back or the test deadline runs out.
    """
    window_start = time.monotonic()
    window_end = window_start + seconds
    sent_bytes = 0
    recv_bytes = 0
    sent_packets = 0
    recv_packets = 0
    last_recv = time.monotonic()

    filler = b"x" * (size - 4)
    seq = 0
    udp.setblocking(False)

    def drain_all() -> None:
        nonlocal recv_bytes, recv_packets, last_recv
        while True:
            try:
                data, _ = udp.recvfrom(65535)
            except (BlockingIOError, OSError):
                return
            try:
                _, _, echo = decode_packet(data)
            except ValueError:
                continue
            recv_bytes += len(echo)
            recv_packets += 1
            last_recv = time.monotonic()

    def send_one() -> bool:
        nonlocal sent_bytes, sent_packets, seq
        payload = struct.pack(">I", seq) + filler
        try:
            udp.sendto(encode_packet(target_host, target_port, payload), relay)
        except BlockingIOError:
            return False
        except OSError:
            return False
        sent_bytes += size
        sent_packets += 1
        seq += 1
        return True

    # Fill the send window as fast as the relay drains us.
    while time.monotonic() < window_end and time.monotonic() < deadline:
        if not send_one():
            drain_all()
            readable, _, writable = select.select([udp], [], [udp], 0.02)
            if readable:
                drain_all()
            if not writable and time.monotonic() < window_end:
                time.sleep(0.001)
        elif sent_packets % 256 == 0:
            drain_all()
    drain_all()

    # Drain whatever is still in flight until everything returned.
    while recv_packets < sent_packets and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select([udp], [], [], min(remaining, 0.2))
        if readable:
            drain_all()
        if time.monotonic() - last_recv > 2.0 and recv_packets > 0:
            break  # quiescent: the rest were dropped in flight

    if time.monotonic() >= deadline and recv_packets < sent_packets:
        return None
    window_ms = int((window_end - window_start) * 1000)
    elapsed_ms = int((last_recv - window_start) * 1000)
    if elapsed_ms < window_ms:
        elapsed_ms = window_ms
    return sent_bytes, recv_bytes, sent_packets, recv_packets, window_ms, elapsed_ms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UDP echo target and SOCKS5 UDP probe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    echo = subparsers.add_parser("echo", help="run a UDP echo service")
    echo.add_argument("--port", type=int, default=DEFAULT_PORT)
    echo.set_defaults(func=command_echo)

    probe = subparsers.add_parser(
        "probe", help="measure UDP throughput through a SOCKS5 proxy"
    )
    probe.add_argument("--proxy", required=True, help="socks5://host:port proxy url")
    probe.add_argument("--target", required=True, help="udp echo target host:port")
    probe.add_argument("--size", type=int, default=DEFAULT_SIZE, help="payload bytes per datagram")
    probe.add_argument("--seconds", type=float, default=2.0, help="throughput window in seconds")
    probe.add_argument("--latency-samples", type=int, default=30, help="echo round-trip samples")
    probe.add_argument("--timeout", type=float, default=20.0, help="overall test timeout in seconds")
    probe.set_defaults(func=command_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
