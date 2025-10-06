"""
Event Emitter for Centralized Event Logging

Provides DRY (Don't Repeat Yourself) event emission with:
- Standardized event structure
- Automatic context enrichment
- Type-safe event definitions
- Metrics integration
- Structured logging
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class EventType(Enum):
    """Standard event types"""
    
    # Lifecycle events
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    
    # Processing events
    PROCESSING = "processing"
    VALIDATED = "validated"
    CACHED = "cached"
    
    # Data events
    EXTRACTED = "extracted"
    MAPPED = "mapped"
    UPLOADED = "uploaded"
    
    # Security events
    PII_DETECTED = "pii_detected"
    PII_BLOCKED = "pii_blocked"
    
    # Performance events
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    RATE_LIMITED = "rate_limited"
    
    # Error events
    ERROR = "error"
    WARNING = "warning"


class EventSeverity(Enum):
    """Event severity levels"""
    
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Event:
    """
    Standard event structure
    
    All events follow this structure for consistency and easy processing.
    """
    
    type: EventType
    """Type of event"""
    
    component: str
    """Component that emitted the event (e.g., 'vision_analyzer', 'schema_extractor')"""
    
    message: str
    """Human-readable event message"""
    
    severity: EventSeverity = EventSeverity.INFO
    """Event severity level"""
    
    context: dict[str, Any] = field(default_factory=dict)
    """Additional context data"""
    
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    """ISO 8601 timestamp"""
    
    session_id: str | None = None
    """Optional session ID for correlation"""
    
    batch_id: str | None = None
    """Optional batch ID for correlation"""
    
    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary"""
        return {
            "type": self.type.value,
            "component": self.component,
            "message": self.message,
            "severity": self.severity.value,
            "context": self.context,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "batch_id": self.batch_id,
        }


class EventEmitter:
    """
    Centralized event emitter
    
    Provides DRY event emission with automatic context enrichment
    and structured logging.
    
    Example:
        emitter = EventEmitter(component="vision_analyzer")
        
        # Emit success event
        emitter.emit_success(
            "Vision analysis completed",
            context={"image_hash": "abc123", "confidence": 0.95}
        )
        
        # Emit error event
        emitter.emit_error(
            "Vision analysis failed",
            error=exception,
            context={"image_path": "/path/to/image.jpg"}
        )
    """
    
    def __init__(
        self,
        component: str,
        session_id: str | None = None,
        batch_id: str | None = None
    ):
        """
        Initialize event emitter
        
        Args:
            component: Component name (e.g., 'vision_analyzer')
            session_id: Optional session ID for all events
            batch_id: Optional batch ID for all events
        """
        self.component = component
        self.session_id = session_id
        self.batch_id = batch_id
    
    def emit(
        self,
        event_type: EventType,
        message: str,
        severity: EventSeverity = EventSeverity.INFO,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        batch_id: str | None = None
    ) -> Event:
        """
        Emit an event
        
        Args:
            event_type: Type of event
            message: Human-readable message
            severity: Event severity
            context: Additional context data
            session_id: Override session ID
            batch_id: Override batch ID
        
        Returns:
            The emitted event
        """
        event = Event(
            type=event_type,
            component=self.component,
            message=message,
            severity=severity,
            context=context or {},
            session_id=session_id or self.session_id,
            batch_id=batch_id or self.batch_id
        )
        
        # Log event with appropriate severity
        log_method = getattr(logger, severity.value)
        log_method(
            message,
            component=self.component,
            event_type=event_type.value,
            **event.context
        )
        
        return event
    
    # Convenience methods for common event patterns
    
    def emit_started(
        self,
        operation: str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit operation started event"""
        return self.emit(
            EventType.STARTED,
            f"{operation} started",
            EventSeverity.INFO,
            context
        )
    
    def emit_completed(
        self,
        operation: str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit operation completed event"""
        return self.emit(
            EventType.COMPLETED,
            f"{operation} completed",
            EventSeverity.INFO,
            context
        )
    
    def emit_failed(
        self,
        operation: str,
        error: Exception | str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit operation failed event"""
        ctx = context or {}
        ctx["error"] = str(error)
        if isinstance(error, Exception):
            ctx["error_type"] = type(error).__name__
        
        return self.emit(
            EventType.FAILED,
            f"{operation} failed",
            EventSeverity.ERROR,
            ctx
        )
    
    def emit_cache_hit(
        self,
        key: str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit cache hit event"""
        ctx = context or {}
        ctx["cache_key"] = key
        
        return self.emit(
            EventType.CACHE_HIT,
            "Cache hit",
            EventSeverity.DEBUG,
            ctx
        )
    
    def emit_cache_miss(
        self,
        key: str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit cache miss event"""
        ctx = context or {}
        ctx["cache_key"] = key
        
        return self.emit(
            EventType.CACHE_MISS,
            "Cache miss",
            EventSeverity.DEBUG,
            ctx
        )
    
    def emit_pii_detected(
        self,
        pii_types: list[str],
        confidence: float,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit PII detected event"""
        ctx = context or {}
        ctx["pii_types"] = pii_types
        ctx["confidence"] = confidence
        
        return self.emit(
            EventType.PII_DETECTED,
            "PII detected",
            EventSeverity.WARNING,
            ctx
        )
    
    def emit_warning(
        self,
        message: str,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit warning event"""
        return self.emit(
            EventType.WARNING,
            message,
            EventSeverity.WARNING,
            context
        )
    
    def emit_error(
        self,
        message: str,
        error: Exception | str | None = None,
        context: dict[str, Any] | None = None
    ) -> Event:
        """Emit error event"""
        ctx = context or {}
        if error:
            ctx["error"] = str(error)
            if isinstance(error, Exception):
                ctx["error_type"] = type(error).__name__
        
        return self.emit(
            EventType.ERROR,
            message,
            EventSeverity.ERROR,
            ctx
        )

