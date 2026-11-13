#!/usr/bin/env python3
"""只读验证隔离恢复的数据关联及归档配置，不启动应用、不请求业务或云服务。

MySQL 查询只通过已验证隔离容器的 TCP 连接执行 SELECT；调用方负责容器/卷隔离断言。
AES-GCM 使用宿主机已有 cryptography，与 app.core.config/security 的密钥算法一致；
依赖缺失或密钥无法解密均记录失败，不把结构完整误认为登录/云连接已验证。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ValidationError(Exception):
    """只含可安全输出的固定错误描述。"""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise ValidationError("invalid_database_identifier")
    return f"`{value}`"


def load_env(path: Path) -> dict[str, str]:
    """读取本项目单行 .env 格式；绝不 source 或执行归档配置。"""
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            raise ValidationError("unsupported_env_line")
        key, value = match.groups()
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote, value = value[0], value[1:]
            chars: list[str] = []
            index = 0
            while index < len(value):
                char = value[index]
                if char == quote:
                    remainder = value[index + 1:].strip()
                    if remainder and not remainder.startswith("#"):
                        raise ValidationError("unsupported_env_quote_suffix")
                    break
                if quote == '"' and char == "\\" and index + 1 < len(value):
                    following = value[index + 1]
                    if following in '\\"$`':
                        chars.append(following)
                        index += 2
                        continue
                if quote == '"' and char in "$`":
                    raise ValidationError("env_interpolation_not_supported")
                chars.append(char)
                index += 1
            else:
                raise ValidationError("unterminated_env_quote")
            value = "".join(chars)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if "$" in value or "`" in value or "\\" in value:
                raise ValidationError("env_interpolation_not_supported")
        result[key] = value
    return result


class MySQL:
    def __init__(self, container: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise ValidationError("invalid_container_identifier")
        self.container = container

    def query(self, sql: str) -> str:
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValidationError("non_readonly_query_rejected")
        command = [
            "docker", "exec", self.container, "sh", "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP '
            '-h127.0.0.1 -P3306 -uroot --batch --raw --skip-column-names -e "$1"',
            "sh", sql,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    encoding="utf-8", errors="strict", timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise ValidationError(f"mysql_query_unavailable:{type(exc).__name__}") from None
        if result.returncode:
            # stdout/stderr can contain data or credentials; never include them in the report.
            raise ValidationError(f"mysql_query_failed:exit={result.returncode}")
        return result.stdout.strip()

    def count(self, sql: str) -> int:
        value = self.query(sql)
        if not re.fullmatch(r"[0-9]+", value):
            raise ValidationError("invalid_mysql_count_output")
        return int(value)

    def objects(self, sql: str) -> list[dict]:
        value = self.query(sql)
        try:
            rows = [json.loads(line) for line in value.splitlines()]
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError
            return rows
        except (ValueError, TypeError):
            raise ValidationError("invalid_mysql_json_output") from None


def valid_https(value: str, *, endpoint: bool = False) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and "\\" not in value
            and not any(ord(c) < 33 for c in value)
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and (not endpoint or parsed.path in {"", "/"})
            and not any(part in {".", ".."} for part in unquote(parsed.path).split("/"))
        )
    except (ValueError, TypeError):
        return False


def valid_storage(row: dict) -> bool:
    bucket = row.get("bucket", "")
    provider = row.get("provider")
    addressing = row.get("addressing_style")
    region = row.get("region") or ""
    if not all(isinstance(row.get(key), str) for key in (
        "provider", "endpoint", "bucket", "public_base_url", "addressing_style"
    )) or not isinstance(region, str):
        return False
    return bool(
        provider in {"oss", "cos", "obs", "s3", "s3_compatible"}
        and addressing in {"auto", "virtual", "path"}
        and valid_https(row.get("endpoint", ""), endpoint=True)
        and valid_https(row.get("public_base_url", ""))
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", bucket)
        and ".." not in bucket
        and (not region or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", region))
        and (provider != "obs" or (
            addressing != "path" and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
        ))
    )


def decrypt_count(values: list[str], env: dict[str, str]) -> dict[str, int]:
    """只返回计数；兼容应用历史明文，明文数单独记录，不声称经过解密。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ValidationError("cryptography_unavailable:decryption_not_verified") from None
    raw = env.get("ENCRYPTION_KEY", "").encode("utf-8")
    if raw:
        try:
            decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        except Exception:
            decoded = b""
        key = decoded if len(decoded) == 32 else hashlib.sha256(raw).digest()
    else:
        key = hashlib.sha256(b"ventasync-encryption|" + env["SECRET_KEY"].encode()).digest()
    encrypted = plaintext = invalid = 0
    for stored in values:
        if not isinstance(stored, str) or not stored.strip():
            invalid += 1
            continue
        if not stored.startswith("enc:v1:"):
            # decrypt_secret() explicitly supports legacy plaintext.
            plaintext += 1
            continue
        try:
            payload = base64.urlsafe_b64decode(stored[len("enc:v1:"):].encode("ascii"))
            decrypted = AESGCM(key).decrypt(payload[:12], payload[12:], None).decode("utf-8")
            if not decrypted.strip():
                raise ValueError
            encrypted += 1
        except Exception:
            invalid += 1
    return {"encrypted_verified": encrypted, "legacy_plaintext": plaintext, "invalid": invalid}


