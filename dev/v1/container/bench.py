"""Prepare and operate the Bench inside the V1 volume."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from identity import identity, require


BENCH = Path("/bench/work/frappe-bench")
SITES = BENCH / "sites"
SOURCE = Path("/source/haxpower_v1")
APP = BENCH / "apps/haxpower_v1"
PYTHON = BENCH / "env/bin/python"
LOCK = json.loads((Path(__file__).resolve().parents[1] / "versions.lock.json").read_text())
PROJECT = os.environ["HP_V1_PROJECT"]
SITE, DATABASE = identity(PROJECT)
UPGRADE = f"{PROJECT}-upgrade.localhost"
TARGETS = {SITE: DATABASE}
if PROJECT.startswith("haxpower-v1-ci-"):
    TARGETS[UPGRADE] = f"{DATABASE}_upgrade"
WEB = os.environ["HP_V1_WEB_PORT"]
SOCKET = os.environ["HP_V1_SOCKET_PORT"]
COMMON = {
    "db_host": "mariadb",
    "db_port": 3306,
    "redis_cache": "redis://redis-cache:6379",
    "redis_queue": "redis://redis-queue:6379",
    "redis_socketio": "redis://redis-queue:6379",
    "socketio_port": int(SOCKET),
    "webserver_port": int(WEB),
    "serve_default_site": False,
    "chromium_path": "/usr/lib/chromium/chromium-headless-shell",
}
SECRET_MARKERS = {
    "@db-root@": Path("/run/secrets/db-root"),
    "@site-admin@": Path("/run/secrets/site-admin"),
}


def run(*args, cwd=BENCH, capture=False, input_text=None):
    return subprocess.run(args, cwd=cwd, text=True, check=True, capture_output=capture,
                          input=input_text).stdout


def site_command(*args, target=SITE, capture=False, expect_failure=False):
    """Keep secrets out of OS process arguments and filter known values before printing."""
    if any(arg in SECRET_MARKERS for arg in args):
        command = [PYTHON, "/opt/hp-v1/bench.py", "secret-command", target]
        cwd = SITES
        data = json.dumps(args)
    else:
        command = ["bench", "--site", target, *args]
        cwd = BENCH
        data = None
    result = subprocess.run(command, cwd=cwd, text=True, input=data, capture_output=True)
    output = redact(result.stdout + result.stderr)
    if expect_failure:
        require(result.returncode != 0, "injected migrate unexpectedly succeeded")
        return output
    if result.returncode or not capture:
        # Progress bars from the real Frappe CLI leave carriage returns in redirected logs.
        print(output, end="", flush=True)
    result.check_returncode()
    return output


def redact(output):
    known = [path.read_text().strip() for path in SECRET_MARKERS.values() if path.is_file()]
    for site in TARGETS:
        config = SITES / site / "site_config.json"
        if config.is_file() and config.resolve() == config:
            known += [value for key, value in json.loads(config.read_text()).items()
                      if isinstance(value, str) and re.search("password|secret|key|token", key)]
    for value in known:
        if value:
            output = output.replace(value, "[redacted]")
    return output


def secret_command(target):
    from frappe.utils.bench_helper import main

    require(target in TARGETS, "unexpected privileged Bench site")
    args = json.load(sys.stdin)
    require(args[0] in {"new-site", "restore"}, "unexpected privileged Bench action")
    sys.argv = ["bench", "frappe", "--site", target, *[
        SECRET_MARKERS[arg].read_text().strip() if arg in SECRET_MARKERS else arg for arg in args
    ]]
    main()


def source_files(root):
    require(root.is_dir() and root.resolve() == root, f"unexpected source root: {root}")
    files = {}
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"unexpected source symlink: {path.relative_to(root)}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[path.relative_to(root)] = path.read_bytes()
    return files


def app_import(name):
    return Path(run(PYTHON, "-c",
                    f"import importlib.util; print(importlib.util.find_spec({name!r}).origin)",
                    capture=True).strip())


def check_upstream(name):
    locked = LOCK["applications"][name]
    path = BENCH / "apps" / name
    require(path.resolve() == path, f"unexpected {name} checkout path")
    remotes = run("git", "remote", "-v", cwd=path, capture=True).splitlines()
    require(bool(remotes) and all(row.split()[1] == locked["repository"] for row in remotes),
            f"unexpected {name} git origin")
    require(run("git", "rev-parse", "HEAD", cwd=path, capture=True).strip() == locked["commit"]
            and not run("git", "status", "--porcelain", cwd=path, capture=True),
            f"{name} must match its pinned commit with a clean checkout")
    require(app_import(name) == path / name / "__init__.py", f"unexpected {name} import source")


def verify_tools():
    tools = {"python": ["python", "--version"], "node": ["node", "--version"],
             "yarn": ["yarn", "--version"], "bench": ["bench", "--version"], "uv": ["uv", "--version"]}
    for name, command in tools.items():
        actual = run(*command, cwd=BENCH.parent, capture=True).strip()
        require(re.search(rf"(?<![0-9.]){re.escape(LOCK['tools'][name])}(?![0-9.])", actual),
                f"unexpected {name} version")


def prepare():
    verify_tools()
    for name, app in LOCK["applications"].items():
        ref = f"refs/tags/{app['tag']}"
        actual = run("git", "ls-remote", "--exit-code", app["repository"], ref, ref + "^{}",
                     cwd=BENCH.parent, capture=True)
        refs = {tag: commit for commit, tag in (row.split() for row in actual.splitlines())}
        require(refs.get(ref + "^{}", refs.get(ref)) == app["commit"], f"{name} tag drift")
    if not BENCH.exists():
        frappe = LOCK["applications"]["frappe"]
        run("bench", "init", "--frappe-path", frappe["repository"], "--frappe-branch", frappe["tag"],
            "--python", sys.executable, "--skip-redis-config-generation", "--skip-assets", "--no-backups",
            BENCH.name, cwd=BENCH.parent)
    check_upstream("frappe")
    write_common()
    check_targets()
    if not (BENCH / "apps/erpnext").exists():
        erpnext = LOCK["applications"]["erpnext"]
        run("bench", "get-app", "--branch", erpnext["tag"], "--skip-assets", erpnext["repository"])
    check_upstream("erpnext")
    deploy_app()
    run("bench", "build", "--app", "frappe,erpnext", "--production")
    check_bench()
    for name, app in LOCK["applications"].items():
        print(f"{name}: {app['tag']} {app['commit']} {app['repository']} (clean checkout)", flush=True)
    print("Bench tools: " + json.dumps({key: LOCK["tools"][key] for key in
                                        ("python", "node", "yarn", "bench", "uv")}, sort_keys=True), flush=True)


def write_common():
    path = SITES / "common_site_config.json"
    require(path.is_file() and path.resolve() == path, "unsafe common configuration path")
    existing = json.loads(path.read_text())
    # Existing installs must be checked before rewriting runtime settings.
    if any((SITES / site).exists() for site in TARGETS):
        check_targets(allow_port_change=True)
    existing.update(COMMON)
    temporary = path.with_suffix(".tmp")
    require(not temporary.is_symlink(), "unsafe temporary common configuration path")
    temporary.write_text(json.dumps(existing, indent=2) + "\n")
    temporary.replace(path)


def deploy_app(*, fixture=None, fault=False):
    require(not APP.is_symlink(), "unexpected App checkout link")
    require(not (fixture or fault) or UPGRADE in TARGETS, "fixture deployment requires a CI project")
    source = fixture if fixture is not None else SOURCE
    source_files(source)
    if APP.exists():
        source_files(APP)
        package = APP / "haxpower_v1"
        if package.is_dir():
            shutil.rmtree(package)
        (APP / "pyproject.toml").unlink(missing_ok=True)
    else:
        APP.mkdir()
    shutil.copytree(source / "haxpower_v1", APP / "haxpower_v1",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(source / "pyproject.toml", APP / "pyproject.toml")
    if fault:
        patch_list = APP / "haxpower_v1/patches.txt"
        patch_list.write_text(patch_list.read_text() + "haxpower_v1.patches.rehearsal_failure\n")
        shutil.copyfile("/opt/hp-v1/rehearsal_failure.py",
                        APP / "haxpower_v1/patches/rehearsal_failure.py")
    run("uv", "pip", "install", "--python", PYTHON, "--no-deps", "--editable", APP)
    apps = SITES / "apps.txt"
    require(apps.resolve() == apps and apps.is_file(), "unsafe Bench app list")
    names = apps.read_text().splitlines()
    require(set(names) <= {"frappe", "erpnext", "haxpower_v1"}, "unexpected registered Bench app")
    if "haxpower_v1" not in names:
        apps.write_text("\n".join([*names, "haxpower_v1"]) + "\n")


def check_targets(target=SITE, allow_port_change=False):
    require(target in TARGETS, "unexpected site target")
    require(SITES.resolve() == SITES, "unexpected sites path")
    path = SITES / "common_site_config.json"
    require(path.resolve() == path, "symbolic link in common configuration")
    common = json.loads(path.read_text())
    for key, value in COMMON.items():
        if not (allow_port_change and key in {"webserver_port", "socketio_port"}):
            require(common.get(key) == value, f"unexpected common {key}")
    require(not ({"db_name", "db_user", "db_socket", "db_password", "default_site", "install_apps"}
                 & common.keys()), "unexpected common site or database override")
    require(not (SITES / "currentsite.txt").exists(), "unexpected default site")
    for child in SITES.iterdir():
        require(child.name in {"assets", "apps.json", "apps.txt", "common_site_config.json", *TARGETS},
                f"unexpected entry in sites directory: {child.name}")
    for site, database in TARGETS.items():
        site_path = SITES / site
        require(site_path.resolve() == site_path, "symbolic link at expected site")
        if not site_path.exists():
            continue
        config_path = site_path / "site_config.json"
        require(config_path.is_file() and config_path.resolve() == config_path,
                "site configuration missing or unsafe")
        config = json.loads(config_path.read_text())
        require(config.get("db_name") == database and config.get("db_user", database) == database
                and config.get("db_type") == "mariadb" and config.get("db_host", "mariadb") == "mariadb"
                and config.get("db_port", 3306) == 3306 and not config.get("db_socket"),
                "unexpected site database identity")
        require(not any(key in config and config[key] != value for key, value in COMMON.items())
                and not ({"default_site", "install_apps", "db_ssl_ca", "db_ssl_cert", "db_ssl_key"}
                         & config.keys()), "unexpected site override")
        require(set(config.get("installed_apps", ())) <= {"frappe", "erpnext", "haxpower_v1"},
                "unexpected App installed on site")
    return (SITES / target).is_dir()


def check_bench():
    check_targets()
    check_upstream("frappe")
    check_upstream("erpnext")
    apps = SITES / "apps.txt"
    require(apps.resolve() == apps and set(apps.read_text().splitlines()) == {"frappe", "erpnext", "haxpower_v1"},
            "unexpected Bench app list")
    require(source_files(APP) == source_files(SOURCE), "installed App differs from the formal source")
    require(app_import("haxpower_v1") == APP / "haxpower_v1/__init__.py", "unexpected formal App import")
    origin = json.loads(run(PYTHON, "-c", "from importlib.metadata import distribution; "
                            "print(distribution('haxpower_v1').read_text('direct_url.json'))", capture=True))
    require(origin == {"url": APP.as_uri(), "dir_info": {"editable": True}}, "unexpected App installation source")
    for forbidden in ("haxpower_platform", "haxpower_stock_spike"):
        require(run(PYTHON, "-c", f"import importlib.util; print(importlib.util.find_spec({forbidden!r}))",
                    capture=True).strip() == "None", f"experimental App is importable: {forbidden}")


def create_site(target=SITE):
    require(not check_targets(target), "site already exists")
    site_command("new-site", target, "--db-type", "mariadb", "--db-host", "mariadb",
                 "--db-name", TARGETS[target], "--mariadb-user-host-login-scope", "%",
                 "--db-root-password", "@db-root@", "--admin-password", "@site-admin@", target=target)
    require(check_targets(target), "site configuration missing after creation")


def test_site(target=SITE):
    site_command("set-config", "allow_tests", "True", "--parse", target=target)
    site_command("run-tests", "--app", "haxpower_v1", target=target)


def check_installed(target=SITE):
    actual = json.loads(site_command("list-apps", "--format", "json", target=target, capture=True))
    require(set(actual[target]) == {"frappe", "erpnext", "haxpower_v1"}, "unexpected site apps")
    probe = run(PYTHON, "/opt/hp-v1/probe.py", "identity", target, cwd=SITES, capture=True)
    print(probe.strip(), flush=True)


def main(action):
    require(os.environ["HP_V1_SITE"] == SITE and os.environ["HP_V1_DB"] == DATABASE, "unexpected site identity")
    if action == "secret-command":
        secret_command(sys.argv[2])
        return
    if action == "namespace":
        if (SITES / "common_site_config.json").is_file():
            check_targets(allow_port_change=True)
        return
    if action == "prepare":
        prepare()
        return
    check_bench()
    if action == "new-site":
        create_site()
    elif action == "integration":
        from rehearsal import integration

        integration()
    else:
        require(check_targets(), "create the expected site first")
        if action == "install-erpnext":
            site_command("install-app", "erpnext")
        elif action == "install-app":
            site_command("install-app", "haxpower_v1")
        elif action == "migrate":
            site_command("migrate")
        elif action == "test":
            test_site()
        elif action == "check":
            check_installed()
        elif action == "enable-scheduler":
            check_installed()
            site_command("enable-scheduler")
        else:
            commands = {
                "web": ["bench", "serve", "--port", WEB, "--noreload"],
                "socketio": ["node", "apps/frappe/socketio.js"],
                "worker": ["bench", "worker"],
                "scheduler": ["bench", "schedule"],
            }
            command = commands[action]
            os.chdir(BENCH)
            os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Bench action failed: {redact(str(error))}", file=sys.stderr)
        sys.exit(1)
