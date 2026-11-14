#!/usr/bin/env python3
"""AndroidForge — Self-Healing System.

Aplica correções não destrutivas no workspace extraído, sem alterar o ZIP
original. Mantém a interface CLI utilizada pelo workflow AndroidForge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def parse_agp_version(content: str) -> str | None:
    patterns = [
        r'id\s*\(\s*["\']com\.android\.application["\']\s*\)\s*version\s*["\']([^"\']+)',
        r'id\s+["\']com\.android\.application["\'].*?version\s*["\']([^"\']+)',
        r"com\.android\.tools\.build:gradle:([^'\"\s]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()
    return None


def parse_gradle_wrapper_version(content: str) -> str | None:
    match = re.search(r"gradle-([\d.]+)-\w+\.zip", content)
    return match.group(1) if match else None


def version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (0,)


AGP_TO_MIN_GRADLE = {
    "8.7": "8.9", "8.6": "8.7", "8.5": "8.7", "8.4": "8.6",
    "8.3": "8.4", "8.2": "8.2", "8.1": "8.0", "8.0": "8.0",
    "7.4": "7.5", "7.3": "7.4", "7.2": "7.3.3", "7.1": "7.2",
    "7.0": "7.0.2", "4.2": "6.7.1", "4.1": "6.5", "4.0": "6.1.1",
    "3.6": "5.6.4", "3.5": "5.4.1", "3.4": "5.1.1",
    "3.3": "4.10.1", "3.2": "4.6", "3.1": "4.4", "3.0": "4.1",
}


def min_gradle_for_agp(agp: str | None) -> str | None:
    if not agp:
        return None
    parts = agp.split(".")
    if len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in AGP_TO_MIN_GRADLE:
        return AGP_TO_MIN_GRADLE[f"{parts[0]}.{parts[1]}"]
    return next((v for k, v in AGP_TO_MIN_GRADLE.items()
                 if k.split(".")[0] == parts[0]), None)


def heal_gradle_wrapper(root: Path, agp_version: str | None, fixes: list[dict]) -> None:
    minimum = min_gradle_for_agp(agp_version)
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if not minimum:
        return
    if not props.exists():
        fixes.append({"name": "no_wrapper_present", "action": "none",
                      "reason": "No gradle-wrapper.properties; build uses system Gradle."})
        return
    content = read_file(props)
    current = parse_gradle_wrapper_version(content)
    if not current or version_tuple(current) >= version_tuple(minimum):
        return
    updated = re.sub(
        r"distributionUrl=[^\s]+",
        f"distributionUrl=https\\\\://services.gradle.org/distributions/gradle-{minimum}-bin.zip",
        content,
    )
    if updated != content:
        props.write_text(updated, encoding="utf-8")
        fixes.append({"name": "bump_gradle_wrapper", "action": "updated distributionUrl",
                      "reason": f"Gradle {current} is below the minimum {minimum} for AGP {agp_version}.",
                      "from": current, "to": minimum})


def heal_missing_gradlew(root: Path, fixes: list[dict]) -> None:
    gradlew = root / "gradlew"
    jar = root / "gradle" / "wrapper" / "gradle-wrapper.jar"
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if (gradlew.exists() and jar.exists()) or not props.exists():
        return
    try:
        result = subprocess.run(
            ["gradle", "wrapper", "--distribution-type", "bin"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and gradlew.exists() and jar.exists():
            fixes.append({"name": "regenerate_gradlew", "action": "ran gradle wrapper",
                          "reason": "Missing wrapper files were regenerated."})
        else:
            fixes.append({"name": "regenerate_gradlew_failed", "action": "none",
                          "reason": f"gradle wrapper exited {result.returncode}: {result.stderr[:200]}"})
    except Exception as exc:
        fixes.append({"name": "regenerate_gradlew_failed", "action": "none",
                      "reason": f"Could not regenerate wrapper: {exc}"})


def heal_missing_local_properties(root: Path, android_sdk: str, fixes: list[dict]) -> None:
    target = root / "local.properties"
    if target.exists() and "sdk.dir" in read_file(target):
        return
    target.write_text(f"sdk.dir={android_sdk}\n", encoding="utf-8")
    fixes.append({"name": "create_local_properties", "action": "wrote local.properties",
                  "reason": f"Set sdk.dir={android_sdk}"})


def detect_duplicate_classes(root: Path, warnings: list[dict]) -> None:
    found: dict[str, list[Path]] = {}
    for pattern in ("*.kt", "*.java"):
        for path in root.rglob(pattern):
            if "build" in path.parts:
                continue
            source = read_file(path)
            pkg = re.search(r"^package\s+([\w.]+)", source, re.MULTILINE)
            package = pkg.group(1) if pkg else ""
            for match in re.finditer(
                r"^\s*(?:(?:public|internal|private)\s+)?(?:final\s+)?"
                r"(?:class|object|interface|enum class)\s+(\w+)",
                source, re.MULTILINE,
            ):
                fqcn = f"{package}.{match.group(1)}" if package else match.group(1)
                found.setdefault(fqcn, []).append(path)
    for name, paths in found.items():
        if len(paths) > 1:
            warnings.append({"kind": "duplicate_class", "class": name,
                             "files": [str(p.relative_to(root)) for p in paths],
                             "hint": "Duplicate top-level class declaration."})


def detect_repositories_mode_conflict(root: Path, warnings: list[dict]) -> None:
    settings = [root / "settings.gradle", root / "settings.gradle.kts"]
    if not any("FAIL_ON_PROJECT_REPOS" in read_file(p) for p in settings if p.exists()):
        return
    for build_file in (root / "build.gradle", root / "build.gradle.kts",
                       root / "app" / "build.gradle", root / "app" / "build.gradle.kts"):
        if not build_file.exists():
            continue
        source = read_file(build_file)
        if re.search(r"^\s*repositories\s*\{", source, re.MULTILINE):
            warnings.append({"kind": "repositories_mode_conflict",
                             "file": str(build_file.relative_to(root)),
                             "hint": "Project repositories conflict with FAIL_ON_PROJECT_REPOS."})


def _has_namespace(source: str) -> bool:
    return bool(re.search(r"\bnamespace\s*(?:=|\s)\s*['\"][^'\"]+['\"]", source))


def _insert_namespace(source: str, namespace: str) -> str:
    """Insert namespace into the first android { } block, preserving DSL style."""
    match = re.search(r"(?m)^([ \t]*)android\s*\{", source)
    if not match:
        return source
    indent = match.group(1) + "    "
    line = f'{indent}namespace = "{namespace}"\n'
    # Groovy and Kotlin DSL both accept namespace = "value".
    return source[:match.end()] + "\n" + line + source[match.end():]


def _remove_manifest_package(source: str) -> str:
    """AGP 8+ requires namespace in Gradle, not package in source manifests."""
    opening = re.search(r"<manifest\b[^>]*>", source, re.IGNORECASE | re.DOTALL)
    if not opening:
        return source
    tag = opening.group(0)
    cleaned = re.sub(r'\s+package\s*=\s*("[^"]*"|\'[^\']*\')', "", tag, count=1)
    return source[:opening.start()] + cleaned + source[opening.end():]


def heal_android_manifest_package(root: Path, fixes: list[dict], warnings: list[dict]) -> None:
    """Ensure Android namespace is declared in Gradle; never add package to Manifest."""
    gradle_file = next((p for p in (
        root / "app" / "build.gradle",
        root / "app" / "build.gradle.kts",
    ) if p.exists()), None)
    if gradle_file is None:
        return

    gradle_source = read_file(gradle_file)
    manifests = list(root.rglob("AndroidManifest.xml"))
    if _has_namespace(gradle_source):
        # Even with a valid namespace, remove legacy package attributes that AGP rejects.
        changed = []
        for manifest in manifests:
            old = read_file(manifest)
            new = _remove_manifest_package(old)
            if new != old:
                manifest.write_text(new, encoding="utf-8")
                changed.append(str(manifest.relative_to(root)))
        if changed:
            fixes.append({"name": "remove_legacy_manifest_package",
                          "action": "removed package attribute from manifests",
                          "files": changed,
                          "reason": "Namespace is already declared in Gradle; AGP 8+ rejects manifest package attributes."})
        return

    appid = re.search(
        r"\bapplicationId\s*(?:=)?\s*['\"]([^'\"]+)['\"]",
        gradle_source,
    )
    if not appid:
        warnings.append({"kind": "namespace_not_added",
                         "file": str(gradle_file.relative_to(root)),
                         "hint": "No namespace or applicationId found; manual review required."})
        return

    namespace = appid.group(1).strip()
    updated_gradle = _insert_namespace(gradle_source, namespace)
    if updated_gradle == gradle_source:
        warnings.append({"kind": "namespace_not_added",
                         "file": str(gradle_file.relative_to(root)),
                         "hint": "No android { } block found; namespace was not inserted."})
        return

    gradle_file.write_text(updated_gradle, encoding="utf-8")
    cleaned_manifests = []
    for manifest in manifests:
        old = read_file(manifest)
        new = _remove_manifest_package(old)
        if new != old:
            manifest.write_text(new, encoding="utf-8")
            cleaned_manifests.append(str(manifest.relative_to(root)))

    fixes.append({"name": "add_gradle_namespace",
                  "action": f'added namespace = "{namespace}" to {gradle_file.relative_to(root)}',
                  "reason": "AGP 8+ requires namespace in the module Gradle file; package is not added to AndroidManifest.xml.",
                  "namespace": namespace,
                  "manifests_cleaned": cleaned_manifests})


def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge self-healing system")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or path)")
    parser.add_argument("--toolchain", required=True, help="Toolchain JSON (string or path)")
    parser.add_argument("--android-sdk", default=os.environ.get("ANDROID_HOME", "/opt/android-sdk"))
    parser.add_argument("--output", default=None, help="Write JSON report to this file")
    args = parser.parse_args()

    def load(value: str) -> Any:
        path = Path(value)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else json.loads(value)

    detect = load(args.detect)
    # Validate the argument even though toolchain data is not needed by current healers.
    load(args.toolchain)
    root = Path(args.root).resolve()
    fixes: list[dict] = []
    warnings: list[dict] = []

    agp_version = (detect.get("versions") or {}).get("agp_version")
    if not agp_version:
        for candidate in (root / "build.gradle.kts", root / "build.gradle",
                          root / "app" / "build.gradle.kts", root / "app" / "build.gradle"):
            if candidate.exists():
                agp_version = parse_agp_version(read_file(candidate))
                if agp_version:
                    break

    heal_gradle_wrapper(root, agp_version, fixes)
    heal_missing_gradlew(root, fixes)
    heal_missing_local_properties(root, args.android_sdk, fixes)
    heal_android_manifest_package(root, fixes, warnings)
    detect_duplicate_classes(root, warnings)
    detect_repositories_mode_conflict(root, warnings)

    report = {"project_root": str(root), "agp_version": agp_version,
              "fixes_applied": fixes, "warnings": warnings,
              "summary": f"{len(fixes)} fix(es) applied, {len(warnings)} warning(s)"}
    output = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as stream:
            stream.write(f"self_heal_fixes={len(fixes)}\n")
            stream.write(f"self_heal_warnings={len(warnings)}\n")
            stream.write(f"json<<EOF\n{json.dumps(report)}\nEOF\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n## Self-healing report\n\n")
            stream.write(f"{len(fixes)} fix(es) applied, {len(warnings)} warning(s)\n\n")
            if fixes:
                stream.write("### Fixes applied\n")
                for fix in fixes:
                    stream.write(f"- **{fix['name']}**: {fix.get('action', '')} — {fix.get('reason', '')}\n")
            if warnings:
                stream.write("\n### Warnings\n")
                for warning in warnings:
                    stream.write(f"- **{warning.get('kind', '?')}**: {warning.get('hint', '')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
