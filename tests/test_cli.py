import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "compose_volume_check.py"
TARGET = "/var/lib/postgresql/data"


def config(name="live_db-data", key="db-data", subpath=None):
    mount = {"type": "volume", "source": key, "target": TARGET}
    if subpath is not None:
        mount["volume"] = {"subpath": subpath}
    return {
        "services": {"db": {"volumes": [mount]}},
        "volumes": {key: {"name": name}},
    }


class VolumeCheckCLI(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.baseline = Path(workspace.name) / "baseline.json"

    def cli(self, *args, data=None):
        if isinstance(data, dict):
            data = json.dumps(data)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            input=data,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def save(self, document):
        result = self.cli("snapshot", "-o", self.baseline, data=document)
        self.assertEqual(result.returncode, 0, result.stderr)

    def check_config(self, document, *args):
        return self.cli("check", self.baseline, *args, data=document)

    def test_resolved_volume_changes_when_project_name_changes(self):
        self.save(config())
        result = self.check_config(config(name="next_db-data"), "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["baseline_mounts"], 1)
        finding, = report["findings"]
        self.assertEqual(finding["target"], TARGET)
        self.assertEqual(finding["code"], "mount_changed")
        self.assertIn("live_db-data", finding["detail"])
        self.assertIn("next_db-data", finding["detail"])

    def test_renamed_logical_key_preserves_volume_identity(self):
        self.save(config(name="state"))
        modified = config(name="state", key="other")
        result = self.check_config(modified)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_service_rename_compares_the_original_mount(self):
        self.save(config())
        after = config()
        after["services"]["postgres"] = after["services"].pop("db")
        unmapped = self.check_config(after)
        self.assertEqual(unmapped.returncode, 1, unmapped.stderr)
        self.assertIn("[mount_missing]", unmapped.stdout)
        mapped = self.check_config(after, "--rename-service", "db=postgres", "--format", "json")
        self.assertEqual(mapped.returncode, 0, mapped.stdout + mapped.stderr)
        report = json.loads(mapped.stdout)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["service_renames"], {"db": "postgres"})

    def test_explicit_service_rename_does_not_hide_a_volume_change(self):
        self.save(config())
        after = config(name="next_db-data")
        after["services"]["postgres"] = after["services"].pop("db")
        result = self.check_config(after, "--rename-service", "db=postgres")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('[mount_changed] service "db"', result.stdout)
        self.assertIn('Comparing renamed service "db" to "postgres".', result.stdout)

    def test_rename_does_not_hide_anonymous_volume_uncertainty(self):
        before = {"services": {"db": {"volumes": [{"type": "volume", "target": TARGET}]}}}
        self.save(before)
        after = {"services": {"postgres": before["services"]["db"]}}
        result = self.check_config(after, "--rename-service", "db=postgres")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[unverified]", result.stdout)

    def test_multiple_service_renames_keep_each_volume_with_its_service(self):
        before = config()
        before["services"]["cache"] = {
            "volumes": [{"type": "volume", "source": "cache-data", "target": "/data"}]
        }
        before["volumes"]["cache-data"] = {"name": "live_cache-data"}
        self.save(before)
        after = config()
        after["volumes"]["cache-data"] = before["volumes"]["cache-data"]
        after["services"] = {
            "postgres": before["services"]["db"],
            "redis": before["services"]["cache"],
        }
        result = self.check_config(
            after, "--rename-service", "db=postgres", "--rename-service", "cache=redis"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Comparing renamed service "cache" to "redis".', result.stdout)

    def test_invalid_rename_mappings_do_not_pass(self):
        before = config()
        before["services"].update({"still_here": {}, "other": {}})
        self.save(before)
        after = config()
        after["services"]["postgres"] = after["services"].pop("db")
        after["services"]["still_here"] = {}
        for flags in (
            ("db",),
            ("db=postgres=extra",),
            ("missing=postgres",),
            ("db=missing",),
            ("still_here=postgres",),
            ("db=still_here",),
            ("db=postgres", "db=postgres"),
            ("db=postgres", "other=postgres"),
            ("db=still_here", "still_here=db"),
        ):
            with self.subTest(flags=flags):
                args = [item for flag in flags for item in ("--rename-service", flag)]
                result = self.check_config(after, *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("No configured data mount changes", result.stdout)

    def test_same_name_switch_to_external_management_requires_review(self):
        self.save(config(name="state"))
        modified = config(name="state")
        modified["volumes"]["db-data"]["external"] = True
        result = self.check_config(modified)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[unverified]", result.stdout)
        self.assertIn("Compose-managed and external", result.stdout)

    def test_profiled_service_missing_from_candidate_is_unverified(self):
        before = config()
        before["services"]["db"]["profiles"] = ["private-profile-name"]
        self.save(before)
        result = self.check_config({"services": {}})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[unverified]", result.stdout)
        self.assertNotIn("[mount_missing]", result.stdout)
        self.assertNotIn("private-profile-name", self.baseline.read_text() + result.stdout)

    def test_removed_service_and_non_data_replacement_trigger_review(self):
        self.save(config())
        removed_service = {"services": {}}
        tmpfs = {"services": {"db": {"volumes": [{"type": "tmpfs", "target": TARGET}]}}}
        for candidate in (removed_service, tmpfs):
            with self.subTest(candidate=candidate):
                result = self.check_config(candidate)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("[mount_missing]", result.stdout)

    def test_new_data_mounts_on_existing_services_trigger_review(self):
        self.save(config())
        candidate = config()
        candidate["services"]["db"]["volumes"].append(
            {"type": "bind", "source": "/new/path", "target": "/another/data"}
        )
        result = self.check_config(candidate)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[mount_added]", result.stdout)
        self.assertNotIn("/new/path", result.stdout)

    def test_unrelated_new_service_has_no_baseline_to_lose(self):
        self.save(config())
        candidate = config()
        candidate["services"]["cache"] = {"volumes": [{"type": "volume", "target": "/cache"}]}
        result = self.check_config(candidate)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_anonymous_and_inherited_mounts_are_unverified(self):
        candidate = {
            "services": {
                "db": {"volumes": [{"type": "volume", "target": TARGET}]},
                "backup": {"volumes_from": ["db:ro"]},
            }
        }
        self.save(candidate)
        result = self.check_config(candidate)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.count("[unverified]"), 2)

    def test_changed_bind_source_and_volume_subpath_are_detected_without_raw_values(self):
        before = config(subpath="private-before-subpath")
        after = config(subpath="private-after-subpath")
        for document, path in (
            (before, r"C:\private-before\db"),
            (after, r"C:\private-after\db"),
        ):
            document["services"]["db"]["volumes"].append(
                {"type": "bind", "source": path, "target": "/work"}
            )
        self.save(before)
        result = self.check_config(after)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.count("[mount_changed]"), 2)
        self.assertNotIn("private-before", self.baseline.read_text() + result.stdout)
        self.assertNotIn("private-after", result.stdout)

    def test_credentials_in_unrelated_fields_never_reach_snapshot_or_report(self):
        document = config()
        service = document["services"]["db"]
        service["environment"] = {"TOKEN": "unique-env-secret"}
        service["labels"] = {"token": "unique-label-secret"}
        service["command"] = ["unique-command-secret"]
        service["secrets"] = [{"source": "unique-secret-reference"}]
        document["secrets"] = {"unique-secret-reference": {"file": "unique-secret-path"}}
        self.save(document)
        result = self.check_config(document, "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unique-", self.baseline.read_text() + result.stdout + result.stderr)

    def test_driver_options_can_change_backing_storage_without_changing_volume_name(self):
        before = config()
        before["volumes"]["db-data"].update(
            {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/private-old"}}
        )
        self.save(before)
        after = config()
        after["volumes"]["db-data"].update(
            {"driver_opts": {"device": "/private-old", "o": "bind", "type": "none"}}
        )
        same = self.check_config(after)
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)

        after["volumes"]["db-data"]["driver_opts"]["device"] = "/private-new"
        changed = self.check_config(after)
        self.assertEqual(changed.returncode, 1, changed.stderr)
        self.assertIn("[unverified]", changed.stdout)
        self.assertIn("Driver settings change", changed.stdout)
        self.assertNotIn("/private-", self.baseline.read_text() + changed.stdout)

    def test_terminal_control_sequences_are_escaped(self):
        self.save(config(name="volume\x1b[2J\nforged message"))
        result = self.check_config(config(name="different"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn(r"\u001b", result.stdout)

    def test_bad_candidate_never_echoes_its_content(self):
        self.save(config())
        for malformed in (
            '{"untrusted-secret":1,"untrusted-secret":2}',
            '{"services": { "db": {"volumes": ["untrusted-secret"]}}}',
            '{"services": {"db": {"volumes": [{"type": "volume", "source": "untrusted-secret", "target": "/data"}]}}}',
            '[' * 1200 + '"untrusted-secret"' + ']' * 1200,
        ):
            with self.subTest(malformed=malformed[:72]):
                result = self.check_config(malformed)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("untrusted-secret", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_bad_snapshot_does_not_overwrite_known_good_snapshot(self):
        self.save(config())
        before = self.baseline.read_bytes()
        result = self.cli("snapshot", "-o", self.baseline, data="not JSON")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.baseline.read_bytes(), before)
        self.save(config(name="new"))
        self.assertIn(b'"new"', self.baseline.read_bytes())

    def test_size_limit_is_enforced(self):
        self.save(config())
        result = self.check_config(" " * (8 * 1024 * 1024 + 1))
        self.assertEqual(result.returncode, 2)
        self.assertIn("8 MiB", result.stderr)

    def test_does_not_misinterpret_unresolved_compose_yaml(self):
        document = config()
        document["volumes"]["db-data"] = {}
        result = self.cli("snapshot", data=document)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Resolved Docker volume name", result.stderr)


if __name__ == "__main__":
    unittest.main()
