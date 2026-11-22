"""可重复导入：读取 sources/*.csv，做结构与引用校验后整批写入 SQLite。

设计要点
--------
- 同一个目录重复导入是幂等的：按文件哈希判断是否变化；六张来源表在
  一个事务内整体替换，本地登记（local_events 等）不受影响。
- 校验分两级：``ImportError`` 级别的硬错误（表头不符、缺列、外键悬空、
  枚举非法、数值非法）会整批回滚；警告级问题（如批号空白）收集返回，
  交给风险与保修模块按“不能归属”处理。
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import store

SOURCE_FILES = {
    "service_intervals": "service_intervals.csv",
    "checks": "checks.csv",
    "lot_corrections": "lot_corrections.csv",
    "warranty_terms": "warranty_terms.csv",
    "interchange": "interchange.csv",
    "replacement_events": "replacement_events.csv",
}

# 与 tests/test_sources.py 的表头契约保持一致
EXPECTED_HEADERS = {
    "service_intervals": [
        "part", "vendor", "lot", "site", "observed_from", "observed_to",
        "usage_hours", "ending",
    ],
    "checks": ["part", "inspection_day", "result", "known_day"],
    "lot_corrections": ["part", "lot", "known_day", "evidence"],
    "warranty_terms": [
        "term", "vendor", "lot", "supported_site", "return_days",
        "unit_cost", "advance_limit",
    ],
    "interchange": ["spare", "site", "approval", "known_day"],
    "replacement_events": [
        "event", "spare", "site", "returned_part", "term", "action", "day",
    ],
}

INT_COLUMNS = {
    "service_intervals": ["observed_from", "observed_to"],
    "checks": ["inspection_day", "known_day"],
    "lot_corrections": ["known_day"],
    "warranty_terms": ["return_days", "advance_limit"],
    "interchange": ["known_day"],
    "replacement_events": ["day"],
}
FLOAT_COLUMNS = {
    "service_intervals": ["usage_hours"],
    "warranty_terms": ["unit_cost"],
}


class ImportError_(Exception):
    """导入硬错误（触发整批回滚）。"""


@dataclass
class ImportReport:
    imported: dict[str, int] = field(default_factory=dict)
    skipped_unchanged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.imported)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path, table: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != EXPECTED_HEADERS[table]:
            raise ImportError_(
                f"{path.name} 表头不符：期望 {EXPECTED_HEADERS[table]}，"
                f"实际 {reader.fieldnames}"
            )
        rows = list(reader)
    if not rows:
        raise ImportError_(f"{path.name} 为空")
    seen: set[tuple] = set()
    for line, row in enumerate(rows, start=2):
        if set(row) != set(EXPECTED_HEADERS[table]):
            raise ImportError_(f"{path.name} 第 {line} 行列集合不合法")
        if None in row.values() or any(v is None for v in row.values()):
            raise ImportError_(f"{path.name} 第 {line} 行存在缺列")
        for key, value in row.items():
            if key != "lot" and not str(value).strip():
                raise ImportError_(
                    f"{path.name} 第 {line} 行 {key} 为空（仅 lot 允许空白）"
                )
        fingerprint = tuple(row[k] for k in EXPECTED_HEADERS[table])
        if fingerprint in seen:
            raise ImportError_(f"{path.name} 第 {line} 行与前文完全重复")
        seen.add(fingerprint)
    return rows


def _coerce(table: str, rows: list[dict[str, str]]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i, row in enumerate(rows, start=2):
        rec: dict[str, object] = dict(row)
        if "lot" in rec:
            rec["lot"] = row["lot"].strip() or None
        for col in INT_COLUMNS.get(table, []):
            try:
                rec[col] = int(row[col])
            except ValueError as exc:
                raise ImportError_(f"{table} 第 {i} 行 {col} 不是整数") from exc
        for col in FLOAT_COLUMNS.get(table, []):
            try:
                rec[col] = float(row[col])
            except ValueError as exc:
                raise ImportError_(f"{table} 第 {i} 行 {col} 不是数值") from exc
        out.append(rec)
    return out


def _validate_enums_and_ranges(data: dict[str, list[dict[str, object]]]) -> list[str]:
    warnings: list[str] = []
    for i, row in enumerate(data["service_intervals"], start=2):
        if row["ending"] not in store.VALID_ENDINGS:
            raise ImportError_(
                f"service_intervals 第 {i} 行 ending 非法: {row['ending']}"
            )
        if row["observed_from"] > row["observed_to"]:
            raise ImportError_(
                f"service_intervals 第 {i} 行观察窗起止倒置"
            )
        if row["usage_hours"] <= 0:
            raise ImportError_(
                f"service_intervals 第 {i} 行 usage_hours 必须为正"
            )
        if row["lot"] is None:
            warnings.append(
                f"service_intervals: {row['part']} 批号空白，"
                "在补录凭证到达前按“不能归属”处理"
            )
    parts = {r["part"] for r in data["service_intervals"]}
    for i, row in enumerate(data["checks"], start=2):
        if row["result"] not in store.VALID_CHECK_RESULTS:
            raise ImportError_(f"checks 第 {i} 行 result 非法: {row['result']}")
        if row["part"] not in parts:
            raise ImportError_(
                f"checks 第 {i} 行引用了不存在的部件 {row['part']}"
            )
        if row["known_day"] < row["inspection_day"]:
            raise ImportError_(
                f"checks 第 {i} 行 known_day 早于 inspection_day"
            )
        if row["known_day"] > row["inspection_day"]:
            warnings.append(
                f"checks: {row['part']} 第 {row['inspection_day']} 天的"
                f"{row['result']} 结论第 {row['known_day']} 天才可知（补交证据）"
            )
    for i, row in enumerate(data["lot_corrections"], start=2):
        if row["part"] not in parts:
            raise ImportError_(
                f"lot_corrections 第 {i} 行引用了不存在的部件 {row['part']}"
            )
    terms = {r["term"] for r in data["warranty_terms"]}
    spares = {r["spare"] for r in data["interchange"]}
    for i, row in enumerate(data["interchange"], start=2):
        if row["approval"] not in store.VALID_APPROVALS:
            raise ImportError_(
                f"interchange 第 {i} 行 approval 非法: {row['approval']}"
            )
    for i, row in enumerate(data["replacement_events"], start=2):
        if row["action"] not in store.VALID_ACTIONS:
            raise ImportError_(
                f"replacement_events 第 {i} 行 action 非法: {row['action']}"
            )
        if row["returned_part"] not in parts:
            raise ImportError_(
                f"replacement_events 第 {i} 行引用了不存在的部件 "
                f"{row['returned_part']}"
            )
        if row["term"] not in terms:
            raise ImportError_(
                f"replacement_events 第 {i} 行引用了不存在的条款 {row['term']}"
            )
        if row["spare"] not in spares:
            raise ImportError_(
                f"replacement_events 第 {i} 行引用了不存在的备件 {row['spare']}"
            )
    return warnings


def _last_hashes(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT source, file_hash FROM import_batches b "
        "WHERE id = (SELECT MAX(id) FROM import_batches WHERE source = b.source)"
    ).fetchall()
    return {r["source"]: r["file_hash"] for r in rows}


def import_sources(
    conn, sources_dir: str | Path, *, force: bool = False
) -> ImportReport:
    """把目录下六张来源 CSV 导入数据库。

    ``force=False`` 时文件哈希未变的表跳过；任一文件存在硬错误则全部
    回滚，数据库保持上一次成功导入的状态。
    """
    sources_dir = Path(sources_dir)
    if not sources_dir.is_dir():
        raise ImportError_(f"来源目录不存在: {sources_dir}")

    raw: dict[str, list[dict[str, object]]] = {}
    hashes: dict[str, str] = {}
    for table, filename in SOURCE_FILES.items():
        path = sources_dir / filename
        if not path.exists():
            raise ImportError_(f"缺少来源文件: {path}")
        hashes[table] = _hash_file(path)
        raw[table] = _coerce(table, _read_csv(path, table))

    warnings = _validate_enums_and_ranges(raw)
    previous = _last_hashes(conn)
    if not force and previous and all(
        previous.get(t) == h for t, h in hashes.items()
    ):
        return ImportReport(
            skipped_unchanged=list(SOURCE_FILES), warnings=warnings
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with conn:  # 单事务
            store.reset_source_tables(conn)
            for table, rows in raw.items():
                cols = EXPECTED_HEADERS[table]
                store.insert_rows(
                    conn, table, cols, [tuple(r[c] for c in cols) for r in rows]
                )
                conn.execute(
                    "INSERT INTO import_batches(source, file_hash, row_count, "
                    "imported_at) VALUES (?, ?, ?, ?)",
                    (table, hashes[table], len(rows), now),
                )
    except Exception as exc:  # pragma: no cover - 防御性回滚
        raise ImportError_(f"写入失败，已回滚: {exc}") from exc
    return ImportReport(
        imported={t: len(rows) for t, rows in raw.items()},
        warnings=warnings,
    )
