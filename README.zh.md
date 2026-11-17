<p align="center">
  <a href="README.ja.md">日本語</a> | <a href="README.md">English</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

# colab-runner

工作室为付费的 Google Colab 窗口提供的共享运行器：所有 Colab 任务的代码都放在这里。它启动一个 Colab 虚拟机上的任务，该任务的生命周期超过了运行环境，并在预算限制内监控它，清除所有返回数据中的令牌，并始终尝试停止会话。

**状态：0.1.0。** 独立的运行器（如下面的项目 1）已构建。项目 2 到 5 尚未构建。

1. **独立的运行器 (0.1.0)。** 在虚拟机上启动一个任务，使用状态文件进行轮询，采用回退机制、`--hold-s`心跳模式，以及一个用于切换到内核驻留循环的标志。恢复功能是一个 Python API 钩子（如下所示）。
2. **启动和卡片保护。** 在虚拟机的第一个步骤中进行卡片断言（计算能力和每张卡片的 VRAM 类型，拒绝 `CARD_MISMATCH`），在运行环境端进行拒绝日志记录，并使用每张卡片的断路器（`CARD_CIRCUIT_OPEN`），并在加载 torch 之前设置 `OMP_NUM_THREADS`。
3. **密钥。** 令牌文件路径：一个 0600 文件，仅由需要它的进程读取，并在虚拟机上被删除，并从所有保留的日志中清除。
4. **Hugging Face 存储桶模块。** 将输出同步到每个窗口的暂存前缀，验证计数和 sha256 与清单的匹配情况，然后写入 `done.json` 收据。它永远不会删除。
5. **`PLATFORM.md` 和 `facts.json`。** Colab 平台实际执行的操作，每个事实都有日期和来源，因此一个工具会检查其假设是否与这些事实一致，如果存在不一致，则会将其标记为拒绝代码，而不是付费惊喜。

各个任务使用此仓库的固定版本，每个拉取请求对应一个任务。

## 安装

colab-runner 尚未发布到 PyPI。各个任务使用固定版本，或者从标签安装它：

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11 或更高版本。该软件包没有运行时依赖项。
- **`colab` 命令行**（Google 的 Colab CLI），已登录到支付窗口费用的帐户。colab-runner 调用它；它不会为您安装或登录。`COLAB_RUNNER_CLI` 指向另一个命令行（如下所示）。
- **平台：** CI 在 Linux 上运行，使用 Python 3.11 和 3.12。工作室也从 Windows 运行它。

## 快速入门

任务打开会话，然后将其交给 `run`，最后通过停止会话来结束：

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

这些数字是占位符。将 `--max-rate` 设置为卡片每小时的预期计算单元，并将 `--cap` 设置为窗口的计算单元预算（请参阅定价）。

`python -m colab_runner --help` 列出所有命令；`run --help` 列出所有标志。

## 从任务中使用 (0.1.0)

**会话属于任务。** 任务打开 Colab 会话并传递其名称。一旦 `run` 在输出目录中写入 `session_open.json`，每次退出都会尝试停止会话，并且只有在确认停止后才会删除该标记。
- **一个保留的标记** 表示会话可能仍然打开：停止失败，或者 `run` 直接终止（SIGKILL，丢失的运行环境）在清理操作运行之前。然后，任务运行 `python -m colab_runner stop --out-dir D`。
- **在标记存在之前**（拒绝或使用错误，两者都以退出代码 1 结束），任务本身运行 `colab stop -s S`。

