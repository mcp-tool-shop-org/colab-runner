<p align="center">
  <a href="README.md">English</a> | <a href="README.ja.md">日本語</a> | <a href="README.zh.md">中文</a> | <a href="README.es.md">Español</a> | <a href="README.fr.md">Français</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.pt-BR.md">Português (BR)</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/mcp-tool-shop-org/brand/main/logos/colab-runner/readme.png" alt="Colab Runner" width="400">
</p>

<p align="center">
  <a href="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml"><img src="https://github.com/mcp-tool-shop-org/colab-runner/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://mcp-tool-shop-org.github.io/colab-runner/"><img src="https://img.shields.io/badge/Landing_Page-live-blue" alt="Landing Page"></a>
</p>

Run a script on a paid Google Colab GPU from your own machine, with a hard spending limit, and get the results
back. You describe the job in a small file; colab-runner opens the session, runs the job, brings the outputs home,
stops the session and writes down what it cost.

## What it does

- **One spec file per job.** Inputs, setup and run commands, outputs, GPU type, spending cap and time limit, all in
  `colab-window.toml`. No per-job launch scripts.
- **A spending limit you set.** The window is priced as rate × elapsed time on your machine. When it reaches the cap
  or the time limit, the job is stopped and the session closed.
- **Try it before you pay.** `--dry` checks the spec with no Colab calls. `--local-cpu` runs the whole job on your
  machine, the same way it runs on the VM.
- **The session is always stopped.** Every way a run can end tries to stop the session. If your machine dies
  mid-run, `stop --out-dir` closes it later.
- **Tokens stay out of the results.** Logs and downloaded files are scrubbed of access tokens before they land.
- **A record of every run.** `outcome.json` in the output folder says how the run ended and what it cost.

## Install

colab-runner is not on PyPI. Install it from a release tag:

```bash
pip install "git+https://github.com/mcp-tool-shop-org/colab-runner@v0.2.0"
```

You need Linux or WSL, Python 3.11 or later, and Google's `colab` command line, signed in to the account that pays. colab-runner
has no other dependencies.

## Quick start

Work inside your project's git repository: files outside it count as protected data and are refused. Write a spec (a full one is in [examples/merchant_league_seats](examples/merchant_league_seats/README.md)):

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

`--local-cpu` runs the setup and job commands on your machine, so it needs whatever they call (here pip:
`sudo apt install python3-pip` on a fresh Ubuntu or WSL). Then check it, rehearse it, and run it:

```bash
python -m colab_runner launch --spec colab-window.toml --out-dir runs/try --dry
python -m colab_runner launch --spec colab-window.toml --out-dir runs/cpu --local-cpu
python -m colab_runner launch --spec colab-window.toml --out-dir runs/w1 --cap 5 --max-rate 2.5
python -m colab_runner status --out-dir runs/w1
```

The outputs land in `runs/w1/collected/`, with `outcome.json` beside them. A live launch needs `--cap` and
`--max-rate` on the command line, so a paid run is always a deliberate choice.

## When a run ends

| Exit | Meaning |
| --- | --- |
| 0 | The job finished, the outputs came back, and the session is stopped. |
| 1 | Refused before anything was opened: fix the spec or the flags (the message says what). |
| 2 | The run ended early (job failed, cap, time limit, stalled or unreachable). The session is stopped; if the stop failed, the message says to run `stop`. |
| 3 | The job finished, but the stop was not confirmed. Run `python -m colab_runner stop --out-dir D`. |

Every refusal and error prints a code, a message and a hint. `python -m colab_runner --help` lists the commands, and
`launch --help` every flag.

**More detail:** attaching to a session you opened yourself, what the job should write so progress is visible, how
downloads are scrubbed, and pricing are all in [docs/reference.md](docs/reference.md).

## Security and threat model

**What it touches.**
- The Colab session you name: it uploads cell files, starts and kills the job it started, downloads the files
  it was asked for, and stops the session. It never sends a signal to a process that belongs to another run of colab-runner (`CR_NOT_OURS`).
- The out dir (`--out-dir`) and its sibling quarantine `<out-dir>.quarantine/` on your machine. A bare `Colab.download`
  with no quarantine dir uses a new `colab-runner-quarantine-*` folder under the system temp dir.
- The environment variables you name with `--env`, which it passes to the VM inside the cell file.

**What it does not touch.** No files outside the out dir, its quarantine and the temp quarantine. No other Colab
session. No account settings, credentials or billing: it reads the account's rate and balance through the `colab`
command line and never changes them.

**Network.** Only through the `colab` command line, to Google Colab. colab-runner opens no connection of its own.

**Secrets.** A secret passed as `--env NAME` stays off every command line. Every output line, error line, traceback
and downloaded text file is scrubbed of tokens; a binary file with a token in it is quarantined unchanged and the
download is refused (above). colab-runner never deletes the quarantine: you must delete it, and never ship it.

**Permissions.** It runs as the user who runs it, with the `colab` sign-in that user already has. It needs no
elevation.

**No telemetry.** colab-runner collects and sends nothing.

Reporting a vulnerability: see [SECURITY.md](SECURITY.md).

## What is not here

No bucket names, account names, launch wrappers or local paths. A caller passes those by flag or
environment variable; tests use placeholders.

## Verify

```bash
bash verify.sh
```

## Licence

MIT. See [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://mcp-tool-shop.github.io/">MCP Tool Shop</a></p>
