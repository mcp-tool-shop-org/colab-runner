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

# colab-runner

有料のGoogle Colab環境向けの、スタジオが共有するランナー。各Colab環境で利用されるコードはすべて、このランナーに集約されます。このランナーは、Colab VM上でジョブを開始し、そのジョブはランナーのライフサイクルを超えて実行されます。また、予算内で実行され、取得したトークンをすべて削除し、セッションを停止しようとします。

**ステータス：0.1.0。** 独立したランナー（以下1）がビルドされました。2から5はまだビルドされていません。

1. **独立したランナー（0.1.0）。** ステータスファイルを使用してVM上でジョブを開始し、バックオフ処理を行い、`--hold-s`ハートビートモードと、カーネルに常駐するループに切り替えるフラグを使用します。リソースの回収は、Python APIフック（以下）によって行われます。
2. **ブートとカードの保護。** VMの最初のステップでカードの検証を行い（コンピューティング能力と、カードタイプごとのVRAM）、`CARD_MISMATCH`を拒否します。また、カードごとにサーキットブレーカーを備えた、ランナー側の拒否ログ（`CARD_CIRCUIT_OPEN`）と、torchがロードされる前に`OMP_NUM_THREADS`を設定します。
3. **シークレット。** トークンファイルのルート：0600のファイルで、必要なプロセスのみが読み取り可能で、VM上で破棄され、保存されたすべてのログから削除されます。
4. **Hugging Face Storage Bucketモジュール。** 出力を、ウィンドウごとに設定されたステージングプレフィックスに同期し、マニフェストに対してカウントとsha256を検証し、その後、`done.json`レシートを書き込みます。削除は行いません。
5. **`PLATFORM.md`と`facts.json`。** 実際のColabプラットフォームで行われる処理で、各事実は日付とソースが記録されており、キットがそれらの仮定を検証し、誤った場合は拒否コードとなり、予期せぬ有料の処理が発生することはありません。

各Colab環境は、このリポジトリの固定されたリリースをベンダーとして使用し、1つのColab環境につき1つのプルリクエストとします。

## インストール

