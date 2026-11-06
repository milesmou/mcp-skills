# Toolbox 命令参考

`toolbox.py` 是单文件、仅依赖 Python 3.8+ 标准库的命令行工具。默认操作一个共享目录，所有工程复用其中同一份工具。

## 全局选项

| 选项 | 说明 |
| --- | --- |
| `--home <目录>` | 覆盖共享目录；不填则读 `TOOLBOX_HOME`（设为 `auto` 表示按机器自动解析），再退回默认位置 |
| `--json` | 以 JSON 输出（必须放在子命令之前，如 `toolbox --json list`） |

共享目录默认位置：

- Windows：**最后一个本地固定盘的根目录 `toolbox`**，如 `D:\toolbox`（自动解析盘符，不写死；U 盘、光驱、网络盘不参与）
- macOS / Linux：`~/.local/share/toolbox`

## 命令

### list / ls

```text
toolbox list [--json]
```

列出每个已安装工具的版本、平台、占用空间与状态（`ok` / `MISSING` 等）。除 `index.json` 外也会扫描工具箱下的各工具目录；扫描到磁盘存在但索引未登记的工具时，会**先立即收编进 `index.json`**（`source: (discovered)`）再列出，并在 stderr 提示「已自动登记 N 个未登记工具」。

### find / search

```text
toolbox find [关键字] [--json]
```

在「已安装的工具」「工具箱目录中实际存在的工具」与「内置 recipe」中按名称模糊查找。扫描到未登记工具时会先自动收编进索引再显示。

### ensure / install

```text
toolbox ensure <名称>[@版本] [<名称>[@版本] ...] [--force] [--json]
```

按内置 recipe 把工具装进共享目录；已存在同版本则跳过，`--force` 强制重装。安装前会用 `shutil.which` 探测系统 PATH（含 recipe 的 exe 别名，如 `ripgrep`→`rg`）：命中则默认跳过安装并提示，`--force` 可强制装入；显式指定版本时不跳过。`get` 在提供 `--name` 时同样先做 PATH 探测。

### sync

```text
toolbox sync [--file <清单路径>] [--force]
```

读取项目清单，逐项 `ensure`。默认依次寻找 `./.toolbox.json`、`./.toolbox`。

### get

```text
toolbox get <URL | github:owner/repo[@tag]>
              [--name 名称] [--version 版本] [--sha256 摘要]
              [--exe 归档内路径或文件名] [--asset 资产名或通配符] [--force]
```

用于安装内置 recipe 之外的工具。

- 直链：`get https://host/tool.zip --name mytool --exe mytool.exe`
- GitHub：`get github:owner/repo`（默认取 latest）或 `github:owner/repo@v1.2.3`
- 省略 `--asset` 时，按当前操作系统与架构给 release 资产打分自动选择；有歧义会列出候选并要求显式指定。
- 省略 `--sha256` 时，如果 release 里有 `<资产名>.sha256` 会自动读取并校验。

### path / which

```text
toolbox path <名称> [--version <版本>] [--json]
```

打印可执行文件的绝对路径。先查 `index.json`，找不到时自动扫描工具箱下的工具目录（**扫描到的未登记工具会先立即收编进索引**），再看目录根部直接投放的 `<名称>[.exe]`；**toolbox 中都没有时会回退到系统 PATH 查找（含 recipe 的 exe 别名），命中则返回该路径并在 JSON 中标注 `"source": "system PATH"`**；toolbox 与 PATH 都没有时以退出码 2 结束。JSON 输出中的 `indexed` 表示是否来自索引。本命令只返回路径，不负责执行。

### remove / rm / uninstall

```text
toolbox remove <名称> [--version <版本>] [--all]
```

删除指定版本或全部版本。删除前会校验目标必须位于共享目录内且不是保留目录。不会生成或依赖 `bin` 启动器。

### gc

```text
toolbox gc [--prune-other-platforms]
```

- 清空安装临时区 `downloads/` 中残留的文件（正常安装后已自动删除，这里兜底中断残留），以及系统临时区过期的 `toolbox-*` 目录；
- 先扫描各工具目录，把未登记但在磁盘上存在的工具收编进索引（避免误删），再删除索引中已丢失文件的条目；
- 不会自动删除任何顶层工具目录（手工放入的 SDK/工具原样保留）；
- 旧版本遗留的 `bin/` 目录若只含启动器文件会被删除；
- 报告释放空间；
- `--prune-other-platforms` 额外删除非当前操作系统/架构的版本（换电脑或换平台后清理旧二进制）。

### repair / relink

```text
toolbox repair [--update-hash] [--json]
```

校验索引与各工具目录，用于**整个共享目录搬家到新电脑或新盘符之后**；同时把未登记的工具收编进索引（日常命令已会自动收编，这里作为全量校验/兜底）。不生成启动器。发现文件哈希不一致时报告，`--update-hash` 会把当前哈希写回索引。

### doctor

```text
toolbox doctor [--json]
```

