"""分析生命周期和快照聚合。

核心约束：一个 generation 只接受同一局面的输出；收到旧搜索的 bestmove
后才允许启动 pending 请求。这样 UI 节流不会把旧局面的 PV 串入新局面。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from .protocol import EngineLine, InfoLine, JqInfo, parse_engine_line


class AnalysisState(str, Enum):
    IDLE = "idle"
    SEARCHING = "searching"
    STOPPING = "stopping"
    WAITING_READY = "waiting_ready"


@dataclass(frozen=True)
class SearchRequest:
    generation: int
    position: str
    go: str
    reason: str = "position"


@dataclass(frozen=True)
class IdentityResult:
    move: str
    identities: dict[str, object]
    mode: str = ""


@dataclass(frozen=True)
class AnalysisSnapshot:
    generation: int
    depth: int = 0
    score_kind: str = ""
    score: int | None = None
    pv: tuple[str, ...] = ()
    multipv: tuple[tuple[int, tuple[str, ...], int | None, str], ...] = ()
    identities: tuple[IdentityResult, ...] = ()
    complete: bool = False

    @property
    def recommend(self) -> str:
        return self.pv[0] if self.pv else ""

    def merge_info(self, info: InfoLine) -> "AnalysisSnapshot":
        depth = max(self.depth, info.depth or 0)
        pv = info.pv or self.pv if info.multipv == 1 else self.pv
        score = info.score if info.score is not None and info.multipv == 1 else self.score
        score_kind = info.score_kind if info.score_kind and info.multipv == 1 else self.score_kind
        entries = list(self.multipv)
        if info.pv or info.score is not None:
            item = (info.multipv, info.pv, info.score, info.score_kind or "")
            entries = [entry for entry in entries if entry[0] != info.multipv]
            entries.append(item)
            entries.sort(key=lambda entry: entry[0])
        return replace(self, depth=depth, score_kind=score_kind, score=score,
                       pv=pv, multipv=tuple(entries))

    def merge_jq(self, jq: JqInfo) -> "AnalysisSnapshot":
        if not jq.identities:
            return self
        move = jq.move or self.recommend
        result = IdentityResult(move, dict(jq.identities), jq.mode)
        values = [item for item in self.identities if item.move != move]
        values.append(result)
        return replace(self, identities=tuple(values))


class EngineController:
    """不持有进程的 UCI 状态机。

    ``send`` 由 GUI/Engine 适配；controller 只负责决定何时 stop、何时
    position/go，以及把 stdout 变成待刷新的快照。
    """

    def __init__(self, send: Callable[[str], None], emit: Callable[[AnalysisSnapshot], None] | None = None):
        self.send = send
        self.emit = emit
        self.state = AnalysisState.IDLE
        self.generation = 0
        self.request: SearchRequest | None = None
        self.pending: SearchRequest | None = None
        self.snapshot = AnalysisSnapshot(0)

    def request_search(self, position: str, go: str, reason: str = "position") -> SearchRequest:
        self.generation += 1
        request = SearchRequest(self.generation, position, go, reason)
        self.snapshot = AnalysisSnapshot(request.generation)
        if self.state in (AnalysisState.SEARCHING, AnalysisState.STOPPING):
            self.pending = request
            if self.state is AnalysisState.SEARCHING:
                self.send("stop")
            self.state = AnalysisState.STOPPING
        else:
            self._start(request)
        return request

    def cancel(self) -> None:
        self.pending = None
        if self.state is AnalysisState.SEARCHING:
            self.send("stop")
            self.state = AnalysisState.STOPPING
        elif self.state is not AnalysisState.IDLE:
            self.state = AnalysisState.IDLE

    def _start(self, request: SearchRequest) -> None:
        self.request = request
        self.pending = None
        # A position payload may contain an optional extension command (for
        # example ``banmoves``).  Keeping it in the request preserves the
        # stop/bestmove ordering without teaching the controller about every
        # engine-specific UCI command.
        for command in request.position.splitlines():
            if command.strip():
                self.send(command)
        self.send(request.go)
        self.state = AnalysisState.SEARCHING

    def stop(self) -> None:
        """停止当前搜索，但不创建新的 pending request。"""
        if self.state is AnalysisState.SEARCHING:
            self.send("stop")
            self.state = AnalysisState.STOPPING

    def on_line(self, line: str) -> EngineLine:
        event = parse_engine_line(line)
        if event.kind == "info" and event.info is not None and self.state is AnalysisState.SEARCHING:
            self.snapshot = self.snapshot.merge_info(event.info)
            self._emit()
        elif event.kind == "jq" and event.jq is not None and self.state is AnalysisState.SEARCHING:
            self.snapshot = self.snapshot.merge_jq(event.jq)
            self._emit()
        elif event.kind == "bestmove":
            self.state = AnalysisState.IDLE
            if self.pending is not None:
                pending = self.pending
                self._start(pending)
        return event

    def _emit(self) -> None:
        if self.emit is not None:
            self.emit(self.snapshot)