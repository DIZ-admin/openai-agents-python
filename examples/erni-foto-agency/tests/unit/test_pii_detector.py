"""Unit tests for PII Detector"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from erni_foto_agency.utils.pii_detector import PIIDetector, PIIType


class TestPIIDetector:
    """Test suite for PII detection functionality"""
    
    @pytest.fixture
    def detector(self):
        """Create PIIDetector instance"""
        return PIIDetector(enable_audit=False)  # Disable audit for unit tests
    
    @pytest.mark.asyncio
    async def test_detect_person_name(self, detector):
        """Test detection of person names"""
        text = "Contact John Smith at the office"
        result = await detector.scan_text(text, "Kunde")
        
        assert result["has_pii"] is True
        assert PIIType.PERSON_NAME in result["pii_types"]
        assert result["confidence"] >= 0.8
        assert result["risk_level"] in ["high", "medium"]
    
    @pytest.mark.asyncio
    async def test_detect_phone_number(self, detector):
        """Test detection of phone numbers"""
        test_cases = [
            "+41 44 123 45 67",
            "044 123 45 67",
            "+41441234567",
            "0041 44 123 45 67",
        ]
        
        for phone in test_cases:
            text = f"Call us at {phone}"
            result = await detector.scan_text(text, "Beschreibung")
            
            assert result["has_pii"] is True, f"Failed to detect phone: {phone}"
            assert PIIType.PHONE_NUMBER in result["pii_types"]
            assert result["confidence"] >= 0.7
    
    @pytest.mark.asyncio
    async def test_detect_email(self, detector):
        """Test detection of email addresses"""
        test_cases = [
            "john.smith@example.com",
            "contact@company.ch",
            "info@test-domain.com",
        ]
        
        for email in test_cases:
            text = f"Send to {email}"
            result = await detector.scan_text(text, "Beschreibung")
            
            assert result["has_pii"] is True, f"Failed to detect email: {email}"
            assert PIIType.EMAIL in result["pii_types"]
            assert result["confidence"] >= 0.9
    
    @pytest.mark.asyncio
    async def test_no_pii_in_safe_text(self, detector):
        """Test that safe text doesn't trigger false positives"""
        safe_texts = [
            "Construction site with wooden beams",
            "Holzbau mit Fassade aus Lärche",
            "Material: Brettschichtholz, Ausführung: sichtbar",
            "Projekt in Zürich, Kategorie: Wohnbau",
        ]
        
        for text in safe_texts:
            result = await detector.scan_text(text, "Beschreibung")
            
            assert result["has_pii"] is False, f"False positive for: {text}"
            assert result["confidence"] < 0.5
    
    @pytest.mark.asyncio
    async def test_blocking_decision_high_risk_high_confidence(self, detector):
        """Test blocking decision for high-risk PII with high confidence"""
        pii_result = {
            "has_pii": True,
            "confidence": 0.9,
            "risk_level": "high",
            "detected_entities": [{"type": "person_name", "text": "John Smith"}]
        }

        should_block, reason = await detector.should_block_processing(pii_result)

        assert should_block is True
        assert "high-risk" in reason.lower() or "high" in reason.lower()

    @pytest.mark.asyncio
    async def test_no_blocking_for_low_confidence(self, detector):
        """Test no blocking for low confidence PII"""
        pii_result = {
            "has_pii": True,
            "confidence": 0.4,
            "risk_level": "high",
            "detected_entities": []
        }

        should_block, reason = await detector.should_block_processing(pii_result)

        assert should_block is False

    @pytest.mark.asyncio
    async def test_no_blocking_when_no_pii(self, detector):
        """Test no blocking when no PII detected"""
        pii_result = {
            "has_pii": False,
            "confidence": 0.0,
            "risk_level": "none",
            "detected_entities": []
        }

        should_block, reason = await detector.should_block_processing(pii_result)

        assert should_block is False
        assert "no pii" in reason.lower()
    
    @pytest.mark.asyncio
    async def test_scan_sharepoint_metadata(self, detector):
        """Test scanning SharePoint metadata for PII"""
        metadata = {
            "Kunde": "ACME Construction AG",
            "OrtohnePLZ": "Zürich",
            "Beschreibung": "Holzbau project with wooden facade",
            "Material": "Brettschichtholz",
        }
        
        result = await detector.scan_sharepoint_metadata(metadata)
        
        assert "has_pii" in result
        assert "confidence" in result
        assert "risk_level" in result
        assert "field_results" in result
    
    @pytest.mark.asyncio
    async def test_pii_in_kunde_field(self, detector):
        """Test PII detection in Kunde field"""
        metadata = {
            "Kunde": "Contact: John Smith, +41 44 123 45 67",
            "Beschreibung": "Regular construction project",
        }
        
        result = await detector.scan_sharepoint_metadata(metadata)
        
        assert result["has_pii"] is True
        assert "Kunde" in result.get("high_risk_fields", [])
    
    @pytest.mark.asyncio
    async def test_rule_based_detection_patterns(self, detector):
        """Test rule-based detection patterns"""
        # Test email pattern
        email_result = await detector._rule_based_detection(
            "contact@example.com", "Beschreibung"
        )
        assert PIIType.EMAIL in email_result["types"]
        
        # Test phone pattern
        phone_result = await detector._rule_based_detection(
            "+41 44 123 45 67", "Beschreibung"
        )
        assert PIIType.PHONE_NUMBER in phone_result["types"]
    
    @pytest.mark.asyncio
    async def test_confidence_threshold_enforcement(self, detector):
        """Test that confidence threshold is properly enforced"""
        # Set custom threshold
        detector.confidence_threshold = 0.85

        pii_result = {
            "has_pii": True,
            "confidence": 0.80,  # Below threshold
            "risk_level": "high",
            "detected_entities": []
        }

        should_block, _ = await detector.should_block_processing(pii_result)

        # Should not block because confidence is below threshold
        assert should_block is False
    
    @pytest.mark.asyncio
    async def test_multiple_pii_types_in_text(self, detector):
        """Test detection of multiple PII types in single text"""
        text = "Contact John Smith at john.smith@example.com or +41 44 123 45 67"
        result = await detector.scan_text(text, "Beschreibung")
        
        assert result["has_pii"] is True
        assert len(result["pii_types"]) >= 2
        assert PIIType.PERSON_NAME in result["pii_types"]
        assert PIIType.EMAIL in result["pii_types"] or PIIType.PHONE_NUMBER in result["pii_types"]
    
    @pytest.mark.asyncio
    async def test_german_text_handling(self, detector):
        """Test PII detection in German text"""
        text = "Kontaktieren Sie Herrn Müller unter mueller@firma.ch"
        result = await detector.scan_text(text, "Kunde")
        
        assert result["has_pii"] is True
        # Should detect at least email
        assert PIIType.EMAIL in result["pii_types"]
    
    @pytest.mark.asyncio
    async def test_empty_text_handling(self, detector):
        """Test handling of empty text"""
        result = await detector.scan_text("", "Beschreibung")
        
        assert result["has_pii"] is False
        assert result["confidence"] == 0.0
    
    @pytest.mark.asyncio
    async def test_none_text_handling(self, detector):
        """Test handling of None text"""
        result = await detector.scan_text(None, "Beschreibung")
        
        assert result["has_pii"] is False
        assert result["confidence"] == 0.0


