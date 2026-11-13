#!/usr/bin/env python3
"""隔离断言回归测试；只检查内存中的 inspect/config fixture，不连接 Docker。"""

import copy
import unittest

from check_drill_isolation import check
from check_drill_runtime import check_runtime

PROJECT = "ventasync-drill-1763000000-1234"
NAME = f"{PROJECT}_mysql_data"
SOURCE = f"/var/lib/docker/volumes/{NAME}/_data"


def fixtures():
    config = {
        "name": PROJECT,
        "volumes": {"mysql_data": {"name": NAME}},
        "services": {"mysql": {"ports": [], "volumes": [
            {"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"},
        ]}},
    }
    container = {
        "Id": "drill-container",
        "State": {"Running": True},
        "Config": {"Labels": {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "mysql"}},
        "HostConfig": {"NetworkMode": f"{PROJECT}_backend", "PortBindings": {}, "Binds": [f"{NAME}:/var/lib/mysql:rw"]},
        "NetworkSettings": {"Ports": {"3306/tcp": None, "33060/tcp": None}},
        "Mounts": [{"Type": "volume", "Name": NAME, "Source": SOURCE, "Destination": "/var/lib/mysql", "RW": True, "Driver": "local"}],
    }
    volume = {
        "Name": NAME, "Driver": "local", "Options": None, "Scope": "local", "Mountpoint": SOURCE,
        "Labels": {"com.docker.compose.project": PROJECT, "com.docker.compose.volume": "mysql_data"},
    }
    main = {
        "Id": "main-container",
        "Config": {"Labels": {"com.docker.compose.project": "ventasync", "com.docker.compose.service": "mysql"}},
        "Mounts": [{"Type": "volume", "Name": "ventasync_mysql_data", "Source": "/var/lib/docker/volumes/ventasync_mysql_data/_data", "Destination": "/var/lib/mysql"}],
    }
    return config, container, volume, main


class IsolationTests(unittest.TestCase):
    def test_existing_self_test_fixture_and_runtime_are_safe(self):
        config, container, volume, main = fixtures()
        self.assertEqual(check(config, PROJECT), [])
        self.assertEqual(check_runtime(config, PROJECT, "ventasync", container, volume, main), [])

    def test_volume_cannot_hide_production_storage(self):
        for patch in (
            {"name": "ventasync_mysql_data"}, {"name": f"{PROJECT}wrongprefix"},
            {"external": True}, {"driver": "nfs"},
            {"driver_opts": {"type": "none", "o": "bind", "device": "/production/mysql"}},
        ):
            with self.subTest(patch=patch):
                config, *_ = fixtures()
                config["volumes"]["mysql_data"].update(patch)
                self.assertTrue(check(config, PROJECT))

    def test_mysql_mount_and_execution_constraints(self):
        for patch in (
            {"volumes": [{"type": "bind", "source": "/production/mysql", "target": "/var/lib/mysql"}]},
            {"volumes": []},
            {"volumes": [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql", "read_only": True}]},
            {"container_name": "main-mysql"}, {"network_mode": "host"},
            {"ports": [{"published": "3306"}]}, {"volumes_from": ["main-mysql"]},
        ):
            with self.subTest(patch=patch):
                config, *_ = fixtures()
                config["services"]["mysql"].update(patch)
                self.assertTrue(check(config, PROJECT))

    def test_runtime_rejects_actual_main_source_even_under_drill_name(self):
        for suffix in ("", "/child"):
            config, container, volume, main = fixtures()
            source = main["Mounts"][0]["Source"] + suffix
            container["Mounts"][0]["Source"] = source
            volume["Mountpoint"] = source
            self.assertTrue(check_runtime(config, PROJECT, "ventasync", container, volume, main))

    def test_runtime_identity_mount_and_ports(self):
        config, baseline, volume, main = fixtures()
        changes = [
            lambda c: c["Config"]["Labels"].update({"com.docker.compose.project": "ventasync"}),
            lambda c: c["Config"]["Labels"].update({"com.docker.compose.service": "api"}),
            lambda c: c["Mounts"][0].update({"Type": "bind"}),
            lambda c: c["Mounts"][0].update({"RW": False}),
            lambda c: c["Mounts"][0].update({"Name": "ventasync_mysql_data"}),
            lambda c: c["Mounts"].append(copy.deepcopy(c["Mounts"][0])),
            lambda c: c["HostConfig"].update({"NetworkMode": "host"}),
            lambda c: c["NetworkSettings"]["Ports"].update({"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3306"}]}),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                container = copy.deepcopy(baseline)
                change(container)
                self.assertTrue(check_runtime(config, PROJECT, "ventasync", container, volume, main))

    def test_runtime_volume_provenance(self):
        for patch in ({"Options": {"device": "/production/mysql"}}, {"Driver": "nfs"}, {"Labels": {}}, {"Mountpoint": "/unexpected"}):
            with self.subTest(patch=patch):
                config, container, volume, main = fixtures()
                volume.update(patch)
                self.assertTrue(check_runtime(config, PROJECT, "ventasync", container, volume, main))


if __name__ == "__main__":
    unittest.main()
