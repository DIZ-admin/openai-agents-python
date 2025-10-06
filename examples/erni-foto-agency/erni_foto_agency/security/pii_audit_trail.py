"""
PII Audit Trail for Compliance and Transparency

Provides comprehensive audit logging for PII detection and validation:
- Immutable audit records
- Compliance reporting
- Retention policies
- Export capabilities
"""

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class PIIAuditAction(Enum):
    """Types of PII audit actions"""
    
    SCAN_INITIATED = "scan_initiated"
    PII_DETECTED = "pii_detected"
    PII_BLOCKED = "pii_blocked"
    PII_ALLOWED = "pii_allowed"
    FALSE_POSITIVE = "false_positive"
    MANUAL_REVIEW = "manual_review"


class PIIRiskLevel(Enum):
    """PII risk levels"""
    
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class PIIAuditRecord:
    """
    Immutable audit record for PII detection
    
    Captures all relevant information for compliance and investigation.
    """
    
    # Unique identifiers
    audit_id: str
    session_id: str
    timestamp: str
    
    # Action details
    action: str  # PIIAuditAction value
    field_name: str
    field_value_hash: str  # SHA-256 hash of field value (never store actual PII)
    
    # Detection results
    has_pii: bool
    confidence: float
    risk_level: str  # PIIRiskLevel value
    pii_types: list[str]
    
    # Decision
    blocked: bool
    block_reason: str | None
    
    # Context
    image_path: str | None = None
    user_id: str | None = None
    ip_address: str | None = None
    
    # Detection details (for investigation)
    detection_method: str = "hybrid"  # rule-based, ai-based, hybrid
    rule_matches: int = 0
    ai_confidence: float = 0.0
    
    # Metadata
    processing_time_ms: float = 0.0
    detector_version: str = "1.0.0"
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PIIAuditRecord":
        """Create from dictionary"""
        return cls(**data)


