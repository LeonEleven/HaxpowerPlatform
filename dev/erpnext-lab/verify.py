#!/usr/bin/env python3
"""Check lab configuration and running base services without creating a Bench/site."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, default=Path.home() / "labs/haxpower-erpnext-v16")
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    lab = args.lab_root.expanduser().resolve()
    source = Path(__file__).resolve().parent
    if lab == source.parents[1] or lab.is_relative_to(source.parents[1]):
        raise SystemExit("Lab must be outside the project")
    lock = json.loads((lab / "versions.lock.json").read_text())
    assert lock == json.loads((source / "versions.lock.json").read_text()), "Version lock differs"
    secret = (lab / "secrets/mariadb-root-password").read_text().strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", secret), "Invalid local secret format"
    compose = ["docker", "compose", "--project-directory", str(lab), "-f", str(lab / "compose.yaml")]
    errors = []
    report = {"checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}

    def run(command, *, stdin=None, timeout=40):
        process = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout)
        output = (process.stdout + process.stderr).replace(secret, "[REDACTED]").strip()
        return process.returncode, output

    def check(name, command, *, stdin=None, expected=None, timeout=40):
        try:
            code, output = run(command, stdin=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            code, output = 124, "Timed out"
        passed = code == 0 and (expected is None or expected(output))
        report.setdefault("checks", {})[name] = {"passed": passed, "exit_code": code, "output": output[:5000]}
        if not passed:
            errors.append(name)
        return output

    code, output = run(compose + ["config", "--format", "json"])
    assert code == 0, output
    config = json.loads(output)
    assert config["name"] == "haxpower-erpnext-v16-lab"
    services = config["services"]
    assert set(services) == {"frappe", "mariadb", "redis-cache", "redis-queue"}
    assert config["networks"]["backend"]["internal"] is True
    assert all(not item.get("external") for item in config["networks"].values())
    assert all(not item.get("external") for item in config["volumes"].values())
    for name, service in services.items():
        image = "bench" if name == "frappe" else "redis" if name.startswith("redis-") else "mariadb"
        assert service["image"] == lock["images"][image]["reference"], name
        assert not service.get("ports") and not service.get("privileged"), name
        assert service.get("network_mode") != "host", name
        assert not service.get("cap_add"), name
        assert not service.get("build"), name
        assert service.get("restart", "no") == "no", name
        assert not any("PROXY" in key.upper() for key in service.get("environment", {})), name
        if name != "frappe":
            assert set(service["networks"]) == {"backend"}, name
    frappe = services["frappe"]
    assert frappe["command"] == ["sleep", "infinity"] and not frappe.get("entrypoint")
    assert frappe["user"] == "1000:1000"
    assert not frappe.get("secrets")
    assert len(frappe["volumes"]) == 1
    mount = frappe["volumes"][0]
    assert mount["source"] == str(lab / "workspace") and mount["target"] == "/workspace/development"
    assert services["mariadb"]["environment"]["MARIADB_ROOT_PASSWORD_FILE"] == "/run/secrets/mariadb_root_password"
    assert "MARIADB_ROOT_PASSWORD" not in services["mariadb"]["environment"]
    assert Path(config["secrets"]["mariadb_root_password"]["file"]).resolve() == lab / "secrets/mariadb-root-password"
    assert (lab.stat().st_mode & 0o777) == 0o700
    assert ((lab / "secrets").stat().st_mode & 0o777) == 0o700
    dev = json.loads((lab / ".devcontainer/devcontainer.json").read_text())
    assert not dev.get("mounts")
    assert not any(key.endswith("Command") for key in dev if key != "overrideCommand")
    assert dev["customizations"]["vscode"]["settings"]["remote.localPortHost"] == "localhost"
    report["configuration"] = {"passed": True, "services": sorted(services), "images_pinned": True,
                               "published_ports": [], "secret_in_environment": False,
                               "ssh_mounts": False, "automatic_install_commands": False}

    if not args.config_only:
        ids = []
        container_ids = {}
        runtime = {}
        for service in services:
            code, container_id = run(compose + ["ps", "--all", "--quiet", service])
            assert code == 0 and container_id, f"Missing container: {service}"
            code, inspected = run(["docker", "inspect", container_id])
            assert code == 0
            item = json.loads(inspected)[0]
            ids.append(container_id)
            container_ids[service] = container_id
            state = item["State"]
            health = state.get("Health", {}).get("Status")
            bindings = item["HostConfig"].get("PortBindings") or {}
            passed = state["Status"] == "running" and health == "healthy" and not bindings
            assert item["Config"]["Labels"]["com.docker.compose.project"] == config["name"]
            image_key = "bench" if service == "frappe" else "redis" if service.startswith("redis-") else "mariadb"
            assert item["Image"] == lock["images"][image_key]["image_id"]
            assert not item["HostConfig"]["Privileged"]
            if service == "frappe":
                assert item["Config"]["User"] == "1000:1000"
                assert "ALL" in item["HostConfig"]["CapDrop"]
                assert "no-new-privileges:true" in item["HostConfig"]["SecurityOpt"]
            assert all(not key.split("=", 1)[0].upper().endswith("PROXY") for key in item["Config"]["Env"])
            runtime[service] = {"passed": passed, "container": item["Name"].lstrip("/"),
                                "status": state["Status"], "health": health,
                                "image_id": item["Image"], "ports": bindings,
                                "memory_limit_bytes": item["HostConfig"]["Memory"],
                                "mounts": [{"type": m["Type"], "destination": m["Destination"],
                                            "volume": m.get("Name")} for m in item["Mounts"]]}
            if not passed:
                errors.append(service)
        report["containers"] = runtime
        networks = {}
        for name in ("backend", "egress"):
            full_name = config["networks"][name]["name"]
            code, output = run(["docker", "network", "inspect", full_name])
            assert code == 0
            network = json.loads(output)[0]
            assert network["Labels"]["com.docker.compose.project"] == config["name"]
            members = set(network.get("Containers", {}))
            assert network["Internal"] == (name == "backend")
            expected = set(ids) if name == "backend" else {container_ids["frappe"]}
            assert members == expected, f"Unexpected containers on {full_name}"
            networks[name] = {"name": full_name, "internal": network["Internal"], "container_count": len(members)}
        report["networks"] = networks
        volume_names = []
        for volume in config["volumes"].values():
            code, output = run(["docker", "volume", "inspect", volume["name"]])
            assert code == 0
            item = json.loads(output)[0]
            assert item["Labels"]["com.docker.compose.project"] == config["name"]
            volume_names.append(item["Name"])
        report["volumes"] = sorted(volume_names)
        exec_base = compose + ["exec", "-T", "frappe"]
        tools_script = """import subprocess,json,os,sys
commands={'python':['python','--version'],'node':['node','--version'],'yarn':['yarn','--version'],'npm':['npm','--version'],'bench':['bench','--version'],'pip':['python','-m','pip','--version'],'bench_commit':['git','-C','/home/frappe/.bench','rev-parse','HEAD'],'uid':['id','-u']}
result={key:subprocess.check_output(cmd,text=True).strip() for key,cmd in commands.items()}
assert sys.version_info[:2]==(3,14)
assert result['node'].startswith('v24.') and result['yarn'].startswith('1.22.')
assert result['uid']=='1000'
assert not os.path.exists('/workspace/development/frappe-bench')
result['bench_workspace_exists']=False
print(json.dumps(result))"""
        check("development_tools", exec_base + ["python", "-c", tools_script])
        sql_script = """import subprocess,sys,tempfile
password=sys.stdin.read().strip()
with tempfile.NamedTemporaryFile(mode='w',prefix='haxpower-db-',suffix='.cnf') as config:
 config.write('[client]\\npassword='+password+'\\n');config.flush()
 process=subprocess.run(['mariadb','--defaults-extra-file='+config.name,'--protocol=tcp','--host=mariadb','--user=root','--batch','--skip-column-names','--execute',"SELECT VERSION(), @@character_set_server, @@collation_server, @@innodb_snapshot_isolation; SHOW DATABASES; SHOW SESSION STATUS LIKE 'Ssl_cipher';"],capture_output=True,text=True,timeout=20)
 print(process.stdout,end='');print(process.stderr,end='',file=sys.stderr)
 sys.exit(process.returncode)"""
        check("mariadb_from_frappe", exec_base + ["python", "-c", sql_script], stdin=secret,
              expected=lambda out: out.startswith("11.8.") and "utf8mb4" in out and "haxpower" not in out)
        reject_script = """import subprocess,tempfile