class Validator:
    def __init__(self, mysql: MySQL, center: str, manifest: dict, env: dict[str, str]):
        self.mysql, self.center_name, self.center = mysql, center, identifier(center)
        self.manifest, self.env = manifest, env
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: dict | str):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})

    def count_check(self, name: str, sql: str, *, positive: bool = False, expected: int = 0):
        try:
            count = self.mysql.count(sql)
            self.add(name, count > 0 if positive else count == expected, {"count": count})
        except ValidationError as exc:
            self.add(name, False, str(exc))

    def run(self) -> dict:
        databases = self.manifest.get("databases")
        if (not isinstance(databases, list) or not databases
                or len(databases) != len(set(databases))
                or self.center_name not in databases
                or self.manifest.get("center_database") != self.center_name):
            raise ValidationError("manifest_database_mapping_invalid")
        for name in databases:
            identifier(name)
        self.add("manifest_databases", True, {"count": len(databases)})
        required = ("MYSQL_ROOT_PASSWORD", "APP_IMAGE_TAG", "SECRET_KEY")
        missing = sum(not self.env.get(key, "").strip() for key in required)
        mode = self.env.get("DEPLOYMENT_MODE", "center")
        tenant_id = self.env.get("TENANT_ID", "0")
        config_ok = (missing == 0 and mode in {"center", "tenant"}
                     and self.env.get("CENTER_DB_NAME", "ventasync_center") == self.center_name
                     and (mode != "tenant" or bool(re.fullmatch(r"[1-9][0-9]*", tenant_id))))
        self.add("archive_config_structure", config_ok, {"missing_required_values": missing})
        self.count_check("center_active_accounts", f"SELECT COUNT(*) FROM {self.center}.users "
            "WHERE status='active' AND CHAR_LENGTH(password_hash)=60 "
            "AND LEFT(password_hash,4) IN ('$2a$','$2b$','$2y$')", positive=True)
        self.count_check("center_member_orphans", f"SELECT COUNT(*) FROM {self.center}.tenant_memberships m "
            f"LEFT JOIN {self.center}.users u ON u.id=m.user_id "
            f"LEFT JOIN {self.center}.tenants t ON t.id=m.tenant_id "
            "WHERE u.id IS NULL OR t.id IS NULL")
        self.count_check("center_active_tenant_owners", f"SELECT COUNT(*) FROM {self.center}.tenants t "
            "WHERE t.status='active' AND NOT EXISTS ("
            f"SELECT 1 FROM {self.center}.tenant_memberships m "
            f"JOIN {self.center}.users u ON u.id=m.user_id "
            "WHERE m.tenant_id=t.id AND m.is_owner=1 AND m.status='active' "
            "AND u.id=t.owner_user_id AND u.status='active' AND CHAR_LENGTH(u.password_hash)=60 "
            "AND LEFT(u.password_hash,4) IN ('$2a$','$2b$','$2y$'))")
        if mode == "center":
            self.count_check("center_platform_admin", f"SELECT COUNT(*) FROM {self.center}.users u "
                f"JOIN {self.center}.platform_user_roles ur ON ur.user_id=u.id "
                f"JOIN {self.center}.platform_roles r ON r.id=ur.role_id "
                "WHERE u.status='active' AND CHAR_LENGTH(u.password_hash)=60 "
                "AND LEFT(u.password_hash,4) IN ('$2a$','$2b$','$2y$') AND r.code='platform_admin'",
                positive=True)
        tenants = [name for name in databases if name != self.center_name]
        self.validate_dump_coverage(tenants)
        if mode == "tenant":
            self.add("bound_tenant_dump", len(tenants) == 1, {"count": len(tenants)})
        for database in tenants:
            self.validate_tenant(database, int(tenant_id) if mode == "tenant" and tenant_id.isdigit() else None)
        self.validate_config_secrets(config_ok)
        return self.report()

    def validate_dump_coverage(self, databases: list[str]):
        """中心库恢复成功不等于租户业务数据已备份，遗漏路由对应库必须失败。"""
        center = self.center
        self.count_check("active_tenants_without_route", f"SELECT COUNT(*) FROM {center}.tenants t "
            f"LEFT JOIN {center}.tenant_databases d ON d.tenant_id=t.id "
            "WHERE t.status='active' AND d.id IS NULL")
        try:
            routes = self.mysql.objects(
                "SELECT JSON_OBJECT('database_name',d.database_name,'status',d.status) "
                f"FROM {center}.tenant_databases d JOIN {center}.tenants t ON t.id=d.tenant_id "
                "WHERE t.status='active'")
            names = [row.get("database_name") for row in routes]
            for name in names:
                identifier(name)
            missing = sorted(set(names) - set(databases))
            self.add("active_tenant_dump_coverage", not missing,
                     {"active_routes": len(routes), "imported_tenant_dumps": len(databases),
                      "missing_databases": missing})
            if missing:
                self.add("tenant_business_validation_coverage", False,
                         {"unvalidated_databases": missing, "reason": "tenant_dump_missing"})
        except ValidationError as exc:
            self.add("active_tenant_dump_coverage", False, str(exc))

    def validate_tenant(self, database: str, bound_id: int | None):
        db, center = identifier(database), self.center
        prefix = f"tenant:{database}:"
        try:
            rows = self.mysql.objects(
                "SELECT JSON_OBJECT('tenant_id',tenant_id,'status',status,'port',port,"
                "'host_present',CHAR_LENGTH(TRIM(host))>0,'user_present',CHAR_LENGTH(TRIM(username))>0) "
                f"FROM {center}.tenant_databases WHERE database_name='{database}'")
            valid = (len(rows) == 1 and isinstance(rows[0].get("tenant_id"), int)
                     and rows[0]["tenant_id"] > 0 and rows[0].get("status") == "ready"
                     and isinstance(rows[0].get("port"), int) and 1 <= rows[0]["port"] <= 65535
                     and rows[0].get("host_present") == 1 and rows[0].get("user_present") == 1)
            self.add(prefix + "route", valid, {"route_count": len(rows)})
            if not valid:
                return
            tid = rows[0]["tenant_id"]
            if bound_id is not None:
                self.add(prefix + "bound_tenant_id_match", tid == bound_id,
                         {"configured_tenant_id": bound_id, "actual_tenant_id": tid})
        except ValidationError as exc:
            self.add(prefix + "route", False, str(exc))
            return
        self.count_check(prefix + "owner_access", f"SELECT COUNT(DISTINCT m.id) FROM {center}.users u "
            f"JOIN {center}.tenant_memberships m ON m.user_id=u.id "
            f"JOIN {center}.tenants t ON t.id=m.tenant_id AND t.owner_user_id=u.id "
            f"JOIN {db}.member_roles mr ON mr.member_id=m.id AND mr.tenant_id=m.tenant_id "
            f"JOIN {db}.roles r ON r.id=mr.role_id AND r.tenant_id=mr.tenant_id "
            f"JOIN {db}.role_permissions rp ON rp.role_id=r.id AND rp.tenant_id=r.tenant_id "
            f"JOIN {db}.role_binding_scopes s ON s.member_role_id=mr.id AND s.tenant_id=mr.tenant_id "
            f"WHERE t.id={tid} AND t.status='active' AND m.status='active' AND m.is_owner=1 "
            "AND u.status='active' AND CHAR_LENGTH(u.password_hash)=60 "
            "AND LEFT(u.password_hash,4) IN ('$2a$','$2b$','$2y$') "
            "AND r.status='active' AND r.code='owner' AND rp.permission_code='catalog.product.read' "
            "AND s.scope_type='all' AND s.scope_id=0", positive=True)
        self.count_check(prefix + "member_role_orphans", f"SELECT COUNT(*) FROM {db}.member_roles mr "
            f"LEFT JOIN {db}.roles r ON r.id=mr.role_id AND r.tenant_id=mr.tenant_id "
            f"LEFT JOIN {center}.tenant_memberships m ON m.id=mr.member_id AND m.tenant_id=mr.tenant_id "
            f"LEFT JOIN {center}.users u ON u.id=m.user_id "
            f"WHERE mr.tenant_id<>{tid} OR r.id IS NULL OR m.id IS NULL OR u.id IS NULL")
        self.count_check(prefix + "permission_orphans", f"SELECT COUNT(*) FROM {db}.role_permissions p "
            f"LEFT JOIN {db}.roles r ON r.id=p.role_id AND r.tenant_id=p.tenant_id "
            f"WHERE p.tenant_id<>{tid} OR r.id IS NULL OR TRIM(p.permission_code)=''")
        self.count_check(prefix + "scope_orphans", f"SELECT COUNT(*) FROM {db}.role_binding_scopes s "
            f"LEFT JOIN {db}.member_roles mr ON mr.id=s.member_role_id AND mr.tenant_id=s.tenant_id "
            f"WHERE s.tenant_id<>{tid} OR mr.id IS NULL")
        self.count_check(prefix + "settings", f"SELECT COUNT(*) FROM {db}.tenant_settings "
            f"WHERE tenant_id={tid} AND permission_version>=1 AND CHAR_LENGTH(TRIM(timezone))>0", expected=1)
        for table in ("products", "product_skus", "sku_identifiers"):
            try:
                total = self.mysql.count(f"SELECT COUNT(*) FROM {db}.{table}")
                foreign = self.mysql.count(f"SELECT COUNT(*) FROM {db}.{table} WHERE tenant_id<>{tid}")
                self.add(prefix + table, foreign == 0, {"count": total, "foreign_tenant_rows": foreign})
            except ValidationError as exc:
                self.add(prefix + table, False, str(exc))
        self.count_check(prefix + "sku_orphans", f"SELECT COUNT(*) FROM {db}.product_skus s "
            f"LEFT JOIN {db}.products p ON p.id=s.product_id AND p.tenant_id=s.tenant_id WHERE p.id IS NULL")
        self.count_check(prefix + "alias_orphans", f"SELECT COUNT(*) FROM {db}.sku_identifiers a "
            f"LEFT JOIN {db}.product_skus s ON s.id=a.sku_id AND s.tenant_id=a.tenant_id WHERE s.id IS NULL")

    def validate_config_secrets(self, config_ok: bool):
        center = self.center
        self.count_check("default_storage_reference", f"SELECT COUNT(*) FROM {center}.platform_system_settings s "
            f"LEFT JOIN {center}.storage_profiles p ON p.id=s.default_storage_profile_id "
            "WHERE s.default_storage_profile_id IS NOT NULL AND (p.id IS NULL OR p.enabled<>1)")
        try:
            profiles = self.mysql.objects("SELECT JSON_OBJECT('provider',provider,'endpoint',endpoint,"
                "'bucket',bucket,'region',region,'public_base_url',public_base_url,"
                "'addressing_style',addressing_style,'key',access_key_id_cipher,"
                f"'secret',secret_access_key_cipher) FROM {center}.storage_profiles WHERE enabled=1")
            self.add("enabled_storage_structure", all(valid_storage(row) for row in profiles),
                     {"count": len(profiles), "invalid": sum(not valid_storage(row) for row in profiles)})
            routes = self.mysql.objects(
                f"SELECT JSON_OBJECT('secret',password_cipher) FROM {center}.tenant_databases")
            values = [row.get("secret") for row in routes]
            values.extend(row.get(key) for row in profiles for key in ("key", "secret"))
            if not config_ok:
                raise ValidationError("archive_config_invalid:decryption_not_verified")
            result = decrypt_count(values, self.env)
            self.add("archived_credentials_decrypt", result["invalid"] == 0, result)
        except ValidationError as exc:
            self.add("archived_credentials_decrypt", False, str(exc))

    def report(self) -> dict:
        return {
            "checks": self.checks,
            "passed": bool(self.checks) and all(check["ok"] for check in self.checks),
            "coverage": "只读 SQL 关联、配置结构及归档密钥离线解密；未验证实际业务登录或云服务连通。",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--center", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValidationError("invalid_manifest_object")
        env_error = None
        try:
            env = load_env(args.env_file)
        except (ValidationError, OSError) as exc:
            env = {}
            env_error = str(exc) if isinstance(exc, ValidationError) else "archive_env_unreadable"
        validator = Validator(MySQL(args.container), args.center, manifest, env)
        if env_error:
            validator.add("archive_env_parse", False, env_error)
        report = validator.run()
    except (ValidationError, OSError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, ValidationError) else f"validation_input_error:{type(exc).__name__}"
        report = {"checks": [{"name": "validation_input", "ok": False, "detail": reason}], "passed": False}
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        handle.write(encoded)
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