**`run` 所需的标志：** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`。
- `--out-dir` 必须不存在，或者为空（否则为 `OUT_DIR_NOT_EMPTY`）。不允许使用文件系统根目录（`OUT_DIR_INVALID`）：隔离区必须是输出目录的同级目录，并且其名称来自其解析后的路径。同样，不允许创建或列出无法创建或列出的输出目录。
- `--cleanup-bound-s` 必须至少等于运行器的清理边界（否则为 `CLEANUP_BOUND_TOO_SMALL`）。在默认的调用超时情况下，该边界为 466 秒，或者在启用 `--resident` 时为 986 秒（`Runner.own_cleanup_bound_s`）。
- 同时传递 `--pre-open-rate` 和 `--pre-open-assignments`，在会话打开之前读取，或者都不传递。如果没有传递这两个参数，则整个帐户的速率限制将归因于此窗口，因此相邻的会话可能会触发速率限制。
- 对于每个共享 `--content` 目录的步骤，传递一个不同的 `--job`（默认值为 `job`）。不允许重复使用 `--job` 的方式：
- 每次运行都有其自身的运行 ID `CR_RUN_ID`，每次都会生成一个新的 ID。该名称是保留的：不允许使用 `--env CR_RUN_ID=…`（或者在 `Runner.run(env=...)` 中使用 `CR_RUN_ID`）（`RUN_ID_RESERVED`），因为如果在多个步骤中重复使用同一个 ID，将会禁用这些步骤之间的拒绝机制。
- 在作业开始之前，虚拟机将运行 ID 写入 `<job>.run_id`。作业的状态文件和每个轮询记录都包含该 ID。
- 作业的文件是 `<job>.pid`、`.status.json`、`.run.out`、`.poll.json` 和 `.run_id`。如果其中任何一个文件存在于另一个运行 ID 下（或者不存在于任何运行 ID 下），则该步骤将以 `JOB_ALREADY_EXISTS` 结束（`start_failed`）。无论另一个作业的进程是活动状态还是已停止，无论是否有状态文件，上述情况都适用。
- 被拒绝的步骤不会触及另一个运行的任何文件，也不会终止其作业，但仍然会停止其自身的会话。在 `--resident` 模式下，拒绝操作会发送到 `<job>.refused.json`，而不是 `<job>.poll.json`。
- 运行器仅获取包含其自身运行 ID 的记录。另一个运行在相同的 `--job` 下（例如，步骤 1 的最终 `<job>.poll.json`）留下的记录，不会导致步骤结束。在 `--resident` 模式下，如果仅在 `--setup-stall-s` 中出现另一个运行的记录，则该步骤将以 `JOB_ALREADY_EXISTS` 结束（`start_failed`）。如果没有 `--resident`，则此类记录将被视为失败的轮询。
- 在看到任何记录之前发生的情况（例如，在另一个运行的作业持有 `--job` 时），仍然不会终止任何内容：终止单元将返回 `CR_NOT_OURS`，并且另一个运行的 `<job>.run.out` 也不会被收集。
- 重新启动相同的运行（即回复丢失的运行）不会被拒绝。
- 在 `--resident` 模式下，如果通过 `--env` 传递的 `CR_RESIDENT_MAX_S` 不是有限的或非正数，则会立即拒绝（`RESIDENT_MAX_INVALID`，退出代码 1）。

**退出代码**（组织约定），用于 `run` 和 `stop --out-dir D`：

| 退出 | 含义 | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`，并且已确认停止 | 已确认停止，或者没有需要停止的内容 |
| 1 | 用户错误 | 拒绝（`REFUSED: …`）或使用错误，在写入会话标记之前 | 没有命名会话的标记（`SESSION_MARKER_INVALID`），或者使用错误 |
| 2 | 运行时错误 | 任何结束，但不是 `done`（无论其停止操作如何），或者 `RUN_ERROR` | 停止操作未被确认，或者 `STOP_ERROR` |
| 3 | 部分成功 | `done`，但停止操作未被确认：`session_open.json` 仍然存在 | （未使用） |

在第 3 步中，任务的工作结果位于输出目录中，并且会话仍然需要使用 `python -m colab_runner stop --out-dir D` 停止。

