"""StructuredVisionAnalyzerAgent - OpenAI Vision API with structured outputs."""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import openai
import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    RunContextWrapper,
    function_tool,
)

from ..config.settings import get_config
from ..models.function_tool_models import (
    ChoiceFieldValidationOutput,
    DetectedFieldValue,
    FieldMappingConfig,
    FieldMappingRule,
    MappedMetadataOutput,
    SharePointSchemaInput,
    VisionAnalysisInput,
    VisionAnalysisOutput,
)
from ..performance.cache_manager import ErniCacheManager
from ..performance.circuit_breaker import call_openai_with_circuit_breaker
from ..performance.cost_optimizer import CostBudget, CostOptimizer, ModelType
from ..performance.rate_limiter import get_rate_limiter
from ..utils.image_processor import ImageProcessor
from ..utils.pii_detector import PIIDetector

logger = structlog.get_logger(__name__)


# Using imported models from function_tool_models


@dataclass
class VisionGuardrailContext:
    inline_image_base64: str | None
    inline_image_bytes: bytes | None
    max_image_size_mb: int
    allowed_mime_types: tuple[str, ...]


def _vision_input_guardrail(
    ctx: RunContextWrapper[VisionGuardrailContext],
    agent: Agent[Any],
    raw_input: str | list[Any],
) -> GuardrailFunctionOutput:
    context = ctx.context
    if not isinstance(context, VisionGuardrailContext):
        return GuardrailFunctionOutput(output_info={"status": "context_missing"}, tripwire_triggered=False)

    if not context.inline_image_base64 or not context.inline_image_bytes:
        return GuardrailFunctionOutput(output_info={"status": "no_inline_image"}, tripwire_triggered=False)

    header, _, _ = context.inline_image_base64.partition(',')
    if not header.startswith('data:image/'):
        return GuardrailFunctionOutput(
            output_info={"reason": "Unsupported data URI", "header": header},
            tripwire_triggered=True,
        )

    mime_type = header.replace('data:', '').replace(';base64', '')
    if mime_type not in context.allowed_mime_types:
        return GuardrailFunctionOutput(
            output_info={"reason": f"Unsupported image format: {mime_type}"},
            tripwire_triggered=True,
        )

    size_mb = len(context.inline_image_bytes) / (1024 * 1024)
    if size_mb > context.max_image_size_mb:
        return GuardrailFunctionOutput(
            output_info={
                "reason": "Image exceeds size limit",
                "size_mb": round(size_mb, 2),
                "limit_mb": context.max_image_size_mb,
            },
            tripwire_triggered=True,
        )

    return GuardrailFunctionOutput(output_info={"status": "ok"}, tripwire_triggered=False)


