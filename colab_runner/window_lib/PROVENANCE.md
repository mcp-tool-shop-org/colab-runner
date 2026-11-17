# window_lib: vendored, byte for byte

These three files are the Colab window library the studio's reviewed launch kits source (cap and RAM guard, bounded
exec and download, salvage after a failed window, the usage and rate reads, the token scrub, the refusal log). They are
copied unchanged. Do not edit them here: a change belongs upstream in the kit that owns them, and is vendored again with
a new row below.

| File | sha256 |
|---|---|
| `colab_window.sh` | `d147ba95ade4ff7b17128a27750927d7092a143151841e0147fa7ecd33453ecf` |
| `launch_guards.py` | `2998b4f8e3aea3aea2ecc9f779c743b4b44824b30e7ff55fb1da782d7a416c69` |
| `remote_runner.py` | `393929ed704ccbacaee2b590010e348a5d635cbdd70361209d3e0103d1fba6eb` |

Lineage: the library the Ripple kit introduced, with the parallel-window rate fix (polyglot #80: the window's own rate is
attributed from the pre-open assignment count plus one, never from the account balance). Taken from the reviewed
aspire-colab kit at the PASS shas above, 2026-11-12.

`tests/test_window_lib.py` fails if any file here stops matching this table.
