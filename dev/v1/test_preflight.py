"""Regression checks for V1 identity, Compose scope and site target validation."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import v1
from container import identity


SETTINGS = {"HP_V1_PROJECT": "haxpower-v1-dev", "HP_V1_WEB_PORT": "8100", "HP_V1_SOCKET_PORT": "9100"}


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = v1.Project(SETTINGS)
        cls.model = json.loads(cls.project.dc("--profile", "*", "config", "--format", "json", capture=True))

    def test_formal_model_and_app_source(self):
        self.project.check_model(self.model)
        v1.check_source()

    def test_unknown_and_experiment_projects(self):
        for name in ("haxpower-erpnext-v16-lab", "haxpower-frappe-only", "haxpower-v1-ci-local", "production"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unknown Compose project"):
                v1.Project(dict(SETTINGS, HP_V1_PROJECT=name))
        model = deepcopy(self.model)
        model["name"] = "haxpower-erpnext-v16-lab"
        with self.assertRaisesRegex(ValueError, "incorrect Compose project"):
            self.project.check_model(model)

    def test_ambient_project_and_docker_target_rejected(self):
        for overrides, message in (({"COMPOSE_PROJECT_NAME": "haxpower-erpnext-v16-lab"}, "ambient Compose"),
                                   ({"DOCKER_HOST": "tcp://unknown:2375"}, "Docker target")):
            with (self.subTest(overrides=overrides),
                  patch.dict("os.environ", {**SETTINGS, **overrides}, clear=True),
                  self.assertRaisesRegex(ValueError, message)):
                v1.options()

    def test_experiment_site_and_alternate_db_identity(self):
        for key, value in (("HP_V1_SITE", "haxpower.localhost"), ("HP_V1_DB", "haxpower_frappe_only_lab")):
            model = deepcopy(self.model)
            model["services"]["bench"]["environment"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "incorrect site environment"):
                self.project.check_model(model)

    def test_experiment_app_mount_and_external_resources(self):
        mutations = [
            ("mounts", lambda model: model["services"]["bench"]["volumes"][1].update(
                source="/workspace/dev/erpnext-lab/haxpower_platform")),
            ("volume", lambda model: model["volumes"]["db-data"].update(external=True)),
            ("network", lambda model: model["networks"]["backend"].update(external=True)),
            ("published ports", lambda model: model["services"]["mariadb"].update(ports=[{"target": 3306}])),
        ]
        for message, mutate in mutations:
            model = deepcopy(self.model)
            mutate(model)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.project.check_model(model)

    def test_experiment_package_symlink(self):
        with TemporaryDirectory() as temp:
            formal = Path(temp) / "haxpower_v1"
            formal.mkdir()
            (formal / "haxpower_v1").symlink_to(v1.ROOT / "dev/erpnext-lab/haxpower_platform")
            with patch.object(v1, "APP", formal), self.assertRaisesRegex(ValueError, "symbolic link"):
                v1.check_source()

    def test_experiment_port_rejected(self):
        with self.assertRaisesRegex(ValueError, "8100"):
            v1.Project(dict(SETTINGS, HP_V1_WEB_PORT="8000"))


class SiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site, database = v1.identity(SETTINGS["HP_V1_PROJECT"])
        with (patch.dict("os.environ", dict(SETTINGS, HP_V1_SITE=site, HP_V1_DB=database)),
              patch.dict("sys.modules", {"identity": identity})):
            cls.bench = importlib.import_module("container.bench")

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.sites = Path(directory)
        self.enterContext(patch.object(self.bench, "SITES", self.sites))
        (self.sites / "common_site_config.json").write_text(json.dumps(self.bench.COMMON))
        site = self.sites / self.bench.SITE
        site.mkdir()
        self.config = site / "site_config.json"
        self.safe_config = {"db_type": "mariadb", "db_host": "mariadb", "db_user": self.bench.DATABASE,
                            "db_name": self.bench.DATABASE}
        self.config.write_text(json.dumps(self.safe_config))

    def test_valid_site_and_database(self):
        self.assertTrue(self.bench.check_targets())

    def test_wrong_site_and_other_site_directory(self):
        with self.assertRaisesRegex(ValueError, "unexpected site target"):
            self.bench.check_targets("haxpower.localhost")
        (self.sites / "haxpower-frappe-only.localhost").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected entry"):
            self.bench.check_targets()

    def test_wrong_database_host_and_user(self):
        for key, value in (("db_host", "legacy-db"), ("db_user", "root"), ("db_name", "haxpower_frappe_only_lab")):
            self.config.write_text(json.dumps(dict(self.safe_config, **{key: value})))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "database identity"):
                self.bench.check_targets()

    def test_wrong_redis_and_experiment_app(self):
        for values, message in (({"redis_queue": "redis://unknown:6379"}, "site override"),
                                ({"installed_apps": ["frappe", "haxpower_stock_spike"]}, "unexpected App")):
            self.config.write_text(json.dumps(dict(self.safe_config, **values)))
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                self.bench.check_targets()


if __name__ == "__main__":
    unittest.main()
