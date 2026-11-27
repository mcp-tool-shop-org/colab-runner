<p align="center">
  <a href="README.md">English</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

自分のマシンから、有料のGoogle Colab GPUでスクリプトを実行し、予算の上限を設定して、結果を取得します。ジョブは小さなファイルに記述します。colab-runner がセッションを開き、ジョブを実行し、出力を手元に持ち帰り、セッションを停止して、かかった費用を記録します。

## 機能

- **ジョブごとに1つのスペックファイル。** 入力、セットアップと実行コマンド、出力、GPUの種類、予算上限、時間制限など、すべてを
`colab-window.toml`に記述します。ジョブごとの起動スクリプトは不要です。
- **自分で設定する予算上限。** 料金は、お客様の環境での実行時間 × 料金で計算されます。予算上限または時間制限に達すると、ジョブは停止し、セッションは終了します。
- **実際に試してから料金を支払う。** `--dry`は、Colabへの呼び出しを行わずにスペックをチェックします。`--local-cpu`は、お客様の環境でジョブ全体を実行します。これは、VMで実行する場合と同じ方法です。
- **セッションは常に停止されます。** 実行が終了するすべての方法で、セッションの停止が試みられます。実行中に環境が停止した場合、`stop --out-dir`が後でセッションを閉じます。
- **トークンは結果に含まれません。** ログとダウンロードされたファイルからアクセス トークンが削除されてから保存されます。
- **すべての実行の記録。** `outcome.json`は、出力フォルダに、実行がどのように終了し、どれだけの費用がかかったかを記録します。
- **公開はご自身で行うことができます。** colab-runnerは何もアップロードしません。出力を共有するには、Hugging Faceの`hf upload`を使用してください（[docs/reference.md](docs/reference.md)を参照）。

## インストール

colab-runnerは PyPI にはありません。リリースタグからインストールしてください。

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

LinuxまたはWSL、Python 3.11以降、およびGoogleの `colab` コマンドラインが必要です。支払いを行うアカウントにログインしてください。colab-runnerには、それ以外の依存関係はありません。

## クイックスタート

プロジェクトの Git リポジトリ内で作業してください。リポジトリ外のファイルは保護されたデータと見なされ、アクセスが拒否されます。仕様を記述してください（完全な仕様は [examples/merchant_league_seats](examples/merchant_league_seats/README.md) にあります）。

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

`--local-cpu` は、お客様のコンピューターでセットアップコマンドとジョブコマンドを実行するため、必要なものがすべて揃っている必要があります（ここでは pip: `sudo apt install python3-pip` を、新しい Ubuntu または WSL にインストールします）。次に、それを確認し、テストし、実行します。

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

出力は `runs/w1/collected/` に保存され、その横に `outcome.json` が表示されます。ライブ起動には、コマンドラインに `--cap` と `--max-rate` が必要です。したがって、有料の実行は常に意図的な選択です。

## 実行が終了したとき

| 終了 | 意味 |
| --- | --- |
| 0 | ジョブが完了し、出力が返され、セッションが停止しました。 |
| 1 | 何も開く前に拒否された場合：仕様またはフラグを修正してください（メッセージに何が問題か表示されます）。 |
| 2 | 実行が早期に終了しました（ジョブの失敗、上限、時間制限、停止、または到達不能）。セッションは停止します。停止に失敗した場合は、メッセージに `stop` を実行するように指示されます。 |
| 3 | ジョブは完了しましたが、停止が確認されませんでした。`python -m colab_runner stop --out-dir D` を実行してください。 |

すべての拒否とエラーは、コード、メッセージ、およびヒントを出力します。`python -m colab_runner --help` はコマンドをリストし、`launch --help` はすべてのフラグをリストします。

**詳細：** 自分で開いたセッションへの接続、ジョブが進行状況を表示するために書き込む内容、ダウンロードがどのように処理されるか、および価格設定はすべて[docs/reference.md](docs/reference.md)に記載されています。

## セキュリティと脅威モデル

**影響を受けるもの。**
- 指定した Colab セッション：セルのファイルをアップロードし、開始したジョブを開始および終了し、要求されたファイルをダウンロードし、セッションを停止します。colab-runner (`CR_NOT_OURS`) の別の実行に属するプロセスには、決してシグナルを送信しません。
- お客様のコンピューターにある出力ディレクトリ（`--out-dir`）とその兄弟である隔離ディレクトリ（`<out-dir>.quarantine/`）。隔離ディレクトリがない単純な `Colab.download` は、システムの一時ディレクトリの下にある新しい `colab-runner-quarantine-*` フォルダを使用します。
- `--env` で指定する環境変数。これは、セルのファイル内の VM に渡されます。

**対象としないもの。** 外部ディレクトリ、隔離ディレクトリ、および一時隔離ディレクトリの外部のファイル。他のColabセッション。アカウント設定、認証情報、または請求：アカウントの料金と残高を `colab` コマンドラインから読み取り、変更することはありません。

**ネットワーク。** Google Colabへの唯一の接続は、`colab` コマンドラインを介して行われます。colab-runnerは、独自の接続を開きません。

**機密情報。** `--env NAME` として渡された機密情報は、コマンドラインに表示されません。すべての出力行、エラー行、トレースバック、およびダウンロードされたテキストファイルからトークンが削除されます。トークンを含むバイナリファイルは、変更せずに隔離され、ダウンロードは拒否されます（上記）。colab-runner は隔離ディレクトリを削除することはありません。お客様が削除する必要があり、決して配布しないでください。

**権限。** ユーザーが実行し、そのユーザーがすでに持っている `colab` にサインインして実行されます。昇格は必要ありません。

**テレメトリなし。** colab-runnerは、データを収集または送信しません。

脆弱性を報告するには、[SECURITY.md](SECURITY.md)を参照してください。

## ここにないもの

バケット名、アカウント名、起動ラッパー、またはローカルパスはありません。呼び出し元は、それらをフラグまたは環境変数で渡します。テストでは、プレースホルダーを使用します。

## 検証

```bash
bash verify.sh
```

## ライセンス

MIT。 [LICENSE](LICENSE)を参照してください。

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
