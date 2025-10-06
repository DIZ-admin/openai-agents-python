"""SharePointUploaderAgent - Upload images and metadata to SharePoint."""

import time
from typing import Any, cast

import structlog

from agents import Agent, function_tool

from ..models.function_tool_models import (
    BatchProcessingInput,
    BatchUploadOutput,
    BatchUploadStatistics,
    DetectedFieldValue,
    MetadataValidationOutput,
    PIIBlockingOutput,
    SharePointLibraryStats,
    SharePointUploadInput,
    SharePointUploadOutput,
    SystemMetricsSnapshot,
    UploadStatisticsOutput,
    WorkflowStatusUpdateOutput,
)
from ..models.sharepoint_models import SharePointMetadata, Status
from ..monitoring.metrics_collector import get_metrics_collector
from ..performance.batch_processor import BatchProcessor, ImageInput
from ..performance.circuit_breaker import call_microsoft_graph_with_circuit_breaker
from ..utils.graph_client import GraphAPIClient
from ..utils.pii_detector import PIIDetector

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


# Using imported models from function_tool_models


def _build_metadata_from_input(upload_input: SharePointUploadInput) -> SharePointMetadata:
    """Construct SharePointMetadata from upload input."""

    metadata_dict: dict[str, Any] = {}

    if upload_input.metadata:
        metadata_dict.update(upload_input.metadata.to_sharepoint_dict())

    if upload_input.metadata_fields:
        for field in upload_input.metadata_fields:
            if field.field_value is not None:
                value = field.field_value
                if field.internal_name in MULTI_VALUE_FIELDS:
                    if isinstance(value, str):
                        parsed = [item.strip() for item in value.split(",") if item.strip()]
                        metadata_dict[field.internal_name] = parsed
                    else:
                        metadata_dict[field.internal_name] = value
                else:
                    metadata_dict[field.internal_name] = value

    return SharePointMetadata(**metadata_dict)


def _fields_from_metadata_dict(metadata: dict[str, Any]) -> list[DetectedFieldValue]:
    """Convert metadata mapping to detected field values."""

    fields: list[DetectedFieldValue] = []
    for internal_name, value in metadata.items():
        str_value = None if value is None else str(value)
        fields.append(
            DetectedFieldValue(
                internal_name=internal_name,
                display_name=None,
                field_value=str_value,
                confidence=1.0,
                reasoning=None,
            )
        )
    return fields


async def _upload_image_to_sharepoint_impl(
    upload_input: SharePointUploadInput,
) -> SharePointUploadOutput:
    """
    Upload image with metadata to SharePoint library

    Args:
        image_path: Path to the image file
        metadata: SharePoint metadata dictionary
        library_name: Name of the SharePoint library
        folder_path: Optional folder path within library
        overwrite: Whether to overwrite existing files

    Returns:
        Upload result with success status and details
    """

    start_time = time.time()
    metrics = get_metrics_collector()
    graph_client: GraphAPIClient | None = None

    try:
        # Simplified implementation for testing
        logger.info(
            "Upload request received",
            image_path=upload_input.image_path,
            library_name=upload_input.library_name,
            metadata_fields_count=len(upload_input.metadata_fields),
        )

        # Prepare sanitized metadata from validation result
        metadata_model = _build_metadata_from_input(upload_input)
        sanitized_metadata = metadata_model.to_sharepoint_dict()
        sanitized_metadata.setdefault("Status", Status.ENTWURF_KI.value)

        # Upload file and apply metadata
        graph_client = GraphAPIClient()

        async def upload_operation() -> dict[str, Any]:
            # Upload file
            upload_result = await graph_client.upload_file(
                library_name=upload_input.library_name,
                file_path=upload_input.image_path,
                folder_path=upload_input.folder_path,
                overwrite=upload_input.overwrite,
            )

            if not upload_result["success"]:
                raise Exception(
                    f"File upload failed: {upload_result.get('error', 'Unknown error')}"
                )

            file_id = upload_result["file_id"]

            # Apply metadata
            metadata_result = await graph_client.update_file_metadata(
                library_name=upload_input.library_name, file_id=file_id, metadata=sanitized_metadata
            )

            return {
                "file_id": file_id,
                "sharepoint_url": upload_result.get("sharepoint_url"),
                "metadata_applied": metadata_result["success"],
            }

        # Execute upload with circuit breaker
        upload_data = cast(
            dict[str, Any], await call_microsoft_graph_with_circuit_breaker(upload_operation)
        )

        processing_time = (time.time() - start_time) * 1000

        # Record success metrics
        metrics.record_batch_processing(1)  # Single file upload

        result = SharePointUploadOutput(
            success=True,
            file_id=upload_data["file_id"],
            sharepoint_url=upload_data["sharepoint_url"],
            metadata_applied=upload_data["metadata_applied"],
            pii_blocked=False,
            validation_errors=[],
            upload_timestamp=str(time.time()),
            processing_time_ms=processing_time,
        )

        logger.info(
            "Image uploaded successfully",
            image_path=upload_input.image_path,
            file_id=upload_data["file_id"],
            processing_time_ms=processing_time,
        )

        return result

    except Exception as e:
        processing_time = (time.time() - start_time) * 1000

        logger.error(
            "Image upload failed",
            image_path=upload_input.image_path,
            error=str(e),
            processing_time_ms=processing_time,
        )

        return SharePointUploadOutput(
            success=False,
            file_id=None,
            sharepoint_url=None,
            metadata_applied=False,
            pii_blocked=False,
            validation_errors=[f"Upload error: {str(e)}"],
            upload_timestamp=str(time.time()),
            processing_time_ms=processing_time,
        )
    finally:
        if graph_client is not None:
            await graph_client.close()


