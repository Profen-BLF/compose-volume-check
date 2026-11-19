# Replaying a PostgreSQL mount change in Paperless-ngx

[Paperless-ngx PR #11084](https://github.com/paperless-ngx/paperless-ngx/pull/11084/files) updated the PostgreSQL mount target in its Compose template. The two revisions below use the `postgres:18` image and resolve the `pgdata` key to the Docker volume `paperless-replay_pgdata`. The container path changes from `/var/lib/postgresql/data` to `/var/lib/postgresql`.

## Reproduce the check

From the root of a checkout of [Compose Volume Check v0.1.0](https://github.com/Profen-BLF/compose-volume-check/releases/tag/v0.1.0), run the following in Bash with `curl`, Python 3.10+, and Docker Compose. Verified with Compose 5.7.0 and Python 3.10. The commands download two pinned templates into a fresh temporary directory and render both with the same Compose project name. Rendering works with an unavailable Docker endpoint and starts no containers.

```bash
checker="$PWD/compose_volume_check.py"
(
  set -eu
  cd "$(mktemp -d)"
  upstream=https://raw.githubusercontent.com/paperless-ngx/paperless-ngx
  template=docker/compose/docker-compose.postgres.yml
  before=4456be72c6f39042f52f195219ab7b10d41d0a25
  after=a206ac78dd214a039a8233d42a69eda76f63ebb0

  curl -fsSL "$upstream/$before/$template" -o before.yml
  curl -fsSL "$upstream/$after/$template" -o after.yml
  docker --host tcp://127.0.0.1:1 compose -p paperless-replay -f before.yml \
    config --no-env-resolution --format json > before.json
  docker --host tcp://127.0.0.1:1 compose -p paperless-replay -f after.yml \
    config --no-env-resolution --format json > after.json
  python3 "$checker" check before.json after.json --format json
)
```

The check exits with status `1` and reports these two findings for service `db`.

| Finding | Container target |
| --- | --- |
| `mount_missing` | `/var/lib/postgresql/data` |
| `mount_added` | `/var/lib/postgresql` |

The checker compares mounts by service and container target. Both entries refer to the same resolved Docker volume. The JSON results describe the rendered configuration, so any existing database's on-disk layout and migration need separate verification. See the [PostgreSQL image storage guidance](https://github.com/docker-library/docs/blob/master/postgres/README.md#pgdata) for version-specific details.
