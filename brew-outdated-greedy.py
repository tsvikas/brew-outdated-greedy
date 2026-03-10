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


def get_app_paths(cask_name: str) -> list[str]:
    """Extract .app paths from cask artifacts."""
    r = run(["brew", "info", "--cask", "--json=v2", cask_name])
    if r.returncode != 0:
        return []
    cask = json.loads(r.stdout)["casks"][0]
    apps = []
    for artifact in cask.get("artifacts", []):
        if isinstance(artifact, dict) and "app" in artifact:
            apps.extend(artifact["app"])
        # Some casks use 'uninstall' with 'delete' listing .app paths
        if isinstance(artifact, dict) and "uninstall" in artifact:
            for entry in artifact["uninstall"]:
                if isinstance(entry, dict):
                    for path in entry.get("delete", []):
                        if path.endswith(".app"):
                            apps.append(path)
    return apps


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

        # Try CFBundleShortVersionString first (human-readable)
        for key in ["CFBundleShortVersionString", "CFBundleVersion"]:
            r = run(["defaults", "read", info_plist, key])
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()

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


def version_matches(actual: str, latest: str) -> bool:
    """Check if the actual version matches (or contains) the latest version."""
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
    return False


def main():
    casks = get_outdated_casks()
    if not casks:
        print("All casks are up to date!")
        return

    results = {"outdated": [], "up_to_date": [], "unknown": []}

    for cask in casks:
        name = cask["name"]
        brew_installed = cask["installed_versions"][0]
        latest = cask["current_version"]

        app_paths = get_app_paths(name)
        actual = None
        for app in app_paths:
            actual = read_bundle_version(app)
            if actual:
                break

        if actual is None:
            results["unknown"].append({
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

    # Print results
    if results["outdated"]:
        print(f"\n{'='*60}")
        print(f" Actually outdated ({len(results['outdated'])})")
        print(f"{'='*60}")
        for c in results["outdated"]:
            print(f"  {c['name']}")
            print(f"    running:   {c['actual']}")
            print(f"    latest:    {c['latest']}")
            if normalize_version(c["brew_installed"]) != normalize_version(c["actual"]):
                print(f"    (brew receipt: {c['brew_installed']})")

    if results["up_to_date"]:
        print(f"\n{'='*60}")
        print(f" Already up to date ({len(results['up_to_date'])})")
        print(f"{'='*60}")
        for c in results["up_to_date"]:
            print(f"  {c['name']}  {c['actual']}")
            if normalize_version(c["brew_installed"]) != normalize_version(c["actual"]):
                print(f"    (brew receipt outdated: {c['brew_installed']})")

    if results["unknown"]:
        print(f"\n{'='*60}")
        print(f" Could not detect version ({len(results['unknown'])})")
        print(f"{'='*60}")
        for c in results["unknown"]:
            print(f"  {c['name']}")
            print(f"    brew receipt: {c['brew_installed']}")
            print(f"    latest:       {c['latest']}")

    print()


if __name__ == "__main__":
    main()
