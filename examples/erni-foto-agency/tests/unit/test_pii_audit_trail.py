"""Unit tests for PII Audit Trail"""

import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from erni_foto_agency.security.pii_audit_trail import (
    PIIAuditAction,
    PIIAuditRecord,
    PIIAuditTrail,
    PIIRiskLevel,
)


class TestPIIAuditRecord:
    """Test suite for PII Audit Record"""
    
    def test_record_creation(self):
        """Test creating audit record"""
        record = PIIAuditRecord(
            audit_id=str(uuid.uuid4()),
            session_id="test-session",
            timestamp=datetime.utcnow().isoformat(),
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash="abc123",
            has_pii=True,
            confidence=0.95,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name", "email"],
            blocked=True,
            block_reason="High-risk PII detected"
        )
        
        assert record.audit_id is not None
        assert record.session_id == "test-session"
        assert record.has_pii is True
        assert record.confidence == 0.95
        assert record.blocked is True
    
    def test_record_to_dict(self):
        """Test converting record to dictionary"""
        record = PIIAuditRecord(
            audit_id="test-id",
            session_id="test-session",
            timestamp=datetime.utcnow().isoformat(),
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash="abc123",
            has_pii=True,
            confidence=0.95,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name"],
            blocked=False,
            block_reason=None
        )
        
        record_dict = record.to_dict()
        
        assert isinstance(record_dict, dict)
        assert record_dict["audit_id"] == "test-id"
        assert record_dict["has_pii"] is True
        assert record_dict["pii_types"] == ["person_name"]
    
    def test_record_from_dict(self):
        """Test creating record from dictionary"""
        data = {
            "audit_id": "test-id",
            "session_id": "test-session",
            "timestamp": datetime.utcnow().isoformat(),
            "action": PIIAuditAction.PII_DETECTED.value,
            "field_name": "Kunde",
            "field_value_hash": "abc123",
            "has_pii": True,
            "confidence": 0.95,
            "risk_level": PIIRiskLevel.HIGH.value,
            "pii_types": ["person_name"],
            "blocked": False,
            "block_reason": None
        }
        
        record = PIIAuditRecord.from_dict(data)
        
        assert record.audit_id == "test-id"
        assert record.has_pii is True