@function_tool
async def analyze_image_vision(analysis_input: VisionAnalysisInput) -> VisionAnalysisOutput:
    """
    Analyze image using OpenAI Vision API with structured outputs

    Args:
        analysis_input: Vision analysis input with image path, schema, and preferences

    Returns:
        Structured analysis result with detected SharePoint fields
    """

    start_time = time.time()

    # Initialize components
    from ..config.settings import get_config
    config = get_config()
    cache_manager = ErniCacheManager(redis_url=config.cache.redis_url)
    await cache_manager.initialize()

    cost_optimizer = CostOptimizer(
        budget=CostBudget(
            hourly_budget_usd=10.0, daily_budget_usd=100.0, cost_per_image_target_usd=0.055
        )
    )

    try:
        # Generate image hash if not provided
        image_hash = analysis_input.image_hash
        if not image_hash:
            image_processor = ImageProcessor()
            image_hash = await image_processor.calculate_hash(analysis_input.image_path)

        # Check cache first
        cached_result = await cache_manager.get_vision_analysis(
            image_hash, analysis_input.model_preference
        )
        if cached_result:
            logger.info("Vision analysis cache hit", image_hash=image_hash)
            cached_fields = _load_detected_fields_from_cache(
                cached_result.get("detected_fields")
            )
            processing_metadata = cached_result.get("processing_metadata", {})
            return VisionAnalysisOutput(
                image_path=analysis_input.image_path,
                image_hash=image_hash,
                analysis_timestamp=cached_result.get("analysis_timestamp", str(time.time())),
                detected_fields=cached_fields,
                confidence_score=float(cached_result.get("confidence_score", 0.0)),
                model_used=cached_result.get("model_used", ""),
                processing_time_ms=float(processing_metadata.get("processing_time_ms", 0.0)),
                api_cost_usd=float(processing_metadata.get("api_cost_usd", 0.0)),
                cache_hit=True,
                pii_detected=bool(cached_result.get("pii_detected", False)),
                pii_fields=list(cached_result.get("pii_fields", [])),
            )

        # Select optimal model
        fields_to_analyze = [field.internal_name for field in analysis_input.json_schema_fields]

        if analysis_input.model_preference == "auto":
            selected_model, selection_reason = cost_optimizer.select_optimal_model(
                fields_to_analyze=fields_to_analyze,
                image_complexity=0.5,  # Default complexity
                budget_pressure=0.3,  # Moderate budget pressure
            )
        else:
            selected_model = (
                ModelType.GPT4O
                if analysis_input.model_preference == "gpt-4o"
                else ModelType.GPT4O_MINI
            )
            selection_reason = f"Manual selection: {analysis_input.model_preference}"

        # Estimate cost and check budget
        estimated_tokens_input = 1200  # Base tokens for image + prompt
        estimated_tokens_output = 400  # Estimated output tokens

        estimated_cost = cost_optimizer.estimate_cost(
            selected_model, estimated_tokens_input, estimated_tokens_output
        )

        budget_ok, budget_message = cost_optimizer.check_budget_availability(estimated_cost)
        if not budget_ok:
            raise Exception(f"Budget constraint: {budget_message}")

        # Prepare image for analysis
        image_processor = ImageProcessor()
        base64_image = await image_processor.encode_image_base64(analysis_input.image_path)

        # Perform vision analysis with circuit breaker
        async def vision_api_call() -> Any:
            # Convert fields to JSON schema format
            schema_dict = {
                "type": "object",
                "properties": {
                    field.internal_name: {
                        "type": "string",
                        "description": field.description or f"Value for {field.display_name or field.internal_name}"
                    }
                    for field in analysis_input.json_schema_fields
                }
            }
            return await _call_openai_vision_api(
                base64_image=base64_image,
                json_schema=json.dumps(schema_dict),
                model=selected_model.value,
                fields_to_analyze=fields_to_analyze,
            )

        api_response = await call_openai_with_circuit_breaker(vision_api_call)

        # Process response
        processing_time = (time.time() - start_time) * 1000

        # Extract structured data
        content = api_response.choices[0].message.content
        detected_fields_raw = json.loads(content) if content else {}
        display_name_lookup = {
            field.internal_name: field.display_name
            for field in analysis_input.json_schema_fields
            if field.display_name
        }

        normalized_fields = _normalize_detected_fields(
            detected_fields_raw, display_name_lookup
        )

        # Calculate actual cost
        usage = api_response.usage
        actual_cost = cost_optimizer.estimate_cost(
            selected_model, usage.prompt_tokens, usage.completion_tokens
        )

        # Record usage
        cost_optimizer.record_usage(
            actual_cost=actual_cost,
            model_used=selected_model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            processing_success=True,
        )

        # Calculate confidence score
        confidence_score = _calculate_confidence_score(detected_fields_raw)

        # Create initial result for PII scanning
        analysis_timestamp = str(time.time())
        initial_result = VisionAnalysisOutput(
            image_path=analysis_input.image_path,
            image_hash=image_hash,
            analysis_timestamp=analysis_timestamp,
            detected_fields=normalized_fields,
            confidence_score=confidence_score,
            model_used=selected_model.value,
            processing_time_ms=processing_time,
            api_cost_usd=actual_cost,
            cache_hit=False,
            pii_detected=False,
            pii_fields=[],
        )

        # PII detection
        enriched_result = await _scan_pii_content_impl(initial_result)

        # Prepare final result
        result = enriched_result.model_copy(update={
            "detected_fields": normalized_fields,
            "confidence_score": confidence_score,
            "processing_time_ms": processing_time,
            "api_cost_usd": actual_cost,
            "cache_hit": False,
        })

        # Cache the result (convert to dict for caching)
        cache_data = {
            "image_path": result.image_path,
            "image_hash": result.image_hash,
            "analysis_timestamp": str(time.time()),
            "model_used": result.model_used,
            "confidence_score": result.confidence_score,
            "detected_fields": [field.model_dump() for field in normalized_fields],
            "pii_detected": result.pii_detected,
            "pii_fields": result.pii_fields,
            "processing_metadata": {
                "processing_time_ms": result.processing_time_ms,
                "api_cost_usd": actual_cost,
                "tokens_used": usage.total_tokens,
                "model_selection_reason": selection_reason,
                "cache_hit": False,
            },
        }

        await cache_manager.cache_vision_analysis(
            image_hash=image_hash,
            model=selected_model.value,
            analysis_result=cache_data,
            ttl=86400,  # 24 hours
        )

        logger.info(
            "Vision analysis completed",
            image_hash=image_hash,
            model=selected_model.value,
            confidence=confidence_score,
            cost=actual_cost,
            processing_time_ms=processing_time,
        )

        return result

    except Exception as e:
        processing_time = (time.time() - start_time) * 1000
        logger.error(
            "Vision analysis failed",
            image_path=analysis_input.image_path,
            error=str(e),
            processing_time_ms=processing_time,
        )
        raise
    finally:
        await cache_manager.close()


