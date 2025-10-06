"""
Erni-Foto Agency Main Application
Orchestrates all agents for end-to-end image processing workflow
"""

import asyncio
import base64
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import openai_agents
from agents import InputGuardrailTripwireTriggered, SQLiteSession

from .config.settings import get_config
from .di_container import DIContainer, get_container
from .erni_agents.sharepoint_uploader import _upload_image_to_sharepoint_impl
from .erni_agents.structured_vision_analyzer import VisionGuardrailContext
from .erni_agents.orchestrator import WorkflowContext
from .models.function_tool_models import (
    DetectedFieldValue,
    SharePointUploadInput,
    SharePointUploadOutput,
)
from .models.sharepoint_models import SharePointMetadata, Status

logger = structlog.get_logger(__name__)


MULTI_VALUE_FIELDS = {
    "Material",
    "Holzart",
    "Sparte_x002f_Kategorie",
    "B_x00f6_denDecken",
    "Fassade",
    "Bauteil",
    "Ausf_x00fc_hrung",
    "Farbbehandlung",
    "G_x00fc_llelager",
}


ProgressEvent = dict[str, Any]
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]] | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_event(
    event_type: str,
    run_id: str,
    agent_id: str,
    data: dict[str, Any],
    *,
    thread_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "run_id": run_id,
        "agent_id": agent_id,
        "timestamp": _now_iso(),
        "data": data,
    }
    if thread_id:
        event["thread_id"] = thread_id
    if extras:
        event.update(extras)
    return event


async def _emit(progress: ProgressCallback, event: dict[str, Any]) -> None:
    if progress is not None:
        await progress(event)


async def _emit_thinking_step(
    progress: ProgressCallback,
    run_id: str,
    agent_id: str,
    step: str,
    *,
    thread_id: str | None = None,
) -> None:
    """Emit THINKING_STEP event for agent reasoning display."""
    await _emit(
        progress,
        _make_event(
            "THINKING_STEP",
            run_id,
            agent_id,
            {"step": step, "message": step},
            thread_id=thread_id,
        ),
    )


