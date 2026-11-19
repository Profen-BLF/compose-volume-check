"""Compare persistent mounts in rendered Docker Compose configurations."""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


__version__ = "0.1.1"
FORMAT = "compose-volume-check/1"
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_LABEL_LENGTH = 4096
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
NON_DATA_MOUNTS = {"tmpfs", "image", "npipe"}


class InputError(ValueError):
    """An input error whose message is safe to display."""


@dataclass(frozen=True)
class Mount:
    kind: str
    source: str | None = None
    subpath: str | None = None
    driver_settings: str | None = None
    external: bool = False

    def description(self):
        if self.kind == "volume":
            return f"Docker volume {quote(self.source)}"
        return {
            "bind": "a host bind mount",
            "anonymous": "an anonymous volume",
            "unknown": "an unsupported mount type",
        }[self.kind]


@dataclass(frozen=True)
class Service:
    mounts: dict[str, Mount]
    inherits_mounts: bool
    uses_profiles: bool


@dataclass(frozen=True)
class Finding:
    code: str
    service: str
    target: str | None
    detail: str


def quote(value):
    return json.dumps(value, ensure_ascii=True)


def require_object(value, description):
    if not isinstance(value, dict):
        raise InputError(f"{description} must be a JSON object.")
    return value


def optional_field(mapping, name, default):
    value = mapping.get(name)
    return default if value is None else value


