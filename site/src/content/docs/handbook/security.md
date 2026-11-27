---
title: Security
description: The threat model, the token scrub, the binary rule and the quarantine.
sidebar:
  order: 4
---

## Threat model

**What it touches.**
- The Colab session the caller names: it uploads cell files, starts and kills the job it started, downloads the files
  it was asked for, and stops the session. It never sends a signal to a process that belongs to another run of colab-runner (`CR_NOT_OURS`).
- The out dir (`--out-dir`) and its sibling quarantine `<out-dir>.quarantine/` on the local machine. A bare `Colab.download`
  with no quarantine dir uses a new `colab-runner-quarantine-*` folder under the system temp dir.
- The environment variables the caller names with `--env`, which it passes to the VM inside the cell file.

**What it does not touch.** No files outside the declared `inputs.files` allowlist under `inputs.root` (during staging), the out dir, its quarantine, and the temp quarantine. Symlinks, NTFS junctions, `..` traversal, whole-path sealed or outside-repo `inputs.root` directories (including any directory without `.git` above the spec), and sealed/restricted paths (matching default patterns or caller `inputs.deny_globs` such as `staging*` or holdout names) are refused unless `[inputs.sealed]` verifies a read-only pre-registration file by SHA-256 and single unfenced declarations of `study`, `allowed_cards`, `allowed_host`, and `sealed_paths` (matched by resolved path against the pre-registration directory), authorized destination card/host, and `--allow-sealed` on live runs. No other Colab session. No account settings, credentials or billing: it reads the account's rate and balance through the `colab` command line and never changes them.

**Network.** Only through the `colab` command line, to Google Colab; colab-runner opens no network connection of its own.

**Secrets.** A secret passed as `--env NAME` stays off every command line. Every output line, error line, traceback
and downloaded text file is scrubbed of tokens; a binary file with a token in it is quarantined unchanged (or removed if
the move fails, still raising `BinaryDownloadRefused` with `.failures`/`.removed`) and the download is refused (below).
colab-runner never deletes the quarantine: the caller must delete it, and must never ship it.

**Permissions.** It runs as the user who runs it, with the `colab` sign-in that user already has. It needs no
elevation.

**No telemetry.** colab-runner collects and sends nothing.

Reporting a vulnerability: see [SECURITY.md](https://github.com/mcp-tool-shop-org/colab-runner/blob/main/SECURITY.md).

## The scrub and the quarantine

**Secrets & binary-safe download scrub.** Pass a secret as `--env NAME`, with NAME set in the caller's environment. `--env NAME=VALUE`
puts the value in the runner's own command line. Values reach the VM inside the uploaded notebook cell file, never as arguments to the `colab` CLI.
Every CLI output line and error line is scrubbed with the vendored library's `scrub_colab_tokens` and stripped of local home directory paths (`~`). Downloaded files and
directories (including partial transfers) are classified by content only (a NUL byte or failed strict UTF-8 decode is binary):
- Text files are scrubbed in place with `scrub_colab_tokens`.
- Clean binary files are left byte-identical. Unreadable files are removed.
- **The binary rule.** A binary file is scanned with byte twins of the library's token patterns.
  - On UTF-8 the twins match exactly what the text patterns match on the decoded text. Every character Python's `\s` matches
    counts as a space, `\x1c`-`\x1f`, NBSP, NEL and EM SPACE included.
  - The one difference is next to a non-ASCII letter, where the `hf_` twin matches and the text pattern does not. So the
    binary rule can catch more than the text rule, never less.
  - One rule is stricter than text: in a binary, `Bearer` counts only when the value after it is token-shaped. That means
    at least 20 characters of `[A-Za-z0-9._-]`, using at least two of lower case, upper case and digits.
  - So a render whose prompt says `Flame Bearer of the North` comes through byte-identical. A real bearer token (an OAuth
    access token, a JWT, a Hugging Face token) is caught.
  - Text files keep the library's patterns unchanged: the same prose in a text file still becomes `Bearer [REDACTED] the
    North`.
- **A hit is never rewritten.**
  - The file is moved unchanged to quarantine, logged and named in `runner_result.json` (`quarantined`) by path and pattern
    name only.
  - The download is refused with `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). That ends `Runner.run` as `error` (CLI
    exit 2), and the session is still stopped.
- **A directory download** is scrubbed of tokens file by file, and one failure never stops the rest: a hit whose move to quarantine
  fails is removed, and a subdirectory that cannot be listed is noted. The first such error is raised once all the remaining
  files have been scrubbed. Only regular files are read. A symlink, to a file or to a directory, and an NTFS junction
  (found on every supported Python, 3.11 included), is unlinked, never followed, and any other entry that is not a regular file or a directory (a FIFO, a socket, a device) is unlinked, never
  read.
- **Where the quarantine is.**
  - Through `Runner` (and so `run`), it is the sibling directory `<out-dir>.quarantine/`. It is outside `--out-dir`, so
    neither `salvage` nor out-dir bundling picks it up.
  - A bare `Colab.download` or `scrub_local` with no `quarantine_dir` moves the file to a new folder
    `colab-runner-quarantine-*` under the system temp dir (`tempfile.gettempdir()`). It never lands next to the file, so
    never inside the folder the file was downloaded into. Its path is in `BinaryDownloadRefused.quarantined`. Pass
    `Colab(quarantine_dir=...)` to choose the place.
  - Every quarantine path is resolved and checked to stay inside its quarantine folder before the move. A file that
    would land outside it is removed instead, and the download fails with `QuarantineEscape` (`QUARANTINE_ESCAPE`).
  - colab-runner never deletes a quarantine. **The caller must delete it and must never ship it.**