async def _emit_tool_result_stream(
    progress: ProgressCallback,
    run_id: str,
    agent_id: str,
    chunk: Any,
    *,
    tool_call_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Emit TOOL_RESULT_STREAM event for streaming tool execution results."""
    event = _make_event(
        "TOOL_RESULT_STREAM",
        run_id,
        agent_id,
        {"chunk": chunk, "content": chunk},
        thread_id=thread_id,
    )
    if tool_call_id:
        event["tool_call_id"] = tool_call_id
    await _emit(progress, event)


def _format_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _final_output(result: Any) -> Any:
    """Return agent final_output if present, otherwise the raw result."""
    return getattr(result, "final_output", result)


# Pydantic models for API
class AgentRequest(BaseModel):
    agent: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    image_data: str | None = None
    workflow_config: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    success: bool
    result: dict[str, Any] | str | None = None
    error: str | None = None
    agent: str
    processing_time: float | None = None


class ErniPhotoAgency:
    """
    Main orchestrator for Erni-Foto Agency
    Coordinates all agents for complete image processing workflow

    Uses Dependency Injection for better testability and maintainability.
    """

    def __init__(self, container: DIContainer | None = None) -> None:
        """
        Initialize Erni-Foto Agency

        Args:
            container: Optional DI container. If None, uses global container.
        """
        # Use provided container or get global one
        self.container = container or get_container()

        # Convenience properties for backward compatibility
        self.config = self.container.config
        self.schema_extractor = self.container.schema_extractor
        self.vision_analyzer = self.container.vision_analyzer
        self.sharepoint_uploader = self.container.sharepoint_uploader
        self.validation_reporter = self.container.validation_reporter
        self.workflow_orchestrator = self.container.workflow_orchestrator
        self.cache_manager = self.container.cache_manager
        self.batch_processor = self.container.batch_processor
        self.image_processor = self.container.image_processor
        self.cost_optimizer = self.container.cost_optimizer
        self.circuit_breaker = self.container.circuit_breaker
        self.metrics_collector = self.container.metrics_collector
        self.health_checker = self.container.health_checker

        # Session storage
        self._session_dir = (
            Path(self.config.cache.redis_url.replace("redis://", ""))
            if self.config.cache.redis_url.startswith("redis:///")
            else Path(__file__).parent.parent / "sessions"
        )
        if not self._session_dir.exists():
            self._session_dir.mkdir(parents=True, exist_ok=True)
        self._session_db_path = self._session_dir / "erni_sessions.sqlite"
        self._sessions: dict[str, SQLiteSession] = {}

        logger.info("Erni-Foto Agency initialized with DI container")

    async def initialize(self) -> None:
        """Initialize all components via DI container"""
        await self.container.initialize()
        logger.info("All components initialized successfully")

    async def shutdown(self) -> None:
        """Shutdown all components gracefully"""
        await self.container.shutdown()
        logger.info("All components shutdown successfully")

    def _get_session(self, session_id: str) -> SQLiteSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = SQLiteSession(session_id, str(self._session_db_path))
        return self._sessions[session_id]

    def _build_orchestration_prompt(
        self, workflow: WorkflowContext, request: "AgentRequest"
    ) -> str:
        image_lines = "\n".join(f"- {path}" for path in workflow.image_paths) or "(no file paths)"
        user_messages = []
        for message in request.messages[-5:]:
            role = message.get("role", "user")
            content = message.get("content") or message.get("text") or ""
            user_messages.append(f"[{role}] {content}")

        prompt_sections = [
            "Coordinate the Erni-Foto Agency workflow end-to-end.",
            f"SharePoint site URL: {workflow.sharepoint_site_url}",
            f"Library name: {workflow.library_name}",
            "Images to process:",
            image_lines,
            "When handing off, always include JSON arguments that match the required schema.",
            "Recent user context:",
            "\n".join(user_messages) if user_messages else "(no recent messages)",
        ]

        if request.workflow_config.get("notes"):
            prompt_sections.append(
                "Additional notes:\n" + str(request.workflow_config.get("notes"))
            )

        return "\n\n".join(section for section in prompt_sections if section)

    async def run_sharepoint_schema(
        self,
        request: "AgentRequest",
        *,
        progress: ProgressCallback = None,
        run_id: str | None = None,
    ) -> "AgentResponse":
        """Run SharePoint schema extraction via orchestrator with handoff.

        This method now delegates to the orchestrator instead of direct agent invocation,
        ensuring consistent handoff-based architecture.
        """
        agent_id = "sharepoint-schema-extractor"
        run_id = run_id or str(uuid4())
        thread_id = request.workflow_config.get("thread_id")
        session_id = request.workflow_config.get("session_id") or run_id
        session = self._get_session(session_id)
        start_time = time.time()

        sharepoint_site_url = request.workflow_config.get(
            "sharepoint_site_url",
            os.getenv("SHAREPOINT_SITE_URL", "https://company.sharepoint.com/sites/erni"),
        )
        library_name = request.workflow_config.get("library_name", "Fertige Referenzfotos")

        await _emit(
            progress,
            _make_event(
                "RUN_STARTED",
                run_id,
                agent_id,
                {
                    "message": f"Извлекаю схему для библиотеки '{library_name}'.",
                    "session_id": session_id,
                },
                thread_id=thread_id,
            ),
        )

        await _emit(
            progress,
            _make_event(
                "THINKING_STEP",
                run_id,
                agent_id,
                {
                    "step_id": f"thinking-{uuid4()}",
                    "message": f"Подключаюсь к SharePoint сайту {sharepoint_site_url}.",
                },
                thread_id=thread_id,
            ),
        )

        tool_call_id = f"tool-{uuid4()}"
        tool_name = "SharePointSchemaExtractor"
        await _emit(
            progress,
            _make_event(
                "TOOL_CALL_START",
                run_id,
                agent_id,
                {
                    "tool_name": tool_name,
                    "tool_call_name": tool_name,
                    "args": {
                        "library_name": library_name,
                        "sharepoint_site_url": sharepoint_site_url,
                    },
                },
                thread_id=thread_id,
                extras={
                    "tool_call_id": tool_call_id,
                    "tool_call_name": tool_name,
                },
            ),
        )

        schema_runner = openai_agents.Runner()
        try:
            schema_result = await schema_runner.run(
                starting_agent=self.schema_extractor,
                input=(
                    f"Extract schema for SharePoint library '{library_name}' "
                    f"at {sharepoint_site_url}"
                ),
                session=session,
            )
            result_payload = _final_output(schema_result)

            await _emit(
                progress,
                _make_event(
                    "TOOL_RESULT_STREAM",
                    run_id,
                    agent_id,
                    {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                        "chunk": {
                            "schema_preview": result_payload,
                        },
                    },
                    thread_id=thread_id,
                    extras={
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                    },
                ),
            )

            total_fields = 0
            if isinstance(result_payload, dict):
                total_fields = result_payload.get("total_fields") or len(
                    result_payload.get("fields", [])
                )

            await _emit(
                progress,
                _make_event(
                    "STATE_UPDATE",
                    run_id,
                    agent_id,
                    {
                        "changes": {
                            "sharepoint_schema": {
                                "library_name": library_name,
                                "total_fields": total_fields,
                            }
                        }
                    },
                    thread_id=thread_id,
                ),
            )

            processing_time = time.time() - start_time
            response = AgentResponse(
                success=True,
                result=result_payload,
                agent=agent_id,
                processing_time=processing_time,
            )

            await _emit(
                progress,
                _make_event(
                    "RUN_FINISHED",
                    run_id,
                    agent_id,
                    {
                        "status": "completed",
                        "result": response.result,
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )

            return response

        except Exception as exc:  # pragma: no cover - defensive
            processing_time = time.time() - start_time
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": str(exc),
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            logger.error(
                "SharePoint schema extraction failed",
                error=str(exc),
                library_name=library_name,
            )
            return AgentResponse(
                success=False,
                result=None,
                error=str(exc),
                agent=agent_id,
                processing_time=processing_time,
            )

    async def run_vision_analyzer(
        self,
        request: "AgentRequest",
        *,
        progress: ProgressCallback = None,
        run_id: str | None = None,
    ) -> "AgentResponse":
        agent_id = "vision-analyzer"
        run_id = run_id or str(uuid4())
        thread_id = request.workflow_config.get("thread_id")
        session_id = request.workflow_config.get("session_id") or run_id
        session = self._get_session(session_id)
        start_time = time.time()
        temp_image_path: str | None = None
        inline_image_bytes: bytes | None = None

        await _emit(
            progress,
            _make_event(
                "RUN_STARTED",
                run_id,
                agent_id,
                {
                    "message": "Готовлю анализ изображения.",
                    "session_id": session_id,
                },
                thread_id=thread_id,
            ),
        )

        try:
            original_filename = request.workflow_config.get("image_filename")
            if request.image_data:
                try:
                    header, delimiter, payload = request.image_data.partition(",")
                    if delimiter and not payload:
                        raise ValueError("Empty data URI payload")
                    base64_payload = payload if delimiter else request.image_data
                    image_bytes = base64.b64decode(base64_payload, validate=True)
                    inline_image_bytes = image_bytes
                    suffix = (
                        Path(original_filename).suffix if original_filename else ".jpg"
                    )
                    stem = Path(original_filename).stem if original_filename else "temp_image"
                    temp_image_path = f"/tmp/{stem}_{int(time.time())}{suffix}"
                    with open(temp_image_path, "wb") as tmp_file:
                        tmp_file.write(image_bytes)
                    image_path = temp_image_path
                except Exception as exc:
                    await _emit(
                        progress,
                        _make_event(
                            "RUN_FAILED",
                            run_id,
                            agent_id,
                            {
                                "error": f"Invalid image data: {exc}",
                            },
                            thread_id=thread_id,
                        ),
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid image data: {exc}",
                    ) from exc
            else:
                image_path = request.workflow_config.get("image_path", "/tmp/test_image.jpg")

            await _emit(
                progress,
                _make_event(
                    "STATE_UPDATE",
                    run_id,
                    agent_id,
                    {
                        "changes": {
                            "selected_image": {
                                "path": image_path,
                                "provided_inline": bool(request.image_data),
                            }
                        }
                    },
                    thread_id=thread_id,
                ),
            )

            tool_call_id = f"tool-{uuid4()}"
            tool_name = "StructuredVisionAnalyzer"
            await _emit(
                progress,
                _make_event(
                    "TOOL_CALL_START",
                    run_id,
                    agent_id,
                    {
                        "tool_name": tool_name,
                        "tool_call_name": tool_name,
                        "args": {"image_path": image_path},
                    },
                    thread_id=thread_id,
                    extras={
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                    },
                ),
            )

            vision_runner = openai_agents.Runner()
            guardrail_context = VisionGuardrailContext(
                inline_image_base64=request.image_data,
                inline_image_bytes=inline_image_bytes,
                max_image_size_mb=self.config.performance.max_image_size_mb,
                allowed_mime_types=(
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "image/bmp",
                    "image/gif",
                ),
            )

            try:
                vision_result = await vision_runner.run(
                    starting_agent=self.vision_analyzer,
                    input=(
                        "Analyze construction image at "
                        f"{image_path} using JSON schema for structured output. "
                        "Focus on detecting fields for SharePoint library with 23 fields and 119 choice values."
                    ),
                    session=session,
                    context=guardrail_context,
                )
            except InputGuardrailTripwireTriggered as guard_exc:
                processing_time = time.time() - start_time
                info = guard_exc.guardrail_result.output.output_info or {}
                await _emit(
                    progress,
                    _make_event(
                        "INTERRUPT_REQUEST",
                        run_id,
                        agent_id,
                        {
                            "reason": info.get("reason", "Input rejected by guardrail"),
                        },
                        thread_id=thread_id,
                    ),
                )
                await _emit(
                    progress,
                    _make_event(
                        "RUN_FAILED",
                        run_id,
                        agent_id,
                        {
                            "error": info.get("reason", "Input guardrail triggered"),
                            "processing_time": processing_time,
                        },
                        thread_id=thread_id,
                    ),
                )
                return AgentResponse(
                    success=False,
                    result=None,
                    error=info.get("reason", "Input guardrail triggered"),
                    agent=agent_id,
                    processing_time=processing_time,
                )

            result_payload = _final_output(vision_result)

            if isinstance(result_payload, dict):
                detected_fields = result_payload.get("detected_fields") or []
                for field in detected_fields[:10]:
                    await _emit(
                        progress,
                        _make_event(
                            "TOOL_RESULT_STREAM",
                            run_id,
                            agent_id,
                            {
                                "tool_call_id": tool_call_id,
                                "tool_call_name": tool_name,
                                "chunk": field,
                            },
                            thread_id=thread_id,
                            extras={
                                "tool_call_id": tool_call_id,
                                "tool_call_name": tool_name,
                            },
                        ),
                    )

            await _emit(
                progress,
                _make_event(
                    "TOOL_RESULT_STREAM",
                    run_id,
                    agent_id,
                    {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                        "chunk": {
                            "detected_fields": result_payload.get("detected_fields")
                            if isinstance(result_payload, dict)
                            else result_payload,
                        },
                    },
                    thread_id=thread_id,
                    extras={
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                    },
                ),
            )

            detected_count = 0
            if isinstance(result_payload, dict):
                detected_fields = result_payload.get("detected_fields") or []
                if isinstance(detected_fields, list):
                    detected_count = len(detected_fields)

            await _emit(
                progress,
                _make_event(
                    "STATE_UPDATE",
                    run_id,
                    agent_id,
                    {
                        "changes": {
                            "analysis": {
                                "detected_fields": detected_count,
                                "pii_detected": bool(
                                    isinstance(result_payload, dict)
                                    and result_payload.get("pii_detected")
                                ),
                            }
                        }
                    },
                    thread_id=thread_id,
                ),
            )

            processing_time = time.time() - start_time
            response = AgentResponse(
                success=True,
                result=result_payload,
                agent=agent_id,
                processing_time=processing_time,
            )

            await _emit(
                progress,
                _make_event(
                    "RUN_FINISHED",
                    run_id,
                    agent_id,
                    {
                        "status": "completed",
                        "result": response.result,
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )

            return response

        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            processing_time = time.time() - start_time
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": str(exc),
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            logger.error("Vision analysis failed", error=str(exc))
            return AgentResponse(
                success=False,
                result=None,
                error=str(exc),
                agent=agent_id,
                processing_time=processing_time,
            )
        finally:
            if request.image_data and temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.remove(temp_image_path)
                except OSError:
                    logger.debug(
                        "Failed to remove temporary image",
                        temp_image_path=temp_image_path,
                    )

    async def run_sharepoint_uploader(
        self,
        request: "AgentRequest",
        *,
        progress: ProgressCallback = None,
        run_id: str | None = None,
    ) -> "AgentResponse":
        agent_id = "sharepoint-uploader"
        run_id = run_id or str(uuid4())
        thread_id = request.workflow_config.get("thread_id")
        session_id = request.workflow_config.get("session_id") or run_id
        session = self._get_session(session_id)
        start_time = time.time()

        library_name = request.workflow_config.get("library_name", "Fertige Referenzfotos")
        image_paths = request.workflow_config.get("image_paths", [])

        await _emit(
            progress,
            _make_event(
                "RUN_STARTED",
                run_id,
                agent_id,
                {
                    "message": f"Загружаю {len(image_paths)} изображений в SharePoint.",
                    "session_id": session_id,
                },
                thread_id=thread_id,
            ),
        )

        if not image_paths:
            processing_time = time.time() - start_time
            error_message = "No image paths provided for upload"
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": error_message,
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            return AgentResponse(
                success=False,
                result=None,
                error=error_message,
                agent=agent_id,
                processing_time=processing_time,
            )

        await _emit(
            progress,
            _make_event(
                "STATE_UPDATE",
                run_id,
                agent_id,
                {
                    "changes": {
                        "upload_batch": {
                            "total_images": len(image_paths),
                            "library_name": library_name,
                        }
                    }
                },
                thread_id=thread_id,
            ),
        )

        tool_call_id = f"tool-{uuid4()}"
        tool_name = "SharePointUploader"
        await _emit(
            progress,
            _make_event(
                "TOOL_CALL_START",
                run_id,
                agent_id,
                {
                    "tool_name": tool_name,
                    "tool_call_name": tool_name,
                    "args": {
                        "library_name": library_name,
                        "image_count": len(image_paths),
                    },
                },
                thread_id=thread_id,
                extras={
                    "tool_call_id": tool_call_id,
                    "tool_call_name": tool_name,
                },
            ),
        )

        try:
            upload_runner = openai_agents.Runner()
            upload_result = await upload_runner.run(
                starting_agent=self.sharepoint_uploader,
                input=(
                    f"Batch upload {len(image_paths)} images with metadata to SharePoint library '{library_name}'. "
                    "Apply PII protection and validate all metadata against 23-field schema."
                ),
                session=session,
            )

            result_payload = _final_output(upload_result)
            if isinstance(result_payload, dict):
                for upload in (result_payload.get("upload_results") or [])[:10]:
                    await _emit(
                        progress,
                        _make_event(
                            "TOOL_RESULT_STREAM",
                            run_id,
                            agent_id,
                            {
                                "tool_call_id": tool_call_id,
                                "tool_call_name": tool_name,
                                "chunk": upload,
                            },
                            thread_id=thread_id,
                            extras={
                                "tool_call_id": tool_call_id,
                                "tool_call_name": tool_name,
                            },
                        ),
                    )
            await _emit(
                progress,
                _make_event(
                    "TOOL_RESULT_STREAM",
                    run_id,
                    agent_id,
                    {
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                        "chunk": {
                            "summary": result_payload,
                        },
                    },
                    thread_id=thread_id,
                    extras={
                        "tool_call_id": tool_call_id,
                        "tool_call_name": tool_name,
                    },
                ),
            )

            processing_time = time.time() - start_time
            response = AgentResponse(
                success=True,
                result=result_payload,
                agent=agent_id,
                processing_time=processing_time,
            )

            await _emit(
                progress,
                _make_event(
                    "RUN_FINISHED",
                    run_id,
                    agent_id,
                    {
                        "status": "completed",
                        "result": response.result,
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )

            return response

        except Exception as exc:  # pragma: no cover - defensive
            processing_time = time.time() - start_time
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": str(exc),
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            logger.error("SharePoint upload failed", error=str(exc))
            return AgentResponse(
                success=False,
                result=None,
                error=str(exc),
                agent=agent_id,
                processing_time=processing_time,
            )

    async def run_validation_report(
        self,
        request: "AgentRequest",
        *,
        progress: ProgressCallback = None,
        run_id: str | None = None,
    ) -> "AgentResponse":
        agent_id = "validation-reporter"
        run_id = run_id or str(uuid4())
        thread_id = request.workflow_config.get("thread_id")
        session_id = request.workflow_config.get("session_id") or run_id
        session = self._get_session(session_id)
        start_time = time.time()

        batch_id = request.workflow_config.get("batch_id", f"batch_{int(time.time())}")
        total_images = request.workflow_config.get("total_images", 1)

        await _emit(
            progress,
            _make_event(
                "RUN_STARTED",
                run_id,
                agent_id,
                {
                    "message": f"Генерирую отчёт по партии {batch_id}.",
                    "session_id": session_id,
                },
                thread_id=thread_id,
            ),
        )

        await _emit(
            progress,
            _make_event(
                "STATE_UPDATE",
                run_id,
                agent_id,
                {
                    "changes": {
                        "validation": {
                            "batch_id": batch_id,
                            "total_images": total_images,
                        }
                    }
                },
                thread_id=thread_id,
            ),
        )

        tool_call_id = f"tool-{uuid4()}"
        tool_name = "ValidationReporter"
        await _emit(
            progress,
            _make_event(
                "TOOL_CALL_START",
                run_id,
                agent_id,
                {
                    "tool_name": tool_name,
                    "tool_call_name": tool_name,
                    "args": {"batch_id": batch_id, "total_images": total_images},
                },
                thread_id=thread_id,
                extras={
                    "tool_call_id": tool_call_id,
                    "tool_call_name": tool_name,
                },
            ),
        )

        try:
            report_runner = openai_agents.Runner()
            validation_result = await report_runner.run(
                starting_agent=self.validation_reporter,
                input=(
                    f"Generate comprehensive validation report for batch {batch_id} with "
                    f"{total_images} processed images. Analyze quality metrics, "
                    "SLO compliance, field validation accuracy, and PII detection effectiveness."
                ),
                session=session,
            )

            result_payload = _final_output(validation_result)

            if isinstance(result_payload, dict):
                highlights = {
                    "slo_compliance": result_payload.get("slo_compliance"),
                    "summary": result_payload.get("summary"),
                }
                await _emit(
                    progress,
                    _make_event(
                        "TOOL_RESULT_STREAM",
                        run_id,
                        agent_id,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_name,
                            "chunk": highlights,
                        },
                        thread_id=thread_id,
                        extras={
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_name,
                        },
                    ),
                )
            else:
                await _emit(
                    progress,
                    _make_event(
                        "TOOL_RESULT_STREAM",
                        run_id,
                        agent_id,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_name,
                            "chunk": {
                                "summary": result_payload,
                            },
                        },
                        thread_id=thread_id,
                        extras={
                            "tool_call_id": tool_call_id,
                            "tool_call_name": tool_name,
                        },
                    ),
                )

            processing_time = time.time() - start_time
            response = AgentResponse(
                success=True,
                result=result_payload,
                agent=agent_id,
                processing_time=processing_time,
            )

            await _emit(
                progress,
                _make_event(
                    "RUN_FINISHED",
                    run_id,
                    agent_id,
                    {
                        "status": "completed",
                        "result": response.result,
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )

            return response

        except Exception as exc:  # pragma: no cover - defensive
            processing_time = time.time() - start_time
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": str(exc),
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            logger.error("Validation report generation failed", error=str(exc))
            return AgentResponse(
                success=False,
                result=None,
                error=str(exc),
                agent=agent_id,
                processing_time=processing_time,
            )

    async def orchestrate_workflow(
        self,
        request: "AgentRequest",
        *,
        progress: ProgressCallback = None,
        run_id: str | None = None,
    ) -> "AgentResponse":
        agent_id = "workflow-orchestrator"
        run_id = run_id or str(uuid4())
        thread_id = request.workflow_config.get("thread_id")
        session_id = request.workflow_config.get("session_id") or run_id
        session = self._get_session(session_id)

        start_time = time.time()

        workflow = WorkflowContext(
            sharepoint_site_url=request.workflow_config.get(
                "sharepoint_site_url",
                os.getenv("SHAREPOINT_SITE_URL", "https://company.sharepoint.com/sites/erni"),
            ),
            library_name=request.workflow_config.get(
                "library_name",
                self.config.sharepoint.library_name,
            ),
            image_paths=request.workflow_config.get("image_paths", []),
            session_id=session_id,
            force_schema_refresh=bool(request.workflow_config.get("force_schema_refresh", False)),
        )

        await _emit(
            progress,
            _make_event(
                "RUN_STARTED",
                run_id,
                agent_id,
                {
                    "message": "Оркеструю полный конвейер обработки изображений.",
                    "session_id": session_id,
                },
                thread_id=thread_id,
            ),
        )

        # Emit thinking steps for workflow planning
        await _emit_thinking_step(
            progress,
            run_id,
            agent_id,
            f"Планирую обработку {len(workflow.image_paths)} изображений для библиотеки '{workflow.library_name}'",
            thread_id=thread_id,
        )

        orchestrator_prompt = self._build_orchestration_prompt(workflow, request)
        runner = openai_agents.Runner()

        await _emit_thinking_step(
            progress,
            run_id,
            agent_id,
            "Инициализирую handoff-цепочку: Schema → Vision → Upload → Validation",
            thread_id=thread_id,
        )

        try:
            orchestrator_result = await runner.run(
                starting_agent=self.workflow_orchestrator,
                input=orchestrator_prompt,
                session=session,
                context=workflow,
            )
        except Exception as exc:  # pragma: no cover - defensive
            processing_time = time.time() - start_time
            await _emit(
                progress,
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {
                        "error": str(exc),
                        "processing_time": processing_time,
                    },
                    thread_id=thread_id,
                ),
            )
            logger.error("Workflow orchestration failed", error=str(exc))
            return AgentResponse(
                success=False,
                result=None,
                error=str(exc),
                agent=agent_id,
                processing_time=processing_time,
            )

        processing_time = time.time() - start_time
        payload = _final_output(orchestrator_result)
        summary = {
            "summary": payload,
            "session_id": session_id,
            "metadata": workflow.metadata,
        }

        await _emit(
            progress,
            _make_event(
                "STATE_UPDATE",
                run_id,
                agent_id,
                {"changes": {"workflow_metadata": workflow.metadata}},
                thread_id=thread_id,
            ),
        )

        await _emit(
            progress,
            _make_event(
                "RUN_FINISHED",
                run_id,
                agent_id,
                {
                    "status": "completed",
                    "result": summary,
                    "processing_time": processing_time,
                },
                thread_id=thread_id,
            ),
        )

        return AgentResponse(
            success=True,
            result=summary,
            agent=agent_id,
            processing_time=processing_time,
        )

    async def process_images_batch(
        self,
        image_paths: list[str],
        sharepoint_site_url: str,
        library_name: str = "Fertige Referenzfotos",
        folder_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Process a batch of images through complete workflow

        Args:
            image_paths: List of image file paths
            sharepoint_site_url: SharePoint site URL
            library_name: SharePoint library name
            folder_path: Optional folder path within library

        Returns:
            Complete processing result with validation report
        """

        batch_id = f"batch_{int(asyncio.get_event_loop().time())}"

        logger.info(
            "Starting batch processing",
            batch_id=batch_id,
            total_images=len(image_paths),
            library_name=library_name,
        )

        try:
            # Step 1: Extract SharePoint schema
            logger.info("Step 1: Extracting SharePoint schema")

            schema_runner = openai_agents.Runner()
            schema_result = await schema_runner.run(starting_agent=self.schema_extractor, input=
                f"Extract schema for SharePoint library '{library_name}' at {sharepoint_site_url}"
            )

            # Extract schema information from final_output
            schema_output = _final_output(schema_result)
            sharepoint_schema: dict[str, Any] | None = None

            if isinstance(schema_output, dict) and schema_output.get("schema"):
                sharepoint_schema = schema_output["schema"]
            else:
                # Fallback: retrieve the processed schema from cache populated by the agent.
                sharepoint_schema = await self.cache_manager.get_sharepoint_schema(
                    library_name
                )

            if not sharepoint_schema:
                raise Exception("Failed to extract SharePoint schema")

            logger.info(
                "SharePoint schema extracted",
                total_fields=sharepoint_schema.get("total_fields", 0),
                choice_values=sharepoint_schema.get("total_choice_values", 0),
            )

            # Step 2: Analyze images with Vision API
            logger.info("Step 2: Analyzing images with Vision API")

            processing_results = []

            for i, image_path in enumerate(image_paths):
                logger.info(f"Processing image {i + 1}/{len(image_paths)}: {image_path}")

                try:
                    vision_runner = openai_agents.Runner()
                    await vision_runner.run(
                        starting_agent=self.vision_analyzer,
                        input=(
                            "Analyze construction image at "
                            f"{image_path} using JSON schema for structured output. "
                            "Focus on detecting fields for SharePoint library with 23 fields and 119 choice values."
                        ),
                    )

                    cached_vision = await self._get_cached_vision_output(image_path)
                    if cached_vision:
                        vision_payload, processing_metadata = cached_vision
                        detected_fields = vision_payload.get("detected_fields", [])
                        metadata_dict = self._build_metadata_from_detection(
                            detected_fields, image_path, sharepoint_schema
                        )
                        metadata = {
                            "model_used": vision_payload.get("model_used"),
                            "processing_time_ms": vision_payload.get("processing_time_ms", 0.0),
                            "api_cost_usd": vision_payload.get("api_cost_usd", 0.0),
                        }
                        metadata.update(processing_metadata)

                        processing_results.append(
                            {
                                "image_path": image_path,
                                "success": True,
                                "detected_fields": detected_fields,
                                "confidence_score": vision_payload.get("confidence_score", 0.0),
                                "pii_detected": {
                                    "has_pii": vision_payload.get("pii_detected", False),
                                    "pii_fields": vision_payload.get("pii_fields", []),
                                },
                                "processing_metadata": metadata,
                                "sharepoint_metadata": metadata_dict,
                            }
                        )
                    else:
                        processing_results.append(
                            {
                                "image_path": image_path,
                                "success": False,
                                "error": "Vision analysis failed or returned no results",
                            }
                        )

                except Exception as e:
                    logger.error(f"Vision analysis failed for {image_path}", error=str(e))
                    processing_results.append(
                        {"image_path": image_path, "success": False, "error": str(e)}
                    )

            successful_analyses = [r for r in processing_results if r["success"]]
            logger.info(
                "Vision analysis completed",
                total_images=len(image_paths),
                successful=len(successful_analyses),
                failed=len(image_paths) - len(successful_analyses),
            )

            # Step 3: Upload to SharePoint
            logger.info("Step 3: Uploading to SharePoint")

            upload_results: list[dict[str, Any]] = []
            successful_uploads = 0
            failed_uploads = 0
            pii_blocked_uploads = 0

            if successful_analyses:
                for analysis in successful_analyses:
                    metadata_dict = analysis.get("sharepoint_metadata", {})

                    sanitized_metadata = metadata_dict
                    metadata_model: SharePointMetadata | None = None
                    if metadata_dict:
                        try:
                            metadata_model = SharePointMetadata.model_validate(metadata_dict)
                            sanitized_metadata = metadata_model.to_sharepoint_dict()
                        except ValidationError as exc:
                            logger.warning(
                                "SharePoint metadata validation failed",
                                error=str(exc),
                                image_path=analysis["image_path"],
                            )

                    metadata_fields = [
                        DetectedFieldValue(
                            internal_name=key,
                            display_name=None,
                            field_value=", ".join(value)
                            if isinstance(value, list)
                            else str(value),
                            confidence=1.0,
                            reasoning=None,
                        )
                        for key, value in sanitized_metadata.items()
                    ]

                    upload_input = SharePointUploadInput(
                        image_path=analysis["image_path"],
                        metadata_fields=metadata_fields,
                        metadata=metadata_model,
                        library_name=library_name,
                        folder_path=folder_path,
                        overwrite=False,
                    )

                    try:
                        upload_response = await _upload_image_to_sharepoint_impl(upload_input)
                    except Exception as upload_error:  # pragma: no cover
                        logger.error(
                            "SharePoint upload failed",
                            image_path=analysis["image_path"],
                            error=str(upload_error),
                        )
                        upload_response = SharePointUploadOutput(
                            success=False,
                            file_id=None,
                            sharepoint_url=None,
                            metadata_applied=False,
                            pii_blocked=False,
                            validation_errors=[str(upload_error)],
                            upload_timestamp=str(time.time()),
                            processing_time_ms=0.0,
                        )

                    upload_results.append(upload_response.model_dump())
                    analysis["upload_result"] = upload_response.model_dump()

                    if upload_response.success:
                        successful_uploads += 1
                    else:
                        failed_uploads += 1

                    if upload_response.pii_blocked:
                        pii_blocked_uploads += 1

                upload_output = {
                    "successful_uploads": successful_uploads,
                    "failed_uploads": failed_uploads,
                    "pii_blocked_uploads": pii_blocked_uploads,
                    "upload_results": upload_results,
                }
            else:
                logger.warning("No successful analyses to upload")
                upload_output = {
                    "successful_uploads": 0,
                    "failed_uploads": 0,
                    "pii_blocked_uploads": 0,
                    "upload_results": [],
                }

            logger.info(
                "SharePoint upload completed",
                successful_uploads=successful_uploads,
                failed_uploads=failed_uploads,
                pii_blocked=pii_blocked_uploads,
            )

            # Step 4: Generate validation report
            logger.info("Step 4: Generating validation report")

            report_runner = openai_agents.Runner()
            validation_report = await report_runner.run(starting_agent=self.validation_reporter, input=
                f"Generate comprehensive validation report for batch {batch_id} with "
                f"{len(processing_results)} processed images. Analyze quality metrics, "
                f"SLO compliance, field validation accuracy, and PII detection effectiveness."
            )

            # Extract validation report from final_output
            validation_output = _final_output(validation_report)

            upload_success_count = successful_uploads
            upload_failure_count = failed_uploads
            upload_success_rate = (
                upload_success_count / len(successful_analyses)
                if successful_analyses
                else 0.0
            )

            # Compile final result
            final_result = {
                "batch_id": batch_id,
                "total_images": len(image_paths),
                "successful_analyses": len(successful_analyses),
                "successful_uploads": upload_success_count,
                "failed_uploads": upload_failure_count,
                "pii_blocked_uploads": pii_blocked_uploads,
                "processing_results": processing_results,
                "upload_summary": upload_output,
                "validation_report": validation_output,
                "sharepoint_schema": sharepoint_schema,
            }

            logger.info(
                "Batch processing completed successfully",
                batch_id=batch_id,
                total_images=len(image_paths),
                success_rate=len(successful_analyses) / len(image_paths) if image_paths else 0,
                upload_success_rate=upload_success_rate,
            )

            return final_result

        except Exception as e:
            logger.error("Batch processing failed", batch_id=batch_id, error=str(e))
            raise

    async def process_single_image(
        self,
        image_path: str,
        sharepoint_site_url: str,
        library_name: str = "Fertige Referenzfotos",
        folder_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Process a single image through complete workflow

        Args:
            image_path: Path to image file
            sharepoint_site_url: SharePoint site URL
            library_name: SharePoint library name
            folder_path: Optional folder path within library

        Returns:
            Complete processing result
        """

        return await self.process_images_batch(
            image_paths=[image_path],
            sharepoint_site_url=sharepoint_site_url,
            library_name=library_name,
            folder_path=folder_path,
        )

    async def _get_cached_vision_output(
        self, image_path: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Retrieve structured vision analysis output from cache if available."""

        image_hash = await self.image_processor.calculate_hash(image_path)
        for model_name in ("gpt-4o", "gpt-4o-mini"):
            cached_analysis = await self.cache_manager.get_vision_analysis(
                image_hash, model_name
            )
            if not cached_analysis:
                continue

            processing_metadata = cached_analysis.get("processing_metadata", {})
            payload = {
                k: v for k, v in cached_analysis.items() if k != "processing_metadata"
            }

            # Ensure required metadata fields exist even if the cached payload is minimal.
            payload.setdefault("model_used", processing_metadata.get("model_used", model_name))
            payload.setdefault(
                "processing_time_ms",
                processing_metadata.get("processing_time_ms", 0.0),
            )
            payload.setdefault(
                "api_cost_usd", processing_metadata.get("api_cost_usd", 0.0)
            )

            return payload, processing_metadata

        logger.warning(
            "Vision analysis cache miss after runner execution",
            image_path=image_path,
        )
        return None

    @staticmethod
    def _build_metadata_from_detection(
        detected_fields: list[dict[str, Any]],
        image_path: str,
        sharepoint_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Create SharePoint metadata mapping from detected fields."""

        allowed_fields = {
            field.get("internal_name")
            for field in sharepoint_schema.get("fields", [])
            if isinstance(field, dict) and field.get("internal_name")
        }
        field_type_lookup = {
            field.get("internal_name"): field.get("field_type")
            for field in sharepoint_schema.get("fields", [])
            if isinstance(field, dict)
        }
        field_choices = {
            field.get("internal_name"): field.get("choices") or []
            for field in sharepoint_schema.get("fields", [])
            if isinstance(field, dict)
        }

        metadata: dict[str, Any] = {}
        for field in detected_fields:
            internal_name = field.get("internal_name")
            value = field.get("field_value")
            if not internal_name or not value:
                continue
            if allowed_fields and internal_name not in allowed_fields:
                continue
            field_type = field_type_lookup.get(internal_name)
            choices = field_choices.get(internal_name, [])

            if isinstance(value, list):
                cleaned_tokens = [str(item).strip() for item in value if str(item).strip()]
            elif internal_name in MULTI_VALUE_FIELDS or field_type == "MultiChoice":
                cleaned_tokens = [
                    token.strip()
                    for token in str(value).split(",")
                    if token.strip()
                ]
            else:
                cleaned_tokens = [str(value).strip()]

            matched_tokens: list[str] = []
            if choices:
                for token in cleaned_tokens:
                    match = ErniPhotoAgency._match_choice(token, choices)
                    if match:
                        matched_tokens.append(match)
            else:
                matched_tokens = [token for token in cleaned_tokens if token]

            if internal_name in MULTI_VALUE_FIELDS or field_type == "MultiChoice":
                if matched_tokens:
                    metadata[internal_name] = matched_tokens
            elif matched_tokens:
                metadata[internal_name] = matched_tokens[0]

        filename = Path(image_path).name
        metadata.setdefault("Title", Path(filename).stem)
        metadata.setdefault("OriginalName", filename)
        metadata.setdefault("Status", Status.ENTWURF_KI.value)

        return metadata

    @staticmethod
    def _match_choice(value: str, choices: list[str]) -> str | None:
        """Find best matching choice value using case-insensitive comparison."""

        normalized = value.lower()
        for choice in choices:
            if normalized == choice.lower():
                return choice

        for choice in choices:
            choice_lower = choice.lower()
            if normalized in choice_lower or choice_lower in normalized:
                return choice

        return None

    async def get_system_health(self) -> dict[str, Any]:
        """Get comprehensive system health status"""

        # Get metrics collector health
        metrics_health = self.metrics_collector.get_health_status()
        slo_status = self.metrics_collector.get_slo_status()

        # Get cache health
        cache_stats = await self.cache_manager.get_stats()

        return {
            "overall_status": "healthy" if metrics_health["status"] == "healthy" else "degraded",
            "slo_compliance": slo_status["overall_compliant"],
            "metrics": metrics_health,
            "slo_status": slo_status,
            "cache_stats": cache_stats,
            "components": {
                "schema_extractor": "healthy",
                "vision_analyzer": "healthy",
                "sharepoint_uploader": "healthy",
                "validation_reporter": "healthy",
            },
        }

    async def cleanup(self) -> None:
        """
        Cleanup resources (deprecated - use shutdown() instead)

        This method is kept for backward compatibility.
        New code should use shutdown() which delegates to DI container.
        """
        logger.warning("cleanup() is deprecated, use shutdown() instead")
        await self.shutdown()

        # Additional cleanup for sessions
        for session in self._sessions.values():
            try:
                session.close()
            except Exception:
                logger.debug("Failed to close session", exc_info=True)
        self._sessions.clear()
        logger.info("Cleanup completed")


async def main() -> None:
    """Main entry point for CLI usage"""

    # Configure logging
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Initialize application
    app = ErniPhotoAgency()
    await app.initialize()

    try:
        # Parse command line arguments
        args = sys.argv[1:]

        # Check for test mode
        if "--test-mode" in args:
            # Health check mode for testing
            health = await app.get_system_health()
            print(f"System status: {health['overall_status']}")
            print(f"SLO compliant: {health['slo_compliance']}")
            print("✅ Test mode: Application started successfully!")

            # Keep running for Docker health checks
            import asyncio
            while True:
                await asyncio.sleep(30)
                health = await app.get_system_health()
                print(f"Health check: {health['overall_status']}")

        elif len(args) > 0 and not any(arg.startswith("--") for arg in args):
            # Process images from command line arguments
            image_paths = args
            sharepoint_site_url = os.getenv(
                "SHAREPOINT_SITE_URL", "https://company.sharepoint.com/sites/erni"
            )

            result = await app.process_images_batch(
                image_paths=image_paths, sharepoint_site_url=sharepoint_site_url
            )

            print(f"Processed {result['total_images']} images")
            print(f"Successful analyses: {result['successful_analyses']}")
            print(f"Successful uploads: {result['successful_uploads']}")
            print(f"Validation report: {result['validation_report']['batch_id']}")

        else:
            # Health check mode
            health = await app.get_system_health()
            print(f"System status: {health['overall_status']}")
            print(f"SLO compliant: {health['slo_compliance']}")

    finally:
        await app.cleanup()


# Application state management (thread-safe)
_app_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Lifespan context manager for FastAPI application with dependency injection

    Manages application lifecycle:
    - Startup: Initialize DI container and all components
    - Shutdown: Gracefully shutdown all components
    """
    # Startup
    logger.info("Starting FastAPI application with DI container...")

    # Initialize global DI container
    container = get_container()
    await container.initialize()

    # Create Erni-Foto Agency with initialized container
    erni_app = ErniPhotoAgency(container=container)
    await erni_app.initialize()

    _app_state["erni_app"] = erni_app
    _app_state["container"] = container

    logger.info("FastAPI application started successfully")

    yield

    # Shutdown
    logger.info("Shutting down FastAPI application...")

    erni_app = _app_state.get("erni_app")
    if erni_app:
        await erni_app.shutdown()

    container = _app_state.get("container")
    if container:
        await container.shutdown()

    _app_state.clear()
    logger.info("FastAPI application shutdown complete")


def get_erni_app() -> ErniPhotoAgency:
    """
    Dependency injection provider for ErniPhotoAgency

    This function is used by FastAPI's Depends() to inject ErniPhotoAgency
    into route handlers, enabling proper dependency injection pattern.

    Benefits:
    - Thread-safe access to application instance
    - Easy testing with mock implementations
    - Clear dependency graph in route handlers

    Returns:
        ErniPhotoAgency instance

    Raises:
        HTTPException: If application is not initialized

    Example:
        @app.post("/api/process")
        async def process(erni_app: ErniPhotoAgency = Depends(get_erni_app)):
            return await erni_app.process_images(...)
    """
    erni_app = _app_state.get("erni_app")
    if not erni_app:
        raise HTTPException(
            status_code=503,
            detail="Application not initialized. Please wait for startup to complete."
        )
    return erni_app


# FastAPI application for REST API
app = FastAPI(
    title="Erni-Foto Agency API",
    description="REST API for AI-powered construction photo processing",
    version="1.0.0",
    lifespan=lifespan
)

# Alias for uvicorn compatibility
api_app = app

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


EndpointCallable = TypeVar("EndpointCallable", bound=Callable[..., Awaitable[Any]])

if TYPE_CHECKING:
    def typed_get(*args: Any, **kwargs: Any) -> Callable[[EndpointCallable], EndpointCallable]: ...

    def typed_post(*args: Any, **kwargs: Any) -> Callable[[EndpointCallable], EndpointCallable]: ...
else:
    typed_get = app.get  # type: ignore[assignment]
    typed_post = app.post  # type: ignore[assignment]


@typed_get("/health")
async def health_check(erni_app: ErniPhotoAgency = Depends(get_erni_app)) -> dict[str, Any]:
    """Health check endpoint"""
    health = await erni_app.get_system_health()
    return health


@typed_get("/metrics")
async def metrics_endpoint(erni_app: ErniPhotoAgency = Depends(get_erni_app)) -> Response:
    """Expose Prometheus metrics collected by the application."""
    metrics_payload = generate_latest(erni_app.metrics_collector.registry)
    return Response(content=metrics_payload, media_type=CONTENT_TYPE_LATEST)


@typed_get("/api/agents")
async def get_agents(erni_app: ErniPhotoAgency = Depends(get_erni_app)) -> dict[str, Any]:
    """Get list of available agents"""
    agents = {
        "sharepoint-schema-extractor": {
            "name": "SharePoint Schema Extractor",
            "description": "Извлекает схему полей из SharePoint для настройки загрузки",
            "endpoint": "/api/agents/sharepoint-schema"
        },
        "vision-analyzer": {
            "name": "Structured Vision Analyzer",
            "description": "Анализирует строительные фотографии с помощью GPT-4 Vision",
            "endpoint": "/api/agents/vision-analyze"
        },
        "sharepoint-uploader": {
            "name": "SharePoint Uploader",
            "description": "Загружает результаты анализа в SharePoint",
            "endpoint": "/api/agents/sharepoint-upload"
        },
        "validation-reporter": {
            "name": "Validation Report Generator",
            "description": "Создает отчеты валидации и проверки качества",
            "endpoint": "/api/agents/validation-report"
        }
    }

    return {
        "agents": agents,
        "total": len(agents),
        "status": "healthy" if erni_app else "not_initialized"
    }


@typed_post("/api/agents/sharepoint-schema")
async def sharepoint_schema_extractor(
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> AgentResponse:
    """SharePoint Schema Extractor Agent endpoint"""
    return await erni_app.run_sharepoint_schema(request)


@typed_post("/api/agents/vision-analyze")
async def vision_analyzer(
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> AgentResponse:
    """Structured Vision Analyzer Agent endpoint"""
    return await erni_app.run_vision_analyzer(request)


@typed_post("/api/agents/sharepoint-upload")
async def sharepoint_uploader(
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> AgentResponse:
    """SharePoint Uploader Agent endpoint"""
    response = await erni_app.run_sharepoint_uploader(request)
    if not response.success and response.error == "No image paths provided for upload":
        raise HTTPException(status_code=400, detail=response.error)
    return response


@typed_post("/api/agents/validation-report")
async def validation_reporter(
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> AgentResponse:
    """Validation Report Generator Agent endpoint"""
    return await erni_app.run_validation_report(request)


@typed_post("/api/agents/orchestrator")
async def orchestrator_agent(
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> AgentResponse:
    """Workflow orchestrator endpoint"""
    return await erni_app.orchestrate_workflow(request)


@typed_post("/api/agents/{agent_id}/stream")
async def stream_agent(
    agent_id: str,
    request: AgentRequest,
    erni_app: ErniPhotoAgency = Depends(get_erni_app)
) -> EventSourceResponse:
    """Stream agent execution progress via Server-Sent Events."""
    run_id = request.workflow_config.get("run_id") or str(uuid4())
    request.workflow_config.setdefault("run_id", run_id)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def progress(event: dict[str, Any]) -> None:
        if "run_id" not in event:
            event["run_id"] = run_id
        await queue.put(event)

    agent_map = {
        "sharepoint-schema-extractor": erni_app.run_sharepoint_schema,
        "vision-analyzer": erni_app.run_vision_analyzer,
        "sharepoint-uploader": erni_app.run_sharepoint_uploader,
        "validation-reporter": erni_app.run_validation_report,
        "workflow-orchestrator": erni_app.orchestrate_workflow,
    }

    handler = agent_map.get(agent_id)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown agent '{agent_id}'")

    async def runner() -> None:
        try:
            await handler(request, progress=progress, run_id=run_id)
        except HTTPException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Agent streaming failed", agent=agent_id, error=str(exc))
            await progress(
                _make_event(
                    "RUN_FAILED",
                    run_id,
                    agent_id,
                    {"error": str(exc)},
                    thread_id=request.workflow_config.get("thread_id"),
                )
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())

    async def event_generator() -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _format_sse(event)
        finally:
            await task

    return EventSourceResponse(event_generator())


@typed_post("/api/process-batch")
async def process_batch(
    request: Request,
    erni_app: ErniPhotoAgency = Depends(get_erni_app),
) -> dict[str, Any]:
    """Process a batch of images through complete workflow"""
    try:
        data = await request.json()
        image_paths = data.get("image_paths", [])
        sharepoint_site_url = data.get("sharepoint_site_url",
                                     os.getenv("SHAREPOINT_SITE_URL", "https://company.sharepoint.com/sites/erni"))
        library_name = data.get("library_name", "Fertige Referenzfotos")
        folder_path = data.get("folder_path")

        if not image_paths:
            raise HTTPException(status_code=400, detail="No image paths provided")

        result = await erni_app.process_images_batch(
            image_paths=image_paths,
            sharepoint_site_url=sharepoint_site_url,
            library_name=library_name,
            folder_path=folder_path
        )

        return result

    except Exception as e:
        logger.error("Batch processing failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e


def run_api_server() -> None:
    """Run the FastAPI server"""
    uvicorn.run(
        "erni_foto_agency.main:api_app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info"
    )


if __name__ == "__main__":
    # Check if we should run API server or CLI
    if "--api" in sys.argv:
        run_api_server()
    else:
        asyncio.run(main())