async def _scan_pii_content_impl(analysis_result: VisionAnalysisOutput) -> VisionAnalysisOutput:
    """
    Scan analysis result for PII content in sensitive fields

    Args:
        analysis_result: Vision analysis result to scan

    Returns:
        PII detection result with confidence and types
    """

    pii_detector = PIIDetector()

    # PII-sensitive fields from SharePoint schema
    pii_sensitive_fields = ["Kunde", "OrtohnePLZ", "Beschreibung"]

    detected_pii = []
    max_confidence = 0.0

    # Convert detected_fields from VisionAnalysisOutput to dict for processing
    detected_fields: dict[str, str | None] = {
        field.internal_name: field.field_value for field in analysis_result.detected_fields
    }

    for field_name in pii_sensitive_fields:
        if field_name in detected_fields:
            field_value = detected_fields[field_name]
            if field_value and isinstance(field_value, str):
                pii_result = await pii_detector.scan_text(field_value)

                if pii_result["has_pii"]:
                    detected_pii.extend(pii_result["pii_types"])
                    max_confidence = max(max_confidence, pii_result["confidence"])

    # Remove duplicates
    detected_pii = list(set(detected_pii))

    pii_result = {
        "has_pii": len(detected_pii) > 0,
        "pii_types": detected_pii,
        "confidence": max_confidence,
        "scanned_fields": pii_sensitive_fields,
    }

    if pii_result["has_pii"]:
        logger.warning(
            "PII detected in analysis result", pii_types=detected_pii, confidence=max_confidence
        )

    # Return updated VisionAnalysisOutput with PII information
    return analysis_result.model_copy(
        update={
            "pii_detected": pii_result["has_pii"],
            "pii_fields": pii_result["pii_types"],
        }
    )


@function_tool
async def scan_pii_content(analysis_result: VisionAnalysisOutput) -> VisionAnalysisOutput:
    return await _scan_pii_content_impl(analysis_result)


@function_tool(strict_mode=False)
async def validate_choice_fields(
    analysis_result: VisionAnalysisOutput,
    sharepoint_schema: SharePointSchemaInput,
) -> ChoiceFieldValidationOutput:
    """Validate Choice/MultiChoice fields against SharePoint schema."""

    detected_fields = {
        field.internal_name: field.field_value or ""
        for field in analysis_result.detected_fields
    }
    schema_fields = {field.internal_name: field for field in sharepoint_schema.fields}

    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    corrected_fields: dict[str, str | list[str]] = {}
    total_validated = 0

    for field_name, raw_value in detected_fields.items():
        field_def = schema_fields.get(field_name)
        if not field_def:
            continue

        total_validated += 1
        valid_choices = field_def.choices or []
        field_type = field_def.field_type

        if field_type == "Choice" and valid_choices:
            if raw_value not in valid_choices:
                validation_errors.append(
                    f"Invalid choice for {field_name}: '{raw_value}' not in {valid_choices}"
                )
                closest_match = _find_closest_choice(raw_value, valid_choices)
                if closest_match:
                    corrected_fields[field_name] = closest_match
                    validation_warnings.append(
                        f"Corrected {field_name}: '{raw_value}' → '{closest_match}'"
                    )

        elif field_type == "MultiChoice" and valid_choices:
            values = [v.strip() for v in raw_value.split(",") if v.strip()]
            invalid_choices = [v for v in values if v not in valid_choices]
            if invalid_choices:
                validation_errors.append(
                    f"Invalid choices for {field_name}: {invalid_choices} not in {valid_choices}"
                )
                valid_values = [v for v in values if v in valid_choices]
                if valid_values:
                    corrected_fields[field_name] = valid_values
                    validation_warnings.append(
                        f"Filtered {field_name}: removed {invalid_choices}"
                    )

    result = ChoiceFieldValidationOutput(
        is_valid=len(validation_errors) == 0,
        errors=validation_errors,
        warnings=validation_warnings,
        corrected_fields=corrected_fields,
        total_fields_validated=total_validated,
    )

    logger.info(
        "Choice field validation completed",
        is_valid=result.is_valid,
        errors_count=len(validation_errors),
        warnings_count=len(validation_warnings),
    )

    return result


