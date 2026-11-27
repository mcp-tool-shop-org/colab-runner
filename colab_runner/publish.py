"""Stdlib-only Hugging Face publish step for `public` and `synthetic` window outputs: upload private first, run
an identity & token scan over every file and the generated card, flip public, and record `repo_url` + flip time."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from colab_runner.colab_cli import _is_link, binary_token_hit, is_binary_bytes, scrub
from colab_runner.window_lib import launch_guards as lg

HF_ENDPOINT = "https://huggingface.co"
TransportFn = Callable[[str, str, Mapping[str, str], Optional[bytes], float], tuple[int, dict[str, str], bytes]]

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_TEXT_IDENTITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("homepath_posix", re.compile(r"(?:/[Uu]sers/|/home/)[A-Za-z0-9._-]+/")),
    ("homepath_win", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+\\+")),
    ("homepath_mnt", re.compile(r"/mnt/[a-z]/[A-Za-z0-9._-]+/")),
    ("lan_ip", re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    (
        "email",
        re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.com\b|example\.org\b|users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
)
_BYTE_IDENTITY_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple(
    (name, re.compile(pat.pattern.encode("ascii")))
    for name, pat in _TEXT_IDENTITY_RULES
)


def _utc_now(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "job").strip().lower()).strip("-.") or "job"


def _hf_home_token_path(raw_home: str, home: Path) -> Path:
    m = _WIN_DRIVE_RE.match(raw_home.strip())
    if m and os.name == "posix":
        posix_cand = Path("/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/")))
        if posix_cand.exists():
            return posix_cand / "token"
    return Path(raw_home.strip()).expanduser() / "token"


def resolve_hf_token(
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a Hugging Face token from environment variables or standard token files without hardcoding paths."""
    env = os.environ if environ is None else environ
    for var in ("COLAB_RUNNER_HF_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        val = str(env.get(var, "")).strip()
        if val:
            return val
    user_home = home or Path.home()
    candidates: list[Path] = []
    hf_home = str(env.get("HF_HOME", "")).strip()
    if hf_home:
        candidates.append(_hf_home_token_path(hf_home, user_home))
    candidates.append(user_home / ".cache" / "huggingface" / "token")
    for cand in candidates:
        try:
            if cand.is_file():
                tok = cand.read_text(encoding="utf-8").strip()
                if tok:
                    return tok
        except OSError:
            continue
    return None


def scan_publish_files(
    files: Mapping[str, bytes],
    *,
    extra_deny: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Scan every file and card for Colab/HF tokens, homepaths, emails, private keys, and caller deny terms."""
    deny_terms = [t.strip() for t in extra_deny if t and t.strip()]
    findings: list[dict[str, str]] = []
    for rel in sorted(files):
        data = files[rel]
        if is_binary_bytes(data):
            tok_hit = binary_token_hit(data)
            if tok_hit is not None:
                findings.append({"path": rel, "reason": "binary_token:%s" % tok_hit})
            for rule_name, bpat in _BYTE_IDENTITY_RULES:
                if bpat.search(data):
                    findings.append({"path": rel, "reason": rule_name})
            for term in deny_terms:
                if term.encode("utf-8") in data:
                    findings.append({"path": rel, "reason": "deny_term"})
            continue
        text = data.decode("utf-8")
        if lg.scrub_colab_tokens(text) != text:
            findings.append({"path": rel, "reason": "token_match"})
        for rule_name, pat in _TEXT_IDENTITY_RULES:
            if pat.search(text):
                findings.append({"path": rel, "reason": rule_name})
        for term in deny_terms:
            if term in text:
                findings.append({"path": rel, "reason": "deny_term"})
    return findings


def _collect_publish_files(out_dir: Path, card_md: str) -> dict[str, bytes]:
    collected = out_dir / "collected"
    files: dict[str, bytes] = {"README.md": card_md.encode("utf-8")}
    if collected.is_dir():
        for dirpath, dirnames, filenames in os.walk(collected, followlinks=False):
            parent = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not _is_link(parent / d))
            for fname in sorted(filenames):
                fp = parent / fname
                if _is_link(fp) or not fp.is_file():
                    continue
                rel = fp.relative_to(collected).as_posix()
                if rel == "README.md":
                    continue
                files[rel] = fp.read_bytes()
    return files


def render_hf_card(
    *,
    spec: Any,
    outcome: Mapping[str, Any],
    file_hashes: Mapping[str, tuple[int, str]],
) -> str:
    """Render a concise public dataset/model README card containing no local filesystem paths or personal identity."""
    proj = outcome.get("project") or {}
    lines = [
        "---",
        "license: mit",
        "tags:",
        "- colab-runner",
        "- %s" % spec.data_class,
        "---",
        "",
        "# %s / %s" % (spec.study, spec.job),
        "",
        "- **Purpose:** %s" % (proj.get("purpose") or spec.purpose or spec.description or spec.job),
        "- **Line of work:** %s" % (proj.get("line") or spec.line or spec.study),
        "- **Data class:** %s" % spec.data_class,
        "- **Card:** %s (%s)" % (outcome.get("card") or spec.card, outcome.get("shape") or "default"),
        "- **Spend (CCU):** %s (elapsed %ss)" % (outcome.get("spend_ccu"), outcome.get("elapsed_s")),
        "",
        "## Artifacts",
        "",
        "| File | Bytes | SHA-256 |",
        "| --- | ---: | --- |",
    ]
    for rel, (nbytes, sha) in sorted(file_hashes.items()):
        lines.append("| `%s` | %d | `%s` |" % (rel, nbytes, sha))
    return "\n".join(lines) + "\n"


def default_hf_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    """Perform one bounded HTTP request over urllib.request using zero third-party packages."""
    req = urllib.request.Request(url=url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def _api_json(
    transport: TransportFn,
    method: str,
    url: str,
    token: str,
    payload: Any = None,
    *,
    content_type: str = "application/json",
    raw_body: Optional[bytes] = None,
) -> tuple[int, dict[str, Any]]:
    body = raw_body if raw_body is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else None
    )
    headers = {"Authorization": "Bearer %s" % token}
    if body is not None:
        headers["Content-Type"] = content_type
    status, _, resp_bytes = transport(method, url, headers, body, 60.0)
    if not resp_bytes:
        return status, {}
    try:
        decoded = json.loads(resp_bytes.decode("utf-8", errors="replace"))
        return status, decoded if isinstance(decoded, dict) else {"value": decoded}
    except ValueError:
        return status, {"raw": scrub(resp_bytes.decode("utf-8", errors="replace")[:300])}


def publish_window_outputs(
    *,
    spec: Any,
    out_dir: Path,
    outcome: Mapping[str, Any],
    transport: Optional[TransportFn] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    log: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Upload collected public/synthetic artifacts private-first to `mcp-tool-shop`, identity-scan, and flip public."""
    org = spec.publish.org if spec.publish is not None else "mcp-tool-shop"
    repo_name = (
        spec.publish.repo_name
        if (spec.publish is not None and spec.publish.repo_name)
        else "%s-%s" % (_slug(spec.study), _slug(spec.job))
    )
    repo_type = spec.publish.repo_type if spec.publish is not None else "dataset"
    repo_id = "%s/%s" % (org, repo_name)
    api_type = "%ss" % repo_type
    web_url = (
        "%s/datasets/%s" % (HF_ENDPOINT, repo_id) if repo_type == "dataset" else "%s/%s" % (HF_ENDPOINT, repo_id)
    )

    prelim = _collect_publish_files(out_dir, "")
    hashes = {rel: (len(b), hashlib.sha256(b).hexdigest()) for rel, b in prelim.items() if rel != "README.md"}
    card_md = render_hf_card(spec=spec, outcome=outcome, file_hashes=hashes)
    file_map = _collect_publish_files(out_dir, card_md)

    env_src = os.environ if environ is None else environ
    extra_deny = [x for x in str(env_src.get("COLAB_RUNNER_SCAN_DENY", "")).split(",") if x.strip()]
    findings = scan_publish_files(file_map, extra_deny=extra_deny)
    if findings:
        log("publish blocked by pre-upload identity scan (%d finding(s))" % len(findings))
        return {
            "status": "scan_failed",
            "repo_id": repo_id,
            "repo_type": repo_type,
            "repo_url": None,
            "flipped_public_at": None,
            "findings": findings,
        }

    token = resolve_hf_token(environ=env_src, home=home)
    if not token:
        log("publish failed: no Hugging Face token configured")
        return {
            "status": "token_missing",
            "repo_id": repo_id,
            "repo_type": repo_type,
            "repo_url": None,
            "flipped_public_at": None,
            "error": "no Hugging Face token configured (COLAB_RUNNER_HF_TOKEN / HF_TOKEN / HF_HOME/token)",
        }

    xfn = transport or default_hf_transport
    st, data = _api_json(
        xfn, "POST", "%s/api/repos/create" % HF_ENDPOINT, token,
        {"name": repo_name, "organization": org, "private": True, "type": repo_type},
    )
    if st == 409:
        st_s, data_s = _api_json(
            xfn, "PUT", "%s/api/%s/%s/settings" % (HF_ENDPOINT, api_type, repo_id), token, {"private": True},
        )
        if st_s >= 400:
            return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                    "flipped_public_at": None, "error": "settings private failed (%s): %s" % (st_s, data_s)}
    elif st >= 400:
        return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "error": "repo create failed (%s): %s" % (st, data)}

    st_chk, chk_data = _api_json(xfn, "GET", "%s/api/%s/%s" % (HF_ENDPOINT, api_type, repo_id), token)
    if st_chk >= 400 or chk_data.get("private") is not True:
        return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "error": "repo %s could not be verified private before upload" % repo_id}

    pre_files = [
        {"path": rel, "size": len(b), "sample": base64.b64encode(b[:512]).decode("ascii")}
        for rel, b in sorted(file_map.items())
    ]
    st_pre, pre_data = _api_json(
        xfn, "POST", "%s/api/%s/%s/preupload/main" % (HF_ENDPOINT, api_type, repo_id), token, {"files": pre_files},
    )
    if st_pre >= 400:
        return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "error": "preupload failed (%s): %s" % (st_pre, pre_data)}

    modes = {str(item.get("path")): str(item.get("uploadMode", "regular")) for item in pre_data.get("files", [])}
    lfs_rels = [rel for rel in sorted(file_map) if modes.get(rel) == "lfs"]
    if lfs_rels:
        lfs_prefix = "datasets/" if repo_type == "dataset" else ""
        batch_objs = [{"oid": hashlib.sha256(file_map[r]).hexdigest(), "size": len(file_map[r])} for r in lfs_rels]
        st_b, b_data = _api_json(
            xfn, "POST", "%s/%s%s.git/info/lfs/objects/batch" % (HF_ENDPOINT, lfs_prefix, repo_id), token,
            {"operation": "upload", "transfers": ["basic"], "objects": batch_objs},
        )
        if st_b >= 400:
            return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                    "flipped_public_at": None, "error": "LFS batch failed (%s): %s" % (st_b, b_data)}
        by_oid = {str(o.get("oid")): o for o in b_data.get("objects", [])}
        for rel in lfs_rels:
            sha = hashlib.sha256(file_map[rel]).hexdigest()
            upload_action = (by_oid.get(sha, {}).get("actions") or {}).get("upload") or {}
            href = upload_action.get("href")
            if href:
                up_hdrs = {str(k): str(v) for k, v in (upload_action.get("header") or {}).items()}
                up_st, _, _ = xfn("PUT", str(href), up_hdrs, file_map[rel], 120.0)
                if up_st >= 400:
                    return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                            "flipped_public_at": None, "error": "LFS PUT failed for %s (%s)" % (rel, up_st)}

    ndjson_lines = [json.dumps({"key": "header", "value": {"summary": "colab-runner: %s/%s" % (spec.study, spec.job)}})]
    for rel in sorted(file_map):
        raw = file_map[rel]
        if modes.get(rel) == "lfs":
            val = {"path": rel, "algo": "sha256", "size": len(raw), "oid": hashlib.sha256(raw).hexdigest()}
            ndjson_lines.append(json.dumps({"key": "lfsFile", "value": val}))
        else:
            val = {"path": rel, "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
            ndjson_lines.append(json.dumps({"key": "file", "value": val}))
    st_cm, cm_data = _api_json(
        xfn, "POST", "%s/api/%s/%s/commit/main" % (HF_ENDPOINT, api_type, repo_id), token,
        content_type="application/x-ndjson", raw_body=("\n".join(ndjson_lines) + "\n").encode("utf-8"),
    )
    if st_cm >= 400:
        return {"status": "upload_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "error": "commit failed (%s): %s" % (st_cm, cm_data)}

    post_findings = scan_publish_files(file_map, extra_deny=extra_deny)
    if post_findings:
        return {"status": "scan_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "findings": post_findings}

    st_pub, pub_data = _api_json(
        xfn, "PUT", "%s/api/%s/%s/settings" % (HF_ENDPOINT, api_type, repo_id), token, {"private": False},
    )
    st_ver, ver_data = _api_json(xfn, "GET", "%s/api/%s/%s" % (HF_ENDPOINT, api_type, repo_id), token)
    if st_pub >= 400 or st_ver >= 400 or ver_data.get("private") is not False:
        return {"status": "flip_failed", "repo_id": repo_id, "repo_type": repo_type, "repo_url": None,
                "flipped_public_at": None, "error": "public flip failed (%s/%s): %s" % (st_pub, st_ver, pub_data)}

    flipped_at = _utc_now()
    log("published %s (%s, %d files) at %s" % (web_url, spec.data_class, len(file_map), flipped_at))
    return {
        "status": "published",
        "repo_id": repo_id,
        "repo_type": repo_type,
        "repo_url": web_url,
        "uploaded_private_first": True,
        "identity_scan": "clean",
        "file_count": len(file_map),
        "commit_oid": cm_data.get("commitOid"),
        "flipped_public_at": flipped_at,
    }


__all__ = [
    "HF_ENDPOINT",
    "default_hf_transport",
    "publish_window_outputs",
    "render_hf_card",
    "resolve_hf_token",
    "scan_publish_files",
]
