# Compose Volume Check

[简体中文](README.zh-CN.md)

Catch unintended storage changes before updating a stateful Docker Compose app. Compose Volume Check compares two rendered configurations offline and flags changes to the named volumes, host bind mounts, or volume subpaths that existing services use.

## Try it

Run these commands from this repository with Python 3.10+ and Docker Compose. The example renders two configurations with different project names without starting containers.

```sh
docker compose -p old -f examples/compose.yaml config --format json | python3 compose_volume_check.py snapshot -o baseline.json
docker compose -p new -f examples/compose.yaml config --format json | python3 compose_volume_check.py check baseline.json
```

The check exits with status `1` and reports:

```text
[mount_changed] service "db", target "/var/lib/postgresql/data": The configured source changes from Docker volume "old_db-data" to Docker volume "new_db-data".
```

The volume key stays `db-data` in both files. The resolved Docker volume name changes because the project name changes. A restarted database could therefore mount a new volume while its original data remains in the old one.

For an upstream example, [replay a PostgreSQL mount change in Paperless-ngx](docs/case-studies/paperless-ngx-postgres-mount.md).

## Check a deployment

Before editing the deployed configuration, render it and save its mount information. Replace the script and snapshot paths with your own.

```sh
docker compose config --format json | python3 /path/to/compose_volume_check.py snapshot -o /path/to/baseline.json
```

Render the proposed configuration and compare it with that baseline.

```sh
docker compose config --format json | python3 /path/to/compose_volume_check.py check /path/to/baseline.json
```

Use the same `-f`, `-p`, `--profile`, and `--env-file` options you would use for the corresponding deployment. On Windows, PowerShell 7 can use `py -3` or `python` in place of `python3`. If you already have two rendered files, run `python3 compose_volume_check.py check before.json after.json`. Add `--format json` for automation.

| Exit code | Meaning |
| --- | --- |
| `0` | No configured data mount changes were found, or the baseline had no data mounts to compare |
| `1` | A change or unverified mount needs review |
| `2` | Invalid input or arguments prevented the check |

### Compare a renamed service

When a service changes from `db` to `postgres`, declare that rename and continue checking its original mount targets.

```sh
docker compose config --format json | python3 compose_volume_check.py check baseline.json --rename-service db=postgres
```

Each old name must appear only in the baseline, and each new name only in the candidate. Repeat the flag for multiple one-to-one renames. A rename keeps all mount checks in effect. JSON reports include the mapping in `service_renames` and retain the baseline service name in each finding.

The check matches mounts by service and container target. Changing the YAML volume key preserves the same volume identity when the resolved Docker name stays the same. A missing profiled service prompts a review of the profiles used to render the candidate.

## Scope and privacy

- The checker reads JSON from a file or standard input using the Python standard library. It needs no Docker daemon, administrator access, or third-party Python packages.
- Compare configurations intended for the same Docker environment. The result describes configured mounts. Verifying the running container, volume contents, backups, or a database image's internal data directory is a separate check. Anonymous volumes, inherited mounts, and changes to volume drivers or external management require manual review.
- Snapshots can contain service names, resolved volume names, and container mount targets. Host bind paths, volume subpaths, and driver settings are stored as SHA-256 fingerprints and omitted from reports. Predictable values can still be guessed from a fingerprint, so treat snapshots as internal files. Pipe the rendered configuration directly when it may contain credentials.
- Inputs are capped at 8 MiB. The parser rejects duplicate JSON keys and excessive nesting, and terminal output escapes control characters.

## Test and report a case

```sh
python3 -m unittest discover -s tests -p test_cli.py -v
COMPOSE_VOLUME_CHECK_TEST_COMPOSE=1 python3 -m unittest discover -s tests -p test_rendered_compose.py -v
```

The rendering tests require Compose and use synthetic inputs and an unavailable Docker endpoint. In PowerShell 7, set `$env:COMPOSE_VOLUME_CHECK_TEST_COMPOSE = '1'` before running the second Python command. Set `COMPOSE_VOLUME_CHECK_COMPOSE_BIN` to use a specific standalone Compose binary. The GitHub Actions workflow is configured to test Python 3.10 and 3.12, and Compose 2.38.2 and 5.7.0.

When reporting a missed change or false alarm, include your OS, Compose version, expected result, and a synthetic configuration pair that reproduces the behavior. Review the example data and output for credentials and private identifiers before sharing.

[MIT License](LICENSE)
