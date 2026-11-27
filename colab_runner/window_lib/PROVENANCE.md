# window_lib: vendored and trimmed

`colab_window.sh` (4,139 lines) and `remote_runner.py` (177 lines) were deleted, and `launch_guards.py`
was trimmed to only the rate, cap, wall, SHA-normalization, OOM, and token-scrub functions that
`colab-runner` invokes (`TOKEN_SCRUB_PATTERNS` and `scrub_colab_tokens` remain verbatim).

| File | sha256 |
|---|---|
| `launch_guards.py` | `dcc30f30dfb32ca3447b32fe93688b431ddcd430079b8d6984a0b13d5aa010b9` |

`tests/test_window_lib.py` verifies each file against this table and enforces Unix LF line endings.
