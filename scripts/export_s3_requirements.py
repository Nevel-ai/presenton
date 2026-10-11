"""Export the locked S3 dependency closure used by both Docker builds."""

import argparse
from pathlib import Path
import tomllib


BACKEND = Path(__file__).resolve().parents[1] / "servers" / "fastapi"
OUTPUT = BACKEND / "requirements-s3.txt"


def render_requirements() -> str:
    with (BACKEND / "uv.lock").open("rb") as source:
        lock = tomllib.load(source)
    with (BACKEND / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)

    packages: dict[str, list[dict]] = {}
    for package in lock["package"]:
        packages.setdefault(package["name"], []).append(package)

    selected = {}
    pending = ["boto3", "botocore", "s3transfer"]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        candidates = packages.get(name, [])
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one locked version of S3 dependency {name}")
        package = candidates[0]
        selected[name] = package["version"]
        pending.extend(dependency["name"] for dependency in package.get("dependencies", []))

    for name in ("boto3", "botocore", "s3transfer"):
        if f"{name}=={selected[name]}" not in project["project"]["dependencies"]:
            raise ValueError(f"pyproject.toml must pin {name} to its locked version")

    header = (
        "# Generated from uv.lock; do not edit by hand.\n"
        "# Regenerate: python scripts/export_s3_requirements.py\n"
        "# Verify: python scripts/export_s3_requirements.py --check\n"
        "# Used as requirements and constraints so later installs retain the S3 graph.\n"
    )
    return header + "".join(f"{name}=={selected[name]}\n" for name in sorted(selected))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the export is stale")
    args = parser.parse_args()
    expected = render_requirements()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != expected:
            raise SystemExit("requirements-s3.txt is stale; run scripts/export_s3_requirements.py")
        print("S3 requirements match uv.lock")
    else:
        OUTPUT.write_text(expected)


if __name__ == "__main__":
    main()
