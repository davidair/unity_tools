#!/usr/bin/env python3
"""
clone_blank_project.py

Clone a lean Unity project from a template directory, optionally enabling a profile and add-ons
by injecting packages into Packages/manifest.json.

Key design goals:
- Fast project creation (copy only Assets/Packages/ProjectSettings)
- Deterministic / reproducible manifests (stable JSON formatting)
- Strict but friendly CLI (template/profile/ide are required)
- Safe operation via --dry-run

Notes on Meta XR All-in-One:
- Although distributed via the Unity Asset Store, it is installed through Package Manager and may require
  entitlement/auth in the Editor. Injecting the dependency into manifest.json may not be sufficient for
  resolution in all environments. This script will inject the dependency and print a reminder.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional


# ----------------------------
# Package/version presets
# ----------------------------
# These are your collected "known-good" versions.
# Users can always override or add mappings using --pkg name=version (repeatable).
UNITY_LINE_PRESETS: Dict[str, Dict[str, str]] = {
    "2022": {
        "com.unity.ide.vscode": "1.2.5",
        "com.unity.ide.visualstudio": "2.0.22",
        "com.unity.ide.rider": "3.0.36",
        "com.unity.inputsystem": "1.18.0",        
        "com.unity.render-pipelines.universal": "14.0.12",
        "com.unity.xr.openxr": "1.14.3",
        "com.unity.xr.management": "4.4.0",
    },
    "6000.3": {
        # Unity 6: VS Code is supported via com.unity.ide.visualstudio; com.unity.ide.vscode is deprecated.
        "com.unity.ide.visualstudio": "2.0.27",
        "com.unity.ide.rider": "3.0.39",
        "com.unity.inputsystem": "1.18.0",
        "com.unity.render-pipelines.universal": "17.3.0",
        "com.unity.xr.openxr": "1.16.1",
        "com.unity.xr.management": "4.5.3",
        # Meta XR All-in-One (Asset Store distributed; may require editor entitlement/auth to resolve)
        "com.meta.xr.sdk.all": "85.0.0",
    },
}

# Profiles define baseline packages beyond what your template already contains.
PROFILE_DEFS: Dict[str, List[str]] = {
    "barebones-builtin": ["com.unity.inputsystem"],
    "barebones-urp": ["com.unity.inputsystem", "com.unity.render-pipelines.universal"],
}

# Add-ons define optional bundles.
ADDON_DEFS: Dict[str, List[str]] = {
    "openxr": ["com.unity.xr.management", "com.unity.xr.openxr"],
    "meta-all-in-one": ["com.meta.xr.sdk.all"],
}


# ----------------------------
# Data model
# ----------------------------

@dataclass(frozen=True)
class Options:
    project_name: str
    template: str
    profile: str
    ide: str
    addons: Tuple[str, ...]
    pkg_overrides: Dict[str, str]
    dry_run: bool


# ----------------------------
# Template discovery / Unity line detection
# ----------------------------

def discover_templates(base_dir: str) -> List[str]:
    """
    Consider a directory a template if it contains ProjectSettings/ProjectVersion.txt.
    """
    candidates: List[str] = []
    for name in os.listdir(base_dir):
        path = os.path.join(base_dir, name)
        if not os.path.isdir(path):
            continue
        pv = os.path.join(path, "ProjectSettings", "ProjectVersion.txt")
        if os.path.exists(pv):
            candidates.append(name)
    candidates.sort()
    return candidates


def read_template_editor_version(template_dir: str) -> Optional[str]:
    """
    Attempts to read the Unity editor version from ProjectSettings/ProjectVersion.txt.
    Typical content:
      m_EditorVersion: 2022.3.22f1
      m_EditorVersionWithRevision: 2022.3.22f1 (....)
    """
    pv = os.path.join(template_dir, "ProjectSettings", "ProjectVersion.txt")
    if not os.path.exists(pv):
        return None
    try:
        with open(pv, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("m_EditorVersion:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        return None
    return None


def detect_unity_line(template_dir: str) -> str:
    """
    Returns a preset bucket key, currently "2022" or "6000.3".
    Uses ProjectVersion.txt if possible; falls back to template name heuristic.
    """
    version = read_template_editor_version(template_dir)
    if version is not None:
        # Unity 6.x uses 6000.* versioning
        if version.startswith("6000."):
            # We bucket at 6000.3 specifically (your current Unity 6 line).
            # If you introduce 6000.0/6000.1 etc, expand this logic accordingly.
            if version.startswith("6000.3"):
                return "6000.3"
            return "6000.3"
        return "2022"

    lowered = os.path.basename(template_dir).lower()
    if "6000.3" in lowered or "unity6" in lowered or "6000" in lowered:
        return "6000.3"
    return "2022"


# ----------------------------
# Manifest helpers
# ----------------------------

def load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def parse_pkg_overrides(pairs: List[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"--pkg expects name=version, got: {pair}")
        name, version = pair.split("=", 1)
        name = name.strip()
        version = version.strip()
        if not name or not version:
            raise argparse.ArgumentTypeError(f"--pkg expects name=version, got: {pair}")
        overrides[name] = version
    return overrides


def compute_required_packages(unity_line: str, profile: str, ide: str, addons: Tuple[str, ...]) -> List[str]:
    """
    Compute the logical package list (no versions here).
    For Unity 6, `ide=vscode` maps to com.unity.ide.visualstudio because com.unity.ide.vscode is deprecated.
    """
    pkgs: List[str] = []

    # Profile
    pkgs.extend(PROFILE_DEFS[profile])

    # IDE (required)
    if ide == "msvs":
        pkgs.append("com.unity.ide.visualstudio")
    elif ide == "rider":
        pkgs.append("com.unity.ide.rider")
    elif ide == "vscode":
        if unity_line == "2022":
            pkgs.append("com.unity.ide.vscode")
        else:
            # Unity 6: VS Code supported via Visual Studio package
            pkgs.append("com.unity.ide.visualstudio")

    # Add-ons
    for addon in addons:
        pkgs.extend(ADDON_DEFS[addon])

    # De-dupe while preserving order
    seen = set()
    ordered: List[str] = []
    for p in pkgs:
        if p in seen:
            continue
        seen.add(p)
        ordered.append(p)

    return ordered


def resolve_versions(
    unity_line: str,
    packages: List[str],
    overrides: Dict[str, str],
) -> Tuple[Dict[str, str], List[str]]:
    """
    Returns (resolved_map, missing_packages) where resolved_map is pkg->version string.
    """
    preset = UNITY_LINE_PRESETS.get(unity_line, {})
    resolved: Dict[str, str] = {}
    missing: List[str] = []

    for pkg in packages:
        version = overrides.get(pkg)
        if version is None:
            version = preset.get(pkg)

        if version is None:
            missing.append(pkg)
            continue

        resolved[pkg] = version

    return resolved, missing


def apply_manifest_changes(manifest: Dict, resolved_versions: Dict[str, str]) -> Dict[str, Tuple[Optional[str], str]]:
    """
    Applies resolved_versions into manifest["dependencies"].
    Returns a change log: pkg -> (old_version_or_None, new_version)
    """
    deps = manifest.setdefault("dependencies", {})
    changes: Dict[str, Tuple[Optional[str], str]] = {}

    for pkg, version in resolved_versions.items():
        old = deps.get(pkg)
        if old == version:
            continue
        deps[pkg] = version
        changes[pkg] = (old, version)

    return changes


# ----------------------------
# File operations
# ----------------------------

def ensure_destination_is_safe(dest_dir: str) -> None:
    if os.path.exists(dest_dir):
        raise RuntimeError(f"Destination already exists: {dest_dir}")


def copy_project_skeleton(source_dir: str, dest_dir: str) -> None:
    for folder in ["Assets", "Packages", "ProjectSettings"]:
        src = os.path.join(source_dir, folder)
        if not os.path.exists(src):
            raise RuntimeError(f"Template is missing required folder: {src}")
        dst = os.path.join(dest_dir, folder)
        shutil.copytree(src, dst)


def patch_project_settings_assets(dest_dir: str, template_name: str, new_project_name: str) -> bool:
    """
    Replace common identifiers in ProjectSettings/ProjectSettings.assets.
    Returns True if file was modified.
    """
    settings_file = os.path.join(dest_dir, "ProjectSettings", "ProjectSettings.assets")
    if not os.path.exists(settings_file):
        return False

    with open(settings_file, "r", encoding="utf-8") as f:
        content = f.read()

    original = content

    # Replace template name occurrences in common fields.
    # This is conservative: it only touches lines that look like "field: <template_name>".
    fields = [
        "metroPackageName",
        "metroApplicationDescription",
        "productName",
    ]

    for field in fields:
        content = content.replace(f"{field}: {template_name}", f"{field}: {new_project_name}")

    if content != original:
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write(content)
        return True

    return False


# ----------------------------
# CLI
# ----------------------------

def parse_args(argv: List[str]) -> Options:
    script_directory = pathlib.Path(__file__).parent.resolve()
    templates = discover_templates(script_directory)

    epilog = """Examples:
  Create a Unity 2022 Built-in barebones project with Visual Studio integration:
    clone_blank_project.py MyGame --template BlankProject2022 --profile barebones-builtin --ide msvs

  Create a Unity 2022 URP barebones project with Rider + OpenXR:
    clone_blank_project.py MyVR --template BlankProject2022 --profile barebones-urp --ide rider --addons openxr

  Create a Unity 6 URP barebones project using VS Code (Unity 6 maps this to com.unity.ide.visualstudio):
    clone_blank_project.py MyUnity6 --template BlankProject6000.3 --profile barebones-urp --ide vscode

  Create a Unity 6 project with OpenXR + Meta All-in-One (may require Package Manager -> My Assets install):
    clone_blank_project.py MyQuest --template BlankProject6000.3 --profile barebones-urp --ide msvs --addons openxr meta-all-in-one

  Preview actions without writing anything:
    clone_blank_project.py MyGame --template BlankProject2022 --profile barebones-builtin --ide msvs --dry-run

  Override a package version if needed:
    clone_blank_project.py MyGame --template BlankProject6000.3 --profile barebones-urp --ide msvs --addons openxr \\
      --pkg com.unity.xr.openxr=1.16.1
