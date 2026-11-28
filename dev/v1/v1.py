#!/usr/bin/env python3
"""Operate one local, isolated Haxpower V1 Compose project."""

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import tomllib

from container.identity import identity, require


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP = ROOT / "apps/haxpower_v1"
LOCK = HERE / "versions.lock.json"
UPGRADE_BASELINE = "b3cf05a06372be9e5e47653d243d250033d00f58"
TOOLS = HERE / "container"
OWNER = "org.haxpower.v1.checkout"
RUNTIME = {"web", "socketio", "worker", "scheduler"}
BASE = {"mariadb", "redis-cache", "redis-queue", "bench"}
VOLUMES = {"bench-data", "db-data", "queue-data"}
NETWORKS = {"backend", "egress", "frontend"}
IMAGE_SERVICES = {
    "bench": RUNTIME | {"bench", "volume-init"},
    "mariadb": {"mariadb"},
    "redis": {"redis-cache", "redis-queue"},
}
LAB_IDENTIFIERS = ("haxpower_platform", "haxpower_stock_spike", "erpnext-lab", "frappe-stock-spike")


def check_source():
    require(APP.is_dir() and APP.resolve() == APP, "unexpected App source path")
    for path in APP.rglob("*"):
        require(not path.is_symlink(), f"symbolic link in App source: {path.relative_to(APP)}")
        if path.suffix in {".py", ".toml", ".txt", ".json"}:
            require(not any(marker in path.read_text() for marker in LAB_IDENTIFIERS),
                    f"experimental reference in App source: {path.relative_to(APP)}")
    metadata = tomllib.loads((APP / "pyproject.toml").read_text())["project"]
    require(metadata["name"] == "haxpower_v1", "unexpected distribution name")
    package = APP / "haxpower_v1"
    require((package / "modules.txt").read_text().splitlines() == ["HP V1"], "unexpected App module")
    for definition in package.glob("*/doctype/*/*.json"):
        doctype = json.loads(definition.read_text())
        require(doctype["name"].startswith("HP V1 ") and doctype["module"] == "HP V1",
                "unexpected DocType namespace")


def lock_data():
    lock = json.loads(LOCK.read_text())
    require(lock["platform"] == "linux/amd64", "unexpected platform")
    for name in ("frappe", "erpnext"):
        app = lock["applications"][name]
        require(app["repository"] == f"https://github.com/frappe/{name}.git"
                and re.fullmatch(r"v16\.\d+\.\d+", app["tag"])
                and re.fullmatch(r"[0-9a-f]{40}", app["commit"]), f"invalid upstream lock for {name}")
    for name, repository in (("bench", "frappe/bench"), ("mariadb", "mariadb"), ("redis", "redis")):
        require(re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", lock["images"][name]),
                f"invalid image lock for {name}")
    return lock


def options():
    defaults = {"HP_V1_PROJECT": "haxpower-v1-dev", "HP_V1_WEB_PORT": "8100", "HP_V1_SOCKET_PORT": "9100"}
    path = HERE / ".env"
    if path.exists() or path.is_symlink():
        require(path.is_file() and not path.is_symlink(), "unsafe .env path")
        seen = set()
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, value = line.partition("=")
            require(key in defaults and key not in seen and value, "invalid .env option")
            defaults[key] = value
            seen.add(key)
    defaults.update({key: os.environ[key] for key in defaults if key in os.environ})
    unknown = {key for key in os.environ if key.startswith("HP_V1_")} - set(defaults)
    require(not unknown, "unsupported HP_V1 environment override")
    require(os.environ.get("COMPOSE_PROJECT_NAME", defaults["HP_V1_PROJECT"]) == defaults["HP_V1_PROJECT"]
            and not any(os.environ.get(key) for key in ("COMPOSE_FILE", "COMPOSE_PROFILES")),
            "conflicting ambient Compose configuration")
    require(os.environ.get("DOCKER_CONTEXT", "default") == "default"
            and os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock") == "unix:///var/run/docker.sock",
            "conflicting Docker target")
    return defaults


def call(command, env=None, capture=False):
    return subprocess.run(command, env=env, text=True, check=True, capture_output=capture).stdout


