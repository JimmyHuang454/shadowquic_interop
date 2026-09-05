import json
import tempfile
import unittest
from pathlib import Path

from shadowquic_interop.report import generate_site


FIXTURE = {
    "schema_version": 1,
    "runner_version": "0.1.0",
    "run_id": "2026-07-19T00:00:00Z",
    "started_at": "2026-07-19T00:00:00Z",
    "finished_at": "2026-07-19T00:00:01Z",
    "target": "https://example.com/",
    "protocols": ["http2", "udp-over-stream"],
    "implementations": [
        {
            "key": "shadowquic",
            "name": "shadowquic",
            "source": "https://example.com/source",
            "image": "example/image",
            "client": True,
            "server": True,
            "note": None,
        }
    ],
    "results": [
        {
            "client": "shadowquic",
            "server": "shadowquic",
            "status": "pass",
            "duration_ms": 1000,
            "message": None,
            "log_dir": "logs",
            "probes": [
                {
                    "protocol": "http2",
                    "status": "pass",
                    "http_status": 200,
                    "duration_ms": 30,
                    "metrics": {"ttfb": 20},
                    "message": None,
                    "output": "ok",
                },
                {
                    "protocol": "udp-over-stream",
                    "status": "pass",
                    "http_status": None,
                    "duration_ms": 2009,
                    "metrics": {
                        "sent_bytes": 472080000,
                        "recv_bytes": 472080000,
                        "sent_packets": 337200,
                        "recv_packets": 337200,
                        "window_ms": 2000,
                    },
                    "message": None,
                    "output": "[UDP]   OK ...",
                },
            ],
        }
    ],
}


class ReportTests(unittest.TestCase):
    def test_generates_self_contained_static_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            output = root / "site"
            results.mkdir()
            (results / "run.json").write_text(json.dumps(FIXTURE))
            count = generate_site(results_dir=results, output_dir=output)
            self.assertEqual(count, 1)
            html = (output / "index.html").read_text()
            self.assertIn("2026-07-19T00:00:00Z", html)
            self.assertNotIn("__SHADOWQUIC_RUN_DATA__", html)
            self.assertTrue((output / "assets" / "app.js").is_file())
            self.assertTrue((output / "assets" / "style.css").is_file())
            self.assertTrue((output / ".nojekyll").is_file())

    def test_empty_result_directory_builds_empty_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            count = generate_site(results_dir=root / "missing", output_dir=root / "site")
            self.assertEqual(count, 0)
            self.assertIn(">[]</script>", (root / "site" / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()