class PIIAuditTrail:
    """
    PII Audit Trail Manager
    
    Features:
    - Immutable audit records stored in SQLite
    - Automatic retention policy enforcement
    - Compliance reporting
    - Export capabilities (JSON, CSV)
    - Query interface for investigations
    
    Example:
        audit = PIIAuditTrail(db_path="pii_audit.db")
        
        record = PIIAuditRecord(
            audit_id=str(uuid.uuid4()),
            session_id="session-123",
            timestamp=datetime.utcnow().isoformat(),
            action=PIIAuditAction.PII_DETECTED.value,
            field_name="Kunde",
            field_value_hash=hashlib.sha256(value.encode()).hexdigest(),
            has_pii=True,
            confidence=0.95,
            risk_level=PIIRiskLevel.HIGH.value,
            pii_types=["person_name", "email"],
            blocked=True,
            block_reason="High-risk PII with high confidence"
        )
        
        await audit.log(record)
    """
    
    def __init__(
        self,
        db_path: str | Path = "data/pii_audit.db",
        retention_days: int = 90
    ):
        """
        Initialize audit trail
        
        Args:
            db_path: Path to SQLite database
            retention_days: Number of days to retain audit records
        """
        self.db_path = Path(db_path)
        self.retention_days = retention_days
        
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._init_database()
        
        logger.info(
            "PII Audit Trail initialized",
            db_path=str(self.db_path),
            retention_days=retention_days
        )
    
    def _init_database(self) -> None:
        """Initialize SQLite database with audit table"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create audit table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pii_audit (
                audit_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                field_name TEXT NOT NULL,
                field_value_hash TEXT NOT NULL,
                has_pii INTEGER NOT NULL,
                confidence REAL NOT NULL,
                risk_level TEXT NOT NULL,
                pii_types TEXT NOT NULL,
                blocked INTEGER NOT NULL,
                block_reason TEXT,
                image_path TEXT,
                user_id TEXT,
                ip_address TEXT,
                detection_method TEXT,
                rule_matches INTEGER,
                ai_confidence REAL,
                processing_time_ms REAL,
                detector_version TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_id 
            ON pii_audit(session_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp 
            ON pii_audit(timestamp)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_blocked 
            ON pii_audit(blocked)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_risk_level 
            ON pii_audit(risk_level)
        """)
        
        conn.commit()
        conn.close()
        
        logger.debug("PII audit database initialized")
    
    async def log(self, record: PIIAuditRecord) -> None:
        """
        Log PII audit record
        
        Args:
            record: Audit record to log
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO pii_audit (
                    audit_id, session_id, timestamp, action, field_name,
                    field_value_hash, has_pii, confidence, risk_level,
                    pii_types, blocked, block_reason, image_path, user_id,
                    ip_address, detection_method, rule_matches, ai_confidence,
                    processing_time_ms, detector_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.audit_id,
                record.session_id,
                record.timestamp,
                record.action,
                record.field_name,
                record.field_value_hash,
                1 if record.has_pii else 0,
                record.confidence,
                record.risk_level,
                json.dumps(record.pii_types),
                1 if record.blocked else 0,
                record.block_reason,
                record.image_path,
                record.user_id,
                record.ip_address,
                record.detection_method,
                record.rule_matches,
                record.ai_confidence,
                record.processing_time_ms,
                record.detector_version
            ))
            
            conn.commit()
            
            logger.info(
                "PII audit record logged",
                audit_id=record.audit_id,
                session_id=record.session_id,
                action=record.action,
                has_pii=record.has_pii,
                blocked=record.blocked
            )
            
        except Exception as e:
            logger.error("Failed to log PII audit record", error=str(e))
            conn.rollback()
            raise
        finally:
            conn.close()
    
    async def get_records(
        self,
        session_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        blocked_only: bool = False,
        limit: int = 100
    ) -> list[PIIAuditRecord]:
        """
        Query audit records
        
        Args:
            session_id: Filter by session ID
            start_date: Filter by start date
            end_date: Filter by end date
            blocked_only: Only return blocked records
            limit: Maximum number of records to return
        
        Returns:
            List of audit records
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        query = "SELECT * FROM pii_audit WHERE 1=1"
        params = []
        
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        
        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date.isoformat())
        
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date.isoformat())
        
        if blocked_only:
            query += " AND blocked = 1"
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        records = []
        for row in rows:
            record_dict = dict(row)
            # Convert JSON strings back to lists
            record_dict["pii_types"] = json.loads(record_dict["pii_types"])
            # Convert integers back to booleans
            record_dict["has_pii"] = bool(record_dict["has_pii"])
            record_dict["blocked"] = bool(record_dict["blocked"])
            # Remove created_at (internal field)
            record_dict.pop("created_at", None)
            
            records.append(PIIAuditRecord.from_dict(record_dict))
        
        return records
    
    async def cleanup_old_records(self) -> int:
        """
        Remove audit records older than retention period
        
        Returns:
            Number of records deleted
        """
        cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM pii_audit 
            WHERE timestamp < ?
        """, (cutoff_date.isoformat(),))
        
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted_count > 0:
            logger.info(
                "Cleaned up old PII audit records",
                deleted_count=deleted_count,
                cutoff_date=cutoff_date.isoformat()
            )
        
        return deleted_count
    
    async def get_statistics(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None
    ) -> dict[str, Any]:
        """
        Get audit statistics for reporting
        
        Args:
            start_date: Start date for statistics
            end_date: End date for statistics
        
        Returns:
            Dictionary with statistics
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        query_filter = "WHERE 1=1"
        params = []
        
        if start_date:
            query_filter += " AND timestamp >= ?"
            params.append(start_date.isoformat())
        
        if end_date:
            query_filter += " AND timestamp <= ?"
            params.append(end_date.isoformat())
        
        # Total scans
        cursor.execute(f"SELECT COUNT(*) FROM pii_audit {query_filter}", params)
        total_scans = cursor.fetchone()[0]
        
        # PII detected
        cursor.execute(
            f"SELECT COUNT(*) FROM pii_audit {query_filter} AND has_pii = 1",
            params
        )
        pii_detected = cursor.fetchone()[0]
        
        # Blocked
        cursor.execute(
            f"SELECT COUNT(*) FROM pii_audit {query_filter} AND blocked = 1",
            params
        )
        blocked_count = cursor.fetchone()[0]
        
        # By risk level
        cursor.execute(
            f"SELECT risk_level, COUNT(*) FROM pii_audit {query_filter} GROUP BY risk_level",
            params
        )
        risk_levels = dict(cursor.fetchall())
        
        conn.close()
        
        return {
            "total_scans": total_scans,
            "pii_detected": pii_detected,
            "pii_detection_rate": pii_detected / total_scans if total_scans > 0 else 0.0,
            "blocked_count": blocked_count,
            "block_rate": blocked_count / pii_detected if pii_detected > 0 else 0.0,
            "risk_levels": risk_levels,
            "period": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None
            }
        }

