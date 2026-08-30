"""GUI 配置持久化；只使用标准库 JSON，写入采用替换保证不留半文件。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class AppConfig:
    engine_path: str = ""
    threads: int = 1
    hash_mb: int = 16
    multipv: int = 1
    dark_mode: str = "Expected"
    start_depth: int = 4
    show_identity: bool = False
    cache_size: int = 128

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "AppConfig":
        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in allowed}
        try:
            result = cls(**values)
        except (TypeError, ValueError):
            return cls()
        result.validate()
        return result

    def validate(self) -> None:
        self.threads = max(1, min(int(self.threads), 1024))
        self.hash_mb = max(1, min(int(self.hash_mb), 33554432))
        self.multipv = max(1, min(int(self.multipv), 500))
        self.start_depth = max(1, min(int(self.start_depth), 30))
        self.cache_size = max(1, min(int(self.cache_size), 4096))
        if self.dark_mode not in ("Expected", "Worst"):
            self.dark_mode = "Expected"
        self.engine_path = str(self.engine_path or "")
        self.show_identity = bool(self.show_identity)

    def save(self, path: str | os.PathLike[str]) -> None:
        self.validate()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(self), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass