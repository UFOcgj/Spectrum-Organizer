from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from validation.evidence_lock import (
    OwnedDirectoryLockError,
    acquire_owned_directory_lock,
    release_owned_directory_lock,
)

STARTUP_STABILIZATION_SECONDS = 2.0
MAX_RUNTIME_TEXT_BYTES = 1024 * 1024
TEXT_RUNTIME_SUFFIXES = frozenset({".csv", ".json", ".jsonl", ".log", ".md", ".tsv", ".txt"})


def run_packaged_smoke(*, evidence_dir: Path, timeout_seconds: int = 180) -> dict:
    evidence_dir = Path(evidence_dir).resolve()
    exe = evidence_dir / "dist" / "Spectrum Organizer" / "Spectrum Organizer.exe"
    if not exe.is_file():
        raise RuntimeError(f"Packaged executable is missing: {exe}")
    runtime_appdata = _new_runtime_appdata(evidence_dir)
    before = {str(path) for path in runtime_appdata.rglob("*")}
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(runtime_appdata)
    process = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    app_root = runtime_appdata / "Spectrum Organizer"
    started = False
    shutdown = "not_started"
    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                shutdown = "exited_early"
                break
            if (app_root / "data").is_dir() and (app_root / "temp").is_dir() and (app_root / "logs").is_dir():
                time.sleep(STARTUP_STABILIZATION_SECONDS)
                if process.poll() is not None:
                    shutdown = "exited_after_startup_dirs"
                    break
                started = True
                shutdown = "terminate_after_start"
                break
            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                shutdown = "kill_after_timeout"
                process.kill()
                process.wait(timeout=8)

    worker_entrypoint_evidence = _probe_packaged_worker_entrypoints(
        exe,
        env,
    )
    final_origin_count = _origin_process_count()
    final_product_count = _product_process_count(exe)
    after = {str(path) for path in runtime_appdata.rglob("*")}
    created = tuple(sorted(after - before))
    runtime_text, runtime_text_bytes = _collect_runtime_text_with_byte_count(runtime_appdata)
    failures = list(
        _clean_gate_failures(
            runtime_text=runtime_text,
            created_paths=created,
            preexisting_user_paths=tuple(sorted(before)),
            final_origin_count=final_origin_count,
            returncode=process.returncode,
            shutdown=shutdown,
        )
    )
    if not started:
        failures.append("packaged app did not create app-state directories before exit/timeout")
    if started and not created:
        failures.append("packaged app startup proof used no newly created runtime paths")
    if final_product_count != 0:
        failures.append("packaged product process remained after startup smoke")
    return {
        "exe": str(exe),
        "runtime_appdata": str(runtime_appdata),
        "started": started,
        "shutdown": shutdown,
        "process_returncode_after_shutdown": process.returncode,
        "final_origin_process_count": final_origin_count,
        "final_product_process_count": final_product_count,
        "worker_entrypoint_evidence": worker_entrypoint_evidence,
        "worker_entrypoint_returncodes": {
            role: evidence["returncode"]
            for role, evidence in worker_entrypoint_evidence.items()
        },
        "created_paths_count": len(created),
        "runtime_text_bytes_checked": runtime_text_bytes,
        "clean_gate_evidence_scope": _clean_gate_evidence_scope(),
        "clean_gate_failures": failures,
    }


def _probe_packaged_worker_entrypoints(
    executable: Path,
    environment: dict[str, str],
    *,
    process_runner=subprocess.run,
    origin_process_count=None,
) -> dict[str, dict[str, object]]:
    process_count = origin_process_count or _origin_process_count
    evidence = {}
    for role, flag in (
        ("output", "--origin-output-worker"),
        ("verifier", "--origin-verifier-worker"),
    ):
        before = int(process_count())
        completed = process_runner(
            [str(executable), flag],
            input="{}",
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
            check=False,
        )
        after = int(process_count())
        try:
            result = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Packaged {role} worker did not return structured invalid-contract evidence"
            ) from exc
        expected = {
            "ok": False,
            "classification": "non_retryable",
            "error": f"{role} payload fields are invalid",
            "error_type": "ValueError",
            "error_notes": [],
        }
        if (
            completed.returncode != 1
            or result != expected
            or completed.stderr
            or before != 0
            or after != 0
        ):
            raise RuntimeError(
                f"Packaged {role} worker did not return exact structured invalid-contract evidence"
            )
        evidence[role] = {
            "returncode": int(completed.returncode),
            "result": result,
            "stderr": completed.stderr,
            "origin_process_count_before": before,
            "origin_process_count_after": after,
        }
    return evidence


def _clean_gate_evidence_scope() -> dict[str, object]:
    return {
        "runtime_text": True,
        "created_paths": "fresh LOCALAPPDATA subtree only",
        "preexisting_user_paths": "fresh LOCALAPPDATA subtree only",
        "final_origin_process_count": True,
        "final_product_process_count": True,
        "packaged_worker_entrypoints": "invalid contract rejection only; no Origin source is opened",
        "shutdown": "controlled startup termination; natural shutdown is not proven by startup-only smoke",
        "worker_open_targets": "not collected; Task 10 real packaged acceptance must collect this evidence",
    }

def _new_runtime_appdata(evidence_dir: Path) -> Path:
    root = evidence_dir / "runtime-localappdata"
    for _ in range(100):
        candidate = root / f"run-{time.strftime('%Y%m%d_%H%M%S')}-{os.getpid()}-{uuid4().hex[:8]}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not create a fresh runtime appdata directory under {root}")