@function_tool
async def upload_image_to_sharepoint(upload_input: SharePointUploadInput) -> SharePointUploadOutput:
    return await _upload_image_to_sharepoint_impl(upload_input)


@function_tool
async def batch_upload_images(batch_input: BatchProcessingInput) -> BatchUploadOutput:
    """
    Batch upload multiple images to SharePoint

    Args:
        batch_input: Batch processing input with upload data

    Returns:
        Batch upload result with statistics
    """

    start_time = time.time()

    # Extract parameters from batch_input
    image_paths = batch_input.image_paths
    library_name = batch_input.library_name
    max_concurrent = batch_input.max_concurrent
    folder_path = batch_input.folder_path

    batch_processor = BatchProcessor(max_batch_size=5, max_concurrent=max_concurrent)

    # Convert inputs to ImageInput objects
    image_inputs = []
    for i, image_path in enumerate(image_paths):
        image_inputs.append(
            ImageInput(
                image_path=image_path,
                image_hash=f"batch_{i}_{int(time.time())}",
                metadata={},  # Empty metadata for batch processing
            )
        )

    # Define upload processor
    async def upload_processor(image_input: ImageInput) -> SharePointUploadOutput:
        metadata_mapping = image_input.metadata or {}
        metadata_model = None
        if metadata_mapping:
            metadata_model = SharePointMetadata(**metadata_mapping)
        upload_input = SharePointUploadInput(
            image_path=image_input.image_path,
            metadata_fields=_fields_from_metadata_dict(metadata_mapping),
            metadata=metadata_model,
            library_name=library_name,
            folder_path=folder_path,
            overwrite=False,
        )
        return await _upload_image_to_sharepoint_impl(upload_input)

    # Process batch
    batch_result = await batch_processor.process_batch(
        image_inputs, upload_processor, f"sharepoint_upload_{library_name}"
    )

    processing_time = (time.time() - start_time) * 1000

    normalized_results: list[SharePointUploadOutput] = []
    for result in batch_result.results:
        data = result.result_data
        try:
            if isinstance(data, SharePointUploadOutput):
                normalized_results.append(data)
            elif isinstance(data, dict):
                normalized_results.append(SharePointUploadOutput.model_validate(data))
            else:
                raise TypeError(f"Unexpected result type: {type(data)!r}")
        except Exception as err:  # pragma: no cover - defensive branch
            logger.error(
                "Failed to normalize batch upload result",
                error=str(err),
                result_type=type(data).__name__,
            )
            normalized_results.append(
                SharePointUploadOutput(
                    success=False,
                    file_id=None,
                    sharepoint_url=None,
                    metadata_applied=False,
                    pii_blocked=False,
                    validation_errors=["Normalization error"],
                    upload_timestamp=str(time.time()),
                    processing_time_ms=result.processing_time_ms,
                )
            )

    successful_uploads = [res for res in normalized_results if res.success]
    failed_uploads = [res for res in normalized_results if not res.success]
    pii_blocked_uploads = [res for res in normalized_results if res.pii_blocked]

    avg_processing_time = (
        sum(r.processing_time_ms for r in batch_result.results) / len(batch_result.results)
        if batch_result.results
        else 0.0
    )

    success_rate = (
        batch_result.successful_images / batch_result.total_images
        if batch_result.total_images > 0
        else 0.0
    )

    batch_output = BatchUploadOutput(
        batch_success=batch_result.status.value == "completed",
        total_images=batch_result.total_images,
        successful_uploads=len(successful_uploads),
        failed_uploads=len(failed_uploads),
        pii_blocked_uploads=len(pii_blocked_uploads),
        total_cost_usd=batch_result.total_cost_usd,
        processing_time_ms=processing_time,
        upload_results=normalized_results,
        batch_statistics=BatchUploadStatistics(
            avg_processing_time_ms=avg_processing_time,
            success_rate=success_rate,
        ),
    )

    logger.info(
        "Batch upload completed",
        total_images=batch_output.total_images,
        successful=batch_output.successful_uploads,
        failed=batch_output.failed_uploads,
        pii_blocked=batch_output.pii_blocked_uploads,
        processing_time_ms=processing_time,
    )

    return batch_output