def require_label(value, description):
    if not isinstance(value, str) or not value or len(value) > MAX_LABEL_LENGTH:
        raise InputError(f"{description} must be a nonempty string of at most {MAX_LABEL_LENGTH} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InputError(f"{description} must contain valid Unicode.") from None
    return value


def fingerprint(value, description):
    return hashlib.sha256(require_label(value, description).encode("utf-8")).hexdigest()


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("JSON object contains a duplicate key.")
        result[key] = value
    return result


def reject_constant(_):
    raise InputError("JSON contains a non-finite numeric constant.")


def read_document(path):
    if path == "-":
        content = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        with Path(path).open("rb") as source:
            content = source.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise InputError(f"Input exceeds {MAX_INPUT_BYTES // (1024 * 1024)} MiB.")
    try:
        document = json.loads(
            content, object_pairs_hook=reject_duplicates, parse_constant=reject_constant
        )
    except InputError:
        raise
    except json.JSONDecodeError as error:
        raise InputError(f"Invalid JSON at line {error.lineno}, column {error.colno}.") from None
    except (UnicodeError, RecursionError):
        raise InputError("JSON encoding or nesting is invalid.") from None
    except ValueError:
        raise InputError("JSON contains an unsupported numeric value.") from None
    return require_object(document, "Input")


def driver_option_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        # Match Compose's Go formatting for numeric options before hashing.
        number = Decimal(repr(value)).normalize()
        exponent = number.adjusted()
        if number.is_zero() or -4 <= exponent < 6:
            return format(number, "f")
        return f"{number.scaleb(-exponent)}e{exponent:+03d}"
    raise InputError("Volume driver option values must be strings, finite numbers, or booleans.")


def rendered_mount(entry, volume_definitions):
    kind = require_label(entry.get("type"), "Mount type")
    if kind in NON_DATA_MOUNTS:
        return None
    if kind == "bind":
        return Mount("bind", fingerprint(entry.get("source"), "Bind source"))
    if kind != "volume":
        return Mount("unknown")

    source = entry.get("source")
    if source is None or source == "":
        return Mount("anonymous")
    source = require_label(source, "Volume source")
    definition = require_object(volume_definitions.get(source), "Named volume definition")
    name = require_label(definition.get("name"), "Resolved Docker volume name")
    options = require_object(optional_field(entry, "volume", {}), "Volume mount options")
    subpath = options.get("subpath")
    subpath_digest = None if subpath in (None, "") else fingerprint(subpath, "Volume subpath")
    driver = require_label(optional_field(definition, "driver", "local"), "Volume driver")
    driver_options = require_object(optional_field(definition, "driver_opts", {}), "Volume driver options")
    settings_digest = None
    if driver != "local" or driver_options:
        normalized_options = {key: driver_option_text(value) for key, value in driver_options.items()}
        serialized = json.dumps([driver, normalized_options], sort_keys=True, ensure_ascii=True)
        settings_digest = hashlib.sha256(serialized.encode("ascii")).hexdigest()
    external = optional_field(definition, "external", False)
    if not isinstance(external, bool):
        raise InputError("Named volume external status must be a boolean.")
    return Mount("volume", name, subpath_digest, settings_digest, external)


def extract_rendered(document):
    raw_services = require_object(document.get("services"), "Compose services")
    volume_definitions = require_object(optional_field(document, "volumes", {}), "Compose volume definitions")
    services = {}
    for name, service in raw_services.items():
        require_label(name, "Service name")
        require_object(service, "Compose service")
        entries = optional_field(service, "volumes", [])
        inherited = optional_field(service, "volumes_from", [])
        profiles = optional_field(service, "profiles", [])
        if not all(isinstance(value, list) for value in (entries, inherited, profiles)):
            raise InputError("Service volumes, volumes_from and profiles must be JSON arrays.")
        mounts = {}
        seen_targets = set()
        for entry in entries:
            require_object(entry, "Mount entry from docker compose config --format json")
            target = require_label(entry.get("target"), "Mount target")
            if target in seen_targets:
                raise InputError("A service declares the same mount target more than once.")
            seen_targets.add(target)
            mount = rendered_mount(entry, volume_definitions)
            if mount is not None:
                mounts[target] = mount
        services[name] = Service(mounts, bool(inherited), bool(profiles))
    return services


def require_digest(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise InputError("Snapshot contains an invalid mount fingerprint.")


def load_saved_mount(value):
    raw = require_object(value, "Snapshot mount")
    kind = raw.get("kind")
    source = raw.get("source")
    subpath = raw.get("subpath")
    driver_settings = raw.get("driver_settings")
    external = raw.get("external", False)
    if not isinstance(external, bool) or (external and kind != "volume"):
        raise InputError("Snapshot contains an invalid external volume status.")
    if kind == "volume":
        require_label(source, "Resolved Docker volume name")
        if subpath is not None:
            require_digest(subpath)
        if driver_settings is not None:
            require_digest(driver_settings)
    elif kind == "bind":
        require_digest(source)
        if subpath is not None or driver_settings is not None:
            raise InputError("Snapshot bind mount contains unexpected options.")
    elif kind not in ("anonymous", "unknown") or any(
        value is not None for value in (source, subpath, driver_settings)
    ):
        raise InputError("Snapshot contains an invalid mount type or source.")
    return Mount(kind, source, subpath, driver_settings, external)


def load_mounts(path):
    document = read_document(path)
    if "format" not in document:
        return extract_rendered(document)
    if document["format"] != FORMAT:
        raise InputError("Unsupported snapshot format.")
    services = {}
    for name, value in require_object(document.get("services"), "Snapshot services").items():
        require_label(name, "Service name")
        raw = require_object(value, "Snapshot service")
        inherited = raw.get("inherits_mounts")
        uses_profiles = raw.get("uses_profiles", False)
        if not isinstance(inherited, bool) or not isinstance(uses_profiles, bool):
            raise InputError("Snapshot service contains invalid profile or inherited-mount status.")
        mounts = {}
        for target, mount in require_object(raw.get("mounts"), "Snapshot service mounts").items():
            require_label(target, "Mount target")
            mounts[target] = load_saved_mount(mount)
        services[name] = Service(mounts, inherited, uses_profiles)
    return services


def compare_mount(previous, candidate, candidate_service):
    if candidate is None:
        if candidate_service is None:
            detail = f"The candidate has no service for the baseline mount from {previous.description()}."
        else:
            detail = f"The candidate no longer declares this data mount from {previous.description()}."
        return "mount_missing", detail
    if previous.kind == "unknown" or candidate.kind == "unknown":
        return "unverified", "This mount uses a type whose data continuity cannot be checked."
    if previous.kind != candidate.kind:
        return "mount_changed", f"The configured source changes from {previous.description()} to {candidate.description()}."
    if previous.kind == "anonymous":
        return "unverified", "Compose configuration does not identify which anonymous volume is attached."
    if previous.source != candidate.source:
        if previous.kind == "bind":
            return "mount_changed", "The configured host bind source changes. Both host paths are omitted."
        return "mount_changed", f"The configured source changes from {previous.description()} to {candidate.description()}."
    if previous.subpath != candidate.subpath:
        return "mount_changed", f"The configured subdirectory changes within {previous.description()}."
    if previous.driver_settings != candidate.driver_settings:
        return "unverified", f"Driver settings change for {previous.description()}. Verify the existing volume on the Docker host."
    if previous.external != candidate.external:
        return "unverified", f"The management of {previous.description()} changes between Compose-managed and external."
    return None


def resolve_service_renames(arguments, baseline, candidate):
    renames = {}
    destinations = set()
    for argument in arguments:
        parts = argument.split("=")
        if len(parts) != 2:
            raise InputError("Use --rename-service OLD=NEW with exactly one equals sign.")
        old = require_label(parts[0], "Baseline service name")
        new = require_label(parts[1], "Candidate service name")
        if old not in baseline or old in candidate:
            raise InputError(f"Rename source {quote(old)} must exist only in the baseline.")
        if new not in candidate or new in baseline:
            raise InputError(f"Rename destination {quote(new)} must exist only in the candidate.")
        if old in renames or new in destinations:
            raise InputError("Service rename mappings must be one-to-one.")
        renames[old] = new
        destinations.add(new)
    return renames


def compare(baseline, candidate, service_renames):
    findings = []
    for name, before in sorted(baseline.items()):
        after = candidate.get(service_renames.get(name, name))
        if after is None and before.uses_profiles and (before.mounts or before.inherits_mounts):
            detail = "The candidate does not include this profiled service. Check which Compose profiles were rendered."
            findings.append(Finding("unverified", name, None, detail))
            continue
        if before.inherits_mounts or (after and after.inherits_mounts):
            findings.append(Finding("unverified", name, None, "Mounts inherited through volumes_from cannot be checked."))
        for target, previous in sorted(before.mounts.items()):
            result = compare_mount(previous, after.mounts.get(target) if after else None, after)
            if result is not None:
                code, detail = result
                findings.append(Finding(code, name, target, detail))
        if after:
            for target in sorted(after.mounts.keys() - before.mounts.keys()):
                mount = after.mounts[target]
                code = "unverified" if mount.kind == "unknown" else "mount_added"
                detail = f"The candidate adds a mount from {mount.description()} at this location."
                findings.append(Finding(code, name, target, detail))
    return findings


def write_snapshot(text, destination):
    if destination is None:
        sys.stdout.write(text)
        return
    path = Path(destination)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="save the mount fields from a rendered Compose configuration")
    snapshot.add_argument("config", nargs="?", default="-", help="rendered JSON file, or - for stdin")
    snapshot.add_argument("-o", "--output", help="write or atomically replace this snapshot file")
    check = commands.add_parser("check", help="review mount changes against a snapshot or rendered configuration")
    check.add_argument("baseline", help="baseline snapshot or rendered JSON file, or - for stdin")
    check.add_argument("candidate", nargs="?", default="-", help="candidate snapshot or rendered JSON file, or - for stdin")
    check.add_argument("--format", choices=("text", "json"), default="text", help="report format")
    check.add_argument(
        "--rename-service", action="append", default=[], metavar="OLD=NEW",
        help="compare a removed baseline service with its explicitly renamed candidate service; repeat as needed",
    )
    args = parser.parse_args()
    if args.command == "snapshot":
        try:
            services = extract_rendered(read_document(args.config))
        except (InputError, OSError) as error:
            print(f"Input: {error if isinstance(error, InputError) else 'Could not read input.'}", file=sys.stderr)
            return 2
        data = {"format": FORMAT, "services": {name: asdict(service) for name, service in services.items()}}
        serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if len(serialized) > MAX_INPUT_BYTES:
            print("The resulting snapshot exceeds the input size limit.", file=sys.stderr)
            return 2
        try:
            write_snapshot(serialized, args.output)
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                detail = " The output directory does not exist."
            elif isinstance(error, NotADirectoryError):
                detail = " A component of the output directory is not a directory."
            elif isinstance(error, PermissionError):
                detail = " Permission denied for the output file or directory."
            else:
                detail = ""
            print(f"Could not write the snapshot.{detail}", file=sys.stderr)
            return 2
        return 0

    if args.baseline == args.candidate == "-":
        parser.error("only one input can be read from stdin")
    inputs = []
    for label, path in (("Baseline", args.baseline), ("Candidate", args.candidate)):
        try:
            inputs.append(load_mounts(path))
        except (InputError, OSError) as error:
            print(f"{label}: {error if isinstance(error, InputError) else 'Could not read input.'}", file=sys.stderr)
            return 2

    baseline, candidate = inputs
    try:
        service_renames = resolve_service_renames(args.rename_service, baseline, candidate)
    except InputError as error:
        print(f"Service rename: {error}", file=sys.stderr)
        return 2
    findings = compare(baseline, candidate, service_renames)
    baseline_count = sum(len(service.mounts) for service in baseline.values())
    if args.format == "json":
        report = {"baseline_mounts": baseline_count, "findings": [asdict(item) for item in findings]}
        if service_renames:
            report["service_renames"] = service_renames
        print(json.dumps(report, indent=2))
    else:
        for old, new in service_renames.items():
            print(f"Comparing renamed service {quote(old)} to {quote(new)}.")
        for item in findings:
            location = f"service {quote(item.service)}"
            if item.target is not None:
                location += f", target {quote(item.target)}"
            print(f"[{item.code}] {location}: {item.detail}")
        if not findings:
            if baseline_count:
                print(f"No configured data mount changes in {baseline_count} baseline mount(s).")
            else:
                print("No baseline data mounts to compare.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
