# window_lib: vendored and trimmed

`colab_window.sh` (4,139 lines) was deleted when `colab-runner` became the sole Colab launcher, and
`launch_guards.py` was trimmed to only the rate, cap, wall, SHA-normalization, OOM, and token-scrub
functions that `colab-runner` invokes (`TOKEN_SCRUB_PATTERNS` and `scrub_colab_tokens` remain verbatim).

| File | sha256 |
|---|---|
| `launch_guards.py` | `2313692a76cc3748903717f42713c6618863e312cfef3acadc9f17b7d34dbda6` |
| `remote_runner.py` | `393929ed704ccbacaee2b590010e348a5d635cbdd70361209d3e0103d1fba6eb` |

`tests/test_window_lib.py` verifies each file against this table and enforces Unix LF line endings.
