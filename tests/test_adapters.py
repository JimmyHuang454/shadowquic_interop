import json
import unittest

from shadowquic_interop.adapters import (
    PASSWORD,
    SERVER_PORT,
    SOCKS_PORT,
    USERNAME,
    Implementation,
    IMPLEMENTATIONS,
    select_implementations,
)


class AdapterTests(unittest.TestCase):
    def test_shadowquic_configs_share_credentials_and_address(self) -> None:
        implementation = IMPLEMENTATIONS["shadowquic"]
        server = implementation.render_server()
        client = implementation.render_client("server-under-test")
        self.assertIn(f'bind-addr: "0.0.0.0:{SERVER_PORT}"', server)
        self.assertIn(f'username: "{USERNAME}"', server)
        self.assertIn(f'password: "{PASSWORD}"', server)
        self.assertIn(f'bind-addr: "0.0.0.0:{SOCKS_PORT}"', client)
        self.assertIn(f'addr: "server-under-test:{SERVER_PORT}"', client)

    def test_quicproxy_configs_are_valid_json(self) -> None:
        implementation = IMPLEMENTATIONS["quicproxy"]
        server = json.loads(implementation.render_server())
        client = json.loads(implementation.render_client("sq-server"))
        self.assertEqual(server["inbounds"]["shadowquic"]["port"], SERVER_PORT)
        self.assertEqual(client["inbounds"]["socks"]["port"], SOCKS_PORT)
        self.assertEqual(
            client["outbounds"]["servers"]["shadowquic"]["address"], "sq-server"
        )
        self.assertEqual(
            client["outbounds"]["servers"]["shadowquic"]["dns"], "docker_dns"
        )
        self.assertEqual(
            client["dns"]["servers"]["docker_dns"]["address"], "127.0.0.11"
        )
        self.assertEqual(
            client["dns"]["servers"]["docker_dns"]["outbound"], "direct"
        )
        self.assertEqual(client["outbounds"]["servers"]["direct"]["type"], "direct")
        self.assertEqual(
            client["outbounds"]["servers"]["shadowquic"]["tls"]["jls_password"],
            PASSWORD,
        )

    def test_mihomo_meta_configs_enable_client_and_server(self) -> None:
        implementation = IMPLEMENTATIONS["mihomo"]
        self.assertTrue(implementation.client)
        self.assertTrue(implementation.server)
        self.assertIn("/tree/Meta", implementation.source)
        self.assertEqual(implementation.command(), ["-f", "/config/config.yaml"])
        server = implementation.render_server()
        client = implementation.render_client("mihomo-server")
        self.assertIn("type: shadowquic", server)
        self.assertIn(f"port: {SERVER_PORT}", server)
        self.assertIn("- MATCH,DIRECT", server)
        self.assertIn(f"socks-port: {SOCKS_PORT}", client)
        self.assertIn('server: "mihomo-server"', client)
        self.assertIn("- MATCH,shadowquic-interop", client)

    def test_clash_rs_config_enables_client_only(self) -> None:
        implementation = IMPLEMENTATIONS["clash-rs"]
        self.assertTrue(implementation.client)
        self.assertFalse(implementation.server)
        self.assertEqual(implementation.image, "shadowquic-interop/clash-rs:latest")
        self.assertTrue(implementation.source.endswith("/releases/latest"))
        self.assertEqual(implementation.command(), ["-c", "/config/config.yaml"])
        with self.assertRaisesRegex(ValueError, "no ShadowQUIC server adapter"):
            implementation.render_server()

        client = implementation.render_client("clash-rs-server")
        self.assertIn(
            "type: socks\n"
            "    listen: 0.0.0.0\n"
            "    allow-lan: true\n"
            f"    port: {SOCKS_PORT}\n"
            "    udp: true",
            client,
        )
        self.assertIn(f"port: {SOCKS_PORT}", client)
        self.assertIn("type: shadowquic", client)
        self.assertIn('server: "clash-rs-server"', client)
        self.assertIn(f"port: {SERVER_PORT}", client)
        self.assertIn(f'username: "{USERNAME}"', client)
        self.assertIn(f'password: "{PASSWORD}"', client)
        self.assertIn("server-name: \"cloudflare.com\"", client)
        self.assertIn("- 127.0.0.11", client)
        self.assertIn("- MATCH,shadowquic-interop", client)

    def test_selection_rejects_unknown_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown implementations"):
            select_implementations(["missing"])

    def test_selection_preserves_order_without_duplicates(self) -> None:
        selected = select_implementations(["quicproxy", "shadowquic", "quicproxy"])
        self.assertEqual([item.key for item in selected], ["quicproxy", "shadowquic"])

    def test_udp_modes_are_client_side_config_choices(self) -> None:
        cases = {
            "shadowquic": ("over-stream: false", "over-stream: true"),
            "mihomo": ("udp-over-stream: false", "udp-over-stream: true"),
            "clash-rs": ("over-stream: false", "over-stream: true"),
        }
        for key, (datagram_line, stream_line) in cases.items():
            implementation = IMPLEMENTATIONS[key]
            with self.subTest(client=key):
                self.assertIn(
                    datagram_line,
                    implementation.render_client("server", udp_mode="datagram"),
                )
                self.assertIn(
                    stream_line,
                    implementation.render_client("server", udp_mode="stream"),
                )

    def test_quicproxy_udp_mode_toggles_udp_mod(self) -> None:
        implementation = IMPLEMENTATIONS["quicproxy"]
        datagram = json.loads(
            implementation.render_client("sq-server", udp_mode="datagram")
        )
        stream = json.loads(implementation.render_client("sq-server", udp_mode="stream"))
        outbound = "outbounds.servers.shadowquic"
        self.assertEqual(
            datagram["outbounds"]["servers"]["shadowquic"]["udp_mod"], "datagram"
        )
        self.assertEqual(
            stream["outbounds"]["servers"]["shadowquic"]["udp_mod"], "stream"
        )

    def test_render_rejects_mode_not_in_udp_modes(self) -> None:
        limited = Implementation(
            key="limited",
            name="limited",
            source="https://example.com/limited",
            image="example/limited",
            config_format="yaml",
            udp_modes=frozenset({"stream"}),
        )
        with self.assertRaisesRegex(ValueError, "UDP-over-datagram"):
            limited.render_client("server", udp_mode="datagram")

    def test_default_client_render_matches_datagram_mode(self) -> None:
        for key in ("shadowquic", "quicproxy", "mihomo", "clash-rs"):
            implementation = IMPLEMENTATIONS[key]
            with self.subTest(client=key):
                self.assertEqual(
                    implementation.render_client("server"),
                    implementation.render_client("server", udp_mode="datagram"),
                )


if __name__ == "__main__":
    unittest.main()
