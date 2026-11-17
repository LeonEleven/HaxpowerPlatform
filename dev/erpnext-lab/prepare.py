#!/usr/bin/env python3
"""Materialize reviewed lab files; never run Docker, Bench or downloaded code."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets


def write_once(path: Path, content: bytes, mode: int = 0o644) -> None:
    if path.is_symlink():
        raise SystemExit(f"Refusing symlink: {path}")
    if path.exists():
        if path.read_bytes() != content:
            raise SystemExit(f"Existing file differs; review it before updating: {path}")
        return
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, default=Path.home() / "labs/haxpower-erpnext-v16")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    requested = args.lab_root.expanduser().absolute()
    if any(part.is_symlink() for part in (requested, *requested.parents)):
        raise SystemExit("Lab path must not traverse symlinks")
    lab = requested.resolve()
    project = source.parents[1]
    if lab == Path.home() or lab == Path("/") or lab.is_relative_to(project):
        raise SystemExit("Use a dedicated lab directory outside the project")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(lab)):
        raise SystemExit("Lab path must be an absolute path without spaces or shell metacharacters")
    if os.getuid() != 1000:
        raise SystemExit("This verified template expects host UID 1000; review ownership mapping first")
    lock = json.loads((source / "versions.lock.json").read_text())
    for item in lock["images"].values():
        if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", item["reference"]):
            raise SystemExit("All images must be pinned by SHA-256 digest")
    lab.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lab.stat().st_uid != os.getuid():
        raise SystemExit("Lab must belong to the current user")
    lab.chmod(0o700)
    for name in ("workspace", ".devcontainer", "secrets"):
        directory = lab / name
        if directory.is_symlink():
            raise SystemExit(f"Refusing symlink: {directory}")
        directory.mkdir(exist_ok=True, mode=0o700 if name == "secrets" else 0o755)
    (lab / "secrets").chmod(0o700)
    write_once(lab / "compose.yaml", (source / "compose.yaml").read_bytes())
    write_once(lab / ".devcontainer/devcontainer.json", (source / "devcontainer.json").read_bytes())
    write_once(lab / "versions.lock.json", (source / "versions.lock.json").read_bytes())
    environment = "\n".join([
        f"LAB_ROOT={lab}",
        f"BENCH_IMAGE={lock['images']['bench']['reference']}",
        f"MARIADB_IMAGE={lock['images']['mariadb']['reference']}",
        f"REDIS_IMAGE={lock['images']['redis']['reference']}",
        "",
    ])
    write_once(lab / ".env", environment.encode(), 0o600)
    secret = lab / "secrets/mariadb-root-password"
    if secret.is_symlink():
        raise SystemExit("Refusing secret symlink")
    if not secret.exists():
        # Bind-mounted Compose secrets retain host permissions. MariaDB's
        # unprivileged entrypoint must read the file; the enclosing 0700
        # directories prevent access by other host users.
        write_once(secret, (secrets.token_urlsafe(48) + "\n").encode(), 0o444)
    if not secret.is_file() or not re.fullmatch(r"[A-Za-z0-9_-]{32,}\n?", secret.read_text()):
        raise SystemExit("Existing secret is missing or invalid; do not rotate a live DB password here")
    print(f"Prepared {lab}; existing data and password retained. No containers or sites started.")


if __name__ == "__main__":
    main()
