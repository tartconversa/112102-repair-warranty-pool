"""SQLite 状态库。

来源六张 CSV 表按原字段入库存档，应用自己的状态（本地登记的事件、
续约方案、审计快照、导入批次）分开存放，重复导入不会覆盖人工登记。
所有日期字段在来源中都是整数“第 N 天”，库内沿用整数。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# 来源表名 -> 列定义（与 sources/*.csv 的表头一一对应）
SOURCE_TABLES = {
    "service_intervals": [
        ("part", "TEXT NOT NULL"),
        ("vendor", "TEXT NOT NULL"),
        ("lot", "TEXT"),
        ("site", "TEXT NOT NULL"),
        ("observed_from", "INTEGER NOT NULL"),
        ("observed_to", "INTEGER NOT NULL"),
        ("usage_hours", "REAL NOT NULL"),
        ("ending", "TEXT NOT NULL"),
    ],
    "checks": [
        ("part", "TEXT NOT NULL"),
        ("inspection_day", "INTEGER NOT NULL"),
        ("result", "TEXT NOT NULL"),
        ("known_day", "INTEGER NOT NULL"),
    ],
    "lot_corrections": [
        ("part", "TEXT NOT NULL"),
        ("lot", "TEXT NOT NULL"),
        ("known_day", "INTEGER NOT NULL"),
        ("evidence", "TEXT NOT NULL"),
    ],
    "warranty_terms": [
        ("term", "TEXT NOT NULL"),
        ("vendor", "TEXT NOT NULL"),
        ("lot", "TEXT NOT NULL"),
        ("supported_site", "TEXT NOT NULL"),
        ("return_days", "INTEGER NOT NULL"),
        ("unit_cost", "REAL NOT NULL"),
        ("advance_limit", "INTEGER NOT NULL"),
    ],
    "interchange": [
        ("spare", "TEXT NOT NULL"),
        ("site", "TEXT NOT NULL"),
        ("approval", "TEXT NOT NULL"),
        ("known_day", "INTEGER NOT NULL"),
    ],
    "replacement_events": [
        ("event", "TEXT NOT NULL"),
        ("spare", "TEXT NOT NULL"),
        ("site", "TEXT NOT NULL"),
        ("returned_part", "TEXT NOT NULL"),
        ("term", "TEXT NOT NULL"),
        ("action", "TEXT NOT NULL"),
        ("day", "INTEGER NOT NULL"),
    ],
}

VALID_ENDINGS = {"in_service", "planned", "transfer", "withdrawn"}
VALID_CHECK_RESULTS = {"clear", "failed"}
VALID_APPROVALS = {"approved", "inspection_required"}
# replacement_events 的动作（含本应用允许本地补登的动作）
VALID_ACTIONS = {
    "requested",
    "reserved",
    "dispatched",
    "awaiting_inspection",
    "return_confirmed",
    "receipt_recorded",
}

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);

-- 续约方案
CREATE TABLE IF NOT EXISTS scenarios (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    horizon_days INTEGER NOT NULL,
    usage_multiplier REAL NOT NULL,
    created_at TEXT NOT NULL,
    is_baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scenario_terms (
    scenario TEXT NOT NULL REFERENCES scenarios(name) ON DELETE CASCADE,
    term TEXT NOT NULL,
    vendor TEXT NOT NULL,
    lot TEXT NOT NULL,
    supported_site TEXT NOT NULL,
    return_days INTEGER NOT NULL,
    unit_cost REAL NOT NULL,
    advance_limit INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (scenario, term, supported_site)
);

-- 本地登记：补登的替换交接、拆检/签收回执（来源表之外的人工记录）
CREATE TABLE IF NOT EXISTS local_events (
    event TEXT PRIMARY KEY,
    spare TEXT NOT NULL,
    site TEXT NOT NULL,
    returned_part TEXT NOT NULL,
    term TEXT,
    action TEXT NOT NULL,
    day INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 人工登记的备件复验放行（把 inspection_required 转为 approved 的证据）
CREATE TABLE IF NOT EXISTS local_approvals (
    spare TEXT NOT NULL,
    site TEXT NOT NULL,
    approval TEXT NOT NULL,
    known_day INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (spare, site, known_day)
);

-- 建议快照与审计：每次刷新可留存当时结论，用于“原建议为何变化”
CREATE TABLE IF NOT EXISTS advice_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_day INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）应用数据库。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    for table, columns in SOURCE_TABLES.items():
        col_sql = ", ".join(f"{name} {decl}" for name, decl in columns)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY "
            f"AUTOINCREMENT, {col_sql})"
        )
    conn.commit()
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    elif int(row["value"]) != SCHEMA_VERSION:  # pragma: no cover - 预留迁移
        raise RuntimeError(f"不支持的数据库版本: {row['value']}")


def reset_source_tables(conn: sqlite3.Connection) -> None:
    """清空六张来源表（在同一事务内由导入流程调用）。"""
    for table in SOURCE_TABLES:
        conn.execute(f"DELETE FROM {table}")


def ensure_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def insert_rows(
    conn: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple]
) -> None:
    placeholders = ", ".join("?" for _ in columns)
    cols = ", ".join(columns)
    conn.executemany(
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", rows
    )


def latest_known_day(conn: sqlite3.Connection) -> int:
    """当前库内所有“可知信息”的最大已知日，作为默认分析时点。"""
    candidates = [
        "SELECT COALESCE(MAX(known_day), 0) FROM checks",
        "SELECT COALESCE(MAX(known_day), 0) FROM lot_corrections",
        "SELECT COALESCE(MAX(known_day), 0) FROM interchange",
        "SELECT COALESCE(MAX(day), 0) FROM replacement_events",
        "SELECT COALESCE(MAX(day), 0) FROM local_events",
        "SELECT COALESCE(MAX(known_day), 0) FROM local_approvals",
        "SELECT COALESCE(MAX(observed_to), 0) FROM service_intervals",
    ]
    return max(conn.execute(sql).fetchone()[0] for sql in candidates)
