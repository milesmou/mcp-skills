---
name: toolbox
description: 命令行工具（CLI）的统一入口。凡是查找、确认是否已安装、下载、安装、升级、运行或定位任何外部命令行工具，都必须先使用本技能。覆盖 rg/ripgrep、fd、jq、yq、bat、fzf、hyperfine、shellcheck、protoc、gitleaks、apktool、jadx、adb、ffmpeg、curl 等，以及用户说「命令找不到 / command not found / 不是内部或外部命令 / 缺个工具 / 帮我装一下 / 本机有没有 / 用某个命令 / 构建需要某工具 / install or download a CLI / run a tool / which or where」的场景。规则：先 toolbox list/find/path，缺失再 ensure/get；不要先扫 PATH、项目目录或往临时目录下载。
---

# Toolbox — 工程工具共享目录

把所有独立命令行工具集中到**同一个共享目录**：用时先查找，缺失再下载。多个工程复用同一份可执行文件，不再各自往临时目录或 `node_modules` 里放一份。

## 触发条件（命中任一，必须先加载并查询工具箱）

- 用户提到**任何外部命令行工具/命令名**，或问它有没有装、版本多少、在哪、怎么用。
- 出现 `command not found`、`不是内部或外部命令`、`找不到命令`、`PATH` 里没有等报错或说法。
- 需要下载、安装、升级、卸载工具，或往项目里引入新的 CLI。
- 构建、打包、测试、反编译等流程需要 `apktool`、`jadx`、`rg`、`jq`、`protoc` 这类工具。
- 你准备直接在 PATH、项目目录、临时目录里找工具或下载工具。

固定执行顺序：`list` → `find` → `path`（已有）→ `ensure` / `get`（缺失）→ 最后才考虑其它来源。**不要因为“可能本机已经装了”就跳过工具箱检查。**

不适用：只是修改业务代码、完全不涉及外部命令时，不必加载。

## 核心规则

1. 所有工具只装到共享目录 `TOOLBOX_HOME`：
   - **Windows（默认）**：优先使用 `D:\toolbox`（仅当 D 盘存在且为本地固定盘时）；D 盘不可用则取最后一个本地固定盘的 `<盘符>:\toolbox`。换电脑后按此规则自动解析。
   - macOS / Linux：`~/.local/share/toolbox`
   - 可用环境变量 `TOOLBOX_HOME` 或命令行 `--home` 覆盖；设成 `TOOLBOX_HOME=auto` 可强制按当前机器自动解析（同样优先 D 盘）。
2. **绝不要**把工具下载或解压到工程目录、`node_modules`、`.venv`、`tmp`、`%TEMP%` 等工程/临时目录。
3. 用工具前先 `list` / `find` / `path`；只有缺失时才 `ensure` / `get`。
4. 项目里只保留一份工具清单（如 `.toolbox`），**不放二进制**；二进制统一在共享目录。

## 目录结构

```text
<TOOLBOX_HOME>/
  <工具名>/      # 每个工具一个目录；单文件工具也单独建目录放进去
  index.json     # 索引：名称、版本、路径、sha256、来源
  recipes.json   # 可选：用户自定义 recipe，覆盖内置
```

不生成 `bin` 启动器；`path` 只返回可执行文件绝对路径，由调用方直接执行。

允许下载到共享目录的 `downloads/`，但**每次安装结束（成功或失败）必须立即清除**，正常情况下不会残留；进程中断留下的残留在下次 `gc` 时清空。

## 查找顺序

`list` / `find` / `path` 解析工具时按以下顺序：

1. `index.json` 中的登记；
2. 扫描 `<TOOLBOX_HOME>/<工具名>/` 下的实际文件（**索引缺失或损坏也能找到**；兼容旧 `store/` 布局）；
3. `<TOOLBOX_HOME>` 下直接投放的 `<名称>[.exe]`。

目录里有、`index.json` 没有的工具会显示为「未登记」；`toolbox repair` 可把它收编进索引，`gc` 也不会误删这类目录。

