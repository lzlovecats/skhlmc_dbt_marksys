"""Pinned localhost GPT-SoVITS inference and allowlisted training adapter."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

import httpx

from system_limits import TTS_TEXT_MAX_CHARS, WORKSTATION_TTS_OUTPUT_MAX_BYTES
from workstation.config import GptSoVitsConfig
from workstation.workloads.errors import WorkloadError


_DATASET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
SUPPORTED_GPT_SOVITS_COMMIT = "d7c2210da8c013e81a94bfc7b811a477c99fd506"
SUPPORTED_GPT_SOVITS_FAMILY = "v2Pro"
_SMALL_ARTIFACT_MAX_BYTES = 64 * 1024


def _small_text(path: Path, *, encoding: str = "utf-8") -> str:
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= _SMALL_ARTIFACT_MAX_BYTES
    ):
        raise ValueError("GPT-SoVITS small artifact is invalid")
    return path.read_bytes().decode(encoding)


def _small_json(path: Path) -> dict:
    value = json.loads(_small_text(path))
    if not isinstance(value, dict):
        raise ValueError("GPT-SoVITS JSON artifact is invalid")
    return value


class GptSoVitsAdapter:
    def __init__(self, config: GptSoVitsConfig):
        self.config = config

    def health(self) -> dict:
        if not self.config.enabled:
            return {"ok": False, "code": "disabled"}
        if not self.config.runtime_root.is_dir():
            return {"ok": False, "code": "runtime_missing"}
        if not self.config.reference_audio.is_file() or not self.config.reference_text_file.is_file():
            return {"ok": False, "code": "reference_missing"}
        if not self.config.model_version:
            return {"ok": False, "code": "model_version_missing"}
        try:
            commit = _small_text(
                self.config.runtime_root / "APPROVED_COMMIT", encoding="ascii",
            ).strip().lower()
        except (OSError, RuntimeError, UnicodeError, ValueError):
            commit = ""
        if commit != SUPPORTED_GPT_SOVITS_COMMIT:
            return {"ok": False, "code": "runtime_commit_mismatch"}
        try:
            receipt = _small_json(self.config.approval_receipt)
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {
                    "schema_version", "model_version", "upstream_commit",
                    "model_family", "inference_config", "gpt_weight",
                    "sovits_weight", "reference_audio", "reference_text",
                }
                or receipt.get("schema_version") != 1
                or receipt.get("model_version") != self.config.model_version
                or receipt.get("upstream_commit") != commit
                or receipt.get("model_family") != SUPPORTED_GPT_SOVITS_FAMILY
            ):
                raise ValueError("voice approval receipt is invalid")
            expected_paths = {
                "inference_config": self.config.inference_config,
                "reference_audio": self.config.reference_audio,
                "reference_text": self.config.reference_text_file,
            }
            for name in (
                "inference_config", "gpt_weight", "sovits_weight",
                "reference_audio", "reference_text",
            ):
                artifact = receipt.get(name)
                if not isinstance(artifact, dict) or set(artifact) != {
                    "path", "sha256", "bytes", "mtime_ns",
                }:
                    raise ValueError("voice approval artifact is invalid")
                path = Path(str(artifact.get("path") or "")).resolve(strict=True)
                if name in expected_paths and path != expected_paths[name].resolve(strict=True):
                    raise ValueError("voice approval path does not match config")
                stat = path.stat()
                if (
                    not path.is_file()
                    or stat.st_size != int(artifact.get("bytes") or 0)
                    or stat.st_mtime_ns != int(artifact.get("mtime_ns") or 0)
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", str(artifact.get("sha256") or "")
                    )
                ):
                    raise ValueError("voice approval artifact changed")
            inference = _small_json(self.config.inference_config)
            custom = inference.get("custom") if isinstance(inference, dict) else None
            if (
                not isinstance(custom, dict)
                or custom.get("version") != SUPPORTED_GPT_SOVITS_FAMILY
                or Path(str(custom.get("t2s_weights_path") or "")).resolve(strict=True)
                != Path(receipt["gpt_weight"]["path"]).resolve(strict=True)
                or Path(str(custom.get("vits_weights_path") or "")).resolve(strict=True)
                != Path(receipt["sovits_weight"]["path"]).resolve(strict=True)
            ):
                raise ValueError("voice inference config is invalid")
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": False, "code": "voice_approval_mismatch"}
        return {
            "ok": True,
            "model_version": self.config.model_version,
            "upstream_commit": commit,
        }

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def verify_artifacts(self) -> dict:
        status = self.health()
        if not status.get("ok"):
            raise WorkloadError("voice_approval_mismatch", "Approved GPT-SoVITS voice is unavailable.")
        try:
            receipt = _small_json(self.config.approval_receipt)
            for name in (
                "inference_config", "gpt_weight", "sovits_weight",
                "reference_audio", "reference_text",
            ):
                artifact = receipt[name]
                if self._file_sha256(Path(artifact["path"])) != artifact["sha256"]:
                    raise ValueError("voice artifact hash mismatch")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise WorkloadError("voice_digest_mismatch", "Approved GPT-SoVITS voice digest changed.") from exc
        return {"ok": True, "model_version": self.config.model_version}

    def wait_until_ready(self, *, timeout_seconds: int = 30) -> None:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < deadline:
            try:
                with httpx.stream("GET", f"{self.config.url}/", timeout=5) as response:
                    if response.status_code < 500:
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        raise WorkloadError(
            "tts_runtime_unavailable", "Pinned GPT-SoVITS inference did not become ready."
        )

    def training_health(self) -> dict:
        scripts = (
            self.config.runtime_root / "runtime" / "bin" / "python",
            self.config.runtime_root / "GPT_SoVITS" / "prepare_datasets" / "1-get-text.py",
            self.config.runtime_root / "GPT_SoVITS" / "prepare_datasets" / "2-get-hubert-wav32k.py",
            self.config.runtime_root / "GPT_SoVITS" / "prepare_datasets" / "2-get-sv.py",
            self.config.runtime_root / "GPT_SoVITS" / "prepare_datasets" / "3-get-semantic.py",
            self.config.runtime_root / "GPT_SoVITS" / "s1_train.py",
            self.config.runtime_root / "GPT_SoVITS" / "s2_train.py",
            self.config.runtime_root / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml",
            self.config.runtime_root / "GPT_SoVITS" / "configs" / "s2v2Pro.json",
            self.config.runtime_root / "GPT_SoVITS" / "pretrained_models" / "s1v3.ckpt",
            self.config.runtime_root / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Gv2Pro.pth",
            self.config.runtime_root / "GPT_SoVITS" / "pretrained_models" / "v2Pro" / "s2Dv2Pro.pth",
        )
        try:
            commit = _small_text(
                self.config.runtime_root / "APPROVED_COMMIT", encoding="ascii",
            ).strip().lower()
        except (OSError, RuntimeError, UnicodeError, ValueError):
            commit = ""
        return {
            "ok": bool(
                commit == SUPPORTED_GPT_SOVITS_COMMIT
                and all(path.is_file() for path in scripts)
            ),
            "model_version": self.config.model_version,
        }

    def synthesize(self, text: str, *, output_dir: Path | None = None) -> dict:
        clean = str(text or "").strip()
        if not clean or len(clean) > TTS_TEXT_MAX_CHARS:
            raise WorkloadError("invalid_tts_text", "TTS text is empty or too long.")
        reference_audio = self.config.reference_audio
        try:
            reference_text = _small_text(
                self.config.reference_text_file,
            ).strip()
        except (OSError, RuntimeError, UnicodeError, ValueError):
            reference_text = ""
        if not reference_audio.is_file() or not reference_text:
            raise WorkloadError("tts_reference_missing", "Approved TTS reference audio is unavailable.")
        try:
            with httpx.stream(
                "POST",
                f"{self.config.url}/tts",
                json={
                    "text": clean,
                    "text_lang": self.config.language,
                    "ref_audio_path": str(reference_audio.resolve(strict=True)),
                    "prompt_text": str(reference_text)[:1_200],
                    "prompt_lang": self.config.language,
                    "text_split_method": "cut5",
                    "batch_size": 1,
                    "media_type": "wav",
                    "streaming_mode": False,
                },
                timeout=httpx.Timeout(120, connect=10),
            ) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                if declared > WORKSTATION_TTS_OUTPUT_MAX_BYTES:
                    raise WorkloadError(
                        "tts_output_invalid",
                        "Local voice output is empty or too large.",
                    )
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > WORKSTATION_TTS_OUTPUT_MAX_BYTES:
                        raise WorkloadError(
                            "tts_output_invalid",
                            "Local voice output is empty or too large.",
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
        except WorkloadError:
            raise
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            marker = str(exc).casefold()
            code = "out_of_memory" if any(item in marker for item in ("out of memory", "cuda", "oom")) else "tts_failed"
            raise WorkloadError(code, "Local voice synthesis failed.", retryable=code == "tts_failed") from exc
        if not data or len(data) > WORKSTATION_TTS_OUTPUT_MAX_BYTES:
            raise WorkloadError("tts_output_invalid", "Local voice output is empty or too large.")
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        handle = tempfile.NamedTemporaryFile(
            prefix="lmc-tts-", suffix=".wav", dir=output_dir, delete=False,
        )
        try:
            handle.write(data)
            handle.flush()
        finally:
            handle.close()
        return {"path": Path(handle.name), "mime_type": "audio/wav", "byte_size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "model_version": self.config.model_version}

    @staticmethod
    def _stop_process(process) -> None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def _run_process(
        self,
        command: list[str],
        *,
        deadline: float,
        cancel_event: threading.Event | None,
        environment: dict[str, str] | None = None,
        resource_gate=None,
        failure_code: str,
    ) -> None:
        try:
            process = subprocess.Popen(
                command,
                cwd=self.config.runtime_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    self._stop_process(process)
                    raise WorkloadError("cancelled", "GPT-SoVITS work was cancelled.")
                if time.monotonic() >= deadline:
                    self._stop_process(process)
                    raise WorkloadError("training_timeout", "GPT-SoVITS work exceeded its safe deadline.")
                if resource_gate is not None:
                    try:
                        resource_gate()
                    except WorkloadError:
                        self._stop_process(process)
                        raise
                time.sleep(0.5)
        except WorkloadError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkloadError(failure_code, "GPT-SoVITS work could not complete.") from exc
        if process.returncode:
            raise WorkloadError(failure_code, "GPT-SoVITS work could not complete.")

    @staticmethod
    def _merge_parts(destination: Path, parts: tuple[Path, ...], *, header: str = "") -> None:
        lines = []
        for path in parts:
            if not path.is_file() or path.stat().st_size <= 0:
                raise WorkloadError("training_preprocess_failed", "GPT-SoVITS preprocessing output is incomplete.")
            lines.extend(path.read_text(encoding="utf-8").strip().splitlines())
        if not lines:
            raise WorkloadError("training_preprocess_failed", "GPT-SoVITS preprocessing output is empty.")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        content = ([header] if header else []) + lines
        temporary.write_text("\n".join(content) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        for path in parts:
            path.unlink(missing_ok=True)

    @staticmethod
    def _has_regular_output(path: Path) -> bool:
        try:
            return path.is_dir() and any(
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.stat().st_size > 0
                for candidate in path.rglob("*")
            )
        except (OSError, RuntimeError):
            return False

    def prepare_training(
        self,
        *,
        dataset_id: str,
        training_list: Path,
        recommendation_file: Path,
        experiment_root: Path,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
        on_stage=None,
        resource_gate=None,
    ) -> tuple[Path, Path]:
        if not _DATASET_RE.fullmatch(str(dataset_id or "")):
            raise WorkloadError("invalid_dataset", "Training dataset identifier is invalid.")
        if not self.training_health().get("ok"):
            raise WorkloadError("training_runtime_missing", "Pinned GPT-SoVITS training runtime is incomplete.")
        try:
            training_list = training_list.resolve(strict=True)
            recommendation_file = recommendation_file.resolve(strict=True)
            dataset_root = training_list.parents[1]
            if dataset_root not in recommendation_file.parents:
                raise ValueError("recommendation is outside dataset")
            recommendation = json.loads(recommendation_file.read_text(encoding="utf-8"))
            if (
                recommendation.get("dataset_readiness") == "BLOCKED_SPLIT"
                or recommendation.get("gpu_info") != "0"
            ):
                raise ValueError("dataset is not approved for the one-GPU profile")
        except (OSError, ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
            raise WorkloadError("training_profile_invalid", "Dataset training recommendation is invalid.") from exc
        if experiment_root.exists():
            raise WorkloadError("checkpoint_exists", "A checkpoint run already exists for this dataset.")
        experiment_root.mkdir(parents=True, mode=0o750)
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        runtime = self.config.runtime_root.resolve(strict=True)
        python = runtime / "runtime/bin/python"
        pretrained = runtime / "GPT_SoVITS/pretrained_models"
        base_environment = os.environ.copy()
        base_environment.update({
            "inp_text": str(training_list),
            "inp_wav_dir": "",
            "exp_name": dataset_id,
            "opt_dir": str(experiment_root),
            "i_part": "0",
            "all_parts": "1",
            "_CUDA_VISIBLE_DEVICES": "0",
            "is_half": "True" if recommendation.get("precision") == "16-mixed" else "False",
        })
        try:
            if on_stage:
                on_stage("training_text_features")
            text_environment = {
                **base_environment,
                "bert_pretrained_dir": str(pretrained / "chinese-roberta-wwm-ext-large"),
            }
            self._run_process(
                [str(python), "-s", "GPT_SoVITS/prepare_datasets/1-get-text.py"],
                deadline=deadline, cancel_event=cancel_event,
                environment=text_environment, resource_gate=resource_gate,
                failure_code="training_text_features_failed",
            )
            self._merge_parts(
                experiment_root / "2-name2text.txt",
                (experiment_root / "2-name2text-0.txt",),
            )

            if on_stage:
                on_stage("training_audio_features")
            audio_environment = {
                **base_environment,
                "cnhubert_base_dir": str(pretrained / "chinese-hubert-base"),
                "sv_path": str(pretrained / "sv/pretrained_eres2netv2w24s4ep4.ckpt"),
            }
            for script in ("2-get-hubert-wav32k.py", "2-get-sv.py"):
                self._run_process(
                    [str(python), "-s", f"GPT_SoVITS/prepare_datasets/{script}"],
                    deadline=deadline, cancel_event=cancel_event,
                    environment=audio_environment, resource_gate=resource_gate,
                    failure_code="training_audio_features_failed",
                )
            if not all(
                self._has_regular_output(experiment_root / name)
                for name in ("3-bert", "4-cnhubert", "5-wav32k")
            ):
                raise WorkloadError("training_audio_features_failed", "GPT-SoVITS audio features are incomplete.")

            if on_stage:
                on_stage("training_semantic_features")
            semantic_environment = {
                **base_environment,
                "pretrained_s2G": str(pretrained / "v2Pro/s2Gv2Pro.pth"),
                "s2config_path": str(runtime / "GPT_SoVITS/configs/s2v2Pro.json"),
            }
            self._run_process(
                [str(python), "-s", "GPT_SoVITS/prepare_datasets/3-get-semantic.py"],
                deadline=deadline, cancel_event=cancel_event,
                environment=semantic_environment, resource_gate=resource_gate,
                failure_code="training_semantic_features_failed",
            )
            self._merge_parts(
                experiment_root / "6-name2semantic.tsv",
                (experiment_root / "6-name2semantic-0.tsv",),
                header="item_name\tsemantic_audio",
            )

            if on_stage:
                on_stage("training_profile")
            worker = Path(__file__).with_name("gpt_sovits_profile_worker.py")
            self._run_process(
                [
                    str(python), str(worker),
                    "--runtime-root", str(runtime),
                    "--experiment-root", str(experiment_root),
                    "--dataset-id", dataset_id,
                    "--recommendation", str(recommendation_file),
                ],
                deadline=deadline, cancel_event=cancel_event,
                resource_gate=resource_gate,
                failure_code="training_profile_invalid",
            )
            gpt_config = experiment_root / "profiles/gpt.yaml"
            sovits_config = experiment_root / "profiles/sovits.json"
            if not gpt_config.is_file() or not sovits_config.is_file():
                raise WorkloadError("training_profile_invalid", "GPT-SoVITS training profile was not generated.")
            return gpt_config, sovits_config
        except WorkloadError as exc:
            marker = experiment_root / "FAILED.json"
            try:
                marker.write_text(json.dumps({"code": exc.code}) + "\n", encoding="utf-8")
                os.chmod(marker, 0o600)
            except OSError:
                pass
            raise

    def training_command(
        self,
        *,
        stage: str,
        dataset_id: str,
        config_file: Path,
        allowed_config_root: Path | None = None,
    ) -> list[str]:
        if not _DATASET_RE.fullmatch(str(dataset_id or "")):
            raise WorkloadError("invalid_dataset", "Training dataset identifier is invalid.")
        allowed_root = (allowed_config_root or self.config.runtime_root).resolve(strict=True)
        resolved_config = config_file.resolve(strict=True)
        if allowed_root not in resolved_config.parents or resolved_config.suffix not in {".yaml", ".json"}:
            raise WorkloadError("invalid_training_config", "Training config is outside the pinned runtime.")
        runtime_root = self.config.runtime_root.resolve(strict=True)
        python = runtime_root / "runtime" / "bin" / "python"
        script_name = {"gpt": "s1_train.py", "sovits": "s2_train.py"}.get(stage)
        if not script_name:
            raise WorkloadError("invalid_training_stage", "Training stage is invalid.")
        script = runtime_root / "GPT_SoVITS" / script_name
        if not python.is_file() or not script.is_file():
            raise WorkloadError("training_runtime_missing", "Pinned GPT-SoVITS training runtime is incomplete.")
        config_flag = "--config_file" if stage == "gpt" else "--config"
        return [str(python), "-s", str(script), config_flag, str(resolved_config)]

    def train(
        self,
        *,
        dataset_id: str,
        gpt_config_file: Path,
        sovits_config_file: Path,
        allowed_config_root: Path | None = None,
        timeout_seconds: int,
        cancel_event: threading.Event | None = None,
        on_stage=None,
        resource_gate=None,
    ) -> None:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        for stage, config_file in (("sovits", sovits_config_file), ("gpt", gpt_config_file)):
            command = self.training_command(
                stage=stage,
                dataset_id=dataset_id,
                config_file=config_file,
                allowed_config_root=allowed_config_root,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise WorkloadError("cancelled", "GPT-SoVITS training was cancelled.")
            if on_stage:
                on_stage(f"training_{stage}")
            self._run_process(
                command,
                deadline=deadline,
                cancel_event=cancel_event,
                resource_gate=resource_gate,
                failure_code="training_failed",
            )