"""

    parser = argparse.ArgumentParser(
        description="Clone a lean Unity project from a template and enable profiles/add-ons via manifest injection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument("project_name", help="New project folder name to create.")
    parser.add_argument(
        "--template",
        required=True,
        help=(
            "Template folder to clone from (must exist next to this script). "
            f"Discovered templates: {', '.join(templates) if templates else '(none found)'}"
        ),
    )
    parser.add_argument(
        "--profile",
        required=True,
        choices=sorted(PROFILE_DEFS.keys()),
        help="Baseline profile (pipeline + minimal packages).",
    )
    parser.add_argument(
        "--ide",
        required=True,
        choices=["msvs", "vscode", "rider"],
        help=(
            "IDE integration (required). "
            "Unity 6: vscode maps to com.unity.ide.visualstudio (com.unity.ide.vscode is deprecated)."
        ),
    )
    parser.add_argument(
        "--addons",
        nargs="*",
        default=[],
        choices=sorted(ADDON_DEFS.keys()),
        help="Optional add-ons to enable (space-separated).",
    )
    parser.add_argument(
        "--pkg",
        action="append",
        default=[],
        help="Override/add a package version mapping: name=version (repeatable).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan without copying or modifying anything.",
    )
    parser.add_argument(
        "--list-templates",
        action="store_true",
        help="List discovered templates and exit.",
    )

    # Hack - if --list-templates is specified, do not parse args.
    # This is needed because otherwise required args would trip an early failure

    if "--list-templates" in argv:
        if not templates:
            print("No templates discovered in current directory.")
        else:
            print("Discovered templates:")
            for t in templates:
                print(f"  - {t}")
        sys.exit(0)

    args = parser.parse_args(argv)

    overrides = parse_pkg_overrides(args.pkg)

    return Options(
        project_name=args.project_name,
        template=args.template,
        profile=args.profile,
        ide=args.ide,
        addons=tuple(args.addons),
        pkg_overrides=overrides,
        dry_run=args.dry_run,
    )


# ----------------------------
# Dry-run reporting
# ----------------------------

def print_plan(
    opts: Options,
    unity_line: str,
    source_dir: str,
    dest_dir: str,
    required_pkgs: List[str],
    resolved_versions: Dict[str, str],
    missing_versions: List[str],
) -> None:
    print("=== Plan ===")
    print(f"Project name : {opts.project_name}")
    print(f"Template     : {opts.template}")
    print(f"Unity line   : {unity_line}")
    editor_version = read_template_editor_version(source_dir)
    if editor_version is not None:
        print(f"Editor ver   : {editor_version}")
    print(f"Profile      : {opts.profile}")
    print(f"IDE          : {opts.ide}")
    if opts.addons:
        print(f"Add-ons      : {', '.join(opts.addons)}")
    else:
        print("Add-ons      : (none)")
    print(f"Destination  : {dest_dir}")
    if os.path.exists(dest_dir):
        print("Dest exists  : YES (would fail unless you choose a new name)")
    else:
        print("Dest exists  : no")

    print("\nCopy:")
    for folder in ["Assets", "Packages", "ProjectSettings"]:
        print(f"  - {os.path.join(source_dir, folder)} -> {os.path.join(dest_dir, folder)}")

    print("\nPatch:")
    print(f"  - Would patch ProjectSettings/ProjectSettings.assets: replace template name '{os.path.basename(source_dir)}' -> '{opts.project_name}'")
    print("  - Would patch Packages/manifest.json: inject/override dependencies listed below")

    print("\nPackages (logical):")
    for p in required_pkgs:
        print(f"  - {p}")

    print("\nPackages (resolved):")
    for p in required_pkgs:
        ver = resolved_versions.get(p)
        if ver is None:
            print(f"  - {p}: (MISSING VERSION)")
        else:
            print(f"  - {p}: {ver}")

    if missing_versions:
        print("\nWARNING: Missing versions for these packages (use --pkg name=version to supply):")
        for p in missing_versions:
            print(f"  - {p}")

    if "meta-all-in-one" in opts.addons:
        print(
            "\nNOTE: Meta XR All-in-One is distributed via the Unity Asset Store. "
            "If the package does not resolve automatically, open Unity -> Package Manager -> My Assets "
            "and install Meta XR All-in-One SDK, or ensure your environment is configured for UPM resolution."
        )

    if unity_line != "2022" and opts.ide == "vscode":
        print(
            "\nNOTE: Unity 6 supports VS Code via the Visual Studio integration package. "
            "After opening the project, set External Script Editor to VS Code in Unity Preferences."
        )


# ----------------------------
# Main
# ----------------------------

def main(argv: List[str]) -> int:
    try:
        opts = parse_args(argv)

        script_directory = pathlib.Path(__file__).parent.resolve()

        source_dir = script_directory /opts.template
        dest_dir = os.path.join(".", opts.project_name)

        if not os.path.isdir(source_dir):
            raise RuntimeError(f"Template directory not found: {source_dir}")

        unity_line = detect_unity_line(source_dir)
        required_pkgs = compute_required_packages(unity_line, opts.profile, opts.ide, opts.addons)
        resolved_versions, missing_versions = resolve_versions(unity_line, required_pkgs, opts.pkg_overrides)

        # Dry-run prints everything and exits successfully (unless template missing).
        if opts.dry_run:
            print_plan(opts, unity_line, source_dir, dest_dir, required_pkgs, resolved_versions, missing_versions)
            return 0

        # Real run
        ensure_destination_is_safe(dest_dir)

        copy_project_skeleton(source_dir, dest_dir)

        # Patch ProjectSettings.assets names based on template folder name -> new project name
        template_name = os.path.basename(os.path.normpath(source_dir))
        patched = patch_project_settings_assets(dest_dir, template_name, opts.project_name)

        # Patch manifest.json
        manifest_path = os.path.join(dest_dir, "Packages", "manifest.json")
        if not os.path.exists(manifest_path):
            raise RuntimeError(f"Missing manifest.json at: {manifest_path}")

        manifest = load_json(manifest_path)
        changes = apply_manifest_changes(manifest, resolved_versions)
        save_json(manifest_path, manifest)

        print(f"Created project: {dest_dir}")
        print(f"Template: {opts.template} (Unity line {unity_line})")
        print(f"Profile: {opts.profile}")
        print(f"IDE: {opts.ide}")
        if opts.addons:
            print(f"Add-ons: {', '.join(opts.addons)}")
        else:
            print("Add-ons: (none)")

        if patched:
            print("Patched: ProjectSettings/ProjectSettings.assets")
        else:
            print("Patched: (no changes) ProjectSettings/ProjectSettings.assets")

        if changes:
            print("Manifest changes:")
            for pkg in sorted(changes.keys()):
                old, new = changes[pkg]
                if old is None:
                    print(f"  + {pkg}: {new}")
                else:
                    print(f"  ~ {pkg}: {old} -> {new}")
        else:
            print("Manifest changes: (none)")

        if missing_versions:
            print("\nWARNING: Some requested packages had no version mapping and were NOT added:")
            for p in missing_versions:
                print(f"  - {p}")
            print("Use --pkg name=version to supply versions, or add them to UNITY_LINE_PRESETS.")

        if "meta-all-in-one" in opts.addons:
            print(
                "\nNOTE: Meta XR All-in-One is distributed via the Unity Asset Store. "
                "If the package does not resolve automatically, open Unity -> Package Manager -> My Assets "
                "and install Meta XR All-in-One SDK, or ensure your environment is configured for UPM resolution."
            )

        if unity_line != "2022" and opts.ide == "vscode":
            print(
                "\nNOTE: Unity 6 supports VS Code via the Visual Studio integration package. "
                "After opening the project, set External Script Editor to VS Code in Unity Preferences."
            )

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