def _collect_runtime_text(runtime_appdata: Path) -> str:
    return _collect_runtime_text_with_byte_count(runtime_appdata)[0]


def _collect_runtime_text_with_byte_count(runtime_appdata: Path) -> tuple[str, int]:
    chunks: list[str] = []
    bytes_checked = 0
    try:
        runtime_root = runtime_appdata.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"Fresh runtime appdata cannot be inspected: {runtime_appdata}"
        ) from exc
    for path in sorted(runtime_appdata.rglob("*")):
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"Runtime evidence cannot be inspected: {path}") from exc
        if not resolved.is_relative_to(runtime_root):
            raise RuntimeError(
                f"Runtime evidence is outside the fresh runtime appdata: {path}"
            )
        if path.suffix.lower() not in TEXT_RUNTIME_SUFFIXES:
            continue
        try:
            metadata = path.stat()
        except OSError as exc:
            raise RuntimeError(f"Runtime text evidence cannot be inspected: {path}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if metadata.st_size > MAX_RUNTIME_TEXT_BYTES:
            raise RuntimeError(f"Runtime text evidence exceeds the size limit: {path}")
        try:
            raw = path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"Runtime text evidence cannot be read: {path}") from exc
        if len(raw) > MAX_RUNTIME_TEXT_BYTES:
            raise RuntimeError(f"Runtime text evidence exceeds the size limit: {path}")
        try:
            chunks.append(raw.decode("utf-8"))
        except UnicodeError as exc:
            raise RuntimeError(f"Runtime text evidence cannot be read as UTF-8: {path}") from exc
        bytes_checked += len(raw)
    return "\n".join(chunks), bytes_checked


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_public_summary(path: Path, *, action: str) -> None:
    withdrawn = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.invalid")
    try:
        os.replace(path, withdrawn)
    except FileNotFoundError:
        return
    except OSError as move_error:
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_error:
            error = RuntimeError(f"Could not {action}: {path}")
            error.retain_package_lock = True
            error.add_note(f"Atomic move failed first: {move_error}")
            raise error from unlink_error
        return
    try:
        withdrawn.unlink(missing_ok=True)
    except OSError:
        pass


def _origin_process_count() -> int:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "@(Get-Process | Where-Object { $_.ProcessName -like 'Origin*' }).Count"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Origin process query failed")
    return int((completed.stdout or "0").strip() or "0")


def _product_process_count(executable: Path) -> int:
    environment = os.environ.copy()
    environment["SPECTRUM_ORGANIZER_PROCESS_QUERY_TARGET"] = str(
        Path(executable).resolve()
    )
    script = (
        "$target=[IO.Path]::GetFullPath("
        "$env:SPECTRUM_ORGANIZER_PROCESS_QUERY_TARGET); "
        "@(Get-CimInstance Win32_Process -Filter \"Name = 'Spectrum Organizer.exe'\" | "
        "Where-Object { $_.ExecutablePath -and "
        "[IO.Path]::GetFullPath($_.ExecutablePath) -eq $target }).Count"
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or "Packaged product process query failed"
        )
    try:
        return int((completed.stdout or "0").strip() or "0")
    except ValueError as exc:
        raise RuntimeError(
            "Packaged product process query returned invalid output"
        ) from exc


def _clean_gate_failures(
    *,
    runtime_text: str,
    created_paths: tuple[str, ...],
    preexisting_user_paths: tuple[str, ...],
    final_origin_count: int,
    returncode: int | None,
    shutdown: str,
) -> tuple[str, ...]:
    gate_path = ROOT / "packaging" / "clean_environment_gate.py"
    spec = importlib.util.spec_from_file_location("task17_clean_environment_gate", gate_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    evidence = module.CleanEnvironmentEvidence(
        runtime_text=runtime_text,
        worker_open_targets=(),
        created_paths=created_paths,
        preexisting_user_paths=preexisting_user_paths,
        final_origin_process_count=final_origin_count,
        process_returncode_after_shutdown=returncode,
        shutdown=shutdown,
    )
    return module.evaluate_clean_environment(
        evidence,
        workspace_root=ROOT,
        original_source_paths=(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    summary_path = evidence_dir / "packaged-smoke-summary.json"
    lock_path = evidence_dir / ".packaged-smoke.lock"
    try:
        lock = acquire_owned_directory_lock(
            lock_path,
            owner_filename="owner-token",
            label="Packaged smoke",
        )
    except OwnedDirectoryLockError as exc:
        raise RuntimeError(str(exc)) from exc

    primary_error: BaseException | None = None
    try:
        _remove_public_summary(
            summary_path,
            action="invalidate previous packaged smoke summary",
        )
        summary = run_packaged_smoke(evidence_dir=evidence_dir)
        _write_json_atomic(summary_path, summary)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        retain_lock = primary_error is not None and bool(
            getattr(primary_error, "retain_package_lock", False)
        )
        if not retain_lock:
            try:
                release_owned_directory_lock(
                    lock,
                    release_error_cleanup=lambda: _remove_public_summary(
                        summary_path,
                        action="withdraw packaged smoke summary after lock release failure",
                    ),
                )
            except OwnedDirectoryLockError as release_error:
                if primary_error is None:
                    _remove_public_summary(
                        summary_path,
                        action="withdraw packaged smoke summary after lock release failure",
                    )
                    raise RuntimeError(str(release_error)) from release_error
                primary_error.add_note(str(release_error))
    print(summary_path)
    if summary["clean_gate_failures"]:
        print(json.dumps(summary["clean_gate_failures"], ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