在标记之后拒绝，并不意味着退出代码为 1。例如，重复使用 `--job` 会导致退出代码为 2：此时，运行器已经拥有该会话，因此它会停止该会话，并且最后一行是 `RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS`，其中包含一个提示，用于命名 `--job`，并将其写入标准错误输出。不会写入任何 `REFUSED:` 行。

**拒绝和错误** 都有一个代码、一个消息和一个提示，并且每一行都会被清理：
- 拒绝会首先在标准错误输出中写入 `REFUSED: CODE: message`，然后写入 `hint: …`（需要更改的内容）。
- 在运行过程中发生的错误会写入 `RUN_ERROR: CODE: message`，然后写入 `hint: …`。代码是错误的自身代码（`BINARY_DOWNLOAD_TOKEN`、`QUARANTINE_ESCAPE`），或者代码为 `UNEXPECTED_ERROR`，并且消息中包含异常的类型。`stop` 以相同的方式写入 `STOP_ERROR: …`。
- 如果在标记之后发生命名拒绝（`JOB_ALREADY_EXISTS`、`BINARY_DOWNLOAD_TOKEN`、`QUARANTINE_ESCAPE`），则会将其记录在 `runner_result.json` 中，并将其显示为 `refusal: {code, message, hint, retryable}`。对于调用者输入的任何拒绝，`retryable` 均为 false：相同的调用会以相同的方式再次失败。
- 每个代码及其提示都位于 `colab_runner/errors.py`（`CODES`）中。

**日志级别**（`run` 和 `stop`）：`--quiet` 仅写入结束行（`RUN_END`、`STOP_END` 或 `STOP_NOTHING`，以及拒绝或错误及其提示）。默认情况下，它会将运行器的日志添加到标准错误输出中。`--verbose` 每轮写入一行。`--debug` 添加错误的堆栈跟踪。在每个级别，令牌都会被清理，包括堆栈跟踪。

**运行结束的方式：** `done`、`failed`（非零退出代码）、`oom`、`wall`、`cap`、`setup_stall`、`progress_stall`、`dead`、`unreachable`、`start_failed`、`interrupted` 或 `error`。
- 除了 `error` 之外的每个结束都会将 `RUN_END reason=… rc=… stopped=…` 写入标准输出的最后一行，并在运行以命名拒绝结束时添加 ` code=CODE`。对于此类拒绝，以及对于未确认的停止，标准错误输出中会显示一个提示。
- `error` 结束会将 `RUN_ERROR: …` 写入标准错误输出，并且不会写入任何 `RUN_END` 行；如果 `session_open.json` 仍然位于输出目录中，则未确认的停止的提示会随后显示。拒绝或用法错误（退出代码 1）仅写入标准错误输出。
- `runner_result.json` 位于输出目录中，记录了在写入标记之后发生的每个结束，包括 `error`。在清理期间拒绝的二进制文件下载会将结束更改为 `error`，并将替换的结束保留在 `prior_reason` 中（以及在详细信息中）。

**`run` 会在输出目录中写入什么**（`--job` 默认为 `job`，`--content` 默认为 `/content`）：
- `session_open.json`，在会话可能仍然打开时；
- `runner_result.json`，表示运行结束。它的 `quarantined` 列出了在运行期间隔离的每个二进制文件，并且来自后续 `Runner.download` 的隔离，通过相同的 `Runner`，会用它重写该文件；
- `<job>.run.out`，表示作业的日志（仅在作业写入日志行后或 `--resident` 收集到日志后才会出现）；
- 带有 `--resident`、`<job>.poll.json`（作业的最新轮询记录，即已下载的记录）和 `<job>.refused.json`，表示本次运行的拒绝记录，当其拒绝的驻留单元写入记录时（一个 `JOB_ALREADY_EXISTS`，仅在另一个运行的记录中出现超时情况时才会为空）；
- `stop_confirmed.json`，表示在确认停止后，或 `stop_pending.json`，表示在失败后。

