"""Declarative Colab window specification (`WindowSpec`), strict input sandbox, verified sealed-data gate, and VM bundle."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import stat
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Optional

from colab_runner.cards import CardSpec, resolve_card, validate_card_and_shape, vm_card_check_py
from colab_runner.colab_cli import _is_link
from colab_runner.errors import Refused
from colab_runner.runner import require_number
from colab_runner.window_lib import launch_guards as lg

SEALED_PATH_PATTERNS = ("sealed*", "*sealed*", "*bakeoff*", "restricted*")
JOB_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PREREG_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
PREREG_FIELD_RE = re.compile(
    r"^ {0,3}(?:[-*] +)?(study|allowed[_ ]cards|allowed[_ ]host|sealed[_ ]paths?)\s*[:=]\s*(.+?)\s*$",
    re.I,
)
DEFAULT_SEALED_HOST = "colab.google.com"


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


@dataclass(frozen=True)
class SealedPolicySpec:
    """Manifest declaration binding sealed or outside-repo inputs to a read-only pre-registration and destination."""

    preregistration_path: Path
    preregistration_sha256: str
    allowed_cards: tuple[str, ...]
    allowed_host: str


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
    min_vram_mib_by_card: dict[str, int] = field(default_factory=dict)
    inputs_root: Path = Path(".")
    deny_globs: tuple[str, ...] = ()
    sealed: Optional[SealedPolicySpec] = None
    input_files: tuple[InputFileSpec, ...] = ()
    setup_commands: tuple[str, ...] = ()
    run_commands: tuple[str, ...] = ()
    cpu_dry_run_commands: tuple[str, ...] = ()
    progress_glob: str = ""
    units_glob: str = ""
    env: tuple[str, ...] = ()
    collect_outputs: tuple[str, ...] = ()
    salvage_outputs: tuple[str, ...] = ()
    spec_base_dir: Path = Path(".")
    spec_path: Optional[Path] = None

    def vram_floor_for(self, card_spec: CardSpec) -> int:
        """Return the VRAM floor in MiB for `card_spec`, preferring per-card overrides then matching catalog floor."""
        if card_spec.name in self.min_vram_mib_by_card:
            return self.min_vram_mib_by_card[card_spec.name]
        if self.min_vram_mib is not None and card_spec.name == self.card:
            return self.min_vram_mib
        return card_spec.min_vram_mib

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
        if not JOB_NAME_RE.fullmatch(job):
            raise Refused("SPEC_INVALID", "project.job must match [A-Za-z0-9_-]+ (got %r)" % (job,))
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
        min_vram_by_card = _parse_vram_by_card(comp.get("min_vram_mib_by_card"))

        if "allow_sealed" in inp or "preregistration" in inp:
            raise Refused(
                "SPEC_INVALID",
                "inputs.allow_sealed and inputs.preregistration were replaced by [inputs.sealed] with path + sha256",
            )
        raw_root = str(inp.get("root", ".")).strip() or "."
        inputs_root = (base_dir / raw_root) if not Path(raw_root).is_absolute() else Path(raw_root)
        deny_globs = tuple(_string_list(inp.get("deny_globs", []), "inputs.deny_globs"))
        sealed = _parse_sealed_policy(inp.get("sealed"), base_dir=base_dir)
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
            min_vram_mib_by_card=min_vram_by_card,
            inputs_root=inputs_root,
            deny_globs=deny_globs,
            sealed=sealed,
            input_files=input_files,
            setup_commands=setup_cmds,
            run_commands=run_cmds,
            cpu_dry_run_commands=cpu_cmds,
            progress_glob=progress_glob,
            units_glob=units_glob,
            env=env,
            collect_outputs=collect_outputs,
            salvage_outputs=salvage_outputs,
            spec_base_dir=base_dir.resolve(),
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


def _parse_vram_by_card(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise Refused("SPEC_INVALID", "compute.min_vram_mib_by_card must be a table/object")
    out: dict[str, int] = {}
    for card_name, val in raw.items():
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise Refused("SPEC_INVALID", "compute.min_vram_mib_by_card[%r] must be a non-negative integer" % (card_name,))
        out[resolve_card(str(card_name)).name] = val
    return out


def _has_glob_chars(text: str) -> bool:
    return any(ch in text for ch in "*?[]{}")


def _parse_sealed_policy(raw: Any, *, base_dir: Path) -> Optional[SealedPolicySpec]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise Refused("SPEC_INVALID", "inputs.sealed must be a table/object")
    p_raw = _require_nonempty_str(raw, "preregistration_path", "inputs.sealed.preregistration_path")
    sha_raw = lg.normalize_hex_sha(_require_nonempty_str(raw, "preregistration_sha256", "inputs.sealed.preregistration_sha256"))
    if not SHA256_RE.fullmatch(sha_raw):
        raise Refused("SEALED_PREREG_INVALID", "inputs.sealed.preregistration_sha256 must be a 64-char hex sha256")
    cards_raw = _string_list(raw.get("allowed_cards"), "inputs.sealed.allowed_cards")
    if not cards_raw:
        raise Refused("SEALED_DESTINATION_REFUSED", "inputs.sealed.allowed_cards must list at least one destination card")
    allowed_cards = tuple(resolve_card(c).name for c in cards_raw)
    host_raw = _require_nonempty_str(raw, "allowed_host", "inputs.sealed.allowed_host").lower()
    if _has_glob_chars(host_raw):
        raise Refused("SEALED_DESTINATION_REFUSED", "inputs.sealed.allowed_host cannot contain glob wildcards")
    prereg_path = (base_dir / p_raw) if not Path(p_raw).is_absolute() else Path(p_raw)
    return SealedPolicySpec(
        preregistration_path=prereg_path,
        preregistration_sha256=sha_raw,
        allowed_cards=allowed_cards,
        allowed_host=host_raw,
    )


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


def _segments_match_sealed(parts: tuple[str, ...], *, deny_globs: tuple[str, ...]) -> Optional[str]:
    patterns = SEALED_PATH_PATTERNS + deny_globs
    for seg in parts:
        low = seg.lower()
        for pat in patterns:
            if fnmatch.fnmatchcase(low, pat.lower()):
                return pat
    return None


def _find_repo_root(start_dir: Path) -> Optional[Path]:
    """Return the enclosing git repository root above `start_dir`, or None when outside any repo."""
    resolved = start_dir.resolve()
    for cand in (resolved, *resolved.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _is_readonly_file(path: Path) -> bool:
    mode = path.stat().st_mode
    if os.name == "posix":
        return (mode & 0o222) == 0
    return (mode & stat.S_IWRITE) == 0


def _strip_quotes(text: str) -> str:
    return text.strip().strip("`\"'")


def _split_structured_list(raw_val: str) -> list[str]:
    cleaned = raw_val.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    parts = [p for chunk in cleaned.split(",") for p in chunk.strip().split() if p]
    return [_strip_quotes(p) for p in parts if _strip_quotes(p)]


def _parse_preregistration(path: Path, raw_bytes: bytes) -> dict[str, Any]:
    """Parse structured pre-registration metadata (study, allowed_cards, allowed_host, sealed_paths) from bytes."""
    got_sha = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s is not valid UTF-8 (%s)" % (path, exc)) from None

    suf = path.suffix.lower()
    study_raw: Optional[Any] = None
    cards_raw: Optional[Any] = None
    host_raw: Optional[Any] = None
    paths_raw: Optional[Any] = None

    if suf == ".json":
        def _reject_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            seen: dict[str, Any] = {}
            for k, v in pairs:
                if k in seen:
                    raise Refused("SEALED_PREREG_INVALID", "duplicate key %r in JSON pre-registration %s" % (k, path))
                seen[k] = v
            return seen

        try:
            obj = json.loads(raw_text, object_pairs_hook=_reject_dup_keys)
        except Refused:
            raise
        except ValueError as exc:
            raise Refused("SEALED_PREREG_INVALID", "invalid JSON pre-registration %s (%s)" % (path, exc)) from None
        if not isinstance(obj, Mapping):
            raise Refused("SEALED_PREREG_INVALID", "JSON pre-registration %s must be an object" % path)
        study_raw = obj.get("study")
        cards_raw = obj.get("allowed_cards")
        host_raw = obj.get("allowed_host")
        paths_raw = obj.get("sealed_paths")
    elif suf == ".toml":
        try:
            obj = tomllib.loads(raw_text)
        except tomllib.TOMLDecodeError as exc:
            raise Refused("SEALED_PREREG_INVALID", "invalid TOML pre-registration %s (%s)" % (path, exc)) from None
        if not isinstance(obj, Mapping):
            raise Refused("SEALED_PREREG_INVALID", "TOML pre-registration %s must be a table" % path)
        study_raw = obj.get("study")
        cards_raw = obj.get("allowed_cards")
        host_raw = obj.get("allowed_host")
        paths_raw = obj.get("sealed_paths")
    else:
        found_paths: list[str] = []
        found_cards: list[str] = []
        seen_fields: set[str] = set()
        in_fence: Optional[tuple[str, int]] = None
        for line in raw_text.splitlines():
            if in_fence is not None:
                f_ch, f_len = in_fence
                stripped_fence = line.strip()
                if stripped_fence and len(stripped_fence) >= f_len and set(stripped_fence) == {f_ch}:
                    in_fence = None
                continue
            fm = PREREG_FENCE_RE.match(line)
            if fm is not None:
                f_run, f_rest = fm.group(1), fm.group(2)
                if not (f_run[0] == "`" and "`" in f_rest):
                    in_fence = (f_run[0], len(f_run))
                continue
            m = PREREG_FIELD_RE.match(line)
            if not m:
                continue
            key = m.group(1).strip().lower().replace(" ", "_")
            canon = "sealed_paths" if key in ("sealed_path", "sealed_paths") else key
            if canon in seen_fields:
                raise Refused("SEALED_PREREG_INVALID", "duplicate %s declaration in %s" % (canon, path))
            seen_fields.add(canon)
            val = m.group(2).strip()
            if key == "study":
                study_raw = _strip_quotes(val)
            elif key == "allowed_cards":
                found_cards = _split_structured_list(val)
            elif key == "allowed_host":
                host_raw = _strip_quotes(val).lower()
            elif key == "sealed_path":
                found_paths = [_strip_quotes(val)]
            elif key == "sealed_paths":
                found_paths = _split_structured_list(val)
        cards_raw = found_cards
        paths_raw = found_paths

    if not isinstance(study_raw, str) or not study_raw.strip() or any(ch.isspace() for ch in study_raw.strip()):
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s missing structured single-token study field" % path)
    study_val = study_raw.strip()

    if not isinstance(cards_raw, list) or not cards_raw or not all(isinstance(c, str) and c.strip() for c in cards_raw):
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s missing structured allowed_cards field" % path)
    try:
        allowed_cards = tuple(resolve_card(c).name for c in cards_raw)
    except Refused as exc:
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s invalid allowed_cards (%s)" % (path, exc.message)) from None

    if (
        not isinstance(host_raw, str)
        or not host_raw.strip()
        or any(ch.isspace() for ch in host_raw.strip())
        or _has_glob_chars(host_raw)
    ):
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s missing structured allowed_host field" % path)
    allowed_host = host_raw.strip().lower()

    if not isinstance(paths_raw, list) or not paths_raw:
        raise Refused("SEALED_PREREG_INVALID", "pre-registration %s declares no sealed_paths" % path)
    clean_paths: list[str] = []
    for entry in paths_raw:
        if not isinstance(entry, str) or not entry.strip():
            raise Refused("SEALED_PREREG_INVALID", "empty sealed path in pre-registration %s" % path)
        if _has_glob_chars(entry):
            raise Refused("SEALED_PREREG_INVALID", "glob patterns are forbidden in pre-registration paths: %r" % entry)
        clean_paths.append(_check_rel_path(entry, False, "sealed_path"))

    prereg_dir = path.resolve().parent
    resolved_paths = [str((prereg_dir / rel).resolve()) for rel in clean_paths]
    return {
        "sha256": got_sha,
        "study": study_val,
        "allowed_cards": allowed_cards,
        "allowed_host": allowed_host,
        "sealed_paths": clean_paths,
        "resolved_paths": resolved_paths,
    }


def _verify_sealed_gate(
    spec: WindowSpec,
    *,
    root: Path,
    resolved_items: list[tuple[InputFileSpec, Path]],
    actual_card: str,
    actual_host: str,
    live: bool,
    local_cpu: bool,
    cli_allow_sealed: bool,
) -> Optional[dict[str, Any]]:
    spec_repo_root = _find_repo_root(spec.spec_path.parent if spec.spec_path else spec.spec_base_dir)
    root_outside_repo = spec_repo_root is None or not root.is_relative_to(spec_repo_root)
    root_sealed_pat = _segments_match_sealed(root.parts, deny_globs=spec.deny_globs)

    sealed_entries: list[tuple[InputFileSpec, Path]] = []
    for item, resolved in resolved_items:
        pat_src = _segments_match_sealed(PurePosixPath(item.src).parts, deny_globs=spec.deny_globs)
        pat_dst = _segments_match_sealed(PurePosixPath(item.dst).parts, deny_globs=spec.deny_globs)
        if root_outside_repo or root_sealed_pat or pat_src or pat_dst:
            sealed_entries.append((item, resolved))

    needs_gate = bool(sealed_entries or root_outside_repo or root_sealed_pat or spec.sealed is not None)
    if not needs_gate:
        return None
    if spec.sealed is None:
        if spec_repo_root is None:
            why = "no .git repository above spec; inputs.root %s is treated as outside-repo without [inputs.sealed]" % root
        elif root_outside_repo:
            why = "inputs.root %s is outside the spec repository %s without [inputs.sealed]" % (root, spec_repo_root)
        elif root_sealed_pat:
            why = "inputs.root %s matches sealed pattern %r without [inputs.sealed]" % (root, root_sealed_pat)
        else:
            why = "manifest declares sealed path(s) %r without [inputs.sealed]" % ([it.src for it, _ in sealed_entries],)
        raise Refused("SEALED_DATA_REFUSED", why)

    prereg = spec.sealed.preregistration_path
    if _is_link(prereg) or not prereg.exists() or not prereg.is_file():
        raise Refused("SEALED_PREREG_INVALID", "pre-registration file missing or not a regular file: %s" % prereg)
    if not _is_readonly_file(prereg):
        raise Refused("SEALED_PREREG_INVALID", "pre-registration file must be read-only before launch: %s" % prereg)

    raw_bytes = prereg.read_bytes()
    parsed = _parse_preregistration(prereg, raw_bytes)
    got_sha = parsed["sha256"]
    if got_sha != spec.sealed.preregistration_sha256:
        raise Refused(
            "SEALED_PREREG_INVALID",
            "pre-registration sha256 mismatch for %s (got %s, expected %s)"
            % (prereg, got_sha, spec.sealed.preregistration_sha256),
        )
    if parsed["study"] != spec.study:
        raise Refused(
            "SEALED_PREREG_INVALID",
            "pre-registration %s authorizes study %r, not %r" % (prereg, parsed["study"], spec.study),
        )

    want_resolved = sorted({str(rpath.resolve()) for _, rpath in sealed_entries})
    prereg_resolved = sorted(set(parsed["resolved_paths"]))
    if want_resolved != prereg_resolved:
        raise Refused(
            "SEALED_PREREG_INVALID",
            "sealed input resolved paths %r do not match pre-registered paths %r" % (want_resolved, prereg_resolved),
        )

    eff_host = actual_host.strip().lower()
    if eff_host != spec.sealed.allowed_host or eff_host != parsed["allowed_host"]:
        raise Refused(
            "SEALED_DESTINATION_REFUSED",
            "host %r is not authorized by [inputs.sealed] and pre-registration %s" % (actual_host, prereg),
        )
    check_card = spec.card if (local_cpu and not live) else resolve_card(actual_card).name
    if (
        check_card not in spec.sealed.allowed_cards
        or check_card not in parsed["allowed_cards"]
        or not set(spec.sealed.allowed_cards).issubset(set(parsed["allowed_cards"]))
    ):
        raise Refused(
            "SEALED_DESTINATION_REFUSED",
            "card %s is not in authorized sealed destination cards %r" % (check_card, spec.sealed.allowed_cards),
        )

    if live and not cli_allow_sealed:
        raise Refused("ALLOW_SEALED_REQUIRED", "live launch with sealed inputs requires --allow-sealed on the CLI")

    return {
        "preregistration_path": str(prereg.resolve()),
        "preregistration_sha256": got_sha,
        "sealed_files": [item.src for item, _ in sealed_entries],
        "resolved_paths": want_resolved,
        "files": [],
        "destination": {
            "allowed_cards": list(spec.sealed.allowed_cards),
            "allowed_host": spec.sealed.allowed_host,
            "actual_card": resolve_card(actual_card).name,
            "actual_host": eff_host,
            "verified": True,
        },
        "allow_sealed_flag": bool(cli_allow_sealed),
    }


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
    *,
    actual_card: Optional[str] = None,
    actual_host: str = DEFAULT_SEALED_HOST,
    live: bool = False,
    local_cpu: bool = False,
    cli_allow_sealed: bool = False,
) -> tuple[list[StagedInputRecord], Optional[dict[str, Any]]]:
    """Validate `spec.input_files`, enforce the sealed pre-registration gate, and pack a deterministic `.tar.gz`."""
    root_raw = Path(spec.inputs_root)
    if _is_link(root_raw) or not root_raw.exists() or not root_raw.is_dir():
        raise Refused("INPUT_ROOT_INVALID", "inputs.root is missing, a link, or not a directory: %s" % root_raw)
    root = root_raw.resolve()

    resolved_items: list[tuple[InputFileSpec, Path]] = []
    seen_dst: set[str] = set()
    for item in spec.input_files:
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
        resolved_items.append((item, resolved))

    eff_card = actual_card or spec.card
    sealed_record = _verify_sealed_gate(
        spec,
        root=root,
        resolved_items=resolved_items,
        actual_card=eff_card,
        actual_host=actual_host,
        live=live,
        local_cpu=local_cpu,
        cli_allow_sealed=cli_allow_sealed,
    )
    sealed_resolved_set = set(sealed_record["resolved_paths"]) if sealed_record is not None else set()

    staged: list[StagedInputRecord] = []
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        for item, resolved in resolved_items:
            if not resolved.is_file():
                raise Refused("INPUT_FILE_MISSING", "input file does not exist as a regular file: %s" % (root / item.src))

            data = resolved.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
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
            if sealed_record is not None and str(resolved) in sealed_resolved_set:
                sealed_record["files"].append({
                    "src": item.src,
                    "dst": item.dst,
                    "resolved_path": str(resolved),
                    "sha256": digest,
                })
    return staged, sealed_record


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
    if card_spec.card_class == "gpu" and card_spec.cli_flag == "--gpu":
        check_src = vm_card_check_py(card_spec, min_vram_mib=spec.vram_floor_for(card_spec))
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
    "DEFAULT_SEALED_HOST",
    "InputFileSpec",
    "SEALED_PATH_PATTERNS",
    "SealedPolicySpec",
    "StagedInputRecord",
    "WindowSpec",
    "render_vm_script",
    "stage_inputs",
]
