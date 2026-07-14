"""版本字串比較工具。app 版本（如 "1.0.10"）不能用字串序比（"1.0.10" < "1.0.9"），
需拆段轉數字；非數字段 fallback 成字串 tuple，混雜列表仍可穩定排序。"""

from __future__ import annotations

from typing import Iterable


def version_key(v: str) -> tuple:
    """把版本字串轉成可排序的 key：數字段 → (0, 0, 主, 次, ...)，含非數字段 → (1, 原字串)。

    首元素 0/1 確保「可解析的數字版本」永遠排在「無法解析的字串」之前，
    兩類混雜時排序仍具決定性。
    """
    parts = str(v or "").split(".")
    try:
        return (0, 0) + tuple(int(p) for p in parts)
    except ValueError:
        return (1, str(v))


def max_version(versions: Iterable[str]) -> str | None:
    vs = [v for v in versions if v]
    return max(vs, key=version_key) if vs else None


def min_version(versions: Iterable[str]) -> str | None:
    vs = [v for v in versions if v]
    return min(vs, key=version_key) if vs else None