**`colab` 命令** 默认为 `colab`，除非 `COLAB_RUNNER_CLI` 指定了另一个命令行。它使用 `shlex.split` 进行拆分，该命令使用 POSIX 引用：在 Windows 上，使用正斜杠写入路径或对其进行引用（`"C:\tools\colab.exe"`），因为未引用的反斜杠会被删除。无法拆分（未关闭的引用）的命令会被拒绝（`CLI_INVALID`，退出代码 1）。

**任务必须写入的内容，以便停滞保护程序可以看到它。** 路径位于虚拟机上；输出目录默认为 `CR_OUT_DIR`，默认情况下为 `<content>/<job>_out`。
- `phase.txt`：在安装期间，不存在、为空或为 `setup`，运行后，则为其他内容。跳过设置阶段的任务。
- `setup.log`：在设置期间，如果运行的 `setup.log` 停止增长 `--setup-stall-s`（默认为 600），则结束 `setup_stall`。
- **进度：** 在设置之后，如果运行期间它能看到的内容在 `--progress-stall-s`（默认为 900）内没有发生任何变化，则运行结束 `progress_stall`。它看到的内容是每个与 `CR_PROGRESS_GLOB`（默认为 `<out>/progress/*`）匹配的文件中的第一个整数的总和，以及与 `CR_UNITS_GLOB` 匹配的文件数，以及保持心跳。
- 如果在连续 `--unreachable-polls` 轮（默认为 10）或 `--unreachable-s`（默认为 1800）中没有有效的轮询，则运行结束 `unreachable`。

**机密信息与二进制安全下载清理。** 将机密信息作为 `--env NAME` 传递，并在通道的环境中设置 NAME。`--env NAME=VALUE` 将该值放入运行器的命令行中。值会进入单元文件中的虚拟机，但不会作为参数传递给 `colab` CLI。
每个 CLI 输出行和错误行都将使用库中的 `scrub_colab_tokens` 进行清理。下载的文件和目录（包括部分传输）仅根据内容进行分类（一个 NULL 字节或严格 UTF-8 解码失败表示二进制文件）：
- 文本文件将使用 `scrub_colab_tokens` 进行原地清理。
- 清理后的二进制文件将保持字节级的完全一致。无法读取的文件将被删除。
- **二进制规则。** 二进制文件将使用库中令牌模式的字节对进行扫描。
- 在 UTF-8 中，这些字节对与解码后的文本中文本模式匹配的内容完全相同。Python 的 `\s` 匹配的每个字符都算作一个空格，包括 `\x1c`-`\x1f`、NBSP、NEL 和 EM SPACE。
- 唯一的区别是，在非 ASCII 字母旁边，`hf_` 字节对匹配，而文本模式不匹配：更多，而不是更少。
- 一条规则比文本规则更严格：在二进制文件中，`Bearer` 仅在它后面的值是令牌形状时才计数。这意味着至少有 20 个 `[A-Za-z0-9._-]` 字符，并且至少使用两个小写字母、大写字母和数字。
- 因此，如果一个渲染的提示信息是“北方之焰”，那么它将以字节级的完全一致的方式呈现。一个真实的令牌（一个 OAuth 访问令牌、一个 JWT、一个 Hugging Face 令牌）将被捕获。
- 文本文件将保持库中的模式不变：文本文件中的相同文本仍然会变成 `Bearer [REDACTED] the North`。
- **如果匹配，则永远不会重写。**
- 该文件将以未更改的形式移动到隔离区，并仅通过路径和模式名称在 `runner_result.json` (`quarantined`) 中进行记录。
- 下载将被 `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`) 拒绝。这将使 `Runner.run` 结束为 `error`（CLI 退出代码 2），并且会话仍然会停止。
- **目录下载** 将逐文件进行清理，并且一次失败不会停止其余部分：如果移动到隔离区的操作失败，则该匹配项将被删除，并且无法列出的子目录将被记录。第一次出现此类错误将在清理了其他文件的之后出现。仅读取常规文件。指向文件或目录的符号链接将被删除，而不会跟踪，并且任何不是常规文件或目录的条目（FIFO、套接字、设备）也将被删除，而不会读取。
- **隔离区的位置。**
- 通过 `Runner`（因此 `run`），它是 `<out-dir>.quarantine/` 的兄弟目录。它位于 `--out-dir` 之外，因此既不会被 `salvage` 拾取，也不会被 out-dir 捆绑。
- 一个不带 `Colab.download` 或 `scrub_local` 的简单命令，并且没有 `quarantine_dir`，会将文件移动到系统临时目录 `tempfile.gettempdir()` 下的一个新文件夹 `colab-runner-quarantine-*` 中。它永远不会与文件位于同一位置，因此也不会位于下载文件的文件夹中。其路径位于 `BinaryDownloadRefused.quarantined` 中。传递 `Colab(quarantine_dir=...)` 以选择位置。
- 在移动之前，将解析每个隔离路径并检查其是否位于隔离文件夹内。如果文件将位于隔离文件夹之外，则该文件将被删除，并且下载将失败，并显示 `QuarantineEscape` (`QUARANTINE_ESCAPE`)。
- 运行器永远不会删除隔离区；**通道必须删除它，并且永远不要将其发送出去。**

