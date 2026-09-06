# shadowquic_interop

`shadowquic_interop` runs a client/server compatibility matrix for the
[ShadowQUIC protocol](https://github.com/spongebob888/shadowquic), stores each
run as versioned JSON, and builds a static report for GitHub Pages.

The runnable matrix currently contains:

| Implementation | Client | Server | Image source |
| --- | :---: | :---: | --- |
| shadowquic | yes | yes | `ghcr.io/spongebob888/shadowquic:latest` |
| QuicProxy | yes | yes | Built from upstream `master` |
| mihomo Meta | yes | yes | Built from upstream `Meta` |
| clash-rs | yes | no | Built from the latest GitHub release |

Mihomo is built explicitly from its
[`Meta` branch](https://github.com/MetaCubeX/mihomo/tree/Meta). That branch
contains the ShadowQUIC outbound and listener implementations; the `main`
branch does not currently expose them.

Clash-rs participates as a client only. The runner builds a local image from
the latest upstream GitHub release binary, which includes the ShadowQUIC
outbound, but clash-rs does not provide a ShadowQUIC server implementation.

The specification's ProxyPen link contains a username typo. The runner builds
the active project at
[`spongebob888/proxypen`](https://github.com/spongebob888/proxypen).

## How it works

Each runnable client/server pair gets a private Docker bridge network:

1. The server starts with a generated ShadowQUIC/JLS configuration. Server
   implementations carry UDP sessions in whatever mode the client requested,
   so no server-side UDP configuration is needed.
2. The client starts with a generated configuration and a SOCKS5 listener.
   HTTP probes use the default (datagram) client configuration.
3. ProxyPen requests the public target over HTTP/2 and HTTP/3 through SOCKS5.
4. UDP probes exercise the SOCKS5 UDP ASSOCIATE path in two transport modes —
   `udp-over-stream` (UDP payloads on reliable QUIC streams) and
   `udp-over-datagram` (RFC 9221 QUIC datagrams). Because each client chooses
   its UDP transport mode at startup, the runner starts one client container
   per requested mode. An in-network UDP echo container answers the probe, so
   the whole path (probe → client SOCKS5 UDP relay → QUIC tunnel → server
   direct outbound → echo target and back) stays inside the cell network.
5. The UDP probe sends 1200-byte datagrams for a fixed window and reports how
   many bytes and packets came back, letting the report show throughput in
   MB/s plus echo coverage. It also paces single-datagram echo round trips to
   measure latency (min/avg/p95/max).
6. Every probe runs a pressure phase afterwards: `--load-connections` (default
   4) concurrent sessions hit the same proxy while a sampler thread records
   the peak memory of the client and server containers, and the latency
   figures collected under that load are merged into the probe metrics.
7. The runner records protocol timings, endpoint output, and a cell status.
8. Containers and the network are removed even when setup or probing fails.

`pass`, `fail`, `error`, and `unsupported` are distinct. A protocol failure
means ProxyPen reached the test path and rejected the result. An error means
the harness, image, or endpoint failed before it could produce a valid probe.
`unsupported` remains part of the schema for future capability differences.

## Requirements

- Python 3.11 or newer
- Docker Engine with Linux containers
- Internet access for image builds and the public test target

The Python package has no third-party runtime dependencies.

## Run locally

```bash
python3 -m unittest discover -s tests -v
python3 -m shadowquic_interop run
python3 -m shadowquic_interop generate
python3 -m http.server 8000 --directory site
```

Open `http://localhost:8000`. Generated endpoint logs are under `work/`, and
machine-readable results are under `results/`.

Useful selections:

```bash
# One pair, both probes, without rebuilding local images
python3 -m shadowquic_interop run \
  --clients quicproxy \
  --servers shadowquic \
  --no-build

# Only HTTP/2 and HTTP/3 with a different public target
python3 -m shadowquic_interop run \
  --protocols http2,http3 \
  --target https://cloudflare.com/

# UDP only: both transport modes through the matrix
python3 -m shadowquic_interop run \
  --protocols udp-over-stream,udp-over-datagram

# No pressure phase (single functional probe per cell)
python3 -m shadowquic_interop run --load-connections 0

# Return nonzero when a runnable matrix cell fails
python3 -m shadowquic_interop run --fail-on-test-failure
```

Run `python3 -m shadowquic_interop implementations` for the endpoint registry
and `python3 -m shadowquic_interop run --help` for every option.

## Result data

Every run creates `results/<UTC timestamp>.json` and refreshes
`results/latest.json`. Schema version 1 includes:

- run timestamps, target, protocols, and runner version
- endpoint source, image, and client/server capabilities
- one result per matrix cell
- one HTTP result per requested protocol, including ProxyPen metrics
- one UDP result per requested mode (`udp-over-stream`,
  `udp-over-datagram`) with byte and packet counts plus the throughput
  window and per-datagram latency; the report derives upload/download rates
  in MB/s
- pressure metrics per probe when `--load-connections` is set: concurrent
  session count/success, aggregated min/avg/p95/max latency in ms, and peak
  client/server container memory in KiB
- an optional error message and endpoint log directory

The report generator reads every valid JSON file in `results/`, de-duplicates
run IDs, and embeds the archive into `site/index.html`. Published URLs accept
`?run=<run-id>&protocol=http3` and the same for `udp-over-stream` or
`udp-over-datagram`.

## GitHub automation

[`ci.yml`](.github/workflows/ci.yml) validates every push and pull request.
[`interop.yml`](.github/workflows/interop.yml) runs the full matrix after every
push to the default branch that touches code, daily at 16:30 UTC, and on
manual dispatch. Its own result commits carry `[skip ci]` so they never
re-trigger it. The workflow builds current upstream images, executes the
matrix, uploads diagnostic logs, commits the new JSON result to the default
branch, and deploys the complete archive through GitHub Pages.

In repository settings, set **Pages > Build and deployment > Source** to
**GitHub Actions**. The workflow needs the included `contents`, `pages`, and
`id-token` permissions; organization or branch rules may still need to allow
the scheduled result commit.

## Endpoint maintenance

Endpoint metadata and config renderers live in
`shadowquic_interop/adapters.py`. Clash-rs, Mihomo Meta, QuicProxy, ProxyPen,
and the UDP echo/probe tool (`docker/udp.Dockerfile`) build definitions live
under `docker/`; only shadowquic uses a published image directly. Pass Docker
build arguments such as
`--build-arg MIHOMO_REF=<tag-or-branch>`, or
`--build-arg QUICPROXY_REF=<tag-or-branch>` when selecting an endpoint version.