colab-runnerはPyPIには含まれていません。各Colab環境は、固定されたリリースをベンダーとして使用するか、タグからインストールします。

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.1.0"
```

- **Python** 3.11以降。このパッケージには、実行時の依存関係はありません。
- **`colab`コマンドライン**（GoogleのColab CLI）。有料のColab環境にログインしているアカウントで実行します。colab-runnerはこれを呼び出しますが、インストールしたり、ログインしたりはしません。`COLAB_RUNNER_CLI`は、別のコマンドライン（以下）を指します。
- **プラットフォーム：** CIは、Python 3.11と3.12を使用するLinux上で実行されます。スタジオは、Windowsからも実行します。

## クイックスタート

この処理は、まずセッションを開始し、次にセッションを`run`に引き渡し、最後にセッションを停止して終了します。

```bash
colab new -s my-window            # the lane's own step: open the session (plus the card's flags)
python -m colab_runner run --session my-window --out-dir runs/w1   --script /content/train.sh --wall-s 3600   --max-rate 13.5 --cap 15 --cleanup-bound-s 600
echo "exit $?"                    # 0 done and stopped; see the exit codes below
```

数値はプレースホルダーです。`--max-rate`を、カードの予想される1時間あたりのユニット数に設定し、`--cap`を、Colab環境のコンピューティングユニットの予算に設定します（価格を参照）。

`python -m colab_runner --help`は、すべてのコマンドをリストします。`run --help`は、すべてのフラグをリストします。

## Colab環境から使用する場合（0.1.0）

**セッションは、Colab環境によって管理されます。** Colab環境は、Colabセッションを開始し、その名前を渡します。`run`が、出力ディレクトリに`session_open.json`を書き込んだ後、すべての終了処理はセッションを停止しようとし、マーカーは、セッションが正常に停止された後にのみ削除されます。
- **マーカーが残っている**ということは、セッションがまだ開いている可能性があります。セッションの停止に失敗した場合、または、クリーンアップが実行される前に、`run`によって強制終了された（SIGKILL、リソースの損失）場合です。その後、Colab環境は`python -m colab_runner stop --out-dir D`を実行します。
- **マーカーが存在しない場合**（拒否または使用エラー、どちらも終了コード1）、Colab環境は`colab stop -s S`自体を実行します。

**`run`に必要なフラグ：** `--session S --out-dir D (--script P | --hold-s N) --wall-s N --max-rate R --cap C --cleanup-bound-s N`。
- `--out-dir`は、まだ存在しないか、空である必要があります（そうでない場合は、`OUT_DIR_NOT_EMPTY`）。ファイルシステムのルートは拒否されます（`OUT_DIR_INVALID`）。隔離領域は、出力ディレクトリの兄弟であり、その解決されたパスから名前が付けられます。作成またはリストできない出力ディレクトリも同様です。
- `--cleanup-bound-s`は、ランナー自身のクリーンアップ境界よりも大きい必要があります（そうでない場合は、`CLEANUP_BOUND_TOO_SMALL`）。デフォルトの呼び出しタイムアウトでは、この境界は466秒、または`--resident`を使用すると986秒です（`Runner.own_cleanup_bound_s`）。
- `--pre-open-rate`と`--pre-open-assignments`を一緒に渡し、セッションが開始される直前に読み取るか、どちらも渡さないようにします。渡さない場合、このColab環境に対してアカウント全体のレートが適用されるため、別のColab環境がレート制限に達する可能性があります。
- 各ステップで、共有する`--content`ディレクトリごとに、異なる`--job`（デフォルトは`job`）を渡します。再利用された`--job`が拒否される方法：
- 各実行には、独自の実行IDである`CR_RUN_ID`があり、毎回新しいIDが生成されます。この名前は予約されており、`--env CR_RUN_ID=…`（または`Runner.run(env=...)`の`CR_RUN_ID`）は拒否されます（`RUN_ID_RESERVED`）。1つのIDを複数のステップで再利用すると、この拒否が無効になります。
- VMは、ジョブが開始される前に、実行IDを`<job>.run_id`に書き込みます。ジョブのステータスファイルと、すべてのポーリングレコードには、このIDが含まれます。
- ジョブのファイルは、`<job>.pid`、`.status.json`、`.run.out`、`.poll.json`、および`.run_id`です。これらのファイルのうち、別の実行ID（またはIDがない）の下に存在する場合、ステップは`start_failed`で終了し、終了コードは`JOB_ALREADY_EXISTS`になります。これは、他のジョブのプロセスがアクティブであるか、非アクティブであるか、ステータスファイルがあるかどうかにかかわらず適用されます。
- 拒否されたステップは、他の実行のファイルにはアクセスせず、そのジョブを強制終了することなく、自身のセッションを停止します。`--resident`モードでは、拒否は`<job>.refused.json`に送信され、`<job>.poll.json`には送信されません。
- ランナーは、自身の実行IDを持つレコードのみを取得します。同じ`--job`（たとえば、ステップ1の最終的な`<job>.poll.json`）の下に、別の実行が残したレコードがある場合、そのレコードはステップを終了させません。`--resident`モードでは、`--setup-stall-s`内に別の実行のレコードのみが存在する場合、ステップは`start_failed`で終了し、終了コードは`JOB_ALREADY_EXISTS`になります。`--resident`がない場合、そのようなレコードは、ポーリングに失敗したと見なされます。
- どのレコードも確認される前にタイムアウトが発生した場合（たとえば、別の実行のジョブが`--job`を保持している間に、壁時間制限に達した場合）、何も強制終了されません。強制終了セルは`CR_NOT_OURS`に応答し、その後、別の実行の`<job>.run.out`も収集されません。
- 同じ実行の再試行（応答が失われた実行）は拒否されません。
- `--resident`モードでは、`--env`を介して渡された、有限または正の値でない`CR_RESIDENT_MAX_S`は、事前に拒否されます（`RESIDENT_MAX_INVALID`、終了コード1）。

**終了コード**（組織の規約）、`run`と`stop --out-dir D`：

| 終了 | 意味 | `run` | `stop` |
| --- | --- | --- | --- |
| 0 | ok | `done`、およびセッションが正常に停止された | セッションが正常に停止されたか、停止するものが何もない |
| 1 | ユーザーエラー | 拒否（`REFUSED: …`）または使用エラー。セッションマーカーが書き込まれる前 | セッションマーカーがない（`SESSION_MARKER_INVALID`）、または使用エラー |
| 2 | 実行時エラー | `done`以外のすべての終了（その停止が何であったかに関わらず）、または`RUN_ERROR` | 停止が確認されなかった、または`STOP_ERROR` |
| 3 | 部分的な成功 | `done`、ただし停止が確認されなかった：`session_open.json`がまだ存在する | （使用されない） |

終了コード3の場合、ジョブの成果物は出力ディレクトリに保存され、セッションは引き続き`python -m colab_runner stop --out-dir D`で停止する必要があります。

マーカーの後に拒否が発生した場合、それは終了処理1ではありません。たとえば、再利用された`--job`は、終了処理2を終了させます。その時点で、実行者がセッションを所有していたため、セッションは停止され、最後の行は`RUN_END reason=start_failed … code=JOB_ALREADY_EXISTS`となり、標準エラー出力に`--job`というヒントが表示されます。`REFUSED:`という行は出力されません。

**拒否とエラー**には、コード、メッセージ、ヒントがあり、すべての行はクリアされます。
- 拒否は、標準エラー出力の最初の行に`REFUSED: CODE: message`を書き込み、次に`hint: …`（変更すべきこと）を書き込みます。
- 実行中のエラーは、`RUN_ERROR: CODE: message`、次に`hint: …`を書き込みます。コードは、エラー自体（`BINARY_DOWNLOAD_TOKEN`、`QUARANTINE_ESCAPE`）または、メッセージ内の例外のタイプを含む`UNEXPECTED_ERROR`です。`stop`は、同じ方法で`STOP_ERROR: …`を書き込みます。
- マーカーの後の名前付きの拒否（`JOB_ALREADY_EXISTS`、`BINARY_DOWNLOAD_TOKEN`、`QUARANTINE_ESCAPE`）で終了する実行は、それを`runner_result.json`に`refusal: {code, message, hint, retryable}`として記録します。`retryable`は、呼び出し元の入力に対するすべての拒否に対してfalseです。同じ呼び出しが、同じ方法で再び失敗します。
- すべてのコードとそのヒントは、`colab_runner/errors.py`（`CODES`）にあります。

**ログレベル**（`run`と`stop`）：`--quiet`は、終了行（`RUN_END`、`STOP_END`または`STOP_NOTHING`、およびそのヒントを持つ拒否またはエラー）のみを書き込みます。デフォルトでは、ランナーのログを標準エラー出力に追加します。`--verbose`は、1回のポーリングごとに1行を追加します。`--debug`は、エラーのトレースバックを追加します。トークンは、すべてのレベルでクリアされ、トレースバックも含まれます。

**実行の終了方法**：`done`、`failed`（ゼロ以外の終了コード）、`oom`、`wall`、`cap`、`setup_stall`、`progress_stall`、`dead`、`unreachable`、`start_failed`、`interrupted`または`error`。
- `error`を除くすべての終了は、標準出力の最後の行に`RUN_END reason=… rc=… stopped=…`を書き込み、名前付きの拒否で実行が終了した場合は、` code=CODE`を追加します。そのような拒否、および確認されなかった停止の場合、ヒントが標準エラー出力に続きます。
- `error`で終了すると、標準エラー出力に`RUN_ERROR: …`を書き込み、`RUN_END`の行は書き込まれません。`session_open.json`がまだ出力ディレクトリにある場合、確認されなかった停止のヒントが続きます。拒否または使用法エラー（終了コード1）は、標準エラー出力にのみ書き込みます。
- 出力ディレクトリ内の`runner_result.json`は、マーカーが書き込まれた後のすべての終了を記録し、`error`も含まれます。クリーンアップ中に拒否されたバイナリのダウンロードは、終了を`error`にし、置き換えられた終了を`prior_reason`（および詳細）に保持します。

**`run`が出力ディレクトリに書き込む内容**（`--job`はデフォルトで`job`、`--content`はデフォルトで`/content`）：
- セッションが開いている間に`session_open.json`を書き込みます。
- `runner_result.json`、実行の終了。その`quarantined`は、実行中に隔離された各バイナリのダウンロードをリストし、後続の`Runner.download`からの同じ`Runner`による隔離は、そのバイナリでファイルを上書きします。
- `<job>.run.out`、ジョブのログ（ジョブがログ行を書き込んだ場合、または`--resident`がそれを収集した場合にのみ表示されます）。
- `--resident`、`<job>.poll.json`（ジョブがダウンロードしたジョブの最新のポーリングレコード）、および`<job>.refused.json`、この実行の独自の拒否レコード（拒否されたレジデントセルが1つ書き込んだ場合。別の実行のレコードでタイムアウトしただけの`JOB_ALREADY_EXISTS`には、それはありません）。
- 確認された停止の後に`stop_confirmed.json`、または失敗した停止の後に`stop_pending.json`を書き込みます。

**`colab`コマンド**は、`COLAB_RUNNER_CLI`が別のコマンドラインを指定しない限り、`colab`です。これは、POSIX引用符を使用する`shlex.split`で分割されます。Windowsでは、パスをスラッシュで記述するか、引用符で囲みます（`"C:\tools\colab.exe"`）。これは、引用符で囲まれていないバックスラッシュが削除されるためです。分割できないもの（閉じられていない引用符）は、拒否されます（`CLI_INVALID`、終了コード1）。

**ジョブが書き込む必要がある内容。これにより、ストールガードがそれを確認できます。** パスはVM上にあり、出力ディレクトリはデフォルトで`CR_OUT_DIR`、`<content>/<job>_out`です。
- `phase.txt`：インストール中、存在しない、空、または`setup`、実行中はそれ以外。保留ジョブは、セットアップフェーズをスキップします。
- `setup.log`：セットアップ中、その`setup.log`が`--setup-stall-s`（デフォルトは600）の間、増加しなくなった実行は、`setup_stall`で終了します。
- **進行状況**：セットアップ後、実行は、実行が確認できるものが`--progress-stall-s`（デフォルトは900）の間、移動しなくなったときに`progress_stall`で終了します。実行が確認できるものは、各ファイルで一致する`CR_PROGRESS_GLOB`（デフォルトは`<out>/progress/*`）の最初の整数の合計、`CR_UNITS_GLOB`に一致するファイルの数、および保留ハートビートの合計です。
- 連続して`--unreachable-polls`回のポーリング（デフォルトは10）、または`--unreachable-s`（デフォルトは1800）の間、有効なポーリングがない実行は、`unreachable`で終了します。

**機密情報とバイナリセーフなダウンロード処理。** 機密情報を `--env NAME` として渡し、レーンの環境で NAME を設定します。`--env NAME=VALUE` は、その値をランナー自身のコマンドラインに配置します。値は、セルのファイル内の VM に到達しますが、決して `colab` CLI への引数としては渡されません。

すべての CLI 出力行とエラー行は、同梱されているライブラリの `scrub_colab_tokens` を使用して処理されます。ダウンロードされたファイルとディレクトリ（部分的な転送を含む）は、コンテンツのみに基づいて分類されます（NULL バイトまたは厳密な UTF-8 デコードの失敗はバイナリと見なされます）。
- テキストファイルは、 `scrub_colab_tokens` を使用してその場で処理されます。
- 変更されていないバイナリファイルはそのまま残されます。読み取り不可能なファイルは削除されます。
- **バイナリルール。** バイナリファイルは、ライブラリのトークンパターンのバイトペアでスキャンされます。
- UTF-8 では、ペアはデコードされたテキストに対してテキストパターンが一致するのと同じものを正確に一致させます。Python の `\s` が一致させるすべての文字は、スペース、`\x1c`-`\x1f`、NBSP、NEL、および EM SPACE としてカウントされます。
- 唯一の違いは、非 ASCII 文字の隣にある場所で、 `hf_` ペアが一致するが、テキストパターンが一致しないことです。多くても少なくてもなりません。
- 1 つのルールはテキストよりも厳格です。バイナリでは、 `Bearer` は、その後の値がトークンのように見える場合にのみカウントされます。つまり、少なくとも 2 つの小文字、大文字、および数字を使用して、少なくとも 20 文字の `[A-Za-z0-9._-]` が必要です。
- したがって、プロンプトに `Flame Bearer of the North` と表示されるレンダリングは、変更なしで処理されます。実際のベアラートークン（ OAuth アクセストークン、JWT、Hugging Face トークン）が検出されます。
- テキストファイルは、ライブラリのパターンをそのまま保持します。テキストファイル内の同じテキストは、引き続き `Bearer [REDACTED] the North` になります。
- **一致した場合、書き換えは行われません。**
- ファイルは変更せずに隔離場所に移動し、パスとパターン名のみを使用して `runner_result.json` (`quarantined`) にログ記録および名前が付けられます。
- ダウンロードは `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`) で拒否されます。これにより、 `Runner.run` が `error` (CLI 終了コード 2) として終了し、セッションは停止したままになります。
- **ディレクトリのダウンロード** は、ファイルごとにトークンが処理され、1 つの失敗が残りの処理を停止することはありません。隔離場所に移動できなかった一致は削除され、リスト表示できないサブディレクトリは記録されます。このような最初のエラーは、残りのファイルがすべて処理された後に 1 回だけ発生します。通常のファイルのみが読み取られます。ファイルまたはディレクトリへのシンボリックリンクは削除され、追跡されることはありません。また、通常のファイルまたはディレクトリではないエントリ（FIFO、ソケット、デバイス）も削除され、読み取られることはありません。
- **隔離場所。**
- `Runner` (したがって `run`) を介して、これは兄弟ディレクトリ `<out-dir>.quarantine/` です。これは `--out-dir` の外にあるため、 `salvage` または out-dir バンドルによって選択されることはありません。
- `quarantine_dir` がない単純な `Colab.download` または `scrub_local` は、ファイルをシステムの一時ディレクトリ (`tempfile.gettempdir()`) の新しいフォルダー `colab-runner-quarantine-*` に移動します。ファイルは、ファイルがダウンロードされたフォルダー内に配置されることはないため、ファイルに隣接して配置されることはありません。そのパスは `BinaryDownloadRefused.quarantined` にあります。 `Colab(quarantine_dir=...)` を渡して、場所を選択します。
- すべての隔離パスは、移動前に解決され、隔離フォルダー内に留まるようにチェックされます。そうでない場合、ファイルは削除され、ダウンロードは `QuarantineEscape` (`QUARANTINE_ESCAPE`) で失敗します。
- ランナーは隔離を削除しません。**レーンは隔離を削除し、決して送信してはなりません。**

**Salvage（サルベージ）** は、Python-API フックのみです。 `Runner.run(..., salvage=callable)` は、停止前にすべての処理（ `done` を除く）で実行されます。CLI にはサルベージ機能はありません。

**価格設定。** ウィンドウの価格は、同梱されているライブラリのレートおよび上限関数を使用して、経過時間に対して `max(--max-rate, the highest stable window rate)` 倍で計算されます。 `--max-rate` を超える安定したレートは `RATE_ABOVE_MAX` をトリガーするため、実行を続けるすべてのウィンドウの価格は `--max-rate` になります。これは下限であり、緩い上限ではありません。カードの予想レートに設定します。緩い `--max-rate` は、上限を早期にトリガーします。アカウントの残高は記録され、ウィンドウの価格には影響しません。

## セキュリティと脅威モデル

**影響を受けるもの。**
- レーンが名前を付ける Colab セッション：セルのファイルをアップロードし、開始したジョブを開始および終了し、要求されたファイルをダウンロードし、セッションを停止します。別の実行が開始されたプロセスを信号で通知することはありません（ `CR_NOT_OURS` ）。
- rig 上の out ディレクトリ（ `--out-dir` ）とその兄弟である隔離領域 `<out-dir>.quarantine/` 。隔離ディレクトリがない単純な `Colab.download` は、システムの一時ディレクトリの下にある新しい `colab-runner-quarantine-*` フォルダを使用します。
- レーンが `--env` を使用して名前を付ける環境変数。これは、セルのファイル内の VM に渡されます。

**影響を受けないもの。** out ディレクトリ、その隔離領域、および一時隔離領域の外部にあるファイル。他の Colab セッション。アカウント設定、資格情報、または請求：アカウントのレートと残高は、 `colab` コマンドラインを介して読み取りますが、変更することはありません。

**ネットワーク。** Google Colab への唯一の通信は、 `colab` コマンドラインを介して行われます。colab-runner は、独自の接続を開きません。

**機密情報。** `--env NAME` として渡された機密情報は、すべてのコマンドラインから除外されます。すべての出力行、エラー行、トレースバック、およびダウンロードされたテキストファイルは、トークンが削除されます。トークンを含むバイナリファイルは、変更せずに隔離され、ダウンロードは拒否されます（上記）。レーンは隔離領域を削除し、決して送信しません。

**権限。** ユーザーが実行し、そのユーザーがすでに持っている `colab` ログインを使用して実行されます。昇格は必要ありません。

**テレメトリなし。** colab-runner は、データを収集または送信しません。

脆弱性の報告：[SECURITY.md](SECURITY.md) を参照してください。

## ここに記載されていないもの

バケット名、アカウント名、起動ラッパー、またはローカルパスはありません。呼び出し元は、それらをフラグまたは環境変数で渡します。テストでは、プレースホルダーを使用します。

## 検証

```bash
bash verify.sh
```

## ライセンス

MIT。 [LICENSE](LICENSE) を参照してください。

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
