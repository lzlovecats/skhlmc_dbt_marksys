"""Execute validated protocol jobs behind the one manager arbiter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time

from ai_model_config import LMC_AI_CONTEXT_LENGTH, resolve_lmc_ai_mode_options
from system_limits import (
    DATASET_ARCHIVE_MAX_BYTES,
    LMC_AI_OUTPUT_MAX_BYTES,
    LOCAL_PRACTICE_AUDIO_MAX_BYTES,
    LOCAL_PRACTICE_AUDIO_MAX_SECONDS,
    WORKSTATION_DATASET_PREPARATION_MAX_SECONDS,
    WORKSTATION_DATASET_QUOTA_BYTES,
    WORKSTATION_CACHE_QUOTA_BYTES,
    WORKSTATION_CHECKPOINT_QUOTA_BYTES,
    WORKSTATION_JOB_TIMEOUT_SECONDS,
    WORKSTATION_MIN_FREE_DISK_BYTES,
    WORKSTATION_TTS_TRAINING_MAX_SECONDS,
    WORKSTATION_VOICE_RESERVE_TIMEOUT_SECONDS,
)
from workstation.config import WorkstationConfig
from workstation.manager.arbiter import ModeArbiter
from workstation.manager.inhibitor import SleepInhibitor
from workstation.privileged_helper.client import PrivilegedActionError, request_privileged
from workstation.workloads.asr import FasterWhisperAdapter
from workstation.workloads.dataset_preparation import DatasetPreparationAdapter
from workstation.workloads.errors import WorkloadError
from workstation.workloads.gpt_sovits import GptSoVitsAdapter
from workstation.workloads.media import probe_audio
from workstation.workloads.ollama import OllamaAdapter
from workstation.workloads.r2_transfer import download_to_path
from workstation.workloads.rag import LocalRagIndex
from workstation.workloads.storage import directory_bytes


class JobExecutor:
    def __init__(
        self,
        config: WorkstationConfig,
        arbiter: ModeArbiter,
        inhibitor: SleepInhibitor,
        *,
        privileged_request=None,
    ):
        self.config = config
        self.arbiter = arbiter
        self.inhibitor = inhibitor
        self.ollama = OllamaAdapter(config.workloads.ollama)
        self.asr = FasterWhisperAdapter(config.workloads.asr)
        self.rag = LocalRagIndex(config.workloads.rag, self.ollama)
        self.gpt_sovits = GptSoVitsAdapter(config.workloads.gpt_sovits)
        self.dataset_preparation = DatasetPreparationAdapter(config.paths.data)
        self._privileged_request = privileged_request or request_privileged

    @staticmethod
    def _remaining(deadline_epoch: int) -> int:
        remaining = int(deadline_epoch) - int(time.time())
        if remaining <= 0:
            raise WorkloadError("deadline_expired", "Workstation job deadline expired.")
        return min(remaining, WORKSTATION_JOB_TIMEOUT_SECONDS)

    def _sync_inhibitor(self) -> None:
        if self.arbiter.snapshot().get("sleep_inhibited"):
            self.inhibitor.acquire()
        else:
            self.inhibitor.release()

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise WorkloadError("cancelled", "Workstation job was cancelled.")

    @staticmethod
    def _ollama_timings(usage: dict) -> dict[str, int]:
        return {
            "model_load": max(0, int(usage.get("load_duration_ms") or 0)),
            "prompt_eval": max(
                0, int(usage.get("prompt_eval_duration_ms") or 0)
            ),
            "generation": max(0, int(
                usage.get("generation_duration_ms")
                or usage.get("wall_duration_ms")
                or 0
            )),
        }

    def _set_gpt_sovits_service(self, state: str) -> None:
        try:
            self._privileged_request({
                "action": "set_service_state",
                "service": "lmc-ai-gpt-sovits.service",
                "state": state,
            })
        except (OSError, PrivilegedActionError) as exc:
            raise WorkloadError(
                "gpu_service_control_failed",
                "Workstation could not change the local voice service state.",
            ) from exc

    def _prepare_non_ollama_gpu(self) -> None:
        if self.config.workloads.gpt_sovits.enabled:
            self._set_gpt_sovits_service("stop")
        self.ollama.unload_all()

    def _prepare_ollama_gpu(self) -> None:
        if self.config.workloads.gpt_sovits.enabled:
            self._set_gpt_sovits_service("stop")

    def _require_storage_quota(self, path: Path, quota: int, code: str) -> int:
        usage = directory_bytes(path)
        if usage is None:
            raise WorkloadError(code, "Workstation could not verify managed storage usage.")
        if usage >= int(quota):
            raise WorkloadError(code, "Workstation managed storage quota has been reached.")
        return usage

    def _require_free_disk(self) -> None:
        try:
            free = shutil.disk_usage(self.config.paths.data).free
        except OSError as exc:
            raise WorkloadError("disk_gate", "Workstation could not verify free disk space.") from exc
        if free < WORKSTATION_MIN_FREE_DISK_BYTES:
            raise WorkloadError(
                "disk_gate", "Workstation requires at least 20 GB free disk space."
            )

    def _training_storage_gate(self) -> None:
        self._require_free_disk()
        self._require_storage_quota(
            self.config.paths.data / "checkpoints",
            WORKSTATION_CHECKPOINT_QUOTA_BYTES,
            "checkpoint_quota",
        )
        self._require_storage_quota(
            self.config.paths.cache,
            WORKSTATION_CACHE_QUOTA_BYTES,
            "cache_quota",
        )

    def _dataset_storage_gate(self, *, projected: bool = False) -> None:
        self._require_free_disk()
        usage = self._require_storage_quota(
            self.config.paths.data / "datasets",
            WORKSTATION_DATASET_QUOTA_BYTES,
            "dataset_quota",
        )
        if projected and usage + DATASET_ARCHIVE_MAX_BYTES > WORKSTATION_DATASET_QUOTA_BYTES:
            raise WorkloadError(
                "dataset_quota",
                "Dataset preparation worst-case output would exceed its managed quota.",
            )
        self._require_storage_quota(
            self.config.paths.cache,
            WORKSTATION_CACHE_QUOTA_BYTES,
            "cache_quota",
        )

    def reserve_voice(
        self,
        session_id: str,
        *,
        expires_epoch: int = 0,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        state = self.arbiter.reserve_voice(session_id, expires_epoch=expires_epoch)
        self._sync_inhibitor()
        if state == "waiting_for_text":
            if not self.arbiter.wait_for_voice(
                session_id,
                WORKSTATION_VOICE_RESERVE_TIMEOUT_SECONDS,
                cancel_event=cancel_event,
            ):
                self.arbiter.cancel_pending_voice(session_id)
                self._sync_inhibitor()
                if cancel_event is not None and cancel_event.is_set():
                    raise WorkloadError(
                        "cancelled", "Voice Coach reservation was cancelled."
                    )
                raise WorkloadError("voice_reserve_timeout", "Voice Coach could not reserve the Workstation in time.", retryable=True)
        return {"reserved": True, "manager": self.arbiter.snapshot()}

    def release_voice(self, session_id: str) -> dict:
        self.arbiter.release_voice(session_id)
        self._sync_inhibitor()
        return {"released": True, "manager": self.arbiter.snapshot()}

    def run_tts_training(
        self,
        *,
        operation_id: str,
        dataset_id: str,
        cancel_event: threading.Event,
        on_stage=None,
        operation_started: bool = False,
    ) -> dict:
        if not operation_started:
            self.arbiter.start_operation(
                operation_id,
                "tts_training",
                stage="training_preflight",
                deadline_epoch=int(time.time()) + WORKSTATION_TTS_TRAINING_MAX_SECONDS,
            )
        self._sync_inhibitor()
        try:
            started = time.monotonic()
            def training_stage(stage: str) -> None:
                self.arbiter.update_operation_stage(operation_id, stage)
                if on_stage:
                    on_stage(stage)

            dataset_root = (
                self.config.paths.data / "datasets" / "prepared" / dataset_id
            )
            try:
                resolved_dataset = dataset_root.resolve(strict=True)
                prepared_root = (
                    self.config.paths.data / "datasets" / "prepared"
                ).resolve(strict=True)
                report = json.loads(
                    (resolved_dataset / "preparation_result.json").read_text(
                        encoding="utf-8"
                    )
                )
                readiness = str((report.get("readiness") or {}).get("status") or "")
                training_list = Path(str(report.get("training_list") or "")).resolve(strict=True)
                recommendation_file = Path(
                    str(report.get("recommended_config") or "")
                ).resolve(strict=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise WorkloadError(
                    "dataset_not_prepared", "Training dataset has not passed preparation."
                ) from exc
            if (
                resolved_dataset.parent != prepared_root
                or str(report.get("status") or "") != "complete"
                or readiness == "BLOCKED_SPLIT"
                or resolved_dataset not in training_list.parents
                or resolved_dataset not in recommendation_file.parents
            ):
                raise WorkloadError(
                    "dataset_not_ready", "Training dataset readiness gate did not pass."
                )
            self._training_storage_gate()
            self._prepare_non_ollama_gpu()
            experiment_root = self.config.paths.data / "checkpoints" / dataset_id
            gpt_config, sovits_config = self.gpt_sovits.prepare_training(
                dataset_id=dataset_id,
                training_list=training_list,
                recommendation_file=recommendation_file,
                experiment_root=experiment_root,
                timeout_seconds=WORKSTATION_TTS_TRAINING_MAX_SECONDS,
                cancel_event=cancel_event,
                on_stage=training_stage,
                resource_gate=self._training_storage_gate,
            )
            elapsed = max(0, int(time.monotonic() - started))
            remaining = WORKSTATION_TTS_TRAINING_MAX_SECONDS - elapsed
            if remaining <= 0:
                raise WorkloadError(
                    "training_timeout", "GPT-SoVITS training exceeded its safe deadline."
                )
            self.gpt_sovits.train(
                dataset_id=dataset_id,
                gpt_config_file=gpt_config,
                sovits_config_file=sovits_config,
                allowed_config_root=experiment_root,
                timeout_seconds=remaining,
                cancel_event=cancel_event,
                on_stage=training_stage,
                resource_gate=self._training_storage_gate,
            )
            result_path = experiment_root / "training-result.json"
            temporary = result_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "schema_version": 1,
                "dataset_id": dataset_id,
                "status": "complete",
                "completed_epoch": int(time.time()),
                "profile_receipt": str(experiment_root / "profiles/receipt.json"),
                "auto_activated": False,
            }, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(result_path)
            self.arbiter.finish_operation(operation_id, success=True)
            return {"trained": True, "dataset_id": dataset_id}
        except WorkloadError as exc:
            if exc.code == "cancelled":
                self.arbiter.cancel_operation(operation_id)
            else:
                self.arbiter.finish_operation(operation_id, success=False, error_code=exc.code)
            raise
        finally:
            self._sync_inhibitor()

    def run_dataset_preparation(
        self,
        *,
        operation_id: str,
        dataset_id: str,
        speaker: str,
        cancel_event: threading.Event,
        operation_started: bool = False,
    ) -> dict:
        if not operation_started:
            self.arbiter.start_operation(
                operation_id,
                "maintenance",
                stage="dataset_preparation",
                deadline_epoch=(
                    int(time.time()) + WORKSTATION_DATASET_PREPARATION_MAX_SECONDS
                ),
            )
        self._sync_inhibitor()
        try:
            self._dataset_storage_gate(projected=True)
            output = self.dataset_preparation.prepare(
                dataset_id=dataset_id,
                speaker=speaker,
                cancel_event=cancel_event,
                resource_gate=self._dataset_storage_gate,
            )
            self.arbiter.finish_operation(operation_id, success=True)
            return {"prepared": True, "dataset_id": dataset_id, "path": str(output)}
        except WorkloadError as exc:
            if exc.code == "cancelled":
                self.arbiter.cancel_operation(operation_id)
            else:
                self.arbiter.finish_operation(
                    operation_id, success=False, error_code=exc.code
                )
            raise
        finally:
            self._sync_inhibitor()

    def run_chat(
        self,
        *,
        operation_id: str,
        model: str,
        messages: list[dict],
        think: bool,
        deadline_epoch: int,
        cancel_event: threading.Event,
        on_started=None,
        on_delta=None,
    ) -> tuple[str, dict]:
        self.arbiter.start_operation(operation_id, "text", deadline_epoch=deadline_epoch, stage="generating")
        self._sync_inhibitor()
        try:
            self._prepare_ollama_gpu()
            result = self.ollama.chat(
                model=model,
                messages=messages,
                think=think,
                keep_alive=self.config.workloads.ollama.text_keep_alive,
                timeout_seconds=self._remaining(deadline_epoch),
                on_started=on_started,
                on_delta=on_delta,
                cancel_event=cancel_event,
            )
            self.arbiter.record_operation_timings(
                operation_id,
                self._ollama_timings(result[1]),
            )
            self.arbiter.finish_operation(operation_id, success=True)
            return result
        except WorkloadError as exc:
            if exc.code == "cancelled":
                self.arbiter.cancel_operation(operation_id)
            else:
                self.arbiter.finish_operation(operation_id, success=False, error_code=exc.code)
            raise
        finally:
            self._sync_inhibitor()

    def run_workstation_job(
        self,
        job: dict,
        *,
        on_stage=None,
        on_provider_started=None,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        kind = job["job_kind"]
        operation_id = job["operation_id"]
        session_id = job.get("session_id") or ""
        payload = job["payload"]
        deadline = job["deadline_epoch"]
        if kind == "voice.reserve":
            return self.reserve_voice(
                session_id,
                expires_epoch=int(payload.get("session_expires_epoch") or 0),
                cancel_event=cancel_event,
            )
        if kind == "voice.release":
            return self.release_voice(session_id)
        mapped_kind = {"asr": "asr", "rag": "rag", "voice_text": "voice_text", "tts": "tts"}[kind]
        self.arbiter.start_operation(
            operation_id,
            mapped_kind,
            session_id=session_id,
            turn_id=job.get("turn_id") or "",
            stage=job.get("stage") or kind,
            deadline_epoch=deadline,
        )
        self._sync_inhibitor()
        def stage_update(stage: str) -> None:
            self.arbiter.update_operation_stage(operation_id, stage)
            if on_stage:
                on_stage(stage)

        try:
            self._raise_if_cancelled(cancel_event)
            stage_update(job.get("stage") or kind)
            if kind == "asr":
                result = self._asr(
                    payload, deadline, cancel_event=cancel_event, on_stage=stage_update,
                )
            elif kind == "rag":
                result = self._rag(
                    payload, cancel_event=cancel_event, on_stage=stage_update,
                )
            elif kind == "voice_text":
                result = self._voice_text(
                    payload,
                    deadline,
                    cancel_event=cancel_event,
                    on_provider_started=on_provider_started,
                )
            else:
                result = self._tts(
                    payload, cancel_event=cancel_event, on_stage=stage_update,
                )
            self._raise_if_cancelled(cancel_event)
            timings = result.get("timings_ms") if isinstance(result, dict) else None
            if isinstance((result or {}).get("prepared_output"), dict):
                timings = result["prepared_output"].get("timings_ms")
            if isinstance(timings, dict):
                self.arbiter.record_operation_timings(operation_id, timings)
            if (
                kind == "tts"
                and isinstance((result or {}).get("prepared_output"), dict)
            ):
                # Persist the externally-owned phase before handing the local
                # file back to Node. If IPC drops at this boundary, Node
                # restart reconciliation can still fail the operation safely.
                self.arbiter.mark_tts_output_upload(operation_id)
            else:
                self.arbiter.finish_operation(operation_id, success=True)
            return result
        except WorkloadError as exc:
            if exc.code == "cancelled":
                self.arbiter.cancel_operation(operation_id)
            else:
                self.arbiter.finish_operation(operation_id, success=False, error_code=exc.code)
            raise
        finally:
            self._sync_inhibitor()

    def _asr(
        self,
        payload: dict,
        deadline_epoch: int,
        *,
        cancel_event: threading.Event | None = None,
        on_stage=None,
    ) -> dict:
        download = payload.get("download") if isinstance(payload.get("download"), dict) else {}
        suffix = str(payload.get("file_ext") or "webm")
        if not suffix.isalnum() or len(suffix) > 8:
            raise WorkloadError("invalid_audio_extension", "Audio extension is invalid.")
        self.config.paths.cache.mkdir(parents=True, exist_ok=True, mode=0o750)
        handle = tempfile.NamedTemporaryFile(prefix="asr-", suffix="." + suffix, dir=self.config.paths.cache, delete=False)
        path = Path(handle.name)
        handle.close()
        timings = {}
        try:
            self._raise_if_cancelled(cancel_event)
            if on_stage:
                on_stage("r2_download")
            started = time.monotonic()
            transfer = download_to_path(
                str(download.get("url") or ""),
                path,
                max_bytes=LOCAL_PRACTICE_AUDIO_MAX_BYTES,
                expected_bytes=int(download.get("byte_size") or 0),
                expected_sha256=str(download.get("sha256") or ""),
                timeout_seconds=self._remaining(deadline_epoch),
            )
            timings["r2_download"] = int((time.monotonic() - started) * 1_000)
            self._raise_if_cancelled(cancel_event)
            if on_stage:
                on_stage("media_probe")
            started = time.monotonic()
            media = probe_audio(
                path,
                maximum_seconds=LOCAL_PRACTICE_AUDIO_MAX_SECONDS,
                declared_mime=str(payload.get("mime_type") or ""),
            )
            timings["media_probe"] = int((time.monotonic() - started) * 1_000)
            self._prepare_non_ollama_gpu()
            self._raise_if_cancelled(cancel_event)
            if on_stage:
                on_stage("asr")
            started = time.monotonic()
            transcript = self.asr.transcribe(
                path,
                timeout_seconds=self._remaining(deadline_epoch),
                cancel_event=cancel_event,
            )
            timings["asr"] = int((time.monotonic() - started) * 1_000)
            self._raise_if_cancelled(cancel_event)
            return {
                "transcript": transcript["text"], "asr": transcript,
                "media": media, "transfer": transfer, "timings_ms": timings,
            }
        finally:
            path.unlink(missing_ok=True)
            self.asr.unload()

    def _rag(
        self, payload: dict, *, cancel_event: threading.Event | None = None,
        on_stage=None,
    ) -> dict:
        query = str(payload.get("query") or "")
        self._prepare_ollama_gpu()
        self._raise_if_cancelled(cancel_event)
        if on_stage:
            on_stage("rag_retrieval")
        started = time.monotonic()
        result = {
            "local": self.rag.retrieve(
                query, top_k=int(payload.get("top_k") or 6)
            )
        }
        timings = {"rag_retrieval": int((time.monotonic() - started) * 1_000)}
        result["timings_ms"] = timings
        return result

    def _voice_text(
        self,
        payload: dict,
        deadline_epoch: int,
        *,
        cancel_event: threading.Event | None = None,
        on_provider_started=None,
    ) -> dict:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise WorkloadError("invalid_messages", "Voice Coach messages are invalid.")
        self._prepare_ollama_gpu()
        mode = resolve_lmc_ai_mode_options()["fast"]
        started = time.monotonic()
        text, usage = self.ollama.chat(
            model=str(mode["model"]),
            messages=messages,
            think=False,
            keep_alive=self.config.workloads.ollama.voice_keep_alive,
            timeout_seconds=self._remaining(deadline_epoch),
            cancel_event=cancel_event,
            on_started=on_provider_started,
        )
        if len(text.encode("utf-8")) > LMC_AI_OUTPUT_MAX_BYTES:
            raise WorkloadError("output_too_large", "Voice Coach answer exceeds the output limit.")
        return {
            "text": text, "usage": usage, "context_length": LMC_AI_CONTEXT_LENGTH,
            "timings_ms": self._ollama_timings({
                **usage,
                "wall_duration_ms": int((time.monotonic() - started) * 1_000),
            }),
        }

    def _tts(
        self, payload: dict, *, cancel_event: threading.Event | None = None,
        on_stage=None,
    ) -> dict:
        self._prepare_non_ollama_gpu()
        self._raise_if_cancelled(cancel_event)
        started = False
        timings = {}
        try:
            if on_stage:
                on_stage("tts_model_load")
            load_started = time.monotonic()
            self._set_gpt_sovits_service("start")
            started = True
            self.gpt_sovits.wait_until_ready()
            timings["tts_model_load"] = int((time.monotonic() - load_started) * 1_000)
            if on_stage:
                on_stage("tts_synthesis")
            synthesis_started = time.monotonic()
            result = self.gpt_sovits.synthesize(
                str(payload.get("text") or ""),
                output_dir=self.config.paths.cache,
            )
            timings["tts_synthesis"] = int((time.monotonic() - synthesis_started) * 1_000)
            output_path = Path(result["path"])
            probe_started = time.monotonic()
            measured = probe_audio(
                output_path,
                maximum_seconds=LOCAL_PRACTICE_AUDIO_MAX_SECONDS,
                declared_mime=str(result.get("mime_type") or ""),
            )
            result["duration_seconds"] = measured["duration_seconds"]
            result["sample_rate"] = measured["sample_rate"]
            result["channels"] = measured["channels"]
            timings["tts_probe"] = int((time.monotonic() - probe_started) * 1_000)
            self._raise_if_cancelled(cancel_event)
        finally:
            if started:
                self._set_gpt_sovits_service("stop")
        return {
            "prepared_output": {
                "path": str(result.pop("path")),
                **result,
                "timings_ms": timings,
            }
        }
