-- ============================================================================
-- VentaSync 表 / 字段注释验收（plan.txt P0：应用表与字段有中文注释）
--
-- 用法（在部署机执行）：
--   docker compose -f deploy/docker-compose.yml exec -T mysql \
--     mysql -uroot -p < deploy/verification/schema_comments.sql
--
-- 期望结果：
--   1. 「缺注释明细」为空结果集；
--   2. 「汇总」的「缺注释总数」= 0、「结果」= 通过。
--
-- 说明：
--   * 本 SQL 检查「注释是否为空」；「是否中文」由 backend/scripts/verify_contracts.py
--     在模型元数据与 Alembic 离线 SQL 层面校验（SQL 侧 CJK 正则依赖排序规则，不做强校验）。
--   * alembic_version 由 Alembic 自动创建，不在业务表注释要求内。
-- ============================================================================

SET NAMES utf8mb4;

-- 按实际部署修改：中心库名 / 租户库名前缀
SET @center_db := 'ventasync_center';
SET @tenant_like := 'vs_tenant_%';

-- 1) 缺注释明细：表注释 + 字段注释（空结果 = 通过）
SELECT
    x.`检查项` AS `检查项`,
    x.`数据库` AS `数据库`,
    x.`对象` AS `对象`,
    x.`问题` AS `问题`
FROM (
    SELECT
        '表注释' AS `检查项`,
        t.TABLE_SCHEMA AS `数据库`,
        t.TABLE_NAME AS `对象`,
        '表缺少注释' AS `问题`
    FROM information_schema.TABLES t
    WHERE (t.TABLE_SCHEMA = @center_db OR t.TABLE_SCHEMA LIKE @tenant_like)
      AND t.TABLE_TYPE = 'BASE TABLE'
      AND t.TABLE_NAME <> 'alembic_version'
      AND (t.TABLE_COMMENT IS NULL OR t.TABLE_COMMENT = '')

    UNION ALL

    SELECT
        '字段注释' AS `检查项`,
        c.TABLE_SCHEMA AS `数据库`,
        CONCAT(c.TABLE_NAME, '.', c.COLUMN_NAME) AS `对象`,
        '字段缺少注释' AS `问题`
    FROM information_schema.COLUMNS c
    JOIN information_schema.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
     AND t.TABLE_NAME = c.TABLE_NAME
     AND t.TABLE_TYPE = 'BASE TABLE'
    WHERE (c.TABLE_SCHEMA = @center_db OR c.TABLE_SCHEMA LIKE @tenant_like)
      AND c.TABLE_NAME <> 'alembic_version'
      AND (c.COLUMN_COMMENT IS NULL OR c.COLUMN_COMMENT = '')
) x
ORDER BY x.`数据库`, x.`对象`;

-- 2) 汇总（必须为 0）
SELECT
    COUNT(*) AS `缺注释总数`,
    IF(COUNT(*) = 0, '通过', '失败：存在缺注释对象') AS `结果`
FROM (
    SELECT t.TABLE_SCHEMA, t.TABLE_NAME, '' AS COLUMN_NAME
    FROM information_schema.TABLES t
    WHERE (t.TABLE_SCHEMA = @center_db OR t.TABLE_SCHEMA LIKE @tenant_like)
      AND t.TABLE_TYPE = 'BASE TABLE'
      AND t.TABLE_NAME <> 'alembic_version'
      AND (t.TABLE_COMMENT IS NULL OR t.TABLE_COMMENT = '')

    UNION ALL

    SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME
    FROM information_schema.COLUMNS c
    JOIN information_schema.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
     AND t.TABLE_NAME = c.TABLE_NAME
     AND t.TABLE_TYPE = 'BASE TABLE'
    WHERE (c.TABLE_SCHEMA = @center_db OR c.TABLE_SCHEMA LIKE @tenant_like)
      AND c.TABLE_NAME <> 'alembic_version'
      AND (c.COLUMN_COMMENT IS NULL OR c.COLUMN_COMMENT = '')
) missing;

-- 3) 表注释抽样（人工确认中文与语义）
SELECT
    t.TABLE_SCHEMA AS `数据库`,
    t.TABLE_NAME AS `表名`,
    t.TABLE_COMMENT AS `表注释`
FROM information_schema.TABLES t
WHERE (t.TABLE_SCHEMA = @center_db OR t.TABLE_SCHEMA LIKE @tenant_like)
  AND t.TABLE_TYPE = 'BASE TABLE'
  AND t.TABLE_NAME <> 'alembic_version'
ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME;