@function_tool
async def validate_upload_metadata(metadata: SharePointMetadata) -> MetadataValidationOutput:
    """
    Validate metadata before SharePoint upload

    Args:
        metadata: Metadata dictionary to validate

    Returns:
        Validation result with sanitized metadata
    """

    try:
        sanitized_metadata_dict = metadata.to_sharepoint_dict()
        sanitized_metadata_dict.setdefault("Status", Status.ENTWURF_KI.value)

        sanitized_model = SharePointMetadata.model_validate(sanitized_metadata_dict)

        return MetadataValidationOutput(
            is_valid=True,
            errors=[],
            warnings=[],
            sanitized_metadata=sanitized_model,
            total_fields=len(sanitized_metadata_dict),
            validation_timestamp=str(time.time()),
        )

    except Exception as e:
        logger.error("Metadata validation failed", error=str(e))
        return MetadataValidationOutput(
            is_valid=False,
            errors=[f"Validation error: {str(e)}"],
            warnings=[],
            sanitized_metadata=None,
            total_fields=0,
            validation_timestamp=str(time.time()),
        )


@function_tool
async def check_pii_blocking(metadata: SharePointMetadata) -> PIIBlockingOutput:
    """
    Check if upload should be blocked due to PII detection

    Args:
        metadata: Metadata to check for PII

    Returns:
        PII blocking decision
    """

    sanitized_metadata = metadata.to_sharepoint_dict()

    pii_detector = PIIDetector()

    # Scan metadata for PII
    pii_result = await pii_detector.scan_sharepoint_metadata(sanitized_metadata)

    # Determine if processing should be blocked
    should_block, reason = await pii_detector.should_block_processing(pii_result)

    result = PIIBlockingOutput(
        should_block=should_block,
        reason=reason,
        pii_detected=pii_result["has_pii"],
        confidence=pii_result["confidence"],
        risk_level=pii_result["risk_level"],
        high_risk_fields=pii_result.get("high_risk_fields", []),
    )

    # Record PII detection metrics
    metrics = get_metrics_collector()
    detection_rate = 1.0 if pii_result["has_pii"] else 0.0
    metrics.record_pii_detection(detection_rate)

    return result


@function_tool
async def update_workflow_status(
    file_id: str, library_name: str, new_status: str, comment: str | None = None
) -> WorkflowStatusUpdateOutput:
    """
    Update workflow status of uploaded file

    Args:
        file_id: SharePoint file ID
        library_name: SharePoint library name
        new_status: New workflow status
        comment: Optional comment for status change

    Returns:
        Status update result
    """

    try:
        # Validate status
        valid_statuses = [s.value for s in Status]
        if new_status not in valid_statuses:
            return WorkflowStatusUpdateOutput(
                success=False,
                updated_fields=[],
                field_count=None,
                error=f"Invalid status '{new_status}'. Valid statuses: {valid_statuses}",
            )

        graph_client = GraphAPIClient()

        # Update metadata with new status
        metadata_update = {"Status": new_status}
        if comment:
            # Add comment to description or separate comment field
            metadata_update["WorkflowComment"] = comment

        async def update_operation() -> dict[str, Any]:
            return await graph_client.update_file_metadata(
                library_name=library_name, file_id=file_id, metadata=metadata_update
            )

        result_dict = cast(
            dict[str, Any], await call_microsoft_graph_with_circuit_breaker(update_operation)
        )

        if result_dict.get("success"):
            logger.info(
                "Workflow status updated", file_id=file_id, new_status=new_status, comment=comment
            )

        return WorkflowStatusUpdateOutput(
            success=bool(result_dict.get("success")),
            updated_fields=list(result_dict.get("updated_fields", [])),
            field_count=result_dict.get("field_count"),
            error=result_dict.get("error"),
        )

    except Exception as e:
        logger.error(
            "Workflow status update failed", file_id=file_id, new_status=new_status, error=str(e)
        )
        return WorkflowStatusUpdateOutput(
            success=False,
            updated_fields=[],
            field_count=None,
            error=str(e),
        )


