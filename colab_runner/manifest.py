"""Declarative Colab window specification (`WindowSpec`), strict input sandbox, sealed-data gate, and VM bundle builder."""

from __future__ import annotations

import fnmatch
import io
import json
import shlex
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Optional

from colab_runner.cards import CardSpec, validate_card_and_shape, vm_card_check_py
from colab_runner.colab_cli import _is_link
from colab_runner.errors import Refused
from colab_runner.runner import require_number
from colab_runner.window_lib import launch_guards as lg

SEALED_PATH_PATTERNS = ("sealed*", "*sealed*", "*bakeoff*", "restricted*")


@dataclass(frozen=True)
class InputFileSpec:
    """One explicitly allowed local file to stage and upload into the VM work directory."""

    src: str
    dst: str


@dataclass(frozen=True)
class StagedInputRecord:
    """Verified staged input metadata (relative path, size, and SHA-256 digest)."""

    src: str
    dst: str
    bytes: int
    sha256: str


@dataclass
class WindowSpec:
    """Parsed declarative window specification loaded from a TOML or JSON file."""

    schema_version: int
    repo: str
    study: str
    job: str
    description: str
    card: str
    shape: Optional[str]
    max_rate: Optional[float]
    cap: float
    wall_s: float
    margin: float = 0.0
    cleanup_bound_s: Optional[float] = None
    poll_s: float = 30.0
    setup_stall_s: float = 600.0
    progress_stall_s: float = 900.0
    unreachable_polls: int = 10
    unreachable_s: float = 1800.0
    resident: bool = False
    omp_num_threads: int = 4
    min_vram_mib: Optional[int] = None
    inputs_root: Path = Path(".")
    allow_sealed: bool = False
    preregistration: str = ""
    deny_globs: tuple[str, ...] = ()
    input_files: tuple[InputFileSpec, ...] = ()
    setup_commands: tuple[str, ...] = ()
    run_commands: tuple[str, ...] = ()
    cpu_dry_run_commands: tuple[str, ...] = ()
    progress_glob: str = ""
    units_glob: str = ""
    env: tuple[str, ...] = ()
    collect_outputs: tuple[str, ...] = ()
    salvage_outputs: tuple[str, ...] = ()
    spec_path: Optional[Path] = None

    @classmethod
    def from_file(cls, path: Path | str) -> "WindowSpec":
        """Load and validate a `.toml` or `.json` window spec without opening any external paths."""
        spec_path = Path(path)
        if not spec_path.is_file():
            raise Refused("SPEC_NOT_FOUND", "window spec does not exist: %s" % spec_path)
        raw_text = spec_path.read_text(encoding="utf-8")
        try:
            if spec_path.suffix.lower() == ".json":
                data = json.loads(raw_text)
            else:
                data = tomllib.loads(raw_text)
        except (ValueError, tomllib.TOMLDecodeError) as exc:
            raise Refused("SPEC_INVALID", "could not parse %s (%s)" % (spec_path, exc)) from None
        if not isinstance(data, dict):
            raise Refused("SPEC_INVALID", "%s must decode to a table/object" % spec_path)
        return cls.from_mapping(data, base_dir=spec_path.resolve().parent, spec_path=spec_path.resolve())

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        base_dir: Path,
        spec_path: Optional[Path] = None,
    ) -> "WindowSpec":
        """Construct a `WindowSpec` from an in-memory mapping, resolving `inputs.root` from `base_dir`."""
        schema_version = data.get("schema_version", 1)
        if schema_version != 1:
            raise Refused("SPEC_INVALID", "schema_version must be 1 (got %r)" % (schema_version,))

        proj = _require_table(data, "project")
        comp = _require_table(data, "compute")
        inp = data.get("inputs", {})
        if not isinstance(inp, Mapping):
            raise Refused("SPEC_INVALID", "inputs must be a table/object")
        job_tbl = _require_table(data, "job")
        out_tbl = data.get("outputs", {})
        if not isinstance(out_tbl, Mapping):
            raise Refused("SPEC_INVALID", "outputs must be a table/object")

        repo = _require_nonempty_str(proj, "repo", "project.repo")
        study = _require_nonempty_str(proj, "study", "project.study")
        job = _require_nonempty_str(proj, "job", "project.job")
        description = str(proj.get("description", "")).strip()

        card = _require_nonempty_str(comp, "card", "compute.card")
        raw_shape = comp.get("shape")
        shape = str(raw_shape).strip().lower() if isinstance(raw_shape, str) and raw_shape.strip() else None

        card_spec, clean_shape = validate_card_and_shape(card, shape)

        raw_max_rate = comp.get("max_rate")
        if raw_max_rate is None and card_spec.card_class == "cpu":
            raw_max_rate = card_spec.default_rate
        require_number("MAX_RATE_INVALID", "compute.max_rate", raw_max_rate)
        require_number("CAP_INVALID", "compute.cap", comp.get("cap"))
        require_number("WALL_INVALID", "compute.wall_s", comp.get("wall_s"))

        margin = comp.get("margin", 0.0)
        require_number("MARGIN_INVALID", "compute.margin", margin, inclusive=True)

        cleanup_bound_s = comp.get("cleanup_bound_s")
        if cleanup_bound_s is not None:
            require_number("CLEANUP_BOUND_INVALID", "compute.cleanup_bound_s", cleanup_bound_s, inclusive=True)

        poll_s = comp.get("poll_s", 30.0)
        require_number("POLL_INVALID", "compute.poll_s", poll_s)
        setup_stall_s = comp.get("setup_stall_s", 600.0)
        require_number("SETUP_STALL_INVALID", "compute.setup_stall_s", setup_stall_s)
        progress_stall_s = comp.get("progress_stall_s", 900.0)
        require_number("PROGRESS_STALL_INVALID", "compute.progress_stall_s", progress_stall_s)
        unreachable_polls = comp.get("unreachable_polls", 10)
        unreachable_s = comp.get("unreachable_s", 1800.0)
        require_number("UNREACHABLE_INVALID", "compute.unreachable_polls", unreachable_polls,
                       low=1, inclusive=True, integer=True)
        require_number("UNREACHABLE_INVALID", "compute.unreachable_s", unreachable_s)

        resident = bool(comp.get("resident", False))
        omp_num_threads = comp.get("omp_num_threads", 4)
        if not isinstance(omp_num_threads, int) or isinstance(omp_num_threads, bool) or omp_num_threads < 1:
            raise Refused("SPEC_INVALID", "compute.omp_num_threads must be a positive integer")
        min_vram_mib = comp.get("min_vram_mib")
        if min_vram_mib is not None:
            if not isinstance(min_vram_mib, int) or isinstance(min_vram_mib, bool) or min_vram_mib < 0:
                raise Refused("SPEC_INVALID", "compute.min_vram_mib must be a non-negative integer")

        raw_root = str(inp.get("root", ".")).strip() or "."
        inputs_root = (base_dir / raw_root) if not Path(raw_root).is_absolute() else Path(raw_root)
        allow_sealed = bool(inp.get("allow_sealed", False))
        prereg = str(inp.get("preregistration", "")).strip()
        deny_globs = tuple(_string_list(inp.get("deny_globs", []), "inputs.deny_globs"))
        input_files = _parse_input_files(inp.get("files", []))

        setup_cmds = tuple(_string_list(job_tbl.get("setup", []), "job.setup"))
        run_cmds = tuple(_string_list(job_tbl.get("run", []), "job.run"))
        if not run_cmds:
            raise Refused("SPEC_INVALID", "job.run must list at least one command")
        cpu_cmds = tuple(_string_list(job_tbl.get("cpu_dry_run", []), "job.cpu_dry_run"))
        progress_glob = str(job_tbl.get("progress_glob", "")).strip()
        units_glob = str(job_tbl.get("units_glob", "")).strip()
        env = tuple(_string_list(job_tbl.get("env", []), "job.env"))

        collect_outputs = tuple(_validate_output_relpaths(out_tbl.get("collect", []), "outputs.collect"))
        salvage_outputs = tuple(_validate_output_relpaths(out_tbl.get("salvage", []), "outputs.salvage"))

        return cls(
            schema_version=schema_version,
            repo=repo,
            study=study,
            job=job,
            description=description,
            card=card_spec.name,
            shape=clean_shape,
            max_rate=float(raw_max_rate),
            cap=float(comp["cap"]),
            wall_s=float(comp["wall_s"]),
            margin=float(margin),
            cleanup_bound_s=float(cleanup_bound_s) if cleanup_bound_s is not None else None,
            poll_s=float(poll_s),
            setup_stall_s=float(setup_stall_s),
            progress_stall_s=float(progress_stall_s),
            unreachable_polls=int(unreachable_polls),
            unreachable_s=float(unreachable_s),
            resident=resident,
            omp_num_threads=omp_num_threads,
            min_vram_mib=min_vram_mib,
            inputs_root=inputs_root,
            allow_sealed=allow_sealed,
            preregistration=prereg,
            deny_globs=deny_globs,
            input_files=input_files,
            setup_commands=setup_cmds,
            run_commands=run_cmds,
            cpu_dry_run_commands=cpu_cmds,
            progress_glob=progress_glob,
            units_glob=units_glob,
            env=env,
            collect_outputs=collect_outputs,
            salvage_outputs=salvage_outputs,
            spec_path=spec_path,
        )


