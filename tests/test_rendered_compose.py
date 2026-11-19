"""Exercise the CLI with actual Compose output without a Docker daemon."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "compose_volume_check.py"
EXAMPLE = (ROOT / "examples" / "compose.yaml").read_text(encoding="utf-8")


@unittest.skipUnless(
    os.environ.get("COMPOSE_VOLUME_CHECK_TEST_COMPOSE") == "1",
    "set COMPOSE_VOLUME_CHECK_TEST_COMPOSE=1 to run Compose integration tests",
)
class RenderedComposeCLI(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.baseline = self.workspace / "baseline.json"
        renderer = os.environ.get("COMPOSE_VOLUME_CHECK_COMPOSE_BIN")
        self.compose = [renderer] if renderer else ["docker", "compose"]
        self.render_env = {
            name: value for name, value in os.environ.items()
            if not name.startswith("COMPOSE_") and name != "DOCKER_CONTEXT"
        }
        unavailable_daemon = socket.socket()
        self.addCleanup(unavailable_daemon.close)
        unavailable_daemon.bind(("127.0.0.1", 0))
        host, port = unavailable_daemon.getsockname()
        self.render_env["DOCKER_HOST"] = f"tcp://{host}:{port}"

    def render(self, project, template=EXAMPLE, *options):
        result = subprocess.run(
            [*self.compose, "-p", project, "-f", "-", *options, "config", "--format", "json"],
            cwd=self.workspace,
            env=self.render_env,
            input=template,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def cli(self, *args, rendered):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            input=rendered,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def snapshot(self, rendered):
        result = self.cli("snapshot", "-o", self.baseline, rendered=rendered)
        self.assertEqual(result.returncode, 0, result.stderr)

    def check_config(self, rendered, *flags, exit_code):
        result = self.cli("check", self.baseline, "--format", "json", *flags, rendered=rendered)
        self.assertEqual(result.returncode, exit_code, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_project_name_change_resolves_to_different_volume(self):
        self.snapshot(self.render("old"))
        report = self.check_config(self.render("new"), exit_code=1)
        finding, = report["findings"]
        self.assertEqual(finding["code"], "mount_changed")
        self.assertIn("old_db-data", finding["detail"])
        self.assertIn("new_db-data", finding["detail"])

    def test_key_change_preserves_explicit_volume_name(self):
        explicit = EXAMPLE + "    name: stable_db_data\n"
        self.snapshot(self.render("old", explicit))
        candidate = explicit.replace("db-data", "database-volume")
        report = self.check_config(self.render("new", candidate), exit_code=0)
        self.assertEqual(report["findings"], [])

    def test_driver_option_change_requires_review_without_leaking_old_or_new_values(self):
        bind_backed = EXAMPLE + (
            "    driver: local\n"
            "    driver_opts:\n"
            "      type: none\n"
            "      o: bind\n"
            "      device: /private-before\n"
        )
        self.snapshot(self.render("live", bind_backed))
        candidate = bind_backed.replace("private-before", "private-after")
        report = self.check_config(self.render("live", candidate), exit_code=1)
        finding, = report["findings"]
        self.assertEqual(finding["code"], "unverified")
        self.assertIn("Driver settings change", finding["detail"])
        self.assertNotIn("private-", self.baseline.read_text(encoding="utf-8") + json.dumps(report))

    def test_disabling_profile_requires_review(self):
        profiled = EXAMPLE.replace("    image:", "    profiles: [database]\n    image:")
        self.snapshot(self.render("live", profiled, "--profile", "database"))
        report = self.check_config(self.render("live", profiled), exit_code=1)
        finding, = report["findings"]
        self.assertEqual(finding["code"], "unverified")
        self.assertIn("profiled service", finding["detail"])

    def test_anonymous_volume_requires_review(self):
        anonymous = EXAMPLE.replace("- db-data:", "- ").split("\nvolumes:", 1)[0]
        rendered = self.render("live", anonymous)
        self.snapshot(rendered)
        report = self.check_config(rendered, exit_code=1)
        finding, = report["findings"]
        self.assertEqual(finding["code"], "unverified")
        self.assertIn("anonymous volume", finding["detail"])

    def test_service_rename_mapping_still_checks_real_volume_name(self):
        self.snapshot(self.render("live"))
        renamed = EXAMPLE.replace("  db:\n", "  postgres:\n")
        missing = self.check_config(self.render("live", renamed), exit_code=1)
        self.assertEqual(missing["findings"][0]["code"], "mount_missing")
        unchanged = self.check_config(
            self.render("live", renamed), "--rename-service", "db=postgres", exit_code=0
        )
        self.assertEqual(unchanged["findings"], [])
        changed = self.check_config(
            self.render("other", renamed), "--rename-service", "db=postgres", exit_code=1
        )
        self.assertEqual(changed["findings"][0]["code"], "mount_changed")

    def test_bind_path_change_remains_private_on_the_rendering_host(self):
        bind = EXAMPLE.split("\nvolumes:", 1)[0].replace("db-data:", "./private-before:")
        before = self.render("live", bind)
        source = json.loads(before)["services"]["db"]["volumes"][0]["source"]
        self.assertTrue(Path(source).is_absolute())
        self.snapshot(before)
        identical = self.check_config(self.render("live", bind), exit_code=0)
        self.assertEqual(identical["findings"], [])
        report = self.check_config(
            self.render("live", bind.replace("private-before", "private-after")), exit_code=1
        )
        finding, = report["findings"]
        self.assertEqual(finding["code"], "mount_changed")
        self.assertNotIn("private-", self.baseline.read_text(encoding="utf-8") + json.dumps(report))


if __name__ == "__main__":
    unittest.main()