手动放入的工具（如 `D:\toolbox\jadx-1.5.6\`、`D:\toolbox\apktool\`）只要目录名与可执行文件/启动器对得上，也会被扫描识别；识别不了的复杂 SDK 目录会原样保留，不会删除。

## 标准流程

脚本路径：`<技能根目录>/scripts/toolbox.py`。以下用 `$TB` 代指：

```powershell
$TB = "<技能根目录>/scripts/toolbox.py"
python -X utf8 $TB list
```

### 1. 先查共享目录

```text
python -X utf8 <TB> list                 # 已安装了哪些、在哪个路径、占多大
python -X utf8 <TB> find rg              # 按名字查已安装或可下载的工具
python -X utf8 <TB> path ripgrep         # 直接拿到可执行文件绝对路径
```

### 2. 缺失再下载

```text
python -X utf8 <TB> ensure ripgrep       # 用内置 recipe 下载最新版到共享目录
python -X utf8 <TB> ensure fd@10.5.1     # 固定版本
python -X utf8 <TB> ensure rg jq fd      # 一次装多个
```

没有内置 recipe 的工具，用直链或 GitHub Release 安装：

```text
python -X utf8 <TB> get https://example.com/tool.zip --name mytool --exe mytool.exe
python -X utf8 <TB> get github:owner/repo@v1.2.3 --asset "tool-*-windows-amd64.zip" --name mytool
python -X utf8 <TB> get github:jqlang/jq --name jq     # 省略 --asset 时按当前平台自动匹配
```

### 3. 使用工具（只返回路径）

```text
python -X utf8 <TB> path ripgrep                 # 返回可执行文件绝对路径
```

拿到路径后由当前 shell 直接执行，例如：

```powershell
& "D:\toolbox\ripgrep\rg.exe" --version
```

`.bat` / `.cmd` 在 PowerShell 里同样用 `&` 调用；不生成 `bin`，也不提供执行封装。

### 4. 项目清单同步（推荐）

在项目根放一个 `.toolbox`（纯文本，一行一个工具，`#` 注释）或 `.toolbox.json`，然后：

```text
python -X utf8 <TB> sync                 # 读取 ./.toolbox 或 ./.toolbox.json
python -X utf8 <TB> sync --file tools.txt
```

`.toolbox` 示例：

```text
# 本项目需要的命令行工具
ripgrep
jq
protoc@36.2
```

`.toolbox.json` 示例：

```json
{
  "tools": ["ripgrep", "jq", "protoc@36.2", {"name": "fd", "version": "10.5.1"}]
}
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `list` | 列出已安装工具、版本、平台、大小、状态 |
| `find [关键字]` | 查找已安装或 recipe 中可下载的工具 |
| `ensure <名称>[@版本]...` | 按 recipe 确保已安装（缺失才下载） |
| `sync [--file <清单>]` | 按项目清单安装所有缺失工具 |
| `get <url\|github:owner/repo[@tag]>` | 安装列表之外的任意工具 |
| `path <名称>` | 打印可执行文件绝对路径（唯一使用入口） |
| `remove <名称> [--version] [--all]` | 移除工具 |
| `repair` | 整目录搬迁后校验索引，并收编目录中的工具 |
| `gc [--prune-other-platforms]` | 清空下载临时区、清理旧平台版本；不删除顶层工具目录 |
| `doctor` | 校验索引路径与可执行文件哈希 |
| `where` | 显示共享目录位置与解析来源 |

所有命令支持 `--json`（放在子命令前，如 `toolbox --json list`）便于程序解析。

## 内置 recipe

`ripgrep`、`fd`、`jq`、`yq`、`bat`、`fzf`、`hyperfine`、`shellcheck`、`protoc`、`gitleaks`。

需要新增常用工具时，在 `$TOOLBOX_HOME/recipes.json` 写同结构条目即可覆盖或扩展内置 recipe，格式见 [命令参考](references/commands.md)。

## 换电脑 / 迁移

索引与工具路径都相对共享目录记录，因此**整个共享目录可以直接搬走**。

- **同一系统架构（推荐）**：把整个 `toolbox` 目录复制到新电脑的 `D:\toolbox`（或该机最后一个固定盘根目录，即 `<盘符>:\toolbox`），跑一次 `repair` 校验即可。路径不必和旧机器相同。
- **只带项目**：不要复制二进制，只把项目里的 `.toolbox` 清单带到新电脑，运行 `toolbox sync` 重新下载当前平台版本；这最干净，也避免跨系统不兼容。
- **换了系统或 CPU 架构**（如 Windows x64 → macOS/arm64）：旧平台二进制不能运行，`sync` 会按新平台下载；旧平台的残留用 `toolbox gc --prune-other-platforms` 清掉。
- 环境变量：`TOOLBOX_HOME` 一般留空或设为 `auto`，Windows 上会优先定位 `D:\toolbox`（D 盘为固定盘时），否则退回最后一个固定盘。

## 维护与排障

```text
python -X utf8 <TB> doctor        # 哈希/路径异常时先跑这个
python -X utf8 <TB> gc            # 空间不足或清理旧版本时
python -X utf8 <TB> remove fd --all
```

- GitHub API 限流时设置 `GITHUB_TOKEN`（或 `GH_TOKEN`）后重试。
- 脚本只用 Python 3.8+ 标准库，无需安装依赖；Windows 也可用 `py -3` 启动。
- 共享目录**不要**放进任何工程的 git 仓库。
- 安装归档损坏或 `doctor` 报哈希不符时：`ensure <名称> --force` 重新下载。

详细参数、清单格式与自定义 recipe 见 [命令参考](references/commands.md)。
