# Contributing to Compose Volume Check

## Report a missed change or false alarm

Open a [GitHub issue](https://github.com/Profen-BLF/compose-volume-check/issues/new) describing the storage change you expected the checker to flag. Include your OS, Python and Docker Compose versions, the command you ran, the expected and actual findings, and the exit code.

Use a small synthetic before-and-after configuration pair. Preserve the relevant mount and Compose project settings while replacing real credentials, host paths, and private identifiers with dummy values. The [Paperless-ngx replay](docs/case-studies/paperless-ngx-postgres-mount.md) shows how to document the inputs, commands, and expected results.

## Propose a change

Open a pull request that explains the problem, the intended behavior, and how you verified it. For a change to mount detection, include a regression case in the relevant test suite. See the [test commands in the README](README.md#test-and-report-a-case) for the CLI tests and the Compose rendering tests.
