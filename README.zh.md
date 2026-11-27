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

在您自己的机器上运行付费的 Google Colab GPU 脚本，设置支出上限，并获取结果。您在一个小文件中描述任务；colab-runner 打开会话，运行任务，将输出结果带回本地，停止会话，并记录成本。

## 它的作用

- **每个任务一个规范文件。** 输入、设置和运行命令、输出、GPU 类型、支出上限和时间限制，全部包含在 `colab-window.toml` 中。没有针对每个任务的启动脚本。
- **您设置的支出上限。** 费用按费率 × 机器上的运行时间计算。当达到上限或时间限制时，任务将停止，会话将关闭。
- **在付费之前进行测试。** `--dry` 检查规范，不调用 Colab。`--local-cpu` 在您的机器上运行整个任务，就像在虚拟机上运行一样。
- **会话始终会停止。** 任何一种运行方式都尝试停止会话。如果您的机器在运行过程中出现故障，`stop --out-dir` 稍后会关闭它。
- **令牌不会出现在结果中。** 在将日志和下载的文件保存之前，会清除其中的访问令牌。
- **记录每次运行。** `outcome.json` 位于输出文件夹中，记录运行结束的方式和成本。

## 安装

colab-runner 不在 PyPI 中。从发布标签安装它：

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

您需要 Linux 或 WSL、Python 3.11 或更高版本，以及 Google 的 `colab` 命令行工具，并使用用于支付的帐户登录。colab-runner 没有其他依赖项。

## 快速入门

在您的项目的 Git 仓库中进行操作：仓库外的文件将被视为受保护的数据，并且会被拒绝访问。编写一份规范（完整的规范位于 [examples/merchant_league_seats](examples/merchant_league_seats/README.md)）：

```toml
schema_version = 1

[project]
repo = "you/your-repo"   # required: your repo, for the records
study = "first-try"      # required: a name for this line of work
job = "train_01"

[compute]
card = "L4"        # CPU, T4, L4, G4 or A100
shape = "standard"
max_rate = 2.5     # expected compute units per hour
cap = 5.0          # never spend more than this
wall_s = 3600      # never run longer than this

[inputs]
root = "."
files = [
  { src = "train.py", dst = "train.py" },
  { src = "requirements.txt", dst = "requirements.txt" },
]

[job]
setup = ["python3 -m pip install -r requirements.txt"]
run = ["python3 train.py"]
cpu_dry_run = ["python3 train.py --smoke"]

[outputs]
collect = ["model", "metrics.json"]
```

`--local-cpu` 在您的机器上运行设置和作业命令，因此它需要调用（此处为 pip：
`sudo apt install python3-pip`，在新的 Ubuntu 或 WSL 系统上）。然后检查、演练并运行它：

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

输出结果将保存在 `runs/w1/collected/` 中，并附带 `outcome.json`。实际运行需要 `--cap` 和 `--max-rate` 在命令行中，因此付费运行始终是一个经过深思熟虑的选择。

## 运行结束时

| 退出 | 含义 |
| --- | --- |
| 0 | 任务已完成，输出结果已返回，会话已停止。 |
| 1 | 在打开任何内容之前拒绝：修复规范或标志（消息会说明）。 |
| 2 | 运行提前结束（任务失败、达到上限、时间限制、停滞或无法访问）。会话已停止；如果停止失败，消息会提示运行 `stop`。 |
| 3 | 任务已完成，但未确认停止。运行 `python -m colab_runner stop --out-dir D`。 |

每次拒绝和错误都会打印一个代码、一条消息和一个提示。`python -m colab_runner --help` 列出了命令，`launch --help` 列出了每个标志。

**更多详细信息：** 连接到您自己打开的会话、任务应如何写入以便显示进度、如何清除下载内容以及定价，所有这些都包含在 [docs/reference.md](docs/reference.md) 中。

## 安全性和威胁模型

**它会影响哪些内容。**
- 您指定的 Colab 会话：它会上传单元格文件，启动并停止它启动的作业，下载它被要求的文件，并停止会话。它绝不会向属于另一个 colab-runner (`CR_NOT_OURS`) 运行的进程发送信号。
- 您的机器上的输出目录 (`--out-dir`) 及其相邻的隔离目录 `<out-dir>.quarantine/`。一个不带隔离目录的简单 `Colab.download` 将在系统临时目录下的一个新 `colab-runner-quarantine-*` 文件夹中使用。
- 您使用 `--env` 指定的环境变量，它会将这些变量传递到单元格文件内的虚拟机中。

**它不会触及哪些内容。** 不会触及输出目录、隔离目录和临时隔离目录之外的任何文件。不会触及任何其他 Colab 会话。不会触及任何帐户设置、凭据或账单：它通过 `colab` 命令行读取帐户的费率和余额，并且绝不会更改它们。

**网络。** 仅通过 `colab` 命令行与 Google Colab 进行连接。colab-runner 不会打开任何自己的连接。

**密钥。** 作为 `--env NAME` 传递的密钥不会出现在任何命令行中。每一行输出、每一行错误、每一个回溯和每一个下载的文本文件都会被清除令牌；包含令牌的二进制文件将被隔离，并且保持不变，并且下载将被拒绝（如上所述）。colab-runner 绝不会删除隔离目录：您必须删除它，并且绝不能将其分发。

**权限。** 它以运行它的用户的身份运行，并使用该用户已拥有的 `colab` 登录信息。它不需要任何权限提升。

**无遥测数据。** colab-runner 不收集或发送任何数据。

报告漏洞：请参阅 [SECURITY.md](SECURITY.md)。

## 此处未包含的内容

没有存储桶名称、帐户名称、启动包装器或本地路径。调用方通过标志或环境变量传递这些内容；测试使用占位符。

## 验证

```bash
bash verify.sh
```

## 许可证

MIT。请参阅 [LICENSE](LICENSE)。

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
