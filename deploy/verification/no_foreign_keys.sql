-- ============================================================================
-- VentaSync 零外键验收（plan.txt §5 验证项 8）
--
-- 用法（在部署机执行，需 MySQL root 或具备 information_schema 读权限的账号）：
--   docker compose -f deploy/docker-compose.yml exec -T mysql \
--     mysql -uroot -p < deploy/verification/no_foreign_keys.sql
--
-- 期望结果：
--   1. 「按库统计」每行「外键数量」均为 0（中心库 + 每个租户库各一行）；
--   2. 「汇总」的「外键总数」= 0，「结果」= 通过。
-- 若某个库找不到，说明库名与下方变量不一致，请先修改 @center_db / @tenant_like。
--
-- 说明：零外键规范同时由离线脚本 backend/scripts/verify_contracts.py 在
-- 模型元数据、Alembic 离线 SQL 两个层面校验；本 SQL 用于真实库的最终验收。
-- ============================================================================

SET NAMES utf8mb4;

-- 按实际部署修改：中心库名 / 租户库名前缀
SET @center_db := 'ventasync_center';
SET @tenant_like := 'vs_tenant_%';

-- 1) 按库统计外键数量（每个库都输出一行，0 表示通过）
SELECT
    s.SCHEMA_NAME AS `数据库`,
    (
        SELECT COUNT(*)
        FROM information_schema.REFERENTIAL_CONSTRAINTS rc
        WHERE rc.CONSTRAINT_SCHEMA = s.SCHEMA_NAME
    ) AS `外键数量`
FROM information_schema.SCHEMATA s
WHERE s.SCHEMA_NAME = @center_db
   OR s.SCHEMA_NAME LIKE @tenant_like
ORDER BY s.SCHEMA_NAME;

-- 2) 汇总（必须为 0）
SELECT
    COUNT(*) AS `外键总数`,
    IF(COUNT(*) = 0, '通过', '失败：存在外键') AS `结果`
FROM information_schema.REFERENTIAL_CONSTRAINTS
WHERE CONSTRAINT_SCHEMA = @center_db
   OR CONSTRAINT_SCHEMA LIKE @tenant_like;

-- 3) 表级外键约束明细（应为空结果集）
--    用于定位问题表；REFERENTIAL_CONSTRAINTS 与 TABLE_CONSTRAINTS 都应无记录。
SELECT
    tc.CONSTRAINT_SCHEMA AS `数据库`,
    tc.TABLE_NAME AS `表名`,
    tc.CONSTRAINT_NAME AS `约束名`
FROM information_schema.TABLE_CONSTRAINTS tc
WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'
  AND (tc.CONSTRAINT_SCHEMA = @center_db OR tc.CONSTRAINT_SCHEMA LIKE @tenant_like)
ORDER BY tc.CONSTRAINT_SCHEMA, tc.TABLE_NAME;

-- 4) 字符集检查（InnoDB + utf8mb4，辅助 P0 验收）
SELECT
    s.SCHEMA_NAME AS `数据库`,
    s.DEFAULT_CHARACTER_SET_NAME AS `字符集`,
    s.DEFAULT_COLLATION_NAME AS `排序规则`,
    IF(s.DEFAULT_CHARACTER_SET_NAME = 'utf8mb4', '通过', '失败：非 utf8mb4') AS `结果`
FROM information_schema.SCHEMATA s
WHERE s.SCHEMA_NAME = @center_db
   OR s.SCHEMA_NAME LIKE @tenant_like
ORDER BY s.SCHEMA_NAME;
