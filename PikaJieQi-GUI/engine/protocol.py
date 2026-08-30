"""解析引擎输出的轻量 UCI 协议层。

Tk 主线程不应该在消费 stdout 时做复杂的正则和业务判断。这里把一行
输出转换为不可变事件；未知的 ``info`` 扩展会被安全忽略。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JqInfo:
    """引擎可选输出的暗子身份分析。

    ``identities`` 的 key 通常是 ``R/A/C/P/N/B``，value 可以是 cp 数值、
    ``{"cp": ..., "move": ...}`` 或任意 JSON 标量。解析层不强行解释
    引擎的评分语义，以便 Expected/Worst 两种模式都能展示。
    """

    move: str = ""
    identities: dict[str, Any] = field(default_factory=dict)
    mode: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InfoLine:
    depth: int | None = None
    seldepth: int | None = None
    multipv: int = 1
    score_kind: str | None = None
    score: int | None = None
    pv: tuple[str, ...] = ()
    jq: JqInfo | None = None
    raw: str = ""


@dataclass(frozen=True)
class EngineLine:
    kind: str
    raw: str
    info: InfoLine | None = None
    jq: JqInfo | None = None
    value: str = ""


def _parse_jq(payload: str) -> JqInfo | None:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    identities = value.get("identities", value.get("types", {}))
    if not isinstance(identities, dict):
        identities = {}
    move = value.get("move", "")
    mode = value.get("mode", "")
    return JqInfo(
        move=move if isinstance(move, str) else "",
        identities={str(k).upper(): v for k, v in identities.items()},
        mode=mode if isinstance(mode, str) else "",
        raw=value,
    )


def _parse_info(line: str) -> InfoLine:
    tokens = line.split()
    depth = seldepth = multipv = score = None
    score_kind = None
    pv: tuple[str, ...] = ()
    jq = None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in ("depth", "seldepth", "multipv") and i + 1 < len(tokens):
            try:
                number = int(tokens[i + 1])
            except ValueError:
                i += 1
            else:
                if token == "depth":
                    depth = number
                elif token == "seldepth":
                    seldepth = number
                else:
                    multipv = max(number, 1)
                i += 2
                continue
        if token == "score" and i + 2 < len(tokens) and tokens[i + 1] in ("cp", "mate"):
            score_kind = tokens[i + 1]
            try:
                score = int(tokens[i + 2])
            except ValueError:
                score = None
            i += 3
            continue
        if token == "pv":
            pv = tuple(tokens[i + 1:])
            break
        i += 1
    return InfoLine(depth, seldepth, multipv or 1, score_kind, score, pv, jq, line)


def parse_engine_line(line: str) -> EngineLine:
    """将一行 UCI/JieQi 输出分类。

    该函数是纯函数，适合在后台线程或测试中使用。``info string JQ ``
    后面的内容必须是 JSON；畸形 JSON 不会影响普通 UCI 分析。
    """
    raw = line.rstrip("\r\n")
    stripped = raw.strip()
    if stripped == "readyok":
        return EngineLine("ready", raw)
    if stripped.startswith("bestmove "):
        parts = stripped.split()
        return EngineLine("bestmove", raw, value=parts[1] if len(parts) > 1 else "")
    if stripped.startswith("info string JQ "):
        jq = _parse_jq(stripped[len("info string JQ "):])
        return EngineLine("jq", raw, jq=jq, value="" if jq is None else "JQ")
    if stripped.startswith("info "):
        return EngineLine("info", raw, info=_parse_info(stripped))
    if stripped.startswith("Unknown command"):
        return EngineLine("error", raw)
    if stripped.startswith("__ENGINE_DIED__:"):
        return EngineLine("died", raw, value=stripped.partition(":")[2])
    return EngineLine("other", raw)