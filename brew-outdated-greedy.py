#!/usr/bin/env python3
"""Check which Homebrew casks are *actually* outdated.

`brew outdated --greedy` reports casks where the installed receipt differs
from the latest formula, but auto-updating apps (Chrome, Slack, …) often
update themselves. This script reads the real version from the .app bundle
and only flags casks that are genuinely behind.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def get_outdated_casks() -> list[dict]:
    r = run(["brew", "outdated", "--greedy", "--cask", "--json=v2"])
    if r.returncode != 0:
        print(f"brew outdated failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(r.stdout)["casks"]


def get_all_app_paths(cask_names: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Extract .app paths and binary symlinks for all casks in a single brew call."""
    if not cask_names:
        return {}, {}
    r = run(["brew", "info", "--cask", "--json=v2"] + cask_names)
    if r.returncode != 0:
        return {}, {}
    prefix = os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew")
    result = {}
    binaries_map = {}
    for cask in json.loads(r.stdout)["casks"]:
        apps = []
        binaries = []
        bundle_ids = []
        for artifact in cask.get("artifacts", []):
            if isinstance(artifact, dict) and "app" in artifact:
                apps.extend(artifact["app"])
            if isinstance(artifact, dict) and "binary" in artifact:
                source = artifact["binary"][0]
                binaries.append(
                    artifact.get("target") or os.path.join(prefix, "bin", os.path.basename(source))
                )
            if isinstance(artifact, dict) and "uninstall" in artifact:
                for entry in artifact["uninstall"]:
                    if isinstance(entry, dict):
                        for path in entry.get("delete", []):
                            if path.endswith(".app"):
                                apps.append(path)
                        # Collect bundle IDs from quit entries for pkg-based casks
                        for bid in entry.get("quit", []):
                            bundle_ids.append(bid)
        # For pkg-based casks with no .app artifact, use mdfind to locate the app
        if not apps and bundle_ids:
            for bid in bundle_ids:
                r = run(["mdfind", f"kMDItemCFBundleIdentifier == '{bid}'"])
                if r.returncode == 0:
                    for line in r.stdout.strip().splitlines():
                        if line.endswith(".app"):
                            apps.append(line)
                            break
                if apps:
                    break
        result[cask["token"]] = apps
        binaries_map[cask["token"]] = binaries
    return result, binaries_map


def read_sdk_version(binary_path: str) -> str | None:
    """Read version from a VERSION file next to a CLI tool's install root.

    Self-updating CLI SDKs (e.g. gcloud-cli) ship no .app, but keep a
    VERSION file a few levels above the real binary that the symlink points to.
    """
    import re
    real = Path(os.path.realpath(binary_path))
    if not real.exists():
        return None
    for parent in list(real.parents)[:3]:
        version_file = parent / "VERSION"
        if version_file.is_file():
            v = version_file.read_text().strip()
            if re.fullmatch(r"\d+(\.\d+)+", v):
                return v
    return None


def read_bundle_version(app_path: str) -> str | None:
    """Read version from an .app bundle using defaults read."""
    # Expand ~ and resolve
    app_path = os.path.expanduser(app_path)

    # Try common locations
    candidates = [app_path]
    if not app_path.startswith("/"):
        candidates = [
            f"/Applications/{app_path}",
            os.path.expanduser(f"~/Applications/{app_path}"),
        ]

    for path in candidates:
        info_plist = os.path.join(path, "Contents", "Info")
        if not os.path.isdir(path):
            continue

        # Read both version keys and pick the most specific one
        versions = {}
        for key in ["CFBundleShortVersionString", "CFBundleVersion"]:
            r = run(["defaults", "read", info_plist, key])
            if r.returncode == 0 and r.stdout.strip():
                versions[key] = r.stdout.strip()
        short = versions.get("CFBundleShortVersionString")
        full = versions.get("CFBundleVersion")
        if short and full:
            # Prefer the longer version when one is a prefix of the other
            if full.startswith(short + ".") or full.startswith(short + ","):
                return full
            return short
        if short or full:
            return short or full

        # Fallback: mdls
        r = run(["mdls", "-name", "kMDItemVersion", "-raw", path])
        if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "(null)":
            return r.stdout.strip()

    return None


def normalize_version(v: str) -> str:
    """Normalize version for comparison.

    Strips build metadata (after comma or hyphen with hex hash),
    so e.g. "3.5.5-95c667e3" -> "3.5.5" and "3.3.1,3.3.1.75249" -> "3.3.1".
    """
    import re
    # Strip comma-separated build info
    v = v.split(",")[0]
    # Strip hyphen followed by hex hash (e.g. -95c667e3)
    v = re.sub(r"-[0-9a-f]{6,}$", "", v)
    return v


def parse_version_tuple(v: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison.

    Non-numeric parts are kept as strings so comparison still works,
    but numeric segments are compared numerically (2 < 10, not "2" > "10").
    """
    import re
    parts = re.split(r"[.\-]", v)
    result = []
    for p in parts:
        try:
            result.append(int(p))
        except ValueError:
            result.append(p)
    return tuple(result)


def version_matches(actual: str, latest: str) -> bool:
    """Check if the actual version is the same as or newer than latest."""
    a = normalize_version(actual)
    l = normalize_version(latest)
    # Direct match
    if a == l:
        return True
    # actual might be the full build string that contains the version
    # e.g. actual="3.3.1.75249" and latest normalized="3.3.1"
    # Check if one starts with the other
    if a.startswith(l + ".") or l.startswith(a + "."):
        return True
    # If actual is newer than latest, it's not outdated
    try:
        if parse_version_tuple(a) >= parse_version_tuple(l):
            return True
    except TypeError:
        pass
    return False


def update_size(running: str, latest: str) -> float:
    """Score how big the jump from running to latest is, on a log10 scale.

    Read the version as one number: the significant digits the versions
    share, followed by the first differing component (leading zeros don't
    count), with the running component zero-padded to the latest one's
    width (1.9 -> 1.10 reads as 109 -> 110). The score is log10 of the
    step in that component relative to that number. A jump from 0 is read
    one decade down, as from 1 to ten times the target (0.9 -> 1.0 like
    1 -> 10, 0.1 -> 2.0 like 1 -> 20). So 1.0 -> 2.0 scores 0,
    1.0 -> 3.0 scores 0.3, 0.2 -> 0.3 scores -0.3, 11.0 -> 12.0 (1/11) scores -1.04,
    2.6 -> 2.7 (1/26) outranks 0.23.1 -> 0.23.4 (3/231), and
    133 -> 134 (1/133) outranks 592 -> 593 (1/592).

    Only that one component is compared numerically, so a carry inside it
    counts by its size (3.29 -> 3.30 scores like 3.28 -> 3.29, not like
    3.2 -> 3.3), while a carry into an earlier component keeps its rank
    (1.9.9 -> 2.0.0 scores 0, like 1.0 -> 2.0).
    """
    import math
    import re
    a = [int(x) for x in re.findall(r"\d+", normalize_version(running))]
    b = [int(x) for x in re.findall(r"\d+", normalize_version(latest))]
    a += [0] * (len(b) - len(a))
    b += [0] * (len(a) - len(b))
    prefix = ""  # significant digits the versions agree on so far
    for x, y in zip(a, b):
        if x != y:
            # Pad x to y's width, so 1.9 -> 1.10 reads as 109 -> 110
            base = int(prefix + str(x).zfill(len(str(y))))
            if base == 0:
                # 0 -> n is infinite growth; read it as 1 -> 10n
                return math.log10(10 * y - 1)
            return math.log10(abs(y - x) / base)
        if prefix or x:
            prefix += str(x)
    return -math.inf  # no numeric difference, e.g. only a letter suffix


def main():
    casks = get_outdated_casks()
    if not casks:
        print("All casks are up to date!")
        return

    results = {"outdated": [], "up_to_date": [], "missing": [], "unknown": []}

    app_paths_map, binaries_map = get_all_app_paths([c["name"] for c in casks])

    for cask in casks:
        name = cask["name"]
        brew_installed = cask["installed_versions"][0]
        latest = cask["current_version"]

        app_paths = app_paths_map.get(name, [])
        actual = None
        for app in app_paths:
            actual = read_bundle_version(app)
            if actual:
                break
        if actual is None:
            for binary in binaries_map.get(name, []):
                actual = read_sdk_version(binary)
                if actual:
                    break

        if actual is None:
            # The cask names an app, but it's not on disk: deleted outside
            # brew, or a manual installer step that never completed
            key = "missing" if app_paths and not binaries_map.get(name) else "unknown"
            results[key].append({
                "name": name,
                "brew_installed": brew_installed,
                "latest": latest,
            })
            continue

        entry = {
            "name": name,
            "actual": actual,
            "brew_installed": brew_installed,
            "latest": latest,
        }

        if version_matches(actual, latest):
            results["up_to_date"].append(entry)
        else:
            results["outdated"].append(entry)

    # Biggest jumps first
    results["outdated"].sort(key=lambda c: (-update_size(c["actual"], c["latest"]), c["name"]))

    # Build rows: (name, running, latest, status)
    rows = []
    for c in results["outdated"]:
        rows.append((c["name"], c["actual"], normalize_version(c["latest"]), "OUTDATED"))
    for c in results["missing"]:
        rows.append((c["name"], "-", normalize_version(c["latest"]), "MISSING"))
    for c in results["unknown"]:
        rows.append((c["name"], c["brew_installed"] + " *", normalize_version(c["latest"]), "UNKNOWN"))
    for c in results["up_to_date"]:
        rows.append((c["name"], c["actual"], normalize_version(c["latest"]), "ok"))

    if not rows:
        print("All casks are up to date!")
        return

    # Calculate column widths
    headers = ("Cask", "Running", "Latest", "Status")
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt_row(cols):
        return "  ".join(c.ljust(w) for c, w in zip(cols, widths))

    print()
    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))

    prev_status = None
    for row in rows:
        if prev_status and prev_status != row[3]:
            print()
        print(fmt_row(row))
        prev_status = row[3]

    # Summary
    n_out = len(results["outdated"])
    n_ok = len(results["up_to_date"])
    n_unk = len(results["unknown"])
    n_miss = len(results["missing"])
    print()
    print(f"{n_out} outdated, {n_ok} already up to date, {n_miss} missing, {n_unk} unknown")
    if results["missing"]:
        print("(MISSING = brew lists the cask, but its app is not on disk)")
    if results["unknown"]:
        print("(* = version from brew receipt, app bundle not found)")


if __name__ == "__main__":
    main()
