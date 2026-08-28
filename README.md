# PikaJieQi

[English](README.md) | [简体中文](README_zh.md)

PikaJieQi is a Windows-oriented toolkit for analyzing **Jieqi (暗棋)** positions. It combines a modified Pikafish UCI engine with a lightweight Tkinter graphical interface.

The project is intended as an analysis assistant: the engine recommends moves, while the GUI lets the user record the actual moves and revealed pieces. It does not automatically play either side.

## Repository layout

| Path | Description |
| --- | --- |
| [`Pikafish-jieqi-old/`](Pikafish-jieqi-old/) | Modified Pikafish source tree and Windows engine build. See its [upstream engine README](Pikafish-jieqi-old/README.md) for general Pikafish and GPL information. |
| [`PikaJieQi-GUI/`](PikaJieQi-GUI/) | Python/Tkinter GUI for editing and analyzing Jieqi positions. See the [GUI README](PikaJieQi-GUI/README.md) for the detailed feature list. |
| [`Pikafish-jieqi-old/src/PikaJieQi.exe`](Pikafish-jieqi-old/src/PikaJieQi.exe) | Prebuilt Windows UCI engine used by the GUI by default. |

## Quick start on Windows

### GUI

1. Make sure Python 3.9 or later is installed. Tkinter is required; it is included in the standard Windows Python distribution.
2. Double-click [`PikaJieQi-GUI/启动GUI.bat`](PikaJieQi-GUI/启动GUI.bat).
3. If the GUI cannot find the engine, use **设置引擎** and select:

   ```text
   Pikafish-jieqi-old\src\PikaJieQi.exe
   ```

The launcher tries to activate the `autogui` Conda environment when it is available, then runs `python app.py`. The GUI can also be started manually:

```bat
conda activate autogui
cd /d D:\code\git\pikayu\PikaJieQi-GUI
python app.py
```

If Conda is not used, run `python app.py` with any Python 3.9+ installation that provides Tkinter.

### Engine directly through UCI

`PikaJieQi.exe` is a UCI engine and can be used from a compatible GUI or a terminal. For a basic smoke test:

```text
uci
isready
position fen xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
go depth 3
quit
```

## Building the engine from source

The repository includes a prebuilt `PikaJieQi.exe`, so building from source is optional.

The engine is built from `Pikafish-jieqi-old/src/` with MinGW-w64. A typical 64-bit AVX-VNNI build is:

```bash
cd /d/code/git/pikayu/Pikafish-jieqi-old/src
mingw32-make -j2 build ARCH=x86-64-avxvnni CXX=g++
```

On the development machine, `make` was not available on `PATH` and the default `g++` command referred to a broken toolchain. The reliable command used to produce the checked-in executable was:

```bash
cd /d/code/git/pikayu/Pikafish-jieqi-old/src
/d/mingw64/bin/mingw32-make.exe -j2 build ARCH=x86-64-avxvnni CXX=/d/mingw64/bin/g++
```

`x86-64-avxvnni` requires a compatible CPU. Select another architecture supported by the Makefile, such as `x86-64-avx2` or the baseline `x86-64`, when distributing the engine to older machines:

```bash
/d/mingw64/bin/mingw32-make.exe -j2 build ARCH=x86-64 CXX=/d/mingw64/bin/g++
```

To remove build artifacts:

```bash
/d/mingw64/bin/mingw32-make.exe clean CXX=/d/mingw64/bin/g++
```

The exact compiler and architecture should be chosen according to the target Windows machine. Do not use AVX-VNNI binaries on CPUs that do not support those instructions.

## Jieqi FEN format

The engine accepts a FEN-like format with a 10-rank by 9-file board. The board section uses the normal piece letters plus the following hidden-piece markers:

- `x` is a hidden black-side piece square.
- `X` is a hidden red-side piece square.
- The side-to-move field uses `w` for red/first side and `b` for black/second side.
- The field after the side to move describes the remaining hidden-piece pools. Uppercase letters are red pieces and lowercase letters are black pieces; a number repeats the preceding piece type.

The default GUI position is:

```text
xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
```

Before a hidden piece is revealed, its legal movement geometry is determined by the standard piece type of that starting square. Its actual identity is selected from the corresponding side's remaining hidden-piece pool when it is revealed. The GUI supports entering `x`/`X`, choosing a revealed piece after a capture or hidden-piece move, and editing the complete FEN directly.

## Engine-specific UCI extensions

### `DarkSearchMode`

The engine adds the option:

```text
setoption name DarkSearchMode value Expected
```

Available values are:

- **`Expected`** (default): aggregates possible hidden-piece identities according to the remaining-piece pool and evaluates their expected score. Its historical score normalization remains anchored to the first side/red side, so changing the search root side does not change Expected semantics.
- **`Worst`**: searches from the root side's perspective. If a hidden piece belongs to the root side, the possible reveal results are minimized; if it belongs to the opponent, the possible results are maximized from the moving side's perspective, representing the opponent's best result and the root side's worst case.

Changing this option clears the transposition table because the two aggregation models produce different scores for the same position.

### `banmoves`

The engine supports a `banmoves` command between `position` and `go` to temporarily exclude root moves:

```text
position fen <jieqi-fen>
banmoves a0a1 b2b3
go depth 12
```

The GUI probes this extension when the engine starts. If the extension is unsupported or is ignored by a compatible engine, the GUI falls back to the standard UCI `searchmoves` whitelist.

### Other Jieqi behavior

- Revealed-piece information can be carried in engine move output for the GUI.
- Scores are displayed from the current side-to-move perspective, using UCI `cp` units where applicable.
- The GUI does not apply the recommended move automatically. The user records the actual move and any revealed piece.

## GUI features

The GUI currently supports:

- resetting to the Jieqi starting position;
- entering a complete FEN through **编辑局面**;
- recording legal moves for either side and tracking the side to move;
- continuous analysis with `go infinite`;
- displaying depth, score, PV, and the first move as a board arrow;
- temporarily banning the current recommendation (**强制变招**), with **清除禁用** to restore choices;
- undo, redo, board rotation, search depth, Threads, Hash, MultiPV, and engine-path settings;
- selecting a revealed piece after a capture or hidden-piece move;
- automatic restart of analysis after the position changes.

See [`PikaJieQi-GUI/README.md`](PikaJieQi-GUI/README.md) for the detailed GUI documentation.

## Notes and limitations

- This repository is a Jieqi-specific fork/customization of Pikafish, not the upstream Pikafish project itself.
- The current build uses the classical evaluation path (`USE_NNUEEVAL 0`); no `pikafish.nnue` file is required by this build.
- A hidden piece's actual identity is known only when it is entered by the user or supplied through the engine's Jieqi move information. The program does not infer hidden information from a game server.
- `Worst` is intended for conservative root-side analysis of uncertainty-sensitive positions, including lines where a hidden pawn may reveal as a cannon and create a double-cannon attack. A precise tactical conclusion still depends on the supplied FEN, remaining-piece pools, search depth, and engine settings.
- Close and restart the GUI after rebuilding the executable so that it does not keep an older engine process loaded.

## License

The engine is derived from Pikafish/Stockfish code and remains subject to the GNU General Public License v3. See [`Pikafish-jieqi-old/Copying.txt`](Pikafish-jieqi-old/Copying.txt), [`Pikafish-jieqi-old/NNUE-License.txt`](Pikafish-jieqi-old/NNUE-License.txt), and [`Pikafish-jieqi-old/AUTHORS`](Pikafish-jieqi-old/AUTHORS) for the applicable licensing and attribution information.