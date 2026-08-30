"""PikaJieQi 暗棋分析 GUI。

只做分析辅助：引擎给出 bestmove，但本程序不会代替任一方走棋。
需要 Python 3.9+；界面只使用标准库 Tkinter。
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from engine.cache import AnalysisCache, CacheKey
from engine.config import AppConfig
from engine.protocol import InfoLine, parse_engine_line
from engine.state import AnalysisSnapshot


START_FEN = "xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1"
PIECES = "RACPNBK"
DARK_PIECES = "RACPNB"
LABEL = {"K": "帅", "A": "仕", "B": "相", "N": "马", "C": "炮", "R": "车", "P": "兵"}
PIECE_LIMITS = {"R": 2, "A": 2, "C": 2, "P": 5, "N": 2, "B": 2, "K": 1}
# 暗位仍按初始标准棋盘位置决定“这一步的几何走法”。暗子翻开前，
# 这个位置类型不能当作暗子的真实身份：例如 d0 是士位，所以 d0→e1
# 只能按士的走法移动，但翻开后可以是暗子池中的任意棋种。
# 行顺序与 GUI 棋盘一致（从 rank 9 到 rank 0），与引擎 BPiece 的镜像
# 暗位编码相同；"." 表示该格不是暗位的标准棋种位置。
DARK_SQUARE_TYPES = (
    "RNBA.ABNR",
    ".........",
    ".C.....C.",
    "P.P.P.P.P",
    ".........",
    ".........",
    "P.P.P.P.P",
    ".C.....C.",
    ".........",
    "RNBA.ABNR",
)
ENGINE_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Pikafish-jieqi-old", "src", "PikaJieQi.exe"))


def config_path() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home()))
    return root / "PikaJieQi" / "gui.json"


def parse_board(fen: str) -> list[list[str]]:
    rows = fen.split()[0].split("/")
    if len(rows) != 10:
        raise ValueError("棋盘必须有 10 行")
    board = []
    for row in rows:
        out = []
        for c in row:
            if c.isdigit(): out.extend(["."] * int(c))
            elif c in "RACPNBKracpnbkxX": out.append(c)
            else: raise ValueError("棋盘包含无效字符")
        if len(out) != 9:
            raise ValueError("每行棋盘必须有 9 格")
        board.append(out)
    return board


def board_fen(board: list[list[str]]) -> str:
    rows = []
    for row in board:
        s, empty = "", 0
        for c in row + ["#"]:
            if c == ".": empty += 1
            else:
                if empty: s += str(empty); empty = 0
                if c != "#": s += c
        rows.append(s)
    return "/".join(rows)


def square(col: int, row: int) -> str:
    # 引擎 UCI 坐标：a0 在底部，GUI 顶部为 9。
    return f"{chr(ord('a') + col)}{9 - row}"


def parse_sq(s: str) -> tuple[int, int]:
    return ord(s[0]) - ord("a"), 9 - int(s[1])


def empty_pool() -> dict[str, int]:
    """创建一份包含红黑双方所有暗子类型的池计数。"""
    return {piece: 0 for piece in DARK_PIECES} | {piece.lower(): 0 for piece in DARK_PIECES}


def parse_pool(tail: str) -> dict[str, int]:
    """解析引擎 FEN 尾部的暗子池字段。

    引擎使用例如 ``R2A2...r2a2...`` 的大小写区分颜色；``-`` 表示空池。
    这里不接受重复棋种或超出完整棋子数量的数量，避免 GUI 显示与引擎输入不一致。
    """
    parts = tail.split()
    token = parts[0] if parts else "-"
    pool = empty_pool()
    if token == "-":
        return pool
    if not re.fullmatch(r"(?:[RACPNB][0-5]|[racpnb][0-5]){0,12}", token):
        raise ValueError("暗子池字段格式错误")
    seen: set[str] = set()
    for piece, count in re.findall(r"([RACPNB]|[racpnb])([0-5])", token):
        if piece in seen or int(count) > PIECE_LIMITS[piece.upper()]:
            raise ValueError("暗子池字段包含重复棋种")
        seen.add(piece)
        pool[piece] = int(count)
    return pool


def pool_fen(pool: dict[str, int]) -> str:
    """按引擎约定生成暗子池字段。"""
    chunks = []
    for piece in DARK_PIECES:
        if pool[piece]: chunks.append(f"{piece}{pool[piece]}")
    for piece in DARK_PIECES:
        lower = piece.lower()
        if pool[lower]: chunks.append(f"{lower}{pool[lower]}")
    return "".join(chunks) or "-"


def replace_pool_in_tail(pool: dict[str, int], template: str = "0 1") -> str:
    parts = template.split()
    clock = " ".join(parts[1:]) if len(parts) > 1 else "0 1"
    return f"{pool_fen(pool)} {clock}".strip()


def copy_pool(pool: dict[str, int]) -> dict[str, int]:
    return {piece: pool.get(piece, 0) for piece in (*DARK_PIECES, *(p.lower() for p in DARK_PIECES))}


@dataclass
class Snapshot:
    board: list[list[str]]
    side: str
    fen_tail: str
    moves: list[str]
    pool: dict[str, int]
    captured: dict[str, int]
    unknown_captured: dict[str, int]


class Engine:
    def __init__(self, path: str, output: queue.Queue[str]):
        self.path, self.output = path, output
        self.command_log: list[str] = []
        self.proc: subprocess.Popen[str] | None = None
        self.ready = False

    def start(self):
        self.ready = False
        # 启动脚本可能先激活 Anaconda；其 DLL 会遮蔽构建引擎所需的
        # MSYS2 ucrt64 DLL，导致 Windows 以 0xC0000139 直接终止引擎。
        # 将构建工具链目录放在 PATH 最前面，同时保留用户自定义 PATH。
        env = os.environ.copy()
        dll_dirs = [
            r"D:\APPS\msys64\ucrt64\bin",
            r"C:\msys64\ucrt64\bin",
        ]
        env["PATH"] = os.pathsep.join(
            [path for path in dll_dirs if os.path.isdir(path)] + [env.get("PATH", "")]
        )
        self.proc = subprocess.Popen([self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                      errors="replace", bufsize=1, cwd=os.path.dirname(self.path), env=env)
        threading.Thread(target=self._read, daemon=True).start()
        self.send("uci")
        self.send("isready")

    def _read(self):
        proc = self.proc
        assert proc and proc.stdout
        for line in proc.stdout:
            self.output.put(line.rstrip())
        # 进程退出（崩溃或正常退出）时，向队列注入哨兵让 GUI 感知。
        self.output.put(f"__ENGINE_DIED__:{proc.poll()}")

    def send(self, command: str):
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            self.command_log.append(command)
            try: self.proc.stdin.write(command + "\n"); self.proc.stdin.flush()
            except OSError: pass

    def close(self):
        self.send("quit")
        if self.proc:
            try: self.proc.terminate()
            except OSError: pass
        self.proc = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PikaJieQi 暗棋局面分析")
        self.geometry("1100x760"); self.minsize(900, 650)
        self.q: queue.Queue[str] = queue.Queue(); self.engine: Engine | None = None
        self.config_file = config_path()
        self.config = AppConfig.load(self.config_file)
        self.board = parse_board(START_FEN); self.side = "w"; self.tail = "R2A2C2P5N2B2r2a2c2p5n2b2 0 1"
        self.pool = parse_pool(self.tail); self.captured = {f"{color}{piece}": 0 for color in "wb" for piece in DARK_PIECES}; self.unknown_captured = {"w": 0, "b": 0}
        self.base_board = [r[:] for r in self.board]; self.base_side = self.side; self.base_tail = self.tail
        self.base_pool = copy_pool(self.pool); self.base_captured = self.captured.copy(); self.base_unknown_captured = self.unknown_captured.copy()
        self.moves: list[str] = []; self.redo: list[Snapshot] = []; self.selected: tuple[int, int] | None = None
        self.depth = tk.IntVar(value=self.config.start_depth); self.engine_path = tk.StringVar(value=self.config.engine_path or ENGINE_DEFAULT)
        self.flipped = False; self.analyzing = False; self.analysis_requested = False; self.pending_analysis = False; self.waiting_for_stop = False; self.position_pending = False; self.restart_scheduled = False; self.restart_waiting_ready = False; self.engine_ready = False; self.analysis_timer = None; self.analysis_ui_timer = None; self.pv: list[str] = []; self.recommend = ""; self.analysis_depth = 0; self.restart_count = 0; self.last_engine_death = 0.0
        self.analysis_generation = 0
        self.analysis_snapshot = AnalysisSnapshot(0)
        self.cache = AnalysisCache(self.config.cache_size)
        self.active_cache_key: CacheKey | None = None
        self.identity_enabled = self.config.show_identity
        self.banned_moves: list[str] = []
        self.ban_mode = "probing"
        self.ban_probe_stage = "idle"
        self.ban_probe_baseline = ""
        self.ban_probe_timer = None
        self.status = tk.StringVar(value="准备就绪"); self.score = tk.StringVar(value="层数：— | 评分：— | 棋谱：—")
        self.turn = tk.StringVar(value="当前行棋：红方")
        self._build(); self.refresh(); self._start_engine(); self.after(80, self._poll)

    def _build(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
        for text, cmd in (("重置", self.reset), ("编辑局面", self.edit_fen), ("撤销", self.undo), ("重做", self.redo_move), ("设置引擎", self.settings)):
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Label(top, text="起始深度").pack(side="left", padx=(18, 3)); ttk.Spinbox(top, from_=1, to=30, width=4, textvariable=self.depth).pack(side="left")
        self.analysis_button = ttk.Button(top, text="开始持续分析", command=self.toggle_analysis); self.analysis_button.pack(side="left", padx=8)
        ttk.Button(top, text="强制变招", command=self.force_variation).pack(side="left", padx=3)
        ttk.Button(top, text="清除禁用", command=self.clear_bans).pack(side="left", padx=3)
        ttk.Button(top, text="翻转棋盘 180°", command=self.toggle_flip).pack(side="left", padx=3)
        ttk.Label(top, textvariable=self.turn, font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)
        ttk.Label(top, textvariable=self.score, font=("Segoe UI", 12, "bold")).pack(side="right", padx=10)

        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=8)
        self.canvas = tk.Canvas(body, bg="#e4b96a", highlightthickness=0); self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw()); self.canvas.bind("<Button-1>", self.click_board)
        right = ttk.Frame(body, width=310); right.pack(side="right", fill="y", padx=(10, 0))
        ttk.Label(right, text="操作说明", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(right, text="点击棋子和目标格输入走法。\n开始持续分析后会不断加深搜索。\n绿色箭头为实时最优主变化，不会自动落子。\n吃暗子/翻暗子使用按钮选择类型。\n“强制变招”会临时禁用当前推荐走法。", justify="left").pack(anchor="w", pady=5)
        ttk.Button(right, text="编辑暗子池", command=self.edit_pool).pack(anchor="w", pady=(12, 4))
        self.pool_text = tk.StringVar()
        ttk.Label(right, textvariable=self.pool_text, justify="left", foreground="#555").pack(anchor="w")
        self.bans_text = tk.StringVar(value="当前禁用：无")
        ttk.Label(right, textvariable=self.bans_text, justify="left", foreground="#8a4b08", wraplength=290).pack(anchor="w", pady=5)
        self.captured_text = tk.StringVar()
        ttk.Label(right, textvariable=self.captured_text, justify="left", foreground="#555").pack(anchor="w", pady=(4, 0))
        ttk.Label(right, text="走法记录", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(15, 2))
        self.history = tk.Listbox(right, height=18); self.history.pack(fill="both", expand=True)
        ttk.Label(right, text="候选主变化", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(8, 2))
        self.multipv_view = ttk.Treeview(right, columns=("rank", "move", "score"), show="headings", height=4)
        for column, title, width in (("rank", "序", 35), ("move", "走法", 75), ("score", "评分", 90)):
            self.multipv_view.heading(column, text=title); self.multipv_view.column(column, width=width, anchor="center")
        self.multipv_view.pack(fill="x")
        ttk.Label(right, text="暗子身份（JQ，可选）", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(8, 2))
        self.identity_view = ttk.Treeview(right, columns=("piece", "score", "count"), show="headings", height=6)
        for column, title, width in (("piece", "身份", 55), ("score", "分数", 90), ("count", "池数量", 60)):
            self.identity_view.heading(column, text=title); self.identity_view.column(column, width=width, anchor="center")
        self.identity_view.pack(fill="x")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    def _start_engine(self):
        if not os.path.isfile(self.engine_path.get()):
            self.status.set("未找到引擎，请在“设置引擎”中选择 PikaJieQi.exe"); return
        try:
            self.engine = Engine(self.engine_path.get(), self.q); self.engine.start()
            self._send_engine_options()
            self._begin_ban_probe()
        except OSError as e: messagebox.showerror("引擎启动失败", str(e))

    def _send_engine_options(self):
        if not self.engine:
            return
        self.engine.send(f"setoption name Threads value {self.config.threads}")
        self.engine.send(f"setoption name Hash value {self.config.hash_mb}")
        self.engine.send(f"setoption name MultiPV value {self.config.multipv}")
        self.engine.send(f"setoption name DarkSearchMode value {self.config.dark_mode}")
        self.engine.send(f"setoption name JieQi GUI Info value {'true' if self.identity_enabled else 'false'}")

    def position_command(self) -> str:
        """把 GUI 当前正在显示的局面原样发送给引擎。

        这里不能使用 ``base_* + moves``：暗子翻开、暗子吃暗子和撤销/重做
        后，GUI 的当前棋盘与基准局面已经不同。引擎的 ``position fen``
        可以直接接收完整的当前局面，因此不再附加 moves，避免引擎重复
        重放历史并与 GUI 显示不一致。
        """
        tail = replace_pool_in_tail(self.pool, self.tail)
        fen = board_fen(self.board) + " " + self.side + " " + tail
        return "position fen " + fen

    def _update_bans_text(self):
        if not hasattr(self, "bans_text"):
            return
        self.bans_text.set("当前禁用：" + ("、".join(self.banned_moves) if self.banned_moves else "无"))

    def _clear_banned_moves(self):
        self.banned_moves.clear()
        self._update_bans_text()

    def _all_legal_moves(self) -> list[str]:
        """枚举当前局面的所有 4 字符 UCI 合法走法。

        暗棋引擎的 searchmoves 只接受坐标部分；暗子真实类型仍由引擎的
        当前局面和暗子池决定，因此这里不为走法附加揭示类型。
        """
        moves: set[str] = set()
        for sr, row in enumerate(self.board):
            for sc, piece in enumerate(row):
                if not self.is_side_piece(piece, self.side):
                    continue
                for dr in range(10):
                    for dc in range(9):
                        if self.legal_piece_types((sc, sr), (dc, dr)):
                            moves.add(square(sc, sr) + square(dc, dr))
        return sorted(moves)

    def _begin_ban_probe(self):
        """启动一次轻量探测，确认当前引擎是否实现了 banmoves 扩展。"""
        self.ban_mode = "probing"
        self.ban_probe_stage = "waiting_ready"
        self.ban_probe_baseline = ""
        if self.ban_probe_timer is not None:
            self.after_cancel(self.ban_probe_timer)
            self.ban_probe_timer = None
        if self.engine and self.engine.ready:
            self._send_ban_probe_baseline()

    def _send_ban_probe_baseline(self):
        if not self.engine or not self.engine.ready:
            return
        self.ban_probe_stage = "baseline"
        # 探测使用固定的、合法且有多个走法的初始局面，避免用户在探测
        # 期间改动当前局面导致 baseline 与 banmoves 不属于同一局面。
        self.engine.send("position fen " + START_FEN)
        self.engine.send("go depth 1")
        self.ban_probe_timer = self.after(5000, self._ban_probe_timeout)

    def _ban_probe_timeout(self):
        self.ban_probe_timer = None
        if self.ban_mode != "probing" or self.ban_probe_stage not in ("baseline", "banned"):
            return
        if self.engine:
            self.engine.send("stop")
        self.ban_mode = "searchmoves"
        self.ban_probe_stage = "cancelled"
        self.ban_probe_baseline = ""
        self.status.set("banmoves 探测超时，已使用标准 searchmoves")
        if self.analysis_requested:
            self.pending_analysis = True

    def _finish_ban_probe(self, mode: str, status: str):
        self.ban_mode = mode
        self.ban_probe_stage = "idle"
        self.ban_probe_baseline = ""
        if self.ban_probe_timer is not None:
            self.after_cancel(self.ban_probe_timer)
            self.ban_probe_timer = None
        if self.engine and self.engine.ready:
            # 探测使用了固定初始局面；恢复 GUI 当前局面，即使用户没有
            # 立即点击“开始分析”，后续手动操作也不会基于探测局面。
            self.engine.send(self.position_command())
        self.status.set(status)
        if self.analysis_requested and self.engine and self.engine.ready:
            self.pending_analysis = False
            self.after(0, self.start_analysis)

    def _handle_ban_probe_line(self, line: str) -> bool:
        """处理探测期间的引擎输出；返回 True 表示该行已被吞掉。"""
        if self.ban_probe_stage not in ("baseline", "banned", "cancelled"):
            return False
        if self.ban_probe_stage == "cancelled":
            if line.startswith("bestmove "):
                self.ban_probe_stage = "idle"
                if self.ban_probe_timer is not None:
                    self.after_cancel(self.ban_probe_timer)
                    self.ban_probe_timer = None
                if self.engine:
                    self.engine.send(self.position_command())
                if self.pending_analysis and self.analysis_requested:
                    self.pending_analysis = False
                    self.after(0, self.start_analysis)
            return True
        if self.ban_probe_stage == "banned" and line.startswith("Unknown command") and "banmoves" in line:
            if self.ban_probe_timer is not None:
                self.after_cancel(self.ban_probe_timer)
                self.ban_probe_timer = None
            self.ban_mode = "searchmoves"
            self.ban_probe_stage = "cancelled"
            self.ban_probe_baseline = ""
            self.status.set("引擎不支持 banmoves，已使用标准 searchmoves")
            if self.analysis_requested:
                self.pending_analysis = True
            return True
        if not line.startswith("bestmove "):
            return True
        parts = line.split()
        move = parts[1] if len(parts) > 1 else "(none)"
        if self.ban_probe_stage == "baseline":
            if move == "(none)" or not re.fullmatch(r"[a-i][0-9][a-i][0-9]", move):
                self._finish_ban_probe("searchmoves", "banmoves 探测失败，已使用标准 searchmoves")
                return True
            self.ban_probe_baseline = move
            self.ban_probe_stage = "banned"
            self.engine.send("position fen " + START_FEN)
            self.engine.send("banmoves " + move)
            self.engine.send("go depth 1")
            return True
        if move != self.ban_probe_baseline and move != "(none)":
            self._finish_ban_probe("banmoves", "已启用 banmoves 强制变招")
        else:
            self._finish_ban_probe("searchmoves", "引擎不支持 banmoves，已使用标准 searchmoves")
        return True

    def _switch_to_searchmoves(self, status: str) -> bool:
        """运行中发现 banmoves 未过滤根走法时切换到标准白名单。"""
        if self.ban_mode != "banmoves":
            return False
        self.ban_mode = "searchmoves"
        self.clear_analysis()
        if self.analysis_requested:
            self.status.set(status)
            self.defer_analysis_restart()
        else:
            self.status.set(status)
        return True

    def force_variation(self):
        """临时禁用当前推荐走法，并从剩余根走法重新分析。"""
        if not self.engine or not self.engine.ready:
            self.status.set("引擎尚未就绪，暂时不能强制变招")
            return
        if self.ban_mode == "probing":
            self.status.set("正在检测引擎能力，请稍候")
            return
        if not self.analyzing and not self.analysis_requested:
            self.status.set("请先开始持续分析，再强制变招")
            return
        if self.waiting_for_stop or self.position_pending or self.restart_scheduled:
            self.status.set("正在切换局面或搜索，请稍候")
            return
        move = self.recommend[:4] if self.recommend else ""
        if not re.fullmatch(r"[a-i][0-9][a-i][0-9]", move):
            self.status.set("暂无可禁用的推荐走法")
            return
        if move in self.banned_moves:
            self.status.set(f"走法 {move} 已经禁用")
            return
        self.banned_moves.append(move)
        self._update_bans_text()
        self.clear_analysis()
        if self.analysis_requested:
            self.defer_analysis_restart()

    def clear_bans(self):
        if not self.banned_moves:
            self.status.set("当前没有禁用走法")
            return
        self._clear_banned_moves()
        self.clear_analysis()
        if self.analysis_requested:
            self.defer_analysis_restart()
        else:
            self.status.set("已清除所有禁用走法")

    def _cancel_ban_probe(self, fallback: bool = True):
        if self.ban_mode != "probing":
            return
        if self.engine and self.ban_probe_stage in ("baseline", "banned"):
            self.engine.send("stop")
            self.ban_probe_stage = "cancelled"
        else:
            self.ban_probe_stage = "idle"
        if fallback:
            self.ban_mode = "searchmoves"

    def _send_go(self) -> bool:
        """按引擎能力和当前禁用列表发送一次持续分析命令。"""
        if not self.engine or not self.engine.ready or self.ban_mode == "probing":
            return False
        legal_moves = self._all_legal_moves()
        remaining = [move for move in legal_moves if move not in self.banned_moves]
        if not legal_moves:
            self.clear_analysis()
            self.analyzing = False
            self.analysis_requested = False
            self.pending_analysis = False
            self.analysis_button.configure(text="开始持续分析")
            self.status.set("当前局面没有合法走法")
            return False
        if self.banned_moves and not remaining:
            self.clear_analysis()
            self.analyzing = False
            self.analysis_requested = False
            self.pending_analysis = False
            self.analysis_button.configure(text="开始持续分析")
            self.status.set("所有合法走法已禁用")
            return False
        # banmoves 必须在 position 之后、go 之前发送；旧引擎会在下一条 go
        # 消费并清空这个列表，所以每次启动搜索都要重新发送。
        self.engine.send(self.position_command())
        if self.ban_mode == "banmoves":
            if self.banned_moves:
                self.engine.send("banmoves " + " ".join(self.banned_moves))
            self.engine.send("go infinite")
        elif self.banned_moves:
            self.engine.send("go infinite searchmoves " + " ".join(remaining))
        else:
            # 没有禁用项时不必限制根走法，避免 GUI 合法走法枚举与引擎
            # 的暗棋细节存在差异时误把正常局面判成无路可走。
            self.engine.send("go infinite")
        return True

    def _analysis_cache_key(self) -> CacheKey:
        options = (
            ("DarkSearchMode", self.config.dark_mode),
            ("MultiPV", str(self.config.multipv)),
            ("Threads", str(self.config.threads)),
            ("Hash", str(self.config.hash_mb)),
            ("JieQi GUI Info", str(self.identity_enabled)),
            ("banmoves", " ".join(self.banned_moves)),
        )
        return CacheKey(self.position_command(), options, int(self.depth.get()))

    def set_new_base(self, board: list[list[str]], side: str, tail: str, pool: dict[str, int],
                     captured: dict[str, int] | None = None,
                     unknown_captured: dict[str, int] | None = None):
        """把当前局面固定为新的引擎基准，避免池被走法历史重复扣减。"""
        self.board, self.side, self.tail = [r[:] for r in board], side, tail
        self.pool = copy_pool(pool)
        self.captured = (captured if captured is not None else
                         {f"{color}{piece}": 0 for color in "wb" for piece in DARK_PIECES}).copy()
        self.unknown_captured = (unknown_captured if unknown_captured is not None else {"w": 0, "b": 0}).copy()
        self.base_board, self.base_side, self.base_tail = [r[:] for r in self.board], self.side, self.tail
        self.base_pool = copy_pool(self.pool); self.base_captured = self.captured.copy(); self.base_unknown_captured = self.unknown_captured.copy()
        self.moves.clear(); self.redo.clear()
        self._clear_banned_moves()

    def captured_from_fen(self, board: list[list[str]], pool: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
        """根据完整棋子总数、已知棋盘棋子和暗子池推导已知被吃统计。

        暗子池记录的是尚未确定身份的候选棋子，不是棋盘上暗子实体的
        第二份棋子。因此，棋盘上的 ``x/X`` 不参与已知棋种的逐类扣除；
        已知被吃按“完整数量 - 已知棋盘 - 暗子池”推导。暗子池多于棋盘
        未知暗子的部分，只能作为未知类型的被吃统计保留。
        """
        board_count = {f"{color}{piece}": 0 for color in "wb" for piece in DARK_PIECES}
        for row in board:
            for value in row:
                if value.upper() in DARK_PIECES:
                    board_count[f"{'w' if value.isupper() else 'b'}{value.upper()}"] += 1
        captured = {key: 0 for key in board_count}
        unknown = {"w": 0, "b": 0}
        for color in "wb":
            for piece in DARK_PIECES:
                remaining = board_count[f"{color}{piece}"] + pool[piece if color == "w" else piece.lower()]
                if remaining > PIECE_LIMITS[piece]:
                    raise ValueError(f"{('红' if color == 'w' else '黑')}{LABEL[piece]}总数超过 {PIECE_LIMITS[piece]}")
            unknown_board = sum(1 for row in board for value in row
                                if value == ("X" if color == "w" else "x"))
            known_board = sum(board_count[f"{color}{piece}"] for piece in DARK_PIECES)
            pool_count = sum(pool[piece if color == "w" else piece.lower()] for piece in DARK_PIECES)
            if pool_count < unknown_board:
                raise ValueError(f"{('红' if color == 'w' else '黑')}方暗子池少于棋盘未知暗子")
            unknown[color] = pool_count - unknown_board
            for piece in DARK_PIECES:
                captured[f"{color}{piece}"] = PIECE_LIMITS[piece] - (
                    board_count[f"{color}{piece}"] + pool[piece if color == "w" else piece.lower()])
        return captured, unknown

    def reset(self):
        was_analyzing = self.analysis_requested
        if was_analyzing: self.defer_analysis_restart()
        self.clear_analysis()
        board, side, tail = parse_board(START_FEN), "w", "R2A2C2P5N2B2r2a2c2p5n2b2 0 1"
        self.set_new_base(board, side, tail, parse_pool(tail))
        self.refresh("已重置"); self.sync()

    def edit_fen(self):
        was_analyzing = self.analysis_requested
        initial = board_fen(self.board) + " " + self.side + " " + self.tail
        value = simpledialog.askstring("编辑当前局面", "请输入完整暗棋 FEN：", initialvalue=initial, parent=self)
        if not value:
            return
        try:
            fields = value.split(); assert len(fields) >= 2 and len(fields[0].split("/")) == 10
            b = parse_board(value); assert all(len(r) == 9 for r in b)
            side = fields[1].lower(); assert side in ("w", "b")
            tail = " ".join(fields[2:]) or "- 0 1"; pool = parse_pool(tail)
            captured, unknown = self.captured_from_fen(b, pool)
            self.set_new_base(b, side, replace_pool_in_tail(pool, tail), pool, captured, unknown)
            if was_analyzing: self.defer_analysis_restart()
            self.clear_analysis(); self.refresh("局面已编辑"); self.sync()
        except (AssertionError, IndexError, ValueError): messagebox.showerror("FEN 错误", "请检查 10 行棋盘、行棋方和暗子池字段。")

    def edit_pool(self):
        """编辑当前局面的剩余暗子池，并以编辑后的局面建立新基准。"""
        was_analyzing = self.analysis_requested
        win = tk.Toplevel(self); win.title("编辑暗子池"); win.transient(self); win.grab_set(); win.resizable(False, False)
        ttk.Label(win, text="确认后当前走法历史会清空，并从新暗子池继续分析。", foreground="#555").grid(
            row=0, column=0, columnspan=3, padx=12, pady=(10, 5), sticky="w")
        variables: dict[str, tk.IntVar] = {}
        for column, (color, title) in enumerate((("w", "红方"), ("b", "黑方"))):
            frame = ttk.LabelFrame(win, text=title); frame.grid(row=1, column=column, padx=8, pady=5, sticky="n")
            for row, piece in enumerate(DARK_PIECES):
                key = piece if color == "w" else piece.lower()
                variables[key] = tk.IntVar(value=self.pool[key])
                ttk.Label(frame, text=f"{LABEL[piece]} ({piece})").grid(row=row, column=0, sticky="w", padx=6, pady=2)
                ttk.Spinbox(frame, from_=0, to=PIECE_LIMITS[piece], width=5,
                            textvariable=variables[key]).grid(row=row, column=1, padx=6, pady=2)

        error = tk.StringVar()
        ttk.Label(win, textvariable=error, foreground="#b00020").grid(row=2, column=0, columnspan=3, padx=10)

        def apply():
            try:
                pool = copy_pool(self.pool)
                for key, variable in variables.items():
                    value = int(variable.get())
                    if not 0 <= value <= PIECE_LIMITS[key.upper()]:
                        raise ValueError(f"{key} 数量超出上限")
                    pool[key] = value
                for color in "wb":
                    board_unknown = sum(1 for row in self.board for value in row
                                        if value == ("X" if color == "w" else "x"))
                    board_known = 0
                    pool_count = 0
                    captured_count = 0
                    for piece in DARK_PIECES:
                        board_count = sum(1 for row in self.board for value in row
                                          if value == (piece if color == "w" else piece.lower()))
                        board_known += board_count
                        pool_count += pool[piece if color == "w" else piece.lower()]
                        captured_count += self.captured[f"{color}{piece}"]
                        if board_count + pool[piece if color == "w" else piece.lower()] + self.captured[f"{color}{piece}"] > PIECE_LIMITS[piece]:
                            raise ValueError(f"{('红' if color == 'w' else '黑')}{LABEL[piece]}总数超过 {PIECE_LIMITS[piece]}")
                    if pool_count < board_unknown + self.unknown_captured[color]:
                        raise ValueError(f"{('红' if color == 'w' else '黑')}方暗子池少于棋盘未知暗子和未知被吃子")
                    if board_known + pool_count + captured_count > sum(PIECE_LIMITS.values()) - PIECE_LIMITS["K"]:
                        raise ValueError(f"{('红' if color == 'w' else '黑')}方暗子总数超过完整数量")
            except (ValueError, tk.TclError) as exc:
                error.set(str(exc)); return
            win.destroy()
            if was_analyzing: self.defer_analysis_restart()
            self.set_new_base(self.board, self.side, replace_pool_in_tail(pool, self.tail), pool,
                              self.captured, self.unknown_captured)
            self.clear_analysis(); self.refresh("暗子池已编辑，走法历史已重置"); self.sync()

        buttons = ttk.Frame(win); buttons.grid(row=3, column=0, columnspan=3, pady=10)
        ttk.Button(buttons, text="确认", command=apply).pack(side="left", padx=5)
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="left", padx=5)
        self.wait_window(win)

    def click_board(self, event):
        cell = min(self.canvas.winfo_width() / 10, self.canvas.winfo_height() / 11); ox, oy = cell, cell
        dc, dr = round((event.x - ox) / cell), round((event.y - oy) / cell)
        c, r = (8 - dc, 9 - dr) if self.flipped else (dc, dr)
        if not (0 <= c < 9 and 0 <= r < 10): return
        if self.selected is None:
            # 空格不能作为起点；只有当前确实有棋子的格子才能被选中。
            if self.board[r][c] == ".": return
            if not self.is_side_piece(self.board[r][c], self.side):
                self.status.set("当前是" + ("红方" if self.side == "w" else "黑方") + "行棋")
                return
            self.selected = (c, r); self.draw(); return
        fc, fr = self.selected
        if (fc, fr) == (c, r):
            self.selected = None
            self.draw()
            return
        self.play_move(square(fc, fr) + square(c, r), (fc, fr), (c, r))

    @staticmethod
    def _clear_path(board: list[list[str]], src: tuple[int, int], dst: tuple[int, int]) -> bool:
        """判断直线棋子从 src 到 dst 的中间是否没有棋子。"""
        sc, sr = src; dc, dr = dst
        step_c = 0 if dc == sc else (1 if dc > sc else -1)
        step_r = 0 if dr == sr else (1 if dr > sr else -1)
        c, r = sc + step_c, sr + step_r
        while (c, r) != (dc, dr):
            if board[r][c] != ".": return False
            c += step_c; r += step_r
        return True

    @classmethod
    def _piece_can_reach(cls, board: list[list[str]], side: str,
                         src: tuple[int, int], dst: tuple[int, int], piece: str) -> bool:
        """只按棋子本身的走法判断，不判断将军和己方棋子占位。"""
        sc, sr = src; dc, dr = dst
        if src == dst or not (0 <= sc < 9 and 0 <= sr < 10 and 0 <= dc < 9 and 0 <= dr < 10):
            return False
        dx, dy = dc - sc, dr - sr
        adx, ady = abs(dx), abs(dy)

        if piece == "R":
            return (sc == dc or sr == dr) and cls._clear_path(board, src, dst)
        if piece == "C":
            if not (sc == dc or sr == dr): return False
            blockers = 0
            step_c = 0 if dc == sc else (1 if dc > sc else -1)
            step_r = 0 if dr == sr else (1 if dr > sr else -1)
            c, r = sc + step_c, sr + step_r
            while (c, r) != (dc, dr):
                blockers += board[r][c] != "."
                c += step_c; r += step_r
            # 炮不吃子时无炮架，吃子时恰好隔一个棋子。
            return blockers == (1 if board[dr][dc] != "." else 0)
        if piece == "N":
            if (adx, ady) not in ((1, 2), (2, 1)): return False
            leg = (sc + (dx // 2), sr) if adx == 2 else (sc, sr + (dy // 2))
            return board[leg[1]][leg[0]] == "."
        if piece == "B":
            if adx != 2 or ady != 2 or board[sr + dy // 2][sc + dx // 2] != ".": return False
            # 揭棋规则允许象过河；仍保留“田”字走法和塞象眼限制。
            return True
        if piece == "A":
            # 揭棋规则允许士过河；士仍只能斜走一步，但不再受九宫限制。
            return adx == 1 and ady == 1
        if piece == "K":
            source_in_palace = (3 <= sc <= 5 and 7 <= sr <= 9) if side == "w" else (3 <= sc <= 5 and 0 <= sr <= 2)
            in_palace = (3 <= dc <= 5 and 7 <= dr <= 9) if side == "w" else (3 <= dc <= 5 and 0 <= dr <= 2)
            return source_in_palace and adx + ady == 1 and in_palace
        if piece == "P":
            forward = -1 if side == "w" else 1
            crossed = sr <= 4 if side == "w" else sr >= 5
            return (dx == 0 and dy == forward) or (crossed and dy == 0 and adx == 1)
        return False

    @classmethod
    def _square_attacked(cls, board: list[list[str]], target: tuple[int, int], by_side: str) -> bool:
        """判断 target 是否被已知棋子攻击；未知暗子只作为阻挡物处理。"""
        tc, tr = target
        for r, row in enumerate(board):
            for c, value in enumerate(row):
                if not cls.is_side_piece(value, by_side) or value in "xX":
                    continue
                if cls._piece_can_reach(board, by_side, (c, r), target, value.upper()):
                    return True

        # 中国象棋的将/帅可以隔空对望，不能只按一步移动判断。
        enemy_king = "K" if by_side == "w" else "k"
        for r, row in enumerate(board):
            for c, value in enumerate(row):
                if value == enemy_king and c == tc and cls._clear_path(board, (c, r), target):
                    return True
        return False

    @classmethod
    def _move_is_legal(cls, board: list[list[str]], side: str,
                       src: tuple[int, int], dst: tuple[int, int], piece: str) -> bool:
        """判断指定真实棋种的走法是否合法，包括不能让己方帅/将被将军。"""
        sc, sr = src; dc, dr = dst
        if not (0 <= sc < 9 and 0 <= sr < 10 and 0 <= dc < 9 and 0 <= dr < 10): return False
        moving = board[sr][sc]
        target = board[dr][dc]
        if not cls.is_side_piece(moving, side) or piece not in PIECES: return False
        if moving not in "xX" and moving.upper() != piece: return False
        if target != "." and cls.is_side_piece(target, side): return False
        # GUI 中暗子只可能是 R/A/C/P/N/B，不允许任何棋子吃帅/将。
        if target.upper() == "K": return False
        if not cls._piece_can_reach(board, side, src, dst, piece): return False

        after = [row[:] for row in board]
        after[sr][sc] = "."
        after[dr][dc] = piece if side == "w" else piece.lower()
        king = "K" if side == "w" else "k"
        for r, row in enumerate(after):
            for c, value in enumerate(row):
                if value == king and not cls._square_attacked(after, (c, r), "b" if side == "w" else "w"):
                    return True
        # 没有帅/将的编辑局面不在这里额外制造异常；引擎的 FEN 校验负责处理该类局面。
        return False

    def legal_piece_types(self, src: tuple[int, int], dst: tuple[int, int]) -> set[str]:
        """返回翻开暗子时可选的真实棋种；空集合表示不是合法走法。

        暗子尚未翻开时，走法几何由它所在的固定暗位决定，而不是由
        候选池中的真实棋种决定。因此 d0→e1 按士的走法验证，但候选
        仍然是池中所有剩余暗子类型。
        """
        moving = self.board[src[1]][src[0]]
        if moving in "xX":
            color = "w" if moving == "X" else "b"
            position_type = DARK_SQUARE_TYPES[src[1]][src[0]]
            if position_type not in DARK_PIECES or not self._move_is_legal(
                    self.board, self.side, src, dst, position_type):
                return set()
            return {piece for piece in DARK_PIECES
                    if self.pool.get(piece if color == "w" else piece.lower(), 0) > 0}
        piece = moving.upper()
        return {piece} if self._move_is_legal(self.board, self.side, src, dst, piece) else set()

    def dark_capture_types(self, captured: str) -> set[str]:
        color = "w" if captured == "X" else "b"
        return {piece for piece in DARK_PIECES
                if self.pool.get(piece if color == "w" else piece.lower(), 0) > 0}

    def play_move(self, uci: str, src: tuple[int, int], dst: tuple[int, int]):
        # 持续分析可能正处于 stop 等待阶段，此时 analyzing 已经为 False，
        # 但用户仍然要求换局面后继续分析，所以必须使用持久状态判断。
        was_analyzing = self.analysis_requested
        moving, captured = self.board[src[1]][src[0]], self.board[dst[1]][dst[0]]
        legal_types = self.legal_piece_types(src, dst)
        if not legal_types:
            self.status.set("非法走法：该棋子不能移动到目标格")
            return
        moving_type = ""
        captured_type = ""
        # 引擎扩展格式：明子吃暗子为 1 个被吃类型；暗子移动为 1 个
        # 移动类型；暗子吃暗子则依次为移动类型、被吃类型。
        if moving in "xX":
            # 暗子第一次移动必须亮出真实棋种；只有被吃的暗子可以保持未知。
            typ = self.choose_piece("翻开暗子", "选择移动暗子的真实类型", legal_types,
                                    allow_unknown=False)
            if not typ:
                self.status.set("已取消本次移动")
                return
            moving_type = typ if moving == "X" else typ.lower()
            if moving_type and captured in "xX":
                allowed = self.dark_capture_types(captured)
                if not allowed:
                    self.status.set("非法走法：暗子池中没有可匹配的被吃棋子")
                    return
                typ = self.choose_piece("暗子被吃", "选择被吃暗子的真实类型", allowed,
                                        allow_unknown=True)
                if typ is None:
                    self.status.set("已取消本次移动")
                    return
                if typ: captured_type = typ if captured == "X" else typ.lower()
        elif captured in "xX":
            allowed = self.dark_capture_types(captured)
            if not allowed:
                self.status.set("非法走法：暗子池中没有可匹配的被吃棋子")
                return
            typ = self.choose_piece("暗子被吃", "选择被吃暗子的真实类型", allowed,
                                    allow_unknown=True)
            if typ is None:
                self.status.set("已取消本次移动")
                return
            if typ: captured_type = typ if captured == "X" else typ.lower()
        suffix = moving_type + captured_type if moving in "xX" else captured_type
        self.redo.clear(); self.moves.append(uci + suffix)
        self.board[dst[1]][dst[0]], self.board[src[1]][src[0]] = moving, "."
        if moving_type: self.board[dst[1]][dst[0]] = moving_type
        self.side = "b" if self.side == "w" else "w"
        self.rebuild()
        self._clear_banned_moves()
        # 只有走法真正记录成功后才取消起点高亮；非法走法或弹窗取消时，
        # play_move() 会直接返回并保留原选中位置，方便继续选择目的地。
        self.selected = None
        # 必须在所有弹窗关闭、且新棋盘已经写入后再安排重启。
        # 否则 wait_window() 期间的 after 回调会按旧局面启动引擎。
        if was_analyzing: self.defer_analysis_restart()
        self.clear_analysis(); self.refresh("走法已记录"); self.sync()

    def undo(self):
        was_analyzing = self.analysis_requested
        if was_analyzing: self.defer_analysis_restart()
        if not self.moves:
            return
        self.redo.append(Snapshot([r[:] for r in self.board], self.side, self.tail, self.moves[:],
                                  copy_pool(self.pool), self.captured.copy(), self.unknown_captured.copy()))
        self.moves.pop(); self.rebuild(); self._clear_banned_moves(); self.clear_analysis(); self.refresh("已撤销"); self.sync()

    def redo_move(self):
        was_analyzing = self.analysis_requested
        if was_analyzing: self.defer_analysis_restart()
        if not self.redo:
            return
        snap = self.redo.pop()
        self.board, self.side, self.tail, self.moves = [r[:] for r in snap.board], snap.side, snap.fen_tail, snap.moves[:]
        self.pool, self.captured, self.unknown_captured = copy_pool(snap.pool), snap.captured.copy(), snap.unknown_captured.copy()
        self._clear_banned_moves(); self.clear_analysis(); self.refresh("已重做"); self.sync()

    def rebuild(self):
        # 本地重建显示及暗子统计；引擎同步时直接读取重建后的当前局面。
        self.board = [r[:] for r in self.base_board]; self.side = self.base_side
        self.pool = copy_pool(self.base_pool)
        self.captured = self.base_captured.copy(); self.unknown_captured = self.base_unknown_captured.copy()
        for m in self.moves:
            f, t = parse_sq(m[:2]), parse_sq(m[2:4])
            p = self.board[f[1]][f[0]]
            captured = self.board[t[1]][t[0]]
            self.board[t[1]][t[0]] = p
            self.board[f[1]][f[0]] = "."
            suffix = m[4:]
            if p in "xX":
                # 暗子走法第 5 个字符是移动暗子的类型；第 6 个字符
                # （仅暗吃暗）是被吃暗子的类型。
                if suffix and suffix[0].upper() in DARK_PIECES:
                    moving_type = suffix[0]
                    self.board[t[1]][t[0]] = moving_type
                    if self.pool.get(moving_type, 0) > 0:
                        self.pool[moving_type] -= 1
                    if captured in "xX":
                        if len(suffix) > 1 and suffix[1].upper() in DARK_PIECES:
                            captured_type = suffix[1]
                            self.captured[f"{'w' if captured == 'X' else 'b'}{captured_type.upper()}"] += 1
                            if self.pool.get(captured_type, 0) > 0:
                                self.pool[captured_type] -= 1
                        else:
                            self.unknown_captured["w" if captured == "X" else "b"] += 1
                elif captured in "xX":
                    self.unknown_captured["w" if captured == "X" else "b"] += 1
            elif captured in "xX":
                # 明子吃暗子时唯一的后缀字符是被吃暗子的类型。
                if suffix and suffix[0].upper() in DARK_PIECES:
                    captured_type = suffix[0]
                    self.captured[f"{'w' if captured == 'X' else 'b'}{captured_type.upper()}"] += 1
                    if self.pool.get(captured_type, 0) > 0:
                        self.pool[captured_type] -= 1
                else:
                    self.unknown_captured["w" if captured == "X" else "b"] += 1
            self.side = "b" if self.side == "w" else "w"
        self.tail = replace_pool_in_tail(self.pool, self.base_tail)

    @staticmethod
    def is_side_piece(piece: str, side: str) -> bool:
        """判断棋盘上的棋子是否属于当前行棋方；X/x 分别代表红/黑暗子。"""
        return piece != "." and (piece.isupper() if side == "w" else piece.islower())

    def sync(self):
        if self.engine and not self.position_pending and self.ban_probe_stage not in ("baseline", "banned", "cancelled"):
            self.engine.send(self.position_command())

    def clear_analysis(self):
        """清掉旧局面的分析结果，避免旧箭头短暂残留在新局面上。"""
        self.pv = []; self.recommend = ""; self.analysis_depth = 0
        self.analysis_generation += 1
        self.analysis_snapshot = AnalysisSnapshot(self.analysis_generation)
        self.score.set("层数：— | 评分：— | 棋谱：—")
        self._clear_analysis_views()
        self.draw()

    def _clear_analysis_views(self):
        if hasattr(self, "multipv_view"):
            for item in self.multipv_view.get_children():
                self.multipv_view.delete(item)
        if hasattr(self, "identity_view"):
            for item in self.identity_view.get_children():
                self.identity_view.delete(item)

    def _queue_analysis_snapshot(self, snapshot: AnalysisSnapshot):
        """只保留最新快照，避免 stdout 高频输出拖垮 Tk 主线程。"""
        if snapshot.generation != self.analysis_generation:
            return
        self.analysis_snapshot = snapshot
        if self.analysis_ui_timer is None:
            self.analysis_ui_timer = self.after(80, self._apply_analysis_snapshot)

    def _apply_analysis_snapshot(self):
        self.analysis_ui_timer = None
        snapshot = self.analysis_snapshot
        if snapshot.generation != self.analysis_generation:
            return
        self.analysis_depth = snapshot.depth
        self.pv = list(snapshot.pv)
        self.recommend = snapshot.recommend
        score = "—" if snapshot.score is None else f"{snapshot.score_kind} {snapshot.score}"
        pv = " ".join(snapshot.pv) if snapshot.pv else "—"
        self.score.set(f"层数：{snapshot.depth or '—'} | 评分：{score} | 棋谱：{pv}")
        self._refresh_analysis_views(snapshot)
        self.draw()

    def _refresh_analysis_views(self, snapshot: AnalysisSnapshot):
        self._clear_analysis_views()
        for rank, pv, score, kind in snapshot.multipv:
            move = pv[0] if pv else "—"
            value = "—" if score is None else f"{kind} {score}"
            self.multipv_view.insert("", "end", values=(rank, move, value))
        # Keep a fixed six-row view so a partial JQ message never implies that
        # the missing identities were searched and scored as zero.
        identity = next((item for item in snapshot.identities if item.move == snapshot.recommend), None)
        labels = {piece: LABEL[piece] for piece in DARK_PIECES}
        if not self.identity_enabled:
            self.identity_view.insert("", "end", values=("—", "设置中未启用", "—"))
            return
        if identity is None:
            self.identity_view.insert("", "end", values=("—", "暂无 JQ 数据", "—"))
            return
        for piece in DARK_PIECES:
            value = identity.identities.get(piece)
            if isinstance(value, dict):
                score = value.get("score", "—")
                count = value.get("count", "—")
            else:
                score, count = value if value is not None else "—", "—"
            self.identity_view.insert("", "end", values=(labels[piece], score, count))

    def defer_analysis_restart(self):
        """stop 是异步的，必须等旧搜索输出 bestmove 后才能重新 go。"""
        if self.ban_mode == "probing":
            self._cancel_ban_probe()
        self.analysis_requested = True
        active_search = self.analyzing or self.waiting_for_stop or self.ban_probe_stage in ("baseline", "banned", "cancelled")
        if not active_search:
            # There is no search that can produce a bestmove (for example an
            # outstanding isready during capability probing).  Do not enter a
            # state that waits forever for a terminator which cannot arrive.
            self.pending_analysis = False
            self.waiting_for_stop = False
            self.position_pending = False
            self.restart_waiting_ready = False
            if not self.engine.ready:
                self.pending_analysis = True
            if self.analysis_timer is not None:
                self.after_cancel(self.analysis_timer)
            self.analysis_timer = self.after(250, self._restart_after_position_change)
            self.status.set("正在合并局面变化…")
            return
        self.pending_analysis = True
        self.waiting_for_stop = True
        self.position_pending = True
        self.restart_scheduled = False
        if self.engine: self.engine.send("stop")
        self.analyzing = False
        self.analysis_button.configure(text="开始持续分析")
        self.status.set("正在切换局面，等待旧分析结束…")
        # 不依赖旧搜索是否及时返回 bestmove。引擎命令处理线程会按顺序
        # 完成 stop，再处理后续 position/go；延迟发送可避免与 stop 同时
        # 写入造成竞态。
        if self.analysis_timer is not None:
            self.after_cancel(self.analysis_timer)
        self.analysis_timer = self.after(250, self._restart_after_position_change)

    def _restart_after_position_change(self):
        self.analysis_timer = None
        if not self.analysis_requested or not self.engine or not self.engine.ready:
            return
        # 250ms is only a debounce window.  The old search owns the UCI input
        # loop until its real bestmove; never send a new position while it is
        # still searching, otherwise late info can belong to either position.
        if self.waiting_for_stop or self.position_pending:
            self.status.set("等待旧搜索返回 bestmove…")
            return
        self.position_pending = True
        self.pending_analysis = False
        self.restart_scheduled = True
        # 这里明确发送新局面；position_pending 仍为 True，避免 sync()
        # 因状态保护而跳过 position 命令。
        if self.engine:
            self.engine.send(self.position_command())
        self.clear_analysis()
        self.restart_waiting_ready = True
        self.active_cache_key = self._analysis_cache_key()
        self.engine.send("isready")
        self.restart_scheduled = False
        self.status.set("正在准备新局面分析…")

    def toggle_analysis(self):
        if self.analyzing:
            self.stop_analysis()
        else:
            self.start_analysis()

    def start_analysis(self):
        if not self.engine: self.status.set("引擎未启动"); return
        self.analysis_requested = True
        if not self.engine.ready:
            self.pending_analysis = True
            self.status.set("等待引擎就绪…")
            return
        if self.ban_mode == "probing":
            self.pending_analysis = True
            self.status.set("正在检测引擎能力，请稍候…")
            return
        # 上一次 stop 仍在等待重启回调；本次请求排队，不能提前发送 go。
        if self.waiting_for_stop:
            self.pending_analysis = True
            self.status.set("正在等待上一次分析结束…")
            return
        # 局面切换尚未收到旧搜索的 bestmove 时，绝不能启动第二个搜索。
        if self.position_pending or self.restart_scheduled: return
        self.pending_analysis = False
        self.waiting_for_stop = False
        self.position_pending = False
        self.restart_scheduled = True
        self.clear_analysis(); self.score.set("层数：计算中 | 评分：计算中… | 棋谱：计算中…")
        cached = self.cache.get(self._analysis_cache_key())
        if cached is not None:
            self._queue_analysis_snapshot(replace(cached, generation=self.analysis_generation, complete=False))
        self.active_cache_key = self._analysis_cache_key()
        self.analyzing = self._send_go()
        if not self.analyzing:
            self.restart_scheduled = False
            return
        # 这里只防止同一次 after 回调重复启动；搜索已经启动后必须允许下一次手动分析。
        self.restart_scheduled = False
        self.analysis_button.configure(text="停止分析"); self.status.set("持续分析中（实时加深，不会自动落子）")

    def stop_analysis(self):
        if self.ban_mode == "probing":
            self._cancel_ban_probe()
        was_searching = self.analyzing or self.waiting_for_stop
        if was_searching and self.engine: self.engine.send("stop")
        self.pending_analysis = False
        self.analysis_requested = False
        if self.analysis_timer is not None:
            self.after_cancel(self.analysis_timer); self.analysis_timer = None
        self.restart_waiting_ready = False
        self.waiting_for_stop = was_searching
        self.position_pending = was_searching
        self.restart_scheduled = False
        self.restart_waiting_ready = False
        self.analyzing = False; self.analysis_button.configure(text="开始持续分析")
        self.status.set("分析已停止（推荐走法未执行）")

    def handle_engine_death(self, exit_code: str = "未知"):
        """引擎进程崩溃/退出时自动重启并恢复分析。"""
        now = time.monotonic()
        self.restart_count = self.restart_count + 1 if now - self.last_engine_death < 10 else 1
        self.last_engine_death = now
        was_analyzing = self.analysis_requested or self.analyzing or self.pending_analysis or self.waiting_for_stop
        # 重置所有引擎相关状态
        self.analyzing = False
        self.analysis_requested = was_analyzing
        self.pending_analysis = False
        self.waiting_for_stop = False
        self.position_pending = False
        self.restart_scheduled = False
        self.analysis_button.configure(text="开始持续分析")
        if self.restart_count > 3:
            self.status.set(f"引擎连续退出（代码 {exit_code}），已停止自动重启，请检查引擎日志")
            return
        self.status.set(f"引擎已退出（代码 {exit_code}），正在重启（{self.restart_count}/3）…")
        # 清空队列中的残留消息
        while True:
            try: self.q.get_nowait()
            except queue.Empty: break
        # 重启引擎进程
        if self.engine:
            try: self.engine.proc.terminate()
            except Exception: pass
            self.engine.proc = None
        if os.path.isfile(self.engine_path.get()):
            try:
                self.engine = Engine(self.engine_path.get(), self.q)
                self.engine.start()
                self._send_engine_options()
                # 引擎重启后重新确认扩展能力；禁用列表不随引擎
                # 进程丢失，但能力模式不能盲目沿用。
                self._begin_ban_probe()
                self.status.set("引擎已重启")
                # 等待真实 readyok 后恢复分析，避免固定延迟造成竞态。
                if was_analyzing:
                    self.pending_analysis = True
            except OSError as e:
                self.status.set(f"引擎重启失败：{e}")
        else:
            self.status.set("引擎崩溃，找不到引擎文件，无法重启")

    def _resume_after_restart(self):
        """引擎重启后尝试继续分析。"""
        if self.pending_analysis and self.engine and self.engine.ready:
            self.pending_analysis = False
            self.sync()
            self.start_analysis()

    def toggle_flip(self):
        self.flipped = not self.flipped; self.selected = None; self.draw()

    def choose_piece(self, title: str, prompt: str, allowed: set[str] | None = None,
                     allow_unknown: bool = True) -> str | None:
        """使用按钮选择暗子类型；取消或点窗口关闭时返回 None。"""
        win = tk.Toplevel(self); win.title(title); win.transient(self); win.grab_set(); win.resizable(False, False)
        result: list[str | None] = [None]
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        ttk.Label(win, text=prompt).pack(padx=14, pady=(12, 7))
        panel = ttk.Frame(win); panel.pack(padx=10)
        for piece in PIECES:
            if piece not in DARK_PIECES or (allowed is not None and piece not in allowed):
                continue
            ttk.Button(panel, text=f"{LABEL[piece]} ({piece})", width=9,
                       command=lambda p=piece: (result.__setitem__(0, p), win.destroy())).pack(side="left", padx=2)
        buttons = ttk.Frame(win); buttons.pack(pady=10)
        if allow_unknown:
            ttk.Button(buttons, text="未知（不输入）", command=lambda: (result.__setitem__(0, ""), win.destroy())).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="left", padx=4)
        self.wait_window(win)
        return result[0]

    def _poll(self):
        """批量处理引擎事件；分析结果只通过快照节流刷新 Tk。"""
        for _ in range(512):
            try:
                line = self.q.get_nowait().strip()
            except queue.Empty:
                break
            event = parse_engine_line(line)
            if event.kind == "died":
                self.handle_engine_death(event.value or "未知")
                continue
            if event.kind == "ready":
                if self.engine:
                    self.engine.ready = True
                if self.ban_probe_stage == "waiting_ready":
                    self._send_ban_probe_baseline()
                elif self.restart_waiting_ready and self.analysis_requested:
                    self.restart_waiting_ready = False
                    self.position_pending = False
                    self.restart_scheduled = False
                    self.clear_analysis()
                    self.analyzing = self._send_go()
                    if self.analyzing:
                        self.analysis_button.configure(text="停止分析")
                        self.status.set("持续分析中（实时加深，不会自动落子）")
                elif self.pending_analysis and not self.waiting_for_stop:
                    self.after(0, self._resume_after_restart)
                continue
            if self._handle_ban_probe_line(line):
                continue
            # A changed position invalidates all info until the old search has
            # acknowledged stop with its real bestmove.
            if self.position_pending and event.kind != "bestmove":
                continue
            if event.kind == "info" and event.info is not None:
                snapshot = self.analysis_snapshot.merge_info(event.info)
                self._queue_analysis_snapshot(snapshot)
                if self.ban_mode == "banmoves" and snapshot.recommend in self.banned_moves:
                    self._switch_to_searchmoves("banmoves 未生效，已切换到 searchmoves")
            elif event.kind == "jq" and event.jq is not None:
                self._queue_analysis_snapshot(self.analysis_snapshot.merge_jq(event.jq))
            elif event.kind == "bestmove":
                if self.active_cache_key is not None and self.analysis_snapshot.depth:
                    completed = replace(self.analysis_snapshot, complete=True)
                    self.cache.put(self.active_cache_key, completed)
                    self.active_cache_key = None
                if not self.position_pending:
                    move = event.value
                    if move == "(none)" or not move:
                        self.clear_analysis()
                        self.analyzing = False
                        self.analysis_requested = False
                        self.pending_analysis = False
                        self.analysis_button.configure(text="开始持续分析")
                        self.status.set("当前局面没有可用推荐走法")
                        continue
                    if self.ban_mode == "banmoves" and move in self.banned_moves:
                        self._switch_to_searchmoves("banmoves 未生效，已切换到 searchmoves")
                        continue
                    if not self.recommend:
                        self._queue_analysis_snapshot(
                            self.analysis_snapshot.merge_info(InfoLine(pv=(move,))))
                    if not self.analysis_requested:
                        completed = replace(self.analysis_snapshot, complete=True)
                        self._queue_analysis_snapshot(completed)
                        self.cache.put(self._analysis_cache_key(), completed)
                if self.pending_analysis:
                    self.pending_analysis = False
                    self.waiting_for_stop = False
                    self.position_pending = False
                    self.restart_scheduled = False
                    if self.analysis_timer is None and self.analysis_requested:
                        self.after(0, self._restart_after_position_change)
                elif not self.analyzing:
                    self.waiting_for_stop = False
                    self.position_pending = False
                    self.status.set("推荐完成（未执行）")
        self.after(50, self._poll)

    def settings(self):
        win = tk.Toplevel(self); win.title("引擎参数"); win.transient(self); win.grab_set()
        ttk.Label(win, text="引擎文件").grid(row=0, column=0, sticky="w", padx=8, pady=8); ttk.Entry(win, textvariable=self.engine_path, width=55).grid(row=0, column=1)
        ttk.Button(win, text="浏览", command=lambda: self.engine_path.set(filedialog.askopenfilename(filetypes=[("Executable", "*.exe"), ("All", "*")]))).grid(row=0, column=2)
        vars_ = {"Threads": tk.IntVar(value=self.config.threads), "Hash": tk.IntVar(value=self.config.hash_mb), "MultiPV": tk.IntVar(value=self.config.multipv)}
        dark_mode = tk.StringVar(value=self.config.dark_mode)
        show_identity = tk.BooleanVar(value=self.identity_enabled)
        for i, (name, var) in enumerate(vars_.items(), 1):
            ttk.Label(win, text=name).grid(row=i, column=0, sticky="w", padx=8); ttk.Spinbox(win, from_=1, to=1024, textvariable=var, width=8).grid(row=i, column=1, sticky="w")
        ttk.Label(win, text="暗子搜索模式").grid(row=4, column=0, sticky="w", padx=8)
        ttk.Combobox(win, textvariable=dark_mode, values=("Expected", "Worst"), state="readonly", width=10).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Checkbutton(win, text="启用六种暗子身份 JQ 输出", variable=show_identity).grid(row=5, column=1, sticky="w", pady=3)
        def apply():
            try:
                self.config.engine_path = self.engine_path.get()
                self.config.threads = int(vars_["Threads"].get())
                self.config.hash_mb = int(vars_["Hash"].get())
                self.config.multipv = int(vars_["MultiPV"].get())
                self.config.dark_mode = dark_mode.get()
                self.config.show_identity = bool(show_identity.get())
                self.config.start_depth = int(self.depth.get())
                self.identity_enabled = self.config.show_identity
                self.config.save(self.config_file)
            except (TypeError, ValueError, OSError) as exc:
                messagebox.showerror("设置错误", f"无法保存设置：{exc}", parent=win)
                return
            if self.engine:
                # Switching options while `go infinite` is running must stop the
                # current search first; the engine's UCI loop is single-threaded.
                if self.analysis_requested:
                    self.defer_analysis_restart()
                self._send_engine_options()
            self._refresh_analysis_views(self.analysis_snapshot)
            win.destroy()
        ttk.Button(win, text="应用", command=apply).grid(row=6, column=1, pady=10)

    def refresh(self, status=None):
        self.turn.set("当前行棋：红方" if self.side == "w" else "当前行棋：黑方")
        pool_summary = " ".join(f"{LABEL[piece]}{self.pool[piece]}" for piece in DARK_PIECES)
        pool_summary += " | " + " ".join(f"{LABEL[piece]}{self.pool[piece.lower()]}" for piece in DARK_PIECES)
        self.pool_text.set(f"剩余暗子池（红 | 黑）：{pool_summary}")
        captured_lines = []
        for color, title in (("w", "红方被吃"), ("b", "黑方被吃")):
            known = " ".join(f"{LABEL[piece]}{self.captured[f'{color}{piece}']}" for piece in DARK_PIECES if self.captured[f"{color}{piece}"])
            unknown = self.unknown_captured[color]
            captured_lines.append(f"{title}：{known or '无'}" + (f"；未知 {unknown}" if unknown else ""))
        self.captured_text.set("\n".join(captured_lines))
        self.draw(); self.history.delete(0, tk.END)
        for i, m in enumerate(self.moves, 1): self.history.insert(tk.END, f"{i}. {m}")
        if status: self.status.set(status)

    def draw(self):
        if not hasattr(self, "canvas"): return
        self.canvas.delete("all"); cell = min(self.canvas.winfo_width() / 10, self.canvas.winfo_height() / 11); ox, oy = cell, cell
        for i in range(9): self.canvas.create_line(ox + i*cell, oy, ox + i*cell, oy + 9*cell, fill="#633d1d"); self.canvas.create_line(ox, oy + i*cell, ox + 8*cell, oy + i*cell, fill="#633d1d")
        self.canvas.create_line(ox, oy + 9*cell, ox + 8*cell, oy + 9*cell, fill="#633d1d")
        for r in range(10):
            for c in range(9):
                p = self.board[r][c]
                if p == ".": continue
                vc, vr = self.view_square(c, r); x, y = ox+vc*cell, oy+vr*cell; fill = "#d94b3d" if p.isupper() else "#252525"; text = "暗" if p in "xX" else LABEL.get(p.upper(), p.upper())
                self.canvas.create_oval(x-cell*.38, y-cell*.38, x+cell*.38, y+cell*.38, fill="#f3d29b", outline=fill, width=2)
                self.canvas.create_text(x, y, text=text, fill=fill, font=("SimSun", max(12, int(cell*.30)), "bold"))
        # 只绘制当前行棋方的第一推荐着法；PV 后续着法保留但不绘制。
        item = self.pv[0] if self.pv else ""
        if len(item) >= 4 and re.match(r"^[a-i][0-9][a-i][0-9]", item):
            try: fc, fr = parse_sq(item[:2]); tc, tr = parse_sq(item[2:4])
            except (ValueError, IndexError): pass
            else:
                # 只接受当前局面的当前行棋方推荐，避免旧搜索结果覆盖
                # 换手后的推荐箭头。
                if self.is_side_piece(self.board[fr][fc], self.side):
                    vfc, vfr = self.view_square(fc, fr); vtc, vtr = self.view_square(tc, tr)
                    self.canvas.create_line(ox+vfc*cell, oy+vfr*cell, ox+vtc*cell, oy+vtr*cell, fill="#168b45", width=max(2, int(cell*.07)), arrow=tk.LAST)
        if self.selected:
            c, r = self.selected; vc, vr = self.view_square(c, r); x, y = ox+vc*cell, oy+vr*cell
            self.canvas.create_oval(x-cell*.44, y-cell*.44, x+cell*.44, y+cell*.44, outline="#1683d8", width=3)

    def view_square(self, col: int, row: int) -> tuple[int, int]:
        return (8 - col, 9 - row) if self.flipped else (col, row)

    def destroy(self):
        if self.analyzing and self.engine: self.engine.send("stop")
        if self.engine: self.engine.close()
        super().destroy()


if __name__ == "__main__":
    App().mainloop()