@function_tool(strict_mode=False)
async def map_vision_to_sharepoint(
    vision_result: dict[str, Any],
    field_mappings: dict[str, Any] | None = None,
) -> MappedMetadataOutput:
    """Map Vision API results to SharePoint fields using configured mappings."""

    # Normalize tool inputs to strongly typed models. We accept generic dictionaries in order
    # to keep the JSON schema simple for the LLM tool definition, but still leverage the
    # rich validation logic implemented in the Pydantic models.
    vision_model = VisionAnalysisOutput.model_validate(vision_result)
    mapping_config = FieldMappingConfig.model_validate(field_mappings or {})

    detected_fields = {
        field.internal_name: field.field_value or ""
        for field in vision_model.detected_fields
    }

    flattened_rules: dict[str, FieldMappingRule] = {}
    for category in mapping_config.mappings.values():
        flattened_rules.update(category.fields)

    mapped_fields: dict[str, str | list[str]] = {}
    mapping_confidence: dict[str, float] = {}

    for field_name, raw_value in detected_fields.items():
        rule = flattened_rules.get(field_name)
        if not rule:
            continue

        value_list = [v.strip() for v in raw_value.split(",") if v.strip()]
        parsed_value: str | list[str]
        if value_list and len(value_list) > 1:
            parsed_value = value_list
        elif value_list:
            parsed_value = value_list[0]
        else:
            parsed_value = raw_value

        field_confidence = _calculate_field_confidence(parsed_value, rule)
        mapping_confidence[field_name] = field_confidence

        min_confidence = rule.confidence_range[0] if rule.confidence_range else 0.5

        if field_confidence >= min_confidence:
            mapped_fields[field_name] = parsed_value
        else:
            logger.warning(
                "Field excluded due to low confidence",
                field_name=field_name,
                confidence=field_confidence,
                threshold=min_confidence,
            )

    for encoded_name, decoded_name in mapping_config.encoded_fields.items():
        if encoded_name in mapped_fields:
            mapped_fields[decoded_name] = mapped_fields.pop(encoded_name)
            mapping_confidence[decoded_name] = mapping_confidence.pop(encoded_name, 0.0)

    if "Status" not in mapped_fields:
        mapped_fields["Status"] = "Entwurf KI"
        mapping_confidence["Status"] = 1.0

    avg_confidence = (
        sum(mapping_confidence.values()) / len(mapping_confidence)
        if mapping_confidence
        else 0.0
    )

    result = MappedMetadataOutput(
        mapped_fields=mapped_fields,
        mapping_confidence=mapping_confidence,
        total_mapped_fields=len(mapped_fields),
        avg_confidence=avg_confidence,
    )

    logger.info(
        "Vision to SharePoint mapping completed",
        mapped_fields=len(mapped_fields),
        avg_confidence=result.avg_confidence,
    )

    return result