for password in ('deliberately-invalid-lab-password',''):
 with tempfile.NamedTemporaryFile(mode='w',prefix='haxpower-auth-',suffix='.cnf') as config:
  config.write('[client]\\npassword='+password+'\\n');config.flush()
  result=subprocess.run(['mariadb','--defaults-extra-file='+config.name,'--protocol=tcp','--host=mariadb','--user=root','--execute','SELECT 1'],capture_output=True,text=True,timeout=15)
  assert result.returncode!=0 and '1045' in result.stderr, 'Expected access denied'
print('Invalid and empty passwords both rejected with access denied (1045).')"""
        check("mariadb_rejects_bad_password", exec_base + ["python", "-c", reject_script])
        for service in ("redis-cache", "redis-queue"):
            check(service + "_from_frappe", exec_base + ["redis-cli", "-h", service, "PING"],
                  expected=lambda out: out == "PONG")
            check(service + "_version", compose + ["exec", "-T", service, "redis-server", "--version"],
                  expected=lambda out: "v=7.4." in out)
        for name, url in {"github_https": "https://github.com", "pypi_https": "https://pypi.org/simple/frappe-bench/",
                          "npm_https": "https://registry.npmjs.org/frappe-ui"}.items():
            script = "import urllib.request; r=urllib.request.urlopen(" + repr(url) + ",timeout=15); print(r.status); r.read(1024)"
            check(name, exec_base + ["python", "-c", script], expected=lambda out: out == "200", timeout=25)
        check("github_git", exec_base + ["git", "-c", "http.connectTimeout=15", "ls-remote", "--exit-code",
              "https://github.com/frappe/frappe.git", "refs/tags/v16.55.0"],
              expected=lambda out: lock["applications"]["frappe"]["commit"] in out, timeout=30)
        check("python_package_index", exec_base + ["python", "-m", "pip", "index", "versions", "frappe-bench",
              "--index-url", "https://pypi.org/simple/", "--timeout", "15", "--retries", "0"], timeout=30)
        check("npm_registry", exec_base + ["npm", "ping", "--registry=https://registry.npmjs.org",
              "--fetch-retries=0", "--fetch-timeout=15000"], timeout=25)
        check("yarn_registry", exec_base + ["yarn", "info", "frappe-ui", "version", "--json",
              "--registry", "https://registry.npmjs.org", "--network-timeout", "15000"], timeout=30)
        check("resource_usage", ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids])
        logs = {}
        for service in services:
            code, output = run(compose + ["logs", "--no-color", "--tail", "80", service])
            if code:
                errors.append(service + "_logs")
            noteworthy = [line for line in output.splitlines() if re.search(r"error|warn|ready|initialized", line, re.I)]
            logs[service] = {"read_exit_code": code, "noteworthy_lines": noteworthy}
        report["log_review"] = logs
    report["passed"] = not errors
    report["failed_checks"] = errors
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        target = args.output.expanduser().absolute()
        assert target.is_relative_to(lab) and not target.is_symlink(), "Evidence must remain inside the lab"
        target.write_text(rendered)
    print(rendered)
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