**Salvage（回收）** 仅是一个 Python API 钩子：在除了 `done` 之外的每个结束时，它都会运行 `Runner.run(..., salvage=callable)`，在停止之前。CLI 没有回收功能。

**定价。** 通过库中的速率和上限函数，窗口的价格为 `max(--max-rate, the highest stable window rate)` 乘以经过的时间。高于 `--max-rate` 的稳定速率会触发 `RATE_ABOVE_MAX`，因此每个持续运行的窗口的价格都为 `--max-rate`：这是一个下限，而不是一个宽松的上限。将其设置为卡的预期速率；一个宽松的 `--max-rate` 会提前触发上限。帐户余额将被记录，并且不会对窗口进行定价。

## 安全性和威胁模型

**它会影响什么。**
- 通道命名的 Colab 会话：它会上传单元文件、启动和终止它启动的任务、下载请求的文件，并停止会话。它不会发出任何进程，表明另一个运行已经开始（`CR_NOT_OURS`）。
- 位于设备上的 out 目录（`--out-dir`）及其兄弟隔离区 `<out-dir>.quarantine/`。一个不带隔离目录的简单 `Colab.download` 命令将在系统临时目录下的一个新 `colab-runner-quarantine-*` 文件夹中使用。
- 通道使用 `--env` 命名的环境变量，它会将这些变量传递到单元文件中的虚拟机中。

**它不会影响什么。** 不会影响 out 目录、其隔离区和临时隔离区之外的任何文件。不会影响任何其他 Colab 会话。不会影响任何帐户设置、凭据或账单：它会通过 `colab` 命令行读取帐户的速率和余额，并且不会对其进行任何更改。

**网络。** 仅通过 `colab` 命令行，连接到 Google Colab。colab-runner 不会打开任何自己的连接。

**机密信息。** 作为 `--env NAME` 传递的机密信息不会出现在任何命令行中。每个输出行、错误行、回溯和下载的文本文件都会被清理掉令牌；包含令牌的二进制文件将被以未更改的形式隔离，并且下载将被拒绝（如上所述）。通道会删除隔离区，并且永远不会将其发送出去。

**权限。** 它以运行它的用户的身份运行，并使用该用户已经拥有的 `colab` 登录。它不需要任何权限提升。

**没有遥测数据。** colab-runner 不会收集或发送任何数据。

报告漏洞：请参阅 [SECURITY.md](SECURITY.md)。

## 此处未提及的内容

没有存储桶名称、帐户名称、启动包装器或本地路径。调用者通过标志或环境变量传递这些内容；测试使用占位符。

## 验证

```bash
bash verify.sh
```

## 许可证

MIT。请参阅 [LICENSE](LICENSE)。

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