校验索引中每个条目的可执行文件是否存在、`exe_sha256` 是否匹配；同时扫描各工具目录，**扫描到的未登记工具会先立即收编进索引**（`source: (discovered)`），无法识别可执行文件的目录（如多候选、SDK 目录）保持不动。索引问题导致退出码为 1。

### where / home

```text
toolbox where [--json]
```

显示共享目录及各工具目录、`downloads`、`index.json` 的绝对路径，以及该路径来自 `--home`、`TOOLBOX_HOME` 还是自动解析。`downloads` 只在安装期间存在，完成后自动删除；`temp` 为系统临时目录。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | `doctor` 发现问题 |
| `2` | 可预期的错误（未安装、找不到版本、校验失败、网络失败等），错误信息输出到 stderr |
| `130` | 用户中断 |

## 项目清单格式

### 纯文本 `.toolbox`

```text
# 一行一个，支持 名称 或 名称@版本
ripgrep
jq
protoc@36.2
```

### JSON `.toolbox.json`

```json
{
  "tools": [
    "ripgrep",
    "jq@1.8.2",
    { "name": "fd", "version": "10.5.1" },
    { "name": "protoc" }
  ]
}
```

也允许清单本身就是数组：

```json
["ripgrep", "jq@1.8.2"]
```

## 自定义 recipe

内置 recipe 位于 `scripts/recipes.json`。可在 `$TOOLBOX_HOME/recipes.json` 放同名条目进行覆盖或扩展，格式：

```json
{
  "mytool": {
    "description": "说明文字",
    "repo": "owner/repo",
    "version": "latest",
    "tag": "v{version}",
    "strip_prefix": ["v"],
    "asset": {
      "win32-x64": "mytool-{version}-windows-amd64.zip",
      "linux-x64": "mytool-{version}-linux-amd64.tar.gz",
      "darwin-arm64": "mytool-{version}-macos-arm64.tar.gz"
    },
    "exe": {
      "win32-x64": "mytool.exe",
      "linux-x64": "mytool",
      "darwin-arm64": "mytool"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `repo` | GitHub 仓库 `owner/repo` |
| `version` | 默认版本，`latest` 表示取最新 release |
| `tag` | 显式指定版本时的 tag 模板；如 jq 的 `jq-{version}` |
| `strip_prefix` | 生成 `{version}` 时依次尝试去掉的前缀，默认 `["v"]` |
| `asset` | 各 `平台-架构` 对应的资产名模板，可用 `{version}`、`{tag}`、`{platform}`；可用 `default` 兜底 |
| `exe` | 各平台可执行文件名或归档内相对路径；可用 `default` 兜底 |
| `sha256` | 可选，固定版本的归档摘要 |

平台键取值：`win32-x64`、`win32-arm64`、`linux-x64`、`linux-arm64`、`darwin-x64`、`darwin-arm64`。

## 存储布局与完整性

```text
<TOOLBOX_HOME>/
  <工具名>/               # 每个工具一个目录；单文件工具也单独建目录
  downloads/             # 安装期间的下载/解压临时区，安装结束立即删除
  index.json
  recipes.json           # 可选
```

不生成 `bin` 启动器；`path` 只返回 `<工具名>/` 里的可执行文件路径，由调用方执行。

安装时会把归档里唯一的一层包装目录剥掉，所以工具文件直接落在 `<工具名>/` 下（例如 `<工具名>/rg.exe`）。单个可执行文件同样放进 `<工具名>/`。

下载与解压可以落在 `downloads/`，但每次安装结束（成功或失败）都会自动删除；正常完成后该目录不存在，进程中断的残留由 `gc` 兜底清空。

- 每个条目记录下载包 `sha256` 与可执行文件 `exe_sha256`；`doctor` 用后者做完整性校验。
- 下载先落到临时目录，校验通过后才移入 `<工具名>/`；失败不会污染索引。
- 归档解压前会检查成员路径，拒绝越界写入。
- 写入索引时使用锁文件，避免并发安装写坏 `index.json`。
- 索引中的路径都相对共享目录记录，因此整个目录可以整体搬迁。

## 换电脑 / 迁移

| 场景 | 做法 |
| --- | --- |
| 同一系统与架构，想连工具一起搬 | 复制整个 `toolbox` 目录到新机器最后一个固定盘的根目录（`<盘符>:\toolbox`），运行 `toolbox repair` 校验；路径不同也没关系 |
| 只迁移项目，工具重下 | 只带项目的 `.toolbox` / `.toolbox.json`，在新机运行 `toolbox sync` |
| 换了系统或 CPU 架构 | 旧平台二进制无法运行，`sync` 会下载新平台版本；用 `toolbox gc --prune-other-platforms` 清掉旧平台残留 |
| 环境变量 | 不要写死盘符；留空或设为 `TOOLBOX_HOME=auto`，每台机器自动定位最后一个固定盘 |

判断当前实际位置用 `toolbox where`，它会显示解析来源。