def _require_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = data.get(key)
    if not isinstance(val, Mapping):
        raise Refused("SPEC_INVALID", "spec section [%s] must be a table/object" % key)
    return val


def _require_nonempty_str(tbl: Mapping[str, Any], key: str, field_name: str) -> str:
    val = tbl.get(key)
    if not isinstance(val, str) or not val.strip():
        raise Refused("SPEC_INVALID", "%s must be a non-empty string" % field_name)
    return val.strip()


def _string_list(raw: Any, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise Refused("SPEC_INVALID", "%s must be a list of strings" % field_name)
    return [x for x in raw if x]


def _raise_rel_path(is_output: bool, msg: str) -> None:
    if is_output:
        raise Refused("OUTPUT_PATH_INVALID", msg)
    raise Refused("INPUT_PATH_OUTSIDE_ROOT", msg)


def _check_rel_path(raw: str, is_output: bool, field_name: str) -> str:
    cleaned = raw.strip().replace("\\", "/")
    win = PureWindowsPath(cleaned)
    pos = PurePosixPath(cleaned)
    if not cleaned or cleaned.startswith("/") or win.drive or pos.is_absolute():
        _raise_rel_path(is_output, "%s must be a non-empty relative path (got %r)" % (field_name, raw))
    if any(part in ("..", "") for part in pos.parts) or "." in pos.parts:
        _raise_rel_path(is_output, "%s must stay strictly relative without '.' or '..' (got %r)" % (field_name, raw))
    return cleaned


def _parse_input_files(raw: Any) -> tuple[InputFileSpec, ...]:
    if not isinstance(raw, list):
        raise Refused("SPEC_INVALID", "inputs.files must be a list of paths or {src, dst} tables")
    items: list[InputFileSpec] = []
    for idx, entry in enumerate(raw):
        if isinstance(entry, str):
            rel = _check_rel_path(entry, False, "inputs.files[%d]" % idx)
            items.append(InputFileSpec(src=rel, dst=rel))
        elif isinstance(entry, Mapping):
            src = entry.get("src")
            dst = entry.get("dst", src)
            if not isinstance(src, str) or not isinstance(dst, str):
                raise Refused("SPEC_INVALID", "inputs.files[%d] requires string src/dst" % idx)
            items.append(InputFileSpec(
                src=_check_rel_path(src, False, "inputs.files[%d].src" % idx),
                dst=_check_rel_path(dst, False, "inputs.files[%d].dst" % idx),
            ))
        else:
            raise Refused("SPEC_INVALID", "inputs.files[%d] must be a string or {src, dst} table" % idx)
    return tuple(items)


def _validate_output_relpaths(raw: Any, field_name: str) -> list[str]:
    items = _string_list(raw, field_name)
    return [_check_rel_path(item, True, field_name) for item in items]


def _check_sealed_segments(rel_path: str, *, deny_globs: tuple[str, ...],
                           allow_sealed: bool, preregistration: str) -> None:
    if allow_sealed and preregistration.strip():
        return
    patterns = SEALED_PATH_PATTERNS + deny_globs
    for seg in PurePosixPath(rel_path).parts:
        low = seg.lower()
        for pat in patterns:
            if fnmatch.fnmatchcase(low, pat.lower()):
                raise Refused(
                    "SEALED_DATA_REFUSED",
                    "path %r matches sealed/restricted pattern %r without allow_sealed + preregistration"
                    % (rel_path, pat),
                )


def _check_no_links(root: Path, rel: str) -> Path:
    cur = root
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        if _is_link(cur):
            raise Refused("INPUT_SYMLINK_REFUSED", "input path %r traverses a symlink or junction at %s" % (rel, cur))
    return cur


def stage_inputs(
    spec: WindowSpec,
    bundle_path: Path,
) -> list[StagedInputRecord]:
    """Validate `spec.input_files` strictly within `spec.inputs_root` and pack a deterministic `.tar.gz` bundle."""
    root_raw = Path(spec.inputs_root)
    if _is_link(root_raw) or not root_raw.exists() or not root_raw.is_dir():
        raise Refused("INPUT_ROOT_INVALID", "inputs.root is missing, a link, or not a directory: %s" % root_raw)
    root = root_raw.resolve()

    staged: list[StagedInputRecord] = []
    seen_dst: set[str] = set()
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        for item in spec.input_files:
            _check_sealed_segments(
                item.src, deny_globs=spec.deny_globs,
                allow_sealed=spec.allow_sealed, preregistration=spec.preregistration,
            )
            _check_sealed_segments(
                item.dst, deny_globs=spec.deny_globs,
                allow_sealed=spec.allow_sealed, preregistration=spec.preregistration,
            )
            if item.dst in seen_dst:
                raise Refused("SPEC_INVALID", "duplicate staged destination path %r in inputs.files" % item.dst)
            seen_dst.add(item.dst)

            cand = _check_no_links(root, item.src)
            resolved = cand.resolve()
            if not resolved.is_relative_to(root):
                raise Refused(
                    "INPUT_PATH_OUTSIDE_ROOT",
                    "input file %r resolves outside inputs.root (%s)" % (item.src, resolved),
                )
            if not resolved.is_file():
                raise Refused("INPUT_FILE_MISSING", "input file does not exist as a regular file: %s" % cand)

            data = resolved.read_bytes()
            digest = lg.sha256_file(resolved)
            info = tarfile.TarInfo(name=item.dst)
            info.size = len(data)
            info.mode = 0o755 if item.dst.endswith(".sh") else 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tf.addfile(info, io.BytesIO(data))
            staged.append(StagedInputRecord(src=item.src, dst=item.dst, bytes=len(data), sha256=digest))
    return staged


def render_vm_script(
    spec: WindowSpec,
    card_spec: CardSpec,
    *,
    bundle_filename: str,
) -> str:
    """Render the self-contained bash script uploaded into `/content` for a window run."""
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"export OMP_NUM_THREADS={spec.omp_num_threads}",
        'export CR_WORK_DIR="${CR_CONTENT%/}/${CR_JOB}_work"',
        'mkdir -p "$CR_OUT_DIR" "$CR_OUT_DIR/progress" "$CR_WORK_DIR"',
        'printf \'setup\\n\' > "$CR_OUT_DIR/phase.tmp" && mv "$CR_OUT_DIR/phase.tmp" "$CR_OUT_DIR/phase.txt"',
        f'echo "[setup] job={spec.job} card={card_spec.name} omp=$OMP_NUM_THREADS" >> "$CR_OUT_DIR/setup.log"',
    ]
    if spec.input_files:
        quoted_bundle = shlex.quote(bundle_filename)
        lines.extend([
            f'tar -xzf "${{CR_CONTENT%/}}/{quoted_bundle}" -C "$CR_WORK_DIR" >> "$CR_OUT_DIR/setup.log" 2>&1',
            f'echo "[setup] unpacked {len(spec.input_files)} input files into $CR_WORK_DIR" >> "$CR_OUT_DIR/setup.log"',
        ])
    if card_spec.card_class == "gpu":
        check_src = vm_card_check_py(card_spec, min_vram_mib=spec.min_vram_mib)
        lines.extend([
            'if [ "${CR_DRY_RUN:-0}" != "1" ]; then',
            '  rc=0',
            '  python3 - <<\'CR_VM_CARD_EOF\' >> "$CR_OUT_DIR/setup.log" 2>&1 || rc=$?',
            check_src.rstrip("\n"),
            "CR_VM_CARD_EOF",
            '  if [ "$rc" -ne 0 ]; then',
            '    exit "$rc"',
            '  fi',
            'fi',
        ])
    if spec.setup_commands:
        lines.append('cd "$CR_WORK_DIR"')
        for cmd in spec.setup_commands:
            lines.append(f"({cmd}) >> \"$CR_OUT_DIR/setup.log\" 2>&1")
    lines.extend([
        'printf \'run\\n\' > "$CR_OUT_DIR/phase.tmp" && mv "$CR_OUT_DIR/phase.tmp" "$CR_OUT_DIR/phase.txt"',
        'cd "$CR_WORK_DIR"',
    ])
    if spec.cpu_dry_run_commands:
        lines.append('if [ "${CR_DRY_RUN:-0}" = "1" ]; then')
        for cmd in spec.cpu_dry_run_commands:
            lines.append(f"  {cmd}")
        lines.append("else")
        for cmd in spec.run_commands:
            lines.append(f"  {cmd}")
        lines.append("fi")
    else:
        for cmd in spec.run_commands:
            lines.append(cmd)
    return "\n".join(lines) + "\n"


__all__ = [
    "InputFileSpec",
    "SEALED_PATH_PATTERNS",
    "StagedInputRecord",
    "WindowSpec",
    "render_vm_script",
    "stage_inputs",
]
