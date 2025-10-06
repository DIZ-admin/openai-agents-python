"""Unit tests for Event Emitter"""

import pytest

from erni_foto_agency.events import Event, EventEmitter, EventSeverity, EventType


class TestEvent:
    """Test suite for Event dataclass"""
    
    def test_event_creation(self):
        """Test creating an event"""
        event = Event(
            type=EventType.COMPLETED,
            component="test_component",
            message="Test completed",
            severity=EventSeverity.INFO,
            context={"key": "value"}
        )
        
        assert event.type == EventType.COMPLETED
        assert event.component == "test_component"
        assert event.message == "Test completed"
        assert event.severity == EventSeverity.INFO
        assert event.context == {"key": "value"}
        assert event.timestamp is not None
    
    def test_event_to_dict(self):
        """Test converting event to dictionary"""
        event = Event(
            type=EventType.STARTED,
            component="test_component",
            message="Test started",
            session_id="session-123",
            batch_id="batch-456"
        )
        
        event_dict = event.to_dict()
        
        assert isinstance(event_dict, dict)
        assert event_dict["type"] == "started"
        assert event_dict["component"] == "test_component"
        assert event_dict["message"] == "Test started"
        assert event_dict["session_id"] == "session-123"
        assert event_dict["batch_id"] == "batch-456"


