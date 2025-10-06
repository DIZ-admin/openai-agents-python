"""
PII Detector for SharePoint fields Kunde, OrtohnePLZ, Beschreibung
Implements privacy compliance with FN-rate ≤ 1.0%
"""

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import openai
import structlog

from ..config.settings import get_config
from ..security.pii_audit_trail import PIIAuditAction, PIIAuditRecord, PIIAuditTrail, PIIRiskLevel

logger = structlog.get_logger(__name__)


class PIIType(Enum):
    PERSON_NAME = "person_name"
    COMPANY_NAME = "company_name"
    LOCATION = "location"
    ADDRESS = "address"
    PHONE_NUMBER = "phone_number"
    EMAIL = "email"
    PERSONAL_INFO = "personal_info"
    OTHER = "other"


@dataclass
class PIIDetectionResult:
    """Result of PII detection"""

    has_pii: bool
    pii_types: list[PIIType]
    confidence: float
    detected_entities: list[dict[str, Any]]
    risk_level: str  # low, medium, high


class PIIDetector:
    """
    PII detector optimized for SharePoint fields with German content
    Focuses on construction industry context with comprehensive audit trail
    """

    def __init__(
        self,
        confidence_threshold: float = 0.80,
        enable_audit: bool = True,
        audit_db_path: str = "data/pii_audit.db"
    ):
        self.confidence_threshold = confidence_threshold
        self.enable_audit = enable_audit

        # Initialize audit trail
        if self.enable_audit:
            self.audit_trail = PIIAuditTrail(db_path=audit_db_path)
        else:
            self.audit_trail = None

        # German name patterns (common Swiss/German names)
        self.german_name_patterns = [
            r"\b[A-ZÄÖÜ][a-zäöüß]+\s+[A-ZÄÖÜ][a-zäöüß]+\b",  # First Last
            r"\b[A-ZÄÖÜ][a-zäöüß]+\s+AG\b",  # Company AG
            r"\b[A-ZÄÖÜ][a-zäöüß]+\s+GmbH\b",  # Company GmbH
            r"\b[A-ZÄÖÜ][a-zäöüß]+\s+&\s+[A-ZÄÖÜ][a-zäöüß]+\b",  # Company & Company
        ]

        # Swiss location patterns
        self.location_patterns = [
            r"\b\d{4}\s+[A-ZÄÖÜ][a-zäöüß]+\b",  # Swiss postal code + city
            r"\b[A-ZÄÖÜ][a-zäöüß]+\s+\d{4}\b",  # City + postal code
            r"\b(?:Zürich|Bern|Basel|Genf|Lausanne|Winterthur|Luzern|St\.\s*Gallen|Biel|Thun|Köniz|La\s*Chaux-de-Fonds|Schaffhausen|Fribourg|Vernier|Chur|Neuchâtel|Uster|Sion|Emmen|Zug|Yverdon-les-Bains|Dübendorf|Dietikon|Montreux|Frauenfeld|Kreuzlingen|Rapperswil-Jona|Wetzikon|Baar|Nyon|Reinach|Allschwil|Renens|Bulle|Wil|Vevey|Kloten|Bülach|Opfikon|Spreitenbach|Illnau-Effretikon|Solothurn|Wohlen|Oftringen|Horgen|Pratteln|Versoix|Küsnacht|Ecublens|Pully|Richterswil|Wallisellen|Carouge|Freienbach|Aarau|Steffisburg|Bellinzona|Langenthal|Cham|Le\s*Locle|Volketswil|Rüti|Sierre|Martigny|Gossau|Muttenz|Münchenstein|Herisau|Arbon|Einsiedeln|Liestal)\b",
        ]

        # Contact information patterns
        self.contact_patterns = [
            r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}\b",  # Swiss phone
            r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",  # Email
            r"\b(?:Tel|Telefon|Phone|Mobile|Handy)[:.\s]*\+?[\d\s\-\(\)]+\b",  # Phone labels
        ]

        # Construction industry whitelist (not PII)
        self.construction_whitelist = [
            "Holzbau",
            "Zimmerei",
            "Schreinerei",
            "Spenglerei",
            "Dachdeckerei",
            "Bauunternehmen",
            "Architektur",
            "Ingenieur",
            "Planer",
            "Bauleitung",
            "Einfamilienhaus",
            "Mehrfamilienhaus",
            "Gewerbe",
            "Industrie",
            "Renovation",
            "Sanierung",
            "Neubau",
            "Umbau",
            "Erweiterung",
        ]

    async def scan_text(
        self,
        text: str,
        field_name: str = "",
        session_id: str | None = None,
        image_path: str | None = None
    ) -> dict[str, Any]:
        """
        Scan text for PII content with audit trail

        Args:
            text: Text to scan
            field_name: Name of the field being scanned
            session_id: Optional session ID for audit trail
            image_path: Optional image path for audit trail

        Returns:
            PII detection result
        """
        start_time = time.time()

        if not text or not text.strip():
            return {
                "has_pii": False,
                "pii_types": [],
                "confidence": 1.0,
                "detected_entities": [],
                "risk_level": "low",
            }

        detected_entities = []
        pii_types = set()
        max_confidence = 0.0

        # Rule-based detection
        rule_results = await self._rule_based_detection(text, field_name)
        detected_entities.extend(rule_results["entities"])
        pii_types.update(rule_results["types"])
        max_confidence = max(max_confidence, rule_results["confidence"])

        # AI-based detection for complex cases
        ai_results = {"entities": [], "types": set(), "confidence": 0.0}
        if max_confidence < self.confidence_threshold:
            ai_results = await self._ai_based_detection(text, field_name)
            detected_entities.extend(ai_results["entities"])
            pii_types.update(ai_results["types"])
            max_confidence = max(max_confidence, ai_results["confidence"])

        # Determine risk level
        risk_level = self._calculate_risk_level(list(pii_types), max_confidence)

        has_pii = max_confidence >= self.confidence_threshold and len(pii_types) > 0

        result = {
            "has_pii": has_pii,
            "pii_types": list(pii_types),
            "confidence": max_confidence,
            "detected_entities": detected_entities,
            "risk_level": risk_level,
        }

        if has_pii:
            logger.warning(
                "PII detected",
                field_name=field_name,
                pii_types=list(pii_types),
                confidence=max_confidence,
                risk_level=risk_level,
            )

        # Log to audit trail
        if self.enable_audit and self.audit_trail:
            processing_time_ms = (time.time() - start_time) * 1000

            # Determine detection method
            detection_method = "hybrid"
            if len(rule_results["entities"]) > 0 and len(ai_results.get("entities", [])) == 0:
                detection_method = "rule-based"
            elif len(rule_results["entities"]) == 0 and len(ai_results.get("entities", [])) > 0:
                detection_method = "ai-based"

            # Create audit record
            audit_record = PIIAuditRecord(
                audit_id=str(uuid.uuid4()),
                session_id=session_id or "unknown",
                timestamp=datetime.utcnow().isoformat(),
                action=PIIAuditAction.PII_DETECTED.value if has_pii else PIIAuditAction.SCAN_INITIATED.value,
                field_name=field_name,
                field_value_hash=hashlib.sha256(text.encode()).hexdigest(),
                has_pii=has_pii,
                confidence=max_confidence,
                risk_level=risk_level,
                pii_types=[t.value if isinstance(t, PIIType) else str(t) for t in pii_types],
                blocked=False,  # Will be updated by should_block_processing
                block_reason=None,
                image_path=image_path,
                detection_method=detection_method,
                rule_matches=len(rule_results["entities"]),
                ai_confidence=ai_results.get("confidence", 0.0),
                processing_time_ms=processing_time_ms
            )

            try:
                await self.audit_trail.log(audit_record)
            except Exception as e:
                logger.error("Failed to log PII audit record", error=str(e))

        return result

    async def _rule_based_detection(self, text: str, field_name: str) -> dict[str, Any]:
        """Rule-based PII detection using regex patterns"""

        entities = []
        types = set()
        confidence = 0.0

        # Check for names (but filter construction terms)
        for pattern in self.german_name_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                matched_text = match.group()

                # Skip if it's a construction industry term
                if not any(
                    term.lower() in matched_text.lower() for term in self.construction_whitelist
                ):
                    entities.append(
                        {
                            "text": matched_text,
                            "type": PIIType.PERSON_NAME.value,
                            "start": match.start(),
                            "end": match.end(),
                            "confidence": 0.85,
                        }
                    )
                    types.add(PIIType.PERSON_NAME)
                    confidence = max(confidence, 0.85)

        # Check for locations
        for pattern in self.location_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                entities.append(
                    {
                        "text": match.group(),
                        "type": PIIType.LOCATION.value,
                        "start": match.start(),
                        "end": match.end(),
                        "confidence": 0.90,
                    }
                )
                types.add(PIIType.LOCATION)
                confidence = max(confidence, 0.90)

        # Check for contact information
        for pattern in self.contact_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                pii_type = PIIType.EMAIL if "@" in match.group() else PIIType.PHONE_NUMBER
                entities.append(
                    {
                        "text": match.group(),
                        "type": pii_type.value,
                        "start": match.start(),
                        "end": match.end(),
                        "confidence": 0.95,
                    }
                )
                types.add(pii_type)
                confidence = max(confidence, 0.95)

        return {"entities": entities, "types": types, "confidence": confidence}

    async def _ai_based_detection(self, text: str, field_name: str) -> dict[str, Any]:
        """AI-based PII detection using OpenAI for complex cases"""

        try:
            # Get API key from secure storage
            config = get_config()
            api_key = config.openai.get_api_key()
            if not api_key:
                logger.warning("OpenAI API key not available, skipping AI-based PII detection")
                return {"entities": [], "types": [], "confidence": 0.0}

            client = openai.AsyncOpenAI(api_key=api_key)

            prompt = f"""
            Analyze the following German text from a construction project SharePoint field "{field_name}" for personally identifiable information (PII).

            Text: "{text}"

            Context: This is from a Swiss construction company's photo metadata system.
            Common non-PII terms include: company types (AG, GmbH), construction terms (Holzbau, Zimmerei),
            building types (Einfamilienhaus, Gewerbe), and technical descriptions.

            Identify any PII such as:
            - Personal names (but not company names without personal identifiers)
            - Specific addresses with street names/numbers
            - Phone numbers or email addresses
            - Other personal identifiers

            Respond with JSON:
            {{
                "has_pii": boolean,
                "confidence": float (0.0-1.0),
                "pii_types": ["person_name", "location", "phone_number", "email", "other"],
                "entities": [
                    {{"text": "detected text", "type": "pii_type", "confidence": 0.0-1.0}}
                ]
            }}
            """

            response = await client.chat.completions.create(
                model="gpt-4o-mini",  # Cost-effective for PII detection
                messages=[
                    {
                        "role": "system",
                        "content": "You are a privacy expert specializing in German PII detection for construction industry content.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=300,
                temperature=0.1,
            )

            message = response.choices[0].message
            content = message.content if message else None

            if not content:
                return {"entities": [], "types": set(), "confidence": 0.0}

            result = json.loads(content)

            # Convert string types to PIIType enums
            pii_types = set()
            for type_str in result.get("pii_types", []):
                try:
                    pii_types.add(PIIType(type_str))
                except ValueError:
                    pii_types.add(PIIType.OTHER)

            return {
                "entities": result.get("entities", []),
                "types": pii_types,
                "confidence": result.get("confidence", 0.0),
            }

        except Exception as e:
            logger.error("AI-based PII detection failed", error=str(e))
            return {"entities": [], "types": set(), "confidence": 0.0}

    def _calculate_risk_level(self, pii_types: list[PIIType], confidence: float) -> str:
        """Calculate risk level based on PII types and confidence"""

        if not pii_types or confidence < 0.5:
            return "low"

        high_risk_types = {
            PIIType.PERSON_NAME,
            PIIType.PHONE_NUMBER,
            PIIType.EMAIL,
            PIIType.ADDRESS,
        }
        medium_risk_types = {PIIType.LOCATION, PIIType.COMPANY_NAME}

        detected_types = set(pii_types)

        if detected_types & high_risk_types and confidence >= 0.8:
            return "high"
        elif detected_types & (high_risk_types | medium_risk_types) and confidence >= 0.6:
            return "medium"
        else:
            return "low"

    async def scan_sharepoint_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Scan complete SharePoint metadata for PII

        Args:
            metadata: SharePoint metadata dictionary

        Returns:
            Comprehensive PII scan result
        """

        pii_sensitive_fields = ["Kunde", "OrtohnePLZ", "Beschreibung"]

        field_results = {}
        overall_has_pii = False
        max_confidence = 0.0
        all_pii_types = set()
        high_risk_fields = []

        for field_name in pii_sensitive_fields:
            if field_name in metadata and metadata[field_name]:
                field_value = str(metadata[field_name])

                scan_result = await self.scan_text(field_value, field_name)
                field_results[field_name] = scan_result

                if scan_result["has_pii"]:
                    overall_has_pii = True
                    max_confidence = max(max_confidence, scan_result["confidence"])
                    all_pii_types.update(scan_result["pii_types"])

                    if scan_result["risk_level"] == "high":
                        high_risk_fields.append(field_name)

        return {
            "has_pii": overall_has_pii,
            "confidence": max_confidence,
            "pii_types": list(all_pii_types),
            "risk_level": "high" if high_risk_fields else ("medium" if overall_has_pii else "low"),
            "field_results": field_results,
            "high_risk_fields": high_risk_fields,
            "scanned_fields": pii_sensitive_fields,
        }

    async def should_block_processing(
        self,
        pii_result: dict[str, Any],
        session_id: str | None = None
    ) -> tuple[bool, str]:
        """
        Determine if processing should be blocked based on PII detection

        Logs blocking decision to audit trail.

        Args:
            pii_result: PII detection result
            session_id: Optional session ID for audit trail

        Returns:
            Tuple of (should_block, reason)
        """

        if not pii_result["has_pii"]:
            return False, "No PII detected"

        confidence = pii_result["confidence"]
        risk_level = pii_result["risk_level"]

        # Determine blocking decision
        should_block = False
        reason = ""

        # Block high-risk PII with high confidence
        if risk_level == "high" and confidence >= 0.8:
            should_block = True
            reason = f"High-risk PII detected with {confidence:.2f} confidence"
        # Block medium-risk PII with very high confidence
        elif risk_level == "medium" and confidence >= 0.9:
            should_block = True
            reason = f"Medium-risk PII detected with {confidence:.2f} confidence"
        else:
            should_block = False
            reason = f"PII detected but below blocking threshold (risk: {risk_level}, confidence: {confidence:.2f})"

        # Log blocking decision to audit trail
        if self.enable_audit and self.audit_trail:
            # Get high risk fields if available
            high_risk_fields = pii_result.get("high_risk_fields", [])

            for field_name in high_risk_fields:
                audit_record = PIIAuditRecord(
                    audit_id=str(uuid.uuid4()),
                    session_id=session_id or "unknown",
                    timestamp=datetime.utcnow().isoformat(),
                    action=PIIAuditAction.PII_BLOCKED.value if should_block else PIIAuditAction.PII_ALLOWED.value,
                    field_name=field_name,
                    field_value_hash="",  # Already hashed in scan_text
                    has_pii=True,
                    confidence=confidence,
                    risk_level=risk_level,
                    pii_types=pii_result.get("pii_types", []),
                    blocked=should_block,
                    block_reason=reason if should_block else None,
                    processing_time_ms=0.0
                )

                try:
                    await self.audit_trail.log(audit_record)
                except Exception as e:
                    logger.error("Failed to log PII blocking decision", error=str(e))

        return should_block, reason

    async def health_check(self) -> dict[str, Any]:
        """Health check for PII detector"""

        try:
            # Test with known PII content
            test_texts = [
                "Max Mustermann AG, Zürich 8001",  # Should detect PII
                "Moderne Holzfassade mit Lärche",  # Should not detect PII
                "Tel: 044 123 45 67",  # Should detect phone
                "",  # Empty text
            ]

            results = []
            for i, text in enumerate(test_texts):
                result = await self.scan_text(text, f"test_field_{i}")
                results.append(
                    {"text": text, "has_pii": result["has_pii"], "confidence": result["confidence"]}
                )

            # Check expected results
            expected_pii = [True, False, True, False]
            correct_detections = sum(
                1 for i, result in enumerate(results) if result["has_pii"] == expected_pii[i]
            )

            accuracy = correct_detections / len(test_texts)

            return {
                "status": "healthy" if accuracy >= 0.75 else "degraded",
                "accuracy": accuracy,
                "test_results": results,
                "confidence_threshold": self.confidence_threshold,
            }

        except Exception as e:
            logger.error("PII detector health check failed", error=str(e))
            return {"status": "unhealthy", "error": str(e)}