class Project:
    def __init__(self, settings):
        self.project = settings["HP_V1_PROJECT"]
        self.site, self.database = identity(self.project)
        self.web = settings["HP_V1_WEB_PORT"]
        self.socket = settings["HP_V1_SOCKET_PORT"]
        require(re.fullmatch(r"81[0-9]{2}", self.web) and re.fullmatch(r"91[0-9]{2}", self.socket),
                "choose Web 8100–8199 and Socket.IO 9100–9199")
        self.lock = lock_data()
        self.owner = hashlib.sha256(os.fsencode(ROOT)).hexdigest()
        self.state = HERE / ".runtime" / self.project
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("COMPOSE_", "DOCKER_", "HP_V1_"))}
        self.environment.update(settings, HP_V1_SITE=self.site, HP_V1_DB=self.database,
                                HP_V1_OWNER=self.owner, HP_V1_SOURCE=str(APP),
                                HP_V1_CONTAINER=str(TOOLS), HP_V1_LOCK=str(LOCK),
                                HP_V1_SECRET_DIR=str(self.state))
        for key, value in self.lock["images"].items():
            self.environment[f"HP_V1_{key.upper()}_IMAGE"] = value
        self.docker = ["docker", "--context", "default"]
        self.compose = self.docker + ["compose", "--env-file", "/dev/null", "--project-directory", str(HERE),
                                     "-f", str(HERE / "compose.yaml"), "-p", self.project]

    def run(self, *args, capture=False):
        return call([*self.docker, *args], env=self.environment, capture=capture)

    def dc(self, *args, capture=False):
        return call([*self.compose, *args], env=self.environment, capture=capture)

    def config(self):
        check_source()
        for path in (HERE / "compose.yaml", TOOLS, LOCK, self.state):
            require(path.resolve() == path, "unsafe configuration, tool or credential path")
        require(not any(path.is_symlink() for path in TOOLS.rglob("*")), "unsafe tool source")
        model = json.loads(self.dc("--profile", "*", "config", "--format", "json", capture=True))
        self.check_model(model)

    def verify_database_services(self):
        for service, command, pattern, version in (
            ("mariadb", ("mariadbd", "--version"), r"Ver (\d+\.\d+\.\d+)", "mariadb"),
            ("redis-cache", ("redis-server", "--version"), r"v=(\d+\.\d+\.\d+)", "redis"),
            ("redis-queue", ("redis-server", "--version"), r"v=(\d+\.\d+\.\d+)", "redis"),
        ):
            output = self.dc("exec", "-T", service, *command, capture=True)
            actual = re.search(pattern, output)
            require(actual and actual.group(1) == self.lock["tools"][version], f"unexpected {service} version")
            print(f"{service}: {actual.group(1)}", flush=True)

    def check_model(self, model):
        require(model["name"] == self.project, "incorrect Compose project")
        require(set(model["volumes"]) == VOLUMES and set(model["networks"]) == NETWORKS,
                "unexpected network or volume definitions")
        for section in ("volumes", "networks"):
            for name, resource in model[section].items():
                require(resource["name"] == f"{self.project}_{name}"
                        and not any(resource.get(key) for key in ("external", "driver_opts", "ipam"))
                        and resource.get("driver", "bridge" if section == "networks" else "local")
                        == ("bridge" if section == "networks" else "local")
                        and resource.get("labels", {}).get(OWNER) == self.owner,
                        "unsafe network or volume definition")
        require(model["networks"]["backend"].get("internal") is True
                and not any(model["networks"][name].get("internal") for name in ("egress", "frontend")),
                "incorrect network boundary")
        require(set(model["services"]) == BASE | RUNTIME | {"volume-init"}, "unexpected service list")
        require(set(model["secrets"]) == {"db-root", "site-admin"}, "unexpected secrets")
        for name, value in model["secrets"].items():
            require(value == {"name": f"{self.project}_{name}", "file": str(self.state / name)}, "unexpected secret source")
        for image, names in IMAGE_SERVICES.items():
            for name in names:
                service = model["services"][name]
                self.check_service(name, service, self.lock["images"][image])

    def check_service(self, name, service, image):
        require(service.get("image") == image and service.get("platform") == "linux/amd64",
                f"unexpected image for {name}")
        require(not any(service.get(key) for key in ("container_name", "privileged", "volumes_from",
                    "external_links", "links", "devices", "pid", "ipc", "extra_hosts", "build",
                    "develop", "env_file", "configs", "extends", "provider", "use_api_socket")),
                f"unsafe service configuration for {name}")
        require(service.get("labels", {}).get(OWNER) == self.owner, f"missing owner label for {name}")
        expected_networks = {"backend"}
        if name == "bench":
            expected_networks.add("egress")
        if name in {"web", "socketio"}:
            expected_networks.add("frontend")
        require((name == "volume-init" and service.get("network_mode") == "none"
                 and not service.get("networks")) or
                (name != "volume-init" and not service.get("network_mode")
                 and set(service.get("networks", {})) == expected_networks),
                f"unexpected networks for {name}")
        ports = service.get("ports", [])
        if name in {"web", "socketio"}:
            port = self.web if name == "web" else self.socket
            require(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1"
                    and str(ports[0]["target"]) == str(ports[0]["published"]) == port
                    and ports[0].get("protocol") == "tcp", f"incorrect port binding for {name}")
        else:
            require(not ports, f"unexpected published ports for {name}")
        expected_mounts = {}
        if name in RUNTIME | {"bench", "volume-init"}:
            expected_mounts["/bench"] = ("volume", "bench-data", False)
        if name in RUNTIME | {"bench"}:
            expected_mounts.update({
                "/source/haxpower_v1": ("bind", str(APP), True),
                "/opt/hp-v1": ("bind", str(TOOLS), True),
                "/opt/versions.lock.json": ("bind", str(LOCK), True),
            })
            environment = service.get("environment", {})
            require(environment == {"HP_V1_PROJECT": self.project, "HP_V1_SITE": self.site,
                                    "HP_V1_DB": self.database, "HP_V1_WEB_PORT": self.web,
                                    "HP_V1_SOCKET_PORT": self.socket, "PYTHONDONTWRITEBYTECODE": "1"},
                    f"incorrect site environment for {name}")
        if name == "mariadb":
            expected_mounts["/var/lib/mysql"] = ("volume", "db-data", False)
            require(service.get("environment") == {"MARIADB_ROOT_PASSWORD_FILE": "/run/secrets/db-root",
                                                   "MARIADB_ROOT_HOST": "%"}, "unexpected MariaDB identity")
        if name == "redis-queue":
            expected_mounts["/data"] = ("volume", "queue-data", False)
        mounts = service.get("volumes", [])
        require(len(mounts) == len(expected_mounts)
                and {m["target"]: (m["type"], m.get("source"), m.get("read_only", False))
                     for m in mounts} == expected_mounts
                and not any(m.get("volume", {}).get("subpath") for m in mounts),
                f"unsafe mounts for {name}")
        service_secrets = {"bench": {"db-root", "site-admin"}, "mariadb": {"db-root"}}.get(name, set())
        require({s["source"] for s in service.get("secrets", [])} == service_secrets
                and all(set(s) <= {"source", "target"} and s.get("target", f"/run/secrets/{s['source']}")
                        == f"/run/secrets/{s['source']}" for s in service.get("secrets", [])),
                f"unexpected secret mounts for {name}")

    def check_container(self, service, container):
        image, = (key for key, services in IMAGE_SERVICES.items() if service in services)
        require(container["Config"]["Image"] == self.lock["images"][image], "unexpected existing image")
        if service in RUNTIME | {"bench"}:
            env = dict(item.split("=", 1) for item in container["Config"]["Env"] if item.startswith("HP_V1_"))
            require({key: env.get(key) for key in ("HP_V1_PROJECT", "HP_V1_SITE", "HP_V1_DB")}
                    == {"HP_V1_PROJECT": self.project, "HP_V1_SITE": self.site, "HP_V1_DB": self.database},
                    f"unexpected existing site identity for {service}")
        sources = {}
        if service in RUNTIME | {"bench", "volume-init"}:
            sources["/bench"] = (f"{self.project}_bench-data", True)
        if service in RUNTIME | {"bench"}:
            sources.update({"/source/haxpower_v1": (str(APP), False), "/opt/hp-v1": (str(TOOLS), False),
                            "/opt/versions.lock.json": (str(LOCK), False)})
        if service in {"bench", "mariadb"}:
            sources["/run/secrets/db-root"] = (str(self.state / "db-root"), False)
        if service == "bench":
            sources["/run/secrets/site-admin"] = (str(self.state / "site-admin"), False)
        if service == "mariadb":
            sources["/var/lib/mysql"] = (f"{self.project}_db-data", True)
        if service == "redis-queue":
            sources["/data"] = (f"{self.project}_queue-data", True)
        mounts = container["Mounts"]
        require(len(mounts) == len(sources)
                and {m["Destination"]: (m["Name"] if m["Type"] == "volume" else m["Source"], m["RW"])
                     for m in mounts} == sources
                and all(m["Type"] in {"bind", "volume"} for m in mounts),
                f"unexpected existing mounts for {service}")
        host = container["HostConfig"]
        networks = {"none"} if service == "volume-init" else {f"{self.project}_backend"}
        if service == "bench":
            networks.add(f"{self.project}_egress")
        if service in {"web", "socketio"}:
            networks.add(f"{self.project}_frontend")
        require(not host["Privileged"] and not host["VolumesFrom"]
                and set(container["NetworkSettings"]["Networks"]) <= networks
                and host["NetworkMode"] in networks,
                f"unsafe existing container networking or privileges for {service}")
        if service in {"web", "socketio"}:
            bindings = host["PortBindings"] or {}
            port = next(iter(bindings), "").removesuffix("/tcp")
            port_pattern = r"81[0-9]{2}" if service == "web" else r"91[0-9]{2}"
            require(re.fullmatch(port_pattern, port)
                    and bindings == {f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]},
                    f"unsafe existing {service} port")
            configured = self.web if service == "web" else self.socket
            require(not container["State"]["Running"] or port == configured,
                    f"stop {service} with its configured port before changing it")
        else:
            require(not host["PortBindings"], f"unexpected existing published ports for {service}")

    def resources(self):
        endpoint = self.run("context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}",
                            capture=True).strip()
        require(endpoint == "unix:///var/run/docker.sock", "Docker default context must use the local socket")
        ids = self.run("ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}",
                       capture=True).split()
        containers = {}
        if ids:
            for container in json.loads(self.run("container", "inspect", *ids, capture=True)):
                labels = container["Config"]["Labels"]
                service = labels.get("com.docker.compose.service")
                require(labels.get(OWNER) == self.owner and service in BASE | RUNTIME | {"volume-init"}
                        and labels.get("com.docker.compose.project.working_dir") == str(HERE)
                        and labels.get("com.docker.compose.project.config_files") == str(HERE / "compose.yaml"),
                        "foreign container in Compose project")
                self.check_container(service, container)
                containers[container["Id"]] = container
        for kind, names in (("volume", VOLUMES), ("network", NETWORKS)):
            existing = self.run(kind, "ls", "--format", "{{.Name}}", capture=True).splitlines()
            for name in names:
                qualified = f"{self.project}_{name}"
                if qualified not in existing:
                    continue
                resource, = json.loads(self.run(kind, "inspect", qualified, capture=True))
                labels = resource["Labels"] or {}
                require(labels.get(OWNER) == self.owner
                        and labels.get("com.docker.compose.project") == self.project
                        and labels.get(f"com.docker.compose.{kind}") == name,
                        f"foreign {kind} {qualified}")
                if kind == "network":
                    require(resource["Driver"] == "bridge" and resource["Internal"] == (name == "backend")
                            and set(resource.get("Containers", {})) <= set(containers),
                            f"unexpected network membership for {qualified}")
                else:
                    require(resource["Driver"] == "local" and not resource["Options"],
                            f"unexpected volume driver for {qualified}")
        return containers

    def check_ports(self, containers):
        try:
            addresses = socket.getaddrinfo(self.site, None, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            if error.errno != socket.EAI_NONAME:
                raise
            addresses = []  # Browser .localhost handling or an explicit loopback Host header.
        require(all(ipaddress.ip_address(item[4][0]).is_loopback for item in addresses),
                "site name resolves outside loopback")
        for service, port in (("web", self.web), ("socketio", self.socket)):
            if any(c["State"]["Running"] and c["Config"]["Labels"]["com.docker.compose.service"] == service
                   and c["NetworkSettings"]["Ports"].get(f"{port}/tcp") == [
                       {"HostIp": "127.0.0.1", "HostPort": port}] for c in containers.values()):
                continue
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", int(port)))
            except OSError as error:
                raise ValueError(f"{service} loopback port {port} unavailable: {error.strerror}") from None

    def credential_files(self, containers):
        for name in ("db-root", "site-admin"):
            path = self.state / name
            if not path.exists():
                require(not containers, "credential file missing for existing environment")
                with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as credential:
                    credential.write(secrets.token_hex(24) + "\n")
                path.chmod(0o444)
            require(not path.is_symlink() and path.is_file() and path.stat().st_uid == os.getuid()
                    and stat.S_IMODE(path.stat().st_mode) == 0o444
                    and re.fullmatch(r"[0-9a-f]{48}\n", path.read_text()), "invalid local credential file")

    def operate(self, action):
        self.config()
        if action == "config":
            print(f"Configuration verified for {self.project} / {self.site}")
            return
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(self.state.stat().st_uid == os.getuid() and stat.S_IMODE(self.state.stat().st_mode) == 0o700,
                "unsafe project state directory")
        lock_file = self.state / "operation.lock"
        with open(lock_file, "a", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)) as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            containers = self.resources()
            if action == "preflight":
                self.check_ports(containers)
                if any(c["State"]["Running"] and c["Config"]["Labels"]["com.docker.compose.service"] == "bench"
                       for c in containers.values()):
                    self.dc("exec", "-T", "bench", "python", "/opt/hp-v1/bench.py", "namespace")
                print(f"Preflight passed for {self.project} / {self.site} / mariadb:{self.database} "
                      f"(loopback {self.web}/{self.socket})")
                return
            if action in {"stop", "ci-clean"}:
                if action == "ci-clean":
                    require(self.project.startswith("haxpower-v1-ci-"), "cleanup requires a CI project")
                    self.dc("--profile", "*", "down", "--volumes")
                    require(not self.resources(), "run-owned containers remain")
                    for kind in ("network", "volume"):
                        require(not self.run(kind, "ls", "--filter", f"label=com.docker.compose.project={self.project}",
                                             "--format", "{{.Name}}", capture=True).strip(),
                                f"run-owned {kind} resources remain")
                    print(f"Cleaned verified CI resources for {self.project}")
                else:
                    self.dc("--profile", "*", "stop")
                return
            self.check_ports(containers)
            if action == "up":
                self.credential_files(containers)
                self.dc("pull", "mariadb", "redis-cache", "redis-queue", "bench")
                for name, image in self.lock["images"].items():
                    digests = json.loads(self.run("image", "inspect", image, "--format", "{{json .RepoDigests}}",
                                                  capture=True))
                    require(image.split("@", 1)[1] in {ref.split("@", 1)[-1] for ref in digests},
                            f"unexpected pulled digest for {name}")
                    print(f"{name}: {image}", flush=True)
                self.dc("run", "--rm", "--no-deps", "-T", "volume-init")
                self.dc("up", "-d", "--wait", "--wait-timeout", "180", *sorted(BASE))
                self.verify_database_services()
                self.dc("exec", "-T", "bench", "python", "/opt/hp-v1/bench.py", "namespace")
                return
            if action == "smoke":
                self.smoke()
                return
            if action in {"prepare", "new-site", "install-erpnext", "install-app", "migrate", "integration"}:
                require(not any(c["State"]["Running"] and c["Config"]["Labels"]["com.docker.compose.service"]
                                in RUNTIME for c in containers.values()), "stop runtime before changing Bench or site")
            command = ("exec", "-T", "bench", "python", "/opt/hp-v1/bench.py")
            if action == "integration":
                baseline = subprocess.check_output(
                    ["git", "archive", "--format=tar", UPGRADE_BASELINE, "apps/haxpower_v1"], cwd=ROOT,
                )
                subprocess.run([*self.compose, *command, action], input=baseline, check=True, env=self.environment)
            else:
                self.dc(*command, "enable-scheduler" if action == "start" else action)
            if action == "start":
                self.dc("--profile", "runtime", "up", "-d", "--no-deps", *sorted(RUNTIME))

    def request(self, port, host, path):
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=3)
        try:
            connection.request("GET", path, headers={"Host": host, "Origin": f"http://{self.site}:{self.web}"})
            response = connection.getresponse()
            return response.status, response.read(2048)
        finally:
            connection.close()

    def smoke(self):
        deadline = time.monotonic() + 90
        while True:
            try:
                status, body = self.request(self.web, self.site, "/api/method/ping")
                socket_status, socket_body = self.request(self.socket, self.site, "/socket.io/?EIO=4&transport=polling")
                if status == 200 and b"pong" in body and socket_status == 200 and socket_body.startswith(b"0{"):
                    break
                reason = f"Web HTTP {status}, Socket.IO HTTP {socket_status}"
            except (OSError, http.client.HTTPException) as error:
                reason = str(error)
            require(time.monotonic() < deadline, f"runtime smoke timed out: {reason}")
            time.sleep(2)
        status, _ = self.request(self.web, "unregistered-v1.localhost", "/api/method/ping")
        require(status == 404, f"unknown Host returned HTTP {status}")
        current = self.resources()
        running = {c["Config"]["Labels"]["com.docker.compose.service"] for c in current.values()
                   if c["State"]["Running"]}
        require(RUNTIME | BASE <= running, "runtime service stopped")
        print(f"Loopback routes verified for {self.site}: Web, Socket.IO, unknown Host rejected; "
              "worker and scheduler running")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("config", "preflight", "up", "prepare", "new-site", "install-erpnext",
                                           "install-app", "migrate", "test", "check", "start", "stop",
                                           "integration", "smoke", "ci-clean"))
    args = parser.parse_args()
    try:
        Project(options()).operate(args.action)
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        print(f"V1 {args.action} failed (exit {error.returncode})", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"V1 {args.action} refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
