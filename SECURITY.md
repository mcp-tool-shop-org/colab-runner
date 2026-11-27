# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
| < 0.1   | No (never released) |

## Reporting a vulnerability

Email: **64996768+mcp-tool-shop@users.noreply.github.com**

Include:
- a description of the vulnerability;
- steps to reproduce;
- the version affected;
- the potential impact.

Please do not open a public issue for a vulnerability, and never paste a real Colab or Hugging Face token into a report.

### Response timeline

| Action | Target |
|--------|--------|
| Acknowledge the report | 48 hours |
| Assess severity | 7 days |
| Release a fix | 30 days |

## Scope

colab-runner runs on the operator's machine and drives a Google Colab session through the `colab` command line.

- **Data touched:** the Colab session the caller names (cell files it uploads, the job it starts, the files it is asked
  to download); the caller's `--out-dir` and its sibling `<out-dir>.quarantine/`; a `colab-runner-quarantine-*` folder
  under the system temp dir when a bare download has no quarantine dir; the environment variables passed with `--env`.
- **Data not touched:** any other file, any other Colab session, and account settings, credentials or billing (the
  account's rate and balance are read, never changed).
- **Network:** only through the `colab` command line, to Google Colab. colab-runner opens no connection of its own.
- **Secrets:** it carries the secrets a caller passes with `--env` to the VM, inside the cell file and never as an
  argument to the `colab` command line. Pass a secret as `--env NAME` (its value read from the environment) to keep it
  off every command line; `--env NAME=VALUE` puts the value on colab-runner's own command line on the local machine. Every output line, error line, traceback and downloaded text file is scrubbed of tokens. A binary file
  holding a token is quarantined unchanged and the download is refused (`BINARY_DOWNLOAD_TOKEN`). The quarantine is
  the caller's to delete and must never be shipped.
- **Permissions:** it runs as the invoking user with that user's existing `colab` sign-in. No elevation.
- **No telemetry** is collected or sent.

In scope: a token that survives the scrub into a kept file or a log line, a quarantine move that lands outside its
quarantine folder, a kill that reaches a job another run started, and a window that keeps billing after the runner
reports a confirmed stop.
