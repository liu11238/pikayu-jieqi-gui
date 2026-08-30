# PikaJieQi

[English](README.md) | [简体中文](README_zh.md)

PikaJieQi 是一个面向 Windows 的**揭棋（暗棋）局面分析工具集**，由定制版 Pikafish UCI 引擎和轻量级 Tkinter 图形界面组成。

本项目定位为分析辅助工具：引擎负责推荐走法，GUI 负责记录用户实际走法和翻开的棋子；程序不会自动替任一方落子。

## 项目结构

| 路径 | 说明 |
| --- | --- |
| [`Pikafish-jieqi-old/`](Pikafish-jieqi-old/) | 定制版 Pikafish 源码和 Windows 引擎构建目录。通用 Pikafish 及 GPL 信息请参考其[上游引擎说明](Pikafish-jieqi-old/README.md)。 |
| [`PikaJieQi-GUI/`](PikaJieQi-GUI/) | 用 Python/Tkinter 编写的揭棋局面编辑和分析 GUI。详细功能请参考 [GUI 说明](PikaJieQi-GUI/README.md)。 |
| [`Pikafish-jieqi-old/src/PikaJieQi.exe`](Pikafish-jieqi-old/src/PikaJieQi.exe) | 已编译的 Windows UCI 引擎，GUI 默认使用此文件。 |

## Windows 快速开始

### 启动 GUI

1. 安装 Python 3.9 或更高版本。需要 Tkinter；标准 Windows Python 通常已经包含它。
2. 双击 [`PikaJieQi-GUI/启动GUI.bat`](PikaJieQi-GUI/启动GUI.bat)。
3. 如果 GUI 找不到引擎，在“设置引擎”中选择：

   ```text
   Pikafish-jieqi-old\src\PikaJieQi.exe
   ```

启动脚本在环境存在时会尝试激活 `autogui` Conda 环境，然后执行 `python app.py`。也可以手动启动：

```bat
conda activate autogui
cd /d D:\code\git\pikayu\PikaJieQi-GUI
python app.py
```

不使用 Conda 时，只要当前 Python 3.9+ 安装包含 Tkinter，直接执行 `python app.py` 即可。

### 直接通过 UCI 使用引擎

`PikaJieQi.exe` 是一个 UCI 引擎，可以由兼容的 GUI 或终端调用。下面是一个基本冒烟测试：

```text
uci
isready
position fen xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
go depth 3
quit
```

## 从源码构建引擎

仓库已经包含编译好的 `PikaJieQi.exe`，一般不需要自行编译。

引擎源码位于 `Pikafish-jieqi-old/src/`，使用 MinGW-w64 构建。典型的 64 位 AVX-VNNI 构建命令如下：

```bash
cd /d/code/git/pikayu/Pikafish-jieqi-old/src
mingw32-make -j2 build ARCH=x86-64-avxvnni CXX=g++
```

开发环境中 `make` 不在 `PATH`，而且默认的 `g++` 指向无法启动 `cc1plus` 的损坏工具链；最终生成仓库中 EXE 的可靠命令为：

```bash
cd /d/code/git/pikayu/Pikafish-jieqi-old/src
/d/mingw64/bin/mingw32-make.exe -j2 build ARCH=x86-64-avxvnni CXX=/d/mingw64/bin/g++
```

`x86-64-avxvnni` 要求 CPU 支持对应指令集。如果需要在较旧的机器上运行，可以选择 Makefile 支持的其他架构，例如 `x86-64-avx2` 或基础版 `x86-64`：

```bash
/d/mingw64/bin/mingw32-make.exe -j2 build ARCH=x86-64 CXX=/d/mingw64/bin/g++
```

清理构建产物：

```bash
/d/mingw64/bin/mingw32-make.exe clean CXX=/d/mingw64/bin/g++
```

请根据目标 Windows 机器选择编译器和架构。没有 AVX-VNNI 支持的 CPU 不能运行 AVX-VNNI 版本的引擎。

## 揭棋 FEN 格式

引擎接受类似 FEN 的格式，棋盘为 10 行、每行 9 列。棋盘部分使用普通棋子字母，并使用以下暗子标记：

- `x` 表示黑方暗子位置。
- `X` 表示红方暗子位置。
- 行棋方字段使用 `w` 表示红方/先手，使用 `b` 表示黑方/后手。
- 行棋方之后的字段表示双方剩余暗子池。大写字母表示红方棋子，小写字母表示黑方棋子；数字表示重复前一个棋种。

