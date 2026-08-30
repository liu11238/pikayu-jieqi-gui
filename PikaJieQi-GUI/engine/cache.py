"""有界、按局面和选项隔离的分析缓存。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Hashable

from .state import AnalysisSnapshot


@dataclass(frozen=True)
class CacheKey:
    position: str
    options: tuple[tuple[str, str], ...]
    depth: int


class AnalysisCache:
    def __init__(self, maxsize: int = 128):
        if maxsize < 1:
            raise ValueError("maxsize 必须大于 0")
        self.maxsize = maxsize
        self._items: OrderedDict[CacheKey, AnalysisSnapshot] = OrderedDict()

    def get(self, key: CacheKey) -> AnalysisSnapshot | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: CacheKey, value: AnalysisSnapshot) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)