class TestPIIDetectorWithMockedAI:
    """Test PII detector with mocked AI detection"""
    
    @pytest.mark.asyncio
    async def test_ai_detection_fallback(self):
        """Test that AI detection is used when rule-based confidence is low"""
        detector = PIIDetector()
        
        # Mock AI detection to return high confidence
        with patch.object(detector, '_ai_based_detection', new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = {
                "entities": [{"type": PIIType.PERSON_NAME, "text": "Complex Name"}],
                "types": [PIIType.PERSON_NAME],
                "confidence": 0.9
            }
            
            # Text that might not trigger rule-based detection
            text = "Project manager: Dr. von Müller-Schmidt"
            result = await detector.scan_text(text, "Kunde")
            
            # AI detection should have been called
            mock_ai.assert_called_once()
            assert result["confidence"] >= 0.9
    
    @pytest.mark.asyncio
    async def test_ai_detection_disabled_when_high_rule_confidence(self):
        """Test that AI detection is skipped when rule-based confidence is high"""
        detector = PIIDetector()
        
        with patch.object(detector, '_ai_based_detection', new_callable=AsyncMock) as mock_ai:
            # Clear email pattern - should have high rule-based confidence
            text = "contact@example.com"
            result = await detector.scan_text(text, "Beschreibung")
            
            # AI detection should NOT have been called
            mock_ai.assert_not_called()
            assert result["confidence"] >= detector.confidence_threshold