class TestPIIAuditTrail:
    """Test suite for PII Audit Trail"""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_audit.db"
            yield db_path
    
    @pytest.fixture
    def audit_trail(self, temp_db):
        """Create audit trail instance"""
        return PIIAuditTrail(db_path=temp_db, retention_days=30)
    
    def test_audit_trail_creation(self, audit_trail, temp_db):
        """Test audit trail initialization"""
        assert audit_trail.db_path == temp_db
        assert audit_trail.retention_days == 30
        assert temp_db.exists()
    
    @pytest.mark.asyncio
    async def test_log_record(self, audit_trail):
        """Test logging audit record"""
        record = PIIAuditRecord(
            audit_id=str(uuid.uuid4()),
            session_id="test-session",
            timestamp=datetime.utcnow().isoformat(),
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash="abc123",
            has_pii=True,
            confidence=0.95,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name", "email"],
            blocked=True,
            block_reason="High-risk PII"
        )
        
        # Should not raise exception
        await audit_trail.log(record)
    
    @pytest.mark.asyncio
    async def test_get_records(self, audit_trail):
        """Test retrieving audit records"""
        # Log multiple records
        for i in range(3):
            record = PIIAuditRecord(
                audit_id=str(uuid.uuid4()),
                session_id=f"session-{i}",
                timestamp=datetime.utcnow().isoformat(),
                action=PIIAuditAction.PII_DETECTED.value,
                field_name="Kunde",
                field_value_hash=f"hash-{i}",
                has_pii=True,
                confidence=0.9,
                risk_level=PIIRiskLevel.HIGH.value,
                pii_types=["person_name"],
                blocked=i % 2 == 0,  # Block every other
                block_reason="Test" if i % 2 == 0 else None
            )
            await audit_trail.log(record)
        
        # Retrieve all records
        records = await audit_trail.get_records(limit=10)
        
        assert len(records) == 3
        assert all(isinstance(r, PIIAuditRecord) for r in records)
    
    @pytest.mark.asyncio
    async def test_get_records_by_session(self, audit_trail):
        """Test filtering records by session ID"""
        session_id = "target-session"
        
        # Log records for different sessions
        for i in range(5):
            record = PIIAuditRecord(
                audit_id=str(uuid.uuid4()),
                session_id=session_id if i < 2 else f"other-session-{i}",
                timestamp=datetime.utcnow().isoformat(),
                action=PIIAuditAction.PII_DETECTED.value,
                field_name="Kunde",
                field_value_hash=f"hash-{i}",
                has_pii=True,
                confidence=0.9,
                risk_level=PIIRiskLevel.HIGH.value,
                pii_types=["person_name"],
                blocked=False,
                block_reason=None
            )
            await audit_trail.log(record)
        
        # Retrieve records for specific session
        records = await audit_trail.get_records(session_id=session_id)
        
        assert len(records) == 2
        assert all(r.session_id == session_id for r in records)
    
    @pytest.mark.asyncio
    async def test_get_records_blocked_only(self, audit_trail):
        """Test filtering for blocked records only"""
        # Log mixed records
        for i in range(4):
            record = PIIAuditRecord(
                audit_id=str(uuid.uuid4()),
                session_id=f"session-{i}",
                timestamp=datetime.utcnow().isoformat(),
                action=PIIAuditAction.PII_BLOCKED.value if i < 2 else PIIAuditAction.PII_ALLOWED.value,
                field_name="Kunde",
                field_value_hash=f"hash-{i}",
                has_pii=True,
                confidence=0.9,
                risk_level=PIIRiskLevel.HIGH.value,
                pii_types=["person_name"],
                blocked=i < 2,
                block_reason="Blocked" if i < 2 else None
            )
            await audit_trail.log(record)
        
        # Retrieve only blocked records
        records = await audit_trail.get_records(blocked_only=True)
        
        assert len(records) == 2
        assert all(r.blocked for r in records)
    
    @pytest.mark.asyncio
    async def test_cleanup_old_records(self, audit_trail):
        """Test cleanup of old audit records"""
        # Log old record
        old_timestamp = (datetime.utcnow() - timedelta(days=100)).isoformat()
        old_record = PIIAuditRecord(
            audit_id=str(uuid.uuid4()),
            session_id="old-session",
            timestamp=old_timestamp,
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash="old-hash",
            has_pii=True,
            confidence=0.9,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name"],
            blocked=False,
            block_reason=None
        )
        await audit_trail.log(old_record)
        
        # Log recent record
        recent_record = PIIAuditRecord(
            audit_id=str(uuid.uuid4()),
            session_id="recent-session",
            timestamp=datetime.utcnow().isoformat(),
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash="recent-hash",
            has_pii=True,
            confidence=0.9,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name"],
            blocked=False,
            block_reason=None
        )
        await audit_trail.log(recent_record)
        
        # Cleanup old records
        deleted_count = await audit_trail.cleanup_old_records()
        
        assert deleted_count == 1
        
        # Verify only recent record remains
        records = await audit_trail.get_records()
        assert len(records) == 1
        assert records[0].session_id == "recent-session"
    
    @pytest.mark.asyncio
    async def test_get_statistics(self, audit_trail):
        """Test getting audit statistics"""
        # Log various records
        for i in range(10):
            record = PIIAuditRecord(
                audit_id=str(uuid.uuid4()),
                session_id=f"session-{i}",
                timestamp=datetime.utcnow().isoformat(),
                action=PIIAuditAction.PII_DETECTED.value,
                field_name="Kunde",
                field_value_hash=f"hash-{i}",
                has_pii=i < 7,  # 7 with PII, 3 without
                confidence=0.9 if i < 7 else 0.1,
                risk_level=PIIRiskLevel.HIGH.value if i < 3 else PIIRiskLevel.LOW.value,
                pii_types=["person_name"] if i < 7 else [],
                blocked=i < 2,  # 2 blocked
                block_reason="Blocked" if i < 2 else None
            )
            await audit_trail.log(record)
        
        # Get statistics
        stats = await audit_trail.get_statistics()
        
        assert stats["total_scans"] == 10
        assert stats["pii_detected"] == 7
        assert stats["pii_detection_rate"] == 0.7
        assert stats["blocked_count"] == 2
        assert stats["block_rate"] == pytest.approx(2/7, rel=0.01)
        assert "risk_levels" in stats