class RetryableAPIError(Exception):
    """Exception for API errors that should trigger retry"""
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.RateLimitError,
        RetryableAPIError,
    )),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _call_openai_vision_api_with_retry(
    base64_image: str,
    json_schema: str,
    model: str,
    fields_to_analyze: list[str],
    api_key: str,
) -> Any:
    """
    Call OpenAI Vision API with retry logic, rate limiting, and exponential backoff

    Retry strategy:
    - Max 3 attempts
    - Exponential backoff: 2s, 4s, 8s (capped at 30s)
    - Retry on: timeout, connection errors, rate limits, 5xx errors
    - No retry on: 4xx errors (except 429 rate limit)

    Rate limiting:
    - Token bucket algorithm for RPM and TPM limits
    - Automatic queuing when limits reached
    - Per-model rate limits (gpt-4o vs gpt-4o-mini)

    Args:
        base64_image: Base64-encoded image
        json_schema: JSON schema for structured output
        model: OpenAI model to use
        fields_to_analyze: List of fields to extract
        api_key: OpenAI API key

    Returns:
        OpenAI API response

    Raises:
        RetryableAPIError: For 5xx and 429 errors
        openai.APIStatusError: For non-retryable 4xx errors
    """
    # Get rate limiter for this model
    rate_limiter = get_rate_limiter(model)

    # Estimate tokens for rate limiting
    # Vision API typically uses ~1000-2000 tokens per image
    # Add tokens for prompt and schema
    estimated_tokens = 1500 + len(json_schema) // 4  # Rough estimate

    try:
        # Acquire rate limit before making request
        async with rate_limiter.acquire(estimated_tokens=estimated_tokens):
            # Create specialized prompt based on fields
            system_prompt = _create_system_prompt(fields_to_analyze)
            user_prompt = _create_user_prompt(fields_to_analyze)

            client = openai.AsyncOpenAI(api_key=api_key)

            response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                        },
                    ],
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "sharepoint_metadata", "schema": json.loads(json_schema)},
            },
            max_tokens=1000,
            temperature=0.1,
        )

        return response

    except openai.APIStatusError as e:
        # Retry on 5xx errors and 429 (rate limit)
        if e.status_code >= 500 or e.status_code == 429:
            logger.warning(
                "Retryable API error",
                status_code=e.status_code,
                error=str(e),
                model=model,
            )
            raise RetryableAPIError(f"API error {e.status_code}: {e}") from e

        # Don't retry on 4xx errors (except 429)
        logger.error(
            "Non-retryable API error",
            status_code=e.status_code,
            error=str(e),
            model=model,
        )
        raise


async def _call_openai_vision_api(
    base64_image: str, json_schema: str, model: str, fields_to_analyze: list[str]
) -> Any:
    """
    Call OpenAI Vision API with structured output (wrapper with retry)

    This function wraps _call_openai_vision_api_with_retry to maintain
    backward compatibility while adding retry logic.
    """
    # Get API key from secure storage
    config = get_config()
    api_key = config.openai.get_api_key()
    if not api_key:
        raise ValueError("OpenAI API key not available in secure storage")

    return await _call_openai_vision_api_with_retry(
        base64_image=base64_image,
        json_schema=json_schema,
        model=model,
        fields_to_analyze=fields_to_analyze,
        api_key=api_key,
    )


def _create_system_prompt(fields_to_analyze: list[str]) -> str:
    """Create specialized system prompt based on fields to analyze"""
    fields_preview = ", ".join(fields_to_analyze[:10])
    return f"""
    You are an expert in Swiss construction and architecture with deep knowledge of building materials,
    construction techniques, and architectural terminology in German.

    Analyze construction photos and extract metadata for these specific fields: {fields_preview}

    Guidelines:
    - Use exact German terms from the provided enums
    - For materials, identify all visible materials in the photo
    - For building components (Bauteil), identify architectural elements
    - For project categories, classify based on building type and use
    - Include confidence assessment for each field
    - If uncertain about a field value, omit it rather than guessing
    - Pay special attention to wood types (Holzart) and surface treatments (Farbbehandlung)
    """


def _create_user_prompt(fields_to_analyze: list[str]) -> str:
    """Create user prompt for specific field analysis"""
    return """
    Analyze this construction photo and extract metadata for the SharePoint library "Fertige Referenzfotos".

    Focus on identifying:
    - Materials visible in the photo (Holz, Metall, Glas, etc.)
    - Building components (Fenster, Türen, Fassade, etc.)
    - Project type and category
    - Interior vs exterior view
    - Architectural details and finishes

    Return only the fields you can identify with reasonable confidence.
    Use the exact German terms from the provided schema.
    """


def _calculate_confidence_score(detected_fields: dict[str, Any]) -> float:
    """Calculate overall confidence score for detected fields"""
    if not detected_fields:
        return 0.0

    # Simple confidence calculation based on number of fields detected
    # and their complexity
    field_count = len(detected_fields)
    max_expected_fields = 15  # Reasonable expectation for vision analysis

    base_confidence = min(field_count / max_expected_fields, 1.0)

    # Boost confidence if key fields are detected
    key_fields = ["Material", "Ansicht", "Bauteil", "Projektkategorie"]
    key_fields_detected = sum(1 for field in key_fields if field in detected_fields)
    key_field_bonus = (key_fields_detected / len(key_fields)) * 0.2

    return min(base_confidence + key_field_bonus, 1.0)