class TestEventEmitter:
    """Test suite for EventEmitter"""
    
    @pytest.fixture
    def emitter(self):
        """Create event emitter instance"""
        return EventEmitter(
            component="test_component",
            session_id="test-session",
            batch_id="test-batch"
        )
    
    def test_emitter_creation(self, emitter):
        """Test creating event emitter"""
        assert emitter.component == "test_component"
        assert emitter.session_id == "test-session"
        assert emitter.batch_id == "test-batch"
    
    def test_emit_basic_event(self, emitter):
        """Test emitting basic event"""
        event = emitter.emit(
            EventType.PROCESSING,
            "Processing data",
            EventSeverity.INFO,
            context={"count": 10}
        )
        
        assert event.type == EventType.PROCESSING
        assert event.component == "test_component"
        assert event.message == "Processing data"
        assert event.severity == EventSeverity.INFO
        assert event.context["count"] == 10
        assert event.session_id == "test-session"
        assert event.batch_id == "test-batch"
    
    def test_emit_started(self, emitter):
        """Test emitting started event"""
        event = emitter.emit_started(
            "Data processing",
            context={"items": 100}
        )
        
        assert event.type == EventType.STARTED
        assert event.message == "Data processing started"
        assert event.severity == EventSeverity.INFO
        assert event.context["items"] == 100
    
    def test_emit_completed(self, emitter):
        """Test emitting completed event"""
        event = emitter.emit_completed(
            "Data processing",
            context={"processed": 100, "duration_ms": 1500}
        )
        
        assert event.type == EventType.COMPLETED
        assert event.message == "Data processing completed"
        assert event.severity == EventSeverity.INFO
        assert event.context["processed"] == 100
        assert event.context["duration_ms"] == 1500
    
    def test_emit_failed(self, emitter):
        """Test emitting failed event"""
        error = ValueError("Test error")
        
        event = emitter.emit_failed(
            "Data processing",
            error=error,
            context={"item_id": "123"}
        )
        
        assert event.type == EventType.FAILED
        assert event.message == "Data processing failed"
        assert event.severity == EventSeverity.ERROR
        assert event.context["error"] == "Test error"
        assert event.context["error_type"] == "ValueError"
        assert event.context["item_id"] == "123"
    
    def test_emit_failed_with_string_error(self, emitter):
        """Test emitting failed event with string error"""
        event = emitter.emit_failed(
            "Data processing",
            error="Something went wrong"
        )
        
        assert event.type == EventType.FAILED
        assert event.context["error"] == "Something went wrong"
        assert "error_type" not in event.context
    
    def test_emit_cache_hit(self, emitter):
        """Test emitting cache hit event"""
        event = emitter.emit_cache_hit(
            "image_hash_abc123",
            context={"ttl": 3600}
        )
        
        assert event.type == EventType.CACHE_HIT
        assert event.message == "Cache hit"
        assert event.severity == EventSeverity.DEBUG
        assert event.context["cache_key"] == "image_hash_abc123"
        assert event.context["ttl"] == 3600
    
    def test_emit_cache_miss(self, emitter):
        """Test emitting cache miss event"""
        event = emitter.emit_cache_miss(
            "image_hash_xyz789"
        )
        
        assert event.type == EventType.CACHE_MISS
        assert event.message == "Cache miss"
        assert event.severity == EventSeverity.DEBUG
        assert event.context["cache_key"] == "image_hash_xyz789"
    
    def test_emit_pii_detected(self, emitter):
        """Test emitting PII detected event"""
        event = emitter.emit_pii_detected(
            pii_types=["person_name", "email"],
            confidence=0.95,
            context={"field_name": "Kunde"}
        )
        
        assert event.type == EventType.PII_DETECTED
        assert event.message == "PII detected"
        assert event.severity == EventSeverity.WARNING
        assert event.context["pii_types"] == ["person_name", "email"]
        assert event.context["confidence"] == 0.95
        assert event.context["field_name"] == "Kunde"
    
    def test_emit_warning(self, emitter):
        """Test emitting warning event"""
        event = emitter.emit_warning(
            "Low confidence detected",
            context={"confidence": 0.45}
        )
        
        assert event.type == EventType.WARNING
        assert event.message == "Low confidence detected"
        assert event.severity == EventSeverity.WARNING
        assert event.context["confidence"] == 0.45
    
    def test_emit_error(self, emitter):
        """Test emitting error event"""
        error = RuntimeError("Test runtime error")
        
        event = emitter.emit_error(
            "Processing failed",
            error=error,
            context={"stage": "validation"}
        )
        
        assert event.type == EventType.ERROR
        assert event.message == "Processing failed"
        assert event.severity == EventSeverity.ERROR
        assert event.context["error"] == "Test runtime error"
        assert event.context["error_type"] == "RuntimeError"
        assert event.context["stage"] == "validation"
    
    def test_emit_error_without_exception(self, emitter):
        """Test emitting error event without exception"""
        event = emitter.emit_error(
            "Processing failed",
            context={"reason": "timeout"}
        )
        
        assert event.type == EventType.ERROR
        assert event.message == "Processing failed"
        assert event.severity == EventSeverity.ERROR
        assert "error" not in event.context
        assert event.context["reason"] == "timeout"
    
    def test_override_session_and_batch_id(self, emitter):
        """Test overriding session and batch IDs"""
        event = emitter.emit(
            EventType.PROCESSING,
            "Processing data",
            session_id="override-session",
            batch_id="override-batch"
        )
        
        assert event.session_id == "override-session"
        assert event.batch_id == "override-batch"
    
    def test_emitter_without_session_and_batch(self):
        """Test emitter without session and batch IDs"""
        emitter = EventEmitter(component="test")
        
        event = emitter.emit(
            EventType.PROCESSING,
            "Processing data"
        )
        
        assert event.session_id is None
        assert event.batch_id is None
    
    def test_event_types_enum(self):
        """Test EventType enum values"""
        assert EventType.STARTED.value == "started"
        assert EventType.COMPLETED.value == "completed"
        assert EventType.FAILED.value == "failed"
        assert EventType.PII_DETECTED.value == "pii_detected"
        assert EventType.CACHE_HIT.value == "cache_hit"
    
    def test_event_severity_enum(self):
        """Test EventSeverity enum values"""
        assert EventSeverity.DEBUG.value == "debug"
        assert EventSeverity.INFO.value == "info"
        assert EventSeverity.WARNING.value == "warning"
        assert EventSeverity.ERROR.value == "error"
        assert EventSeverity.CRITICAL.value == "critical"