@function_tool
async def get_upload_statistics(library_name: str, days: int = 7) -> UploadStatisticsOutput:
    """
    Get upload statistics for the library

    Args:
        library_name: SharePoint library name
        days: Number of days to analyze

    Returns:
        Upload statistics
    """

    try:
        graph_client = GraphAPIClient()

        # Get recent uploads
        async def stats_operation() -> dict[str, Any]:
            return await graph_client.get_library_statistics(library_name, days)

        stats = cast(
            dict[str, Any], await call_microsoft_graph_with_circuit_breaker(stats_operation)
        )

        file_types_raw = stats.get("file_types", {}) or {}
        file_types = {str(k): int(v) for k, v in file_types_raw.items()}

        sharepoint_stats = SharePointLibraryStats(
            total_files=int(stats.get("total_files", 0)),
            recent_uploads=int(stats.get("recent_uploads", 0)),
            avg_uploads_per_day=float(stats.get("avg_uploads_per_day", 0.0)),
            storage_used_mb=float(stats.get("storage_used_mb", 0.0)),
            most_active_day=str(stats.get("most_active_day", "unknown")),
            file_types=file_types,
        )

        metrics = get_metrics_collector()
        slo_status = metrics.get_slo_status()
        system_metrics = SystemMetricsSnapshot(
            current_success_rate=float(slo_status["current_values"]["success_rate"]),
            current_p95_latency_ms=float(slo_status["current_values"]["p95_latency_ms"]),
            current_cost_per_image_usd=float(
                slo_status["current_values"]["cost_per_image_usd"]
            ),
            slo_compliant=bool(slo_status["overall_compliant"]),
        )

        return UploadStatisticsOutput(
            library_name=library_name,
            analysis_period_days=days,
            sharepoint_stats=sharepoint_stats,
            system_metrics=system_metrics,
            timestamp=str(time.time()),
        )

    except Exception as e:
        logger.error("Failed to get upload statistics", library_name=library_name, error=str(e))
        return UploadStatisticsOutput(
            library_name=library_name,
            analysis_period_days=days,
            sharepoint_stats=SharePointLibraryStats(
                total_files=0,
                recent_uploads=0,
                avg_uploads_per_day=0.0,
                storage_used_mb=0.0,
                most_active_day="unknown",
                file_types={},
            ),
            system_metrics=SystemMetricsSnapshot(
                current_success_rate=0.0,
                current_p95_latency_ms=0.0,
                current_cost_per_image_usd=0.0,
                slo_compliant=False,
            ),
            timestamp=str(time.time()),
        )


class SharePointUploaderAgent(Agent):
    """Agent for uploading images and metadata to SharePoint"""

    def __init__(self) -> None:
        super().__init__(
            name="SharePointUploader",
            instructions="""
            You are an expert in SharePoint file uploads and metadata management.

            Your responsibilities:
            1. Upload images to SharePoint libraries with proper metadata
            2. Validate metadata against 23-field schema with 119 choice values
            3. Apply PII protection by blocking uploads with sensitive data
            4. Handle batch uploads with optimal performance (max 5 concurrent)
            5. Manage workflow status transitions (Entwurf KI → Bearbeitet Marketing → Bearbeitet Verkauf → Fertig)
            6. Provide upload statistics and monitoring

            Always validate metadata before upload and apply PII blocking when confidence ≥ 0.80.
            Use circuit breaker pattern for Microsoft Graph API calls.
            Record metrics for SLO monitoring.

            Target performance:
            - Batch upload success rate ≥ 0.96
            - PII detection FN-rate ≤ 1.0%
            - Upload latency p95 ≤ 30s
            """,
            tools=[upload_image_to_sharepoint, batch_upload_images, validate_upload_metadata],
        )