def _normalize_detected_fields(
    raw_fields: Any, display_name_lookup: dict[str, str] | None = None
) -> list[DetectedFieldValue]:
    """Normalize raw vision output into DetectedFieldValue items."""

    if not isinstance(raw_fields, dict):
        return []

    normalized: list[DetectedFieldValue] = []

    for field_name, value in raw_fields.items():
        internal_name = field_name
        display_name = (
            display_name_lookup.get(internal_name)
            if display_name_lookup and internal_name in display_name_lookup
            else None
        )
        reasoning = None
        confidence = 0.0
        raw_value = value

        if isinstance(value, dict):
            raw_value = value.get("value")
            reasoning = value.get("reasoning")
            try:
                confidence = float(value.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            display_name = value.get("display_name", display_name)

        if isinstance(raw_value, str) or raw_value is None:
            field_value: str | None = raw_value
        elif isinstance(raw_value, list):
            field_value = ", ".join(str(item) for item in raw_value)
        else:
            field_value = json.dumps(raw_value, ensure_ascii=False)

        normalized.append(
            DetectedFieldValue(
                internal_name=internal_name,
                display_name=display_name,
                field_value=field_value,
                confidence=confidence,
                reasoning=reasoning,
            )
        )

    return normalized


def _load_detected_fields_from_cache(cached_fields: Any) -> list[DetectedFieldValue]:
    """Rehydrate cached detected fields into DetectedFieldValue objects."""

    if isinstance(cached_fields, list):
        normalized: list[DetectedFieldValue] = []
        for item in cached_fields:
            if isinstance(item, dict):
                try:
                    normalized.append(DetectedFieldValue.model_validate(item))
                except Exception:
                    normalized.extend(
                        _normalize_detected_fields({item.get("internal_name", ""): item})
                    )
        return normalized

    if isinstance(cached_fields, dict):
        return _normalize_detected_fields(cached_fields)

    return []


def _calculate_field_confidence(field_value: Any, field_mapping: FieldMappingRule | None) -> float:
    """Calculate confidence for a specific field"""
    if field_mapping and field_mapping.confidence_range:
        min_conf, max_conf = field_mapping.confidence_range
    else:
        min_conf, max_conf = 0.5, 1.0

    # Simple confidence calculation - can be enhanced with ML models
    if isinstance(field_value, list):
        # For multi-choice fields, confidence decreases with more selections
        return max(min_conf, max_conf - (len(field_value) * 0.1))
    else:
        # For single values, use middle of confidence range
        return (min_conf + max_conf) / 2


def _find_closest_choice(value: str, valid_choices: list[str]) -> str | None:
    """Find closest matching choice using simple string similarity"""
    if not value or not valid_choices:
        return None

    value_lower = value.lower()
    best_match: str | None = None
    best_score: float = 0.0

    for choice in valid_choices:
        choice_lower = choice.lower()

        # Simple similarity based on common substrings
        if value_lower in choice_lower or choice_lower in value_lower:
            score = min(len(value_lower), len(choice_lower)) / max(
                len(value_lower), len(choice_lower)
            )
            if score > best_score:
                best_score = score
                best_match = choice

    return best_match if best_score > 0.5 else None


class StructuredVisionAnalyzerAgent(Agent):
    """Agent for analyzing images with OpenAI Vision API and structured outputs"""

    def __init__(self) -> None:
        super().__init__(
            name="StructuredVisionAnalyzer",
            instructions="""
            You are an expert in construction image analysis using OpenAI Vision API.

            Your responsibilities:
            1. Analyze construction photos using Vision API with structured outputs
            2. Extract metadata for 23 SharePoint fields with 119 choice values
            3. Optimize costs by selecting appropriate models (gpt-4o vs gpt-4o-mini)
            4. Validate choice fields against SharePoint schema
            5. Detect PII in sensitive fields (Kunde, OrtohnePLZ, Beschreibung)
            6. Map Vision API results to SharePoint field structure
            7. Cache results for performance optimization

            Target SLO metrics:
            - Structured JSON success ≥ 0.96
            - Choice field accuracy ≥ 0.90
            - Cost per image ≤ $0.055
            - Processing time ≤ 15s per image

            Always use circuit breaker pattern and intelligent caching.
            """,
            tools=[
                analyze_image_vision,
                scan_pii_content,
                validate_choice_fields,
                map_vision_to_sharepoint,
            ],
            input_guardrails=[
                InputGuardrail(_vision_input_guardrail, name="vision_image_guardrail")
            ],
        )