GUI 默认局面为：

```text
xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX w R2A2C2P5N2B2r2a2c2p5n2b2 0 1
```

暗子翻开前，其合法走法几何由该暗位在初始棋盘上的标准棋种决定；实际棋种未知，翻开时从对应一方的剩余暗子池中确定。GUI 支持输入 `x`/`X`，在吃子或移动暗子后选择翻开的棋种，也支持直接编辑完整 FEN。

## 引擎专用 UCI 扩展

### `DarkSearchMode`

引擎新增以下选项：

```text
setoption name DarkSearchMode value Expected
```

可选值如下：

- **`Expected`**（默认）：按照剩余暗子池中的棋种及数量聚合可能结果，计算期望评分。历史行为仍以先手/红方作为评分归一化锚点，因此改变搜索根方不会改变 Expected 的语义。
- **`Worst`**：从搜索根方视角进行搜索。根方固定为本次搜索开始时局面的行棋方，不会因递归搜索中的走子而改变。若暗子属于根方，则对所有可能翻法取最小值；若暗子属于对手，则在行棋方视角取最大值，这代表对手的最佳结果，也就是根方的最差情况。每个可能的暗子身份都会作为独立分支搜索，再统一进行最差结果聚合。

切换该选项时会自动清空置换表，因为两种聚合模型对同一局面可能产生不同评分。

### `banmoves`

引擎支持在 `position` 和 `go` 之间发送 `banmoves` 命令，临时排除根节点走法：

```text
position fen <jieqi-fen>
banmoves a0a1 b2b3
go depth 12
```

GUI 启动引擎时会自动探测该扩展。如果扩展不受支持或被兼容引擎忽略，GUI 会回退到标准 UCI `searchmoves` 白名单方式。

### 其他揭棋行为

- 引擎输出的走法信息可以携带翻开后的真实棋种，供 GUI 更新局面。
- 评分以当前行棋方为视角；适用时使用 UCI 的 `cp`（厘兵）单位。
- GUI 不会自动执行推荐走法，用户需要自行记录实际走法和翻开棋种。

## GUI 功能

当前 GUI 支持：

- 重置到揭棋初始局面；
- 通过“编辑局面”输入完整 FEN；
- 记录双方合法走法并跟踪当前行棋方；
- 使用 `go infinite` 持续分析；
- 显示搜索层数、评分、PV，并在棋盘上显示第一步箭头；
- 通过“强制变招”临时禁用当前推荐走法，并用“清除禁用”恢复选择；
- 撤销、重做、棋盘翻转、搜索深度、Threads、Hash、MultiPV 和引擎路径设置；
- 在“设置引擎”中选择暗子搜索模式：`Expected`（默认）或 `Worst`；
- 吃子或移动暗子后选择翻开棋种；
- 局面变化后自动重新启动分析。

详细 GUI 说明请参考 [`PikaJieQi-GUI/README.md`](PikaJieQi-GUI/README.md)。

## 说明与限制

- 本仓库是 Pikafish/Stockfish 的揭棋专用分支和定制版本，并非上游 Pikafish 项目本身。
- 当前构建使用经典评估路径（`USE_NNUEEVAL 0`），不需要 `pikafish.nnue` 网络文件。
- 暗子的真实棋种只有在用户输入，或由引擎揭棋走法信息提供时才是已知信息；程序不会从棋局服务器自动推断暗子。
- `Worst` 用于从根方角度对不确定性较高的局面进行保守分析，包括暗兵可能翻为炮并形成双炮攻杀的分支。具体战术结论仍取决于输入 FEN、剩余棋子池、搜索深度和引擎设置。
- 重新编译 EXE 后，请关闭并重新启动 GUI，避免 GUI 继续使用已经加载的旧引擎进程。

## 许可证

引擎源自 Pikafish/Stockfish 代码，继续遵循 GNU General Public License v3。适用的许可和署名信息请参见 [`Pikafish-jieqi-old/Copying.txt`](Pikafish-jieqi-old/Copying.txt)、[`Pikafish-jieqi-old/NNUE-License.txt`](Pikafish-jieqi-old/NNUE-License.txt) 和 [`Pikafish-jieqi-old/AUTHORS`](Pikafish-jieqi-old/AUTHORS)。