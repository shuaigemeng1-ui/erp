"""隔离恢复验收的失败关闭回归；不连接 Docker、数据库或外部服务。"""

import base64
import hashlib
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


HELPER = Path(__file__).resolve().parents[3] / "deploy/scripts/validate_drill_data.py"
SPEC = importlib.util.spec_from_file_location("validate_drill_data", HELPER)
drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drill)


class RestoredDatabase:
    """有效租户与配置，商品数可以是零；有针对性地注入恢复故障。"""

    def __init__(self, failure=None):
        self.failure = failure

    def count(self, sql):
        if self.failure == "query" and ".products" in sql:
            raise drill.ValidationError("mysql_query_failed:exit=1")
        if self.failure == "orphan" and "WHERE s.id IS NULL" in sql:
            return 1
        if "COUNT(DISTINCT m.id)" in sql or ".tenant_settings " in sql:
            return 1
        if ".users WHERE status='active'" in sql:
            return 1
        return 0

    def objects(self, sql):
        if "WHERE database_name=" in sql:
            return [{"tenant_id": 3, "status": "ready", "port": 3306,
                     "host_present": 1, "user_present": 1}]
        if "'database_name',d.database_name" in sql:
            return [{"database_name": "vs_tenant_test", "status": "ready"}]
        if ".storage_profiles" in sql:
            return []
        if "'secret',password_cipher" in sql:
            return [{"secret": "legacy-test-value"}]
        raise AssertionError("Unexpected JSON query")


class RestoreValidationTests(unittest.TestCase):
    def report(self, failure=None, *, include_tenant=True, configured_tenant_id="3"):
        manifest = {"center_database": "ventasync_center", "databases": ["ventasync_center"]}
        if include_tenant:
            manifest["databases"].append("vs_tenant_test")
        env = {"DEPLOYMENT_MODE": "tenant", "TENANT_ID": configured_tenant_id,
               "MYSQL_ROOT_PASSWORD": "test", "APP_IMAGE_TAG": "test", "SECRET_KEY": "test"}
        with patch.object(drill, "decrypt_count", return_value={
            "encrypted_verified": 0, "legacy_plaintext": 1, "invalid": 0,
        }):
            return drill.Validator(RestoredDatabase(failure), "ventasync_center", manifest, env).run()

    def test_real_empty_products_are_valid(self):
        report = self.report()
        self.assertTrue(report["passed"], report)
        product = next(c for c in report["checks"] if c["name"].endswith(":products"))
        self.assertEqual(product["detail"]["count"], 0)

    def test_query_error_cannot_become_empty_products(self):
        report = self.report("query")
        self.assertFalse(report["passed"])
        self.assertTrue(any(not c["ok"] and "query_failed" in str(c["detail"])
                            for c in report["checks"]))

    def test_alias_orphan_rejects_restore(self):
        report = self.report("orphan")
        self.assertFalse(report["passed"])
        orphan = next(c for c in report["checks"] if c["name"].endswith(":alias_orphans"))
        self.assertFalse(orphan["ok"])

    def test_center_only_archive_preserves_center_evidence_but_fails_coverage(self):
        report = self.report(include_tenant=False)
        self.assertFalse(report["passed"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertTrue(checks["center_active_accounts"]["ok"])
        self.assertFalse(checks["active_tenant_dump_coverage"]["ok"])
        self.assertEqual(checks["active_tenant_dump_coverage"]["detail"]["missing_databases"],
                         ["vs_tenant_test"])

    def test_config_binding_mismatch_does_not_skip_restored_data_checks(self):
        report = self.report(configured_tenant_id="1")
        self.assertFalse(report["passed"])
        checks = {check["name"]: check for check in report["checks"]}
        prefix = "tenant:vs_tenant_test:"
        binding = checks[prefix + "bound_tenant_id_match"]
        self.assertFalse(binding["ok"])
        self.assertEqual(binding["detail"], {"configured_tenant_id": 1, "actual_tenant_id": 3})
        for name in ("route", "owner_access", "settings", "products", "product_skus",
                     "sku_identifiers", "sku_orphans", "alias_orphans"):
            self.assertTrue(checks[prefix + name]["ok"], name)

    def test_mysql_exit_or_non_numeric_stdout_is_failure_without_leaking_output(self):
        client = drill.MySQL("drill-mysql")
        for rc, output in ((1, "private-data"), (0, ""), (0, "0\nprivate-data")):
            with self.subTest(returncode=rc, output=output), patch.object(
                drill.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, output, "secret")
            ) as run:
                with self.assertRaises(drill.ValidationError) as caught:
                    client.count("SELECT COUNT(*) FROM example")
                self.assertNotIn("private-data", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                command = run.call_args.args[0]
                self.assertNotIn("-i", command)
                self.assertEqual(command[-1], "SELECT COUNT(*) FROM example")

    def test_archived_aes_key_matches_app_and_rejects_wrong_key(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = b"test-nonce12"
        # 应用支持32字节base64密钥、任意文本派生、SECRET_KEY历史回退。
        cases = [
            ({"ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode(), "SECRET_KEY": "s"}, b"x" * 32),
            ({"ENCRYPTION_KEY": "archive-test-key", "SECRET_KEY": "s"}, hashlib.sha256(b"archive-test-key").digest()),
            ({"SECRET_KEY": "test-secret"}, hashlib.sha256(b"ventasync-encryption|test-secret").digest()),
        ]
        for env, key in cases:
            with self.subTest(key_mode=bool(env.get("ENCRYPTION_KEY"))):
                payload = nonce + AESGCM(key).encrypt(nonce, b"sensitive-fixture", None)
                stored = "enc:v1:" + base64.urlsafe_b64encode(payload).decode()
                valid = drill.decrypt_count([stored], env)
                invalid = drill.decrypt_count([stored], {"ENCRYPTION_KEY": "wrong", "SECRET_KEY": "wrong"})
                self.assertEqual(valid["encrypted_verified"], 1)
                self.assertEqual(invalid["invalid"], 1)
                self.assertNotIn("sensitive-fixture", str(valid))


if __name__ == "__main__":
    unittest.main()
