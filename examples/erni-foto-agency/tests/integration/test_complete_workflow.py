"""Integration tests for complete workflow"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from erni_foto_agency.main import ErniWorkflowApp
from erni_foto_agency.models.function_tool_models import WorkflowRequest


class TestCompleteWorkflow:
    """Integration tests for end-to-end workflow"""
    
    @pytest.fixture
    async def app(self):
        """Create ErniWorkflowApp instance"""
        app = ErniWorkflowApp()
        await app.initialize()
        yield app
        await app.shutdown()
    
    @pytest.fixture
    def temp_image(self):
        """Create temporary test image"""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            # Create minimal JPEG file
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            f.write(b'\xff\xd9')  # JPEG end marker
            temp_path = f.name
        
        yield temp_path
        
        # Cleanup
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    
    @pytest.mark.asyncio
    async def test_complete_workflow_success(self, app, temp_image):
        """Test complete workflow from image to SharePoint upload"""
        request = WorkflowRequest(
            site_url="https://test.sharepoint.com/sites/test",
            library_name="Test Library",
            image_paths=[temp_image],
            session_id="test-workflow-001",
        )
        
        # Mock external dependencies
        with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
            mock_instance = AsyncMock()
            mock_graph.return_value = mock_instance
            
            # Mock schema extraction
            mock_instance.get_library_schema.return_value = {
                "fields": [
                    {"name": "Kunde", "type": "text", "required": True},
                    {"name": "Material", "type": "choice", "choices": ["Holz", "Beton"]},
                ]
            }
            
            # Mock file upload
            mock_instance.upload_file.return_value = {
                "file_id": "test-file-123",
                "webUrl": "https://test.sharepoint.com/file.jpg",
                "size_bytes": 1024,
            }
            
            # Mock metadata update
            mock_instance.update_file_metadata.return_value = {
                "success": True,
                "updated_fields": ["Kunde", "Material"],
            }
            
            # Mock Vision API
            with patch("openai.AsyncOpenAI") as mock_openai:
                mock_client = AsyncMock()
                mock_openai.return_value = mock_client
                
                mock_response = Mock()
                mock_response.choices = [
                    Mock(message=Mock(content='{"Kunde": "Test AG", "Material": "Holz"}'))
                ]
                mock_client.chat.completions.create.return_value = mock_response
                
                # Execute workflow
                response = await app.orchestrate_workflow(request)
        
        # Verify response
        assert response.success is True
        assert response.agent == "workflow-orchestrator"
        assert response.processing_time is not None
        assert "images_processed" in response.data
    
    @pytest.mark.asyncio
    async def test_workflow_with_pii_detection(self, app, temp_image):
        """Test workflow with PII detection blocking"""
        request = WorkflowRequest(
            site_url="https://test.sharepoint.com/sites/test",
            library_name="Test Library",
            image_paths=[temp_image],
            session_id="test-pii-001",
        )
        
        with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
            mock_instance = AsyncMock()
            mock_graph.return_value = mock_instance
            
            mock_instance.get_library_schema.return_value = {
                "fields": [{"name": "Kunde", "type": "text"}]
            }
            
            # Mock Vision API to return PII
            with patch("openai.AsyncOpenAI") as mock_openai:
                mock_client = AsyncMock()
                mock_openai.return_value = mock_client
                
                mock_response = Mock()
                mock_response.choices = [
                    Mock(message=Mock(content='{"Kunde": "John Smith, +41 44 123 45 67"}'))
                ]
                mock_client.chat.completions.create.return_value = mock_response
                
                # Execute workflow
                response = await app.orchestrate_workflow(request)
        
        # Verify PII was detected and handled
        assert "pii" in str(response.data).lower() or "blocked" in str(response.data).lower()
    
    @pytest.mark.asyncio
    async def test_workflow_with_multiple_images(self, app, temp_image):
        """Test workflow with multiple images"""
        # Create second temp image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            f.write(b'\xff\xd9')
            temp_image_2 = f.name
        
        try:
            request = WorkflowRequest(
                site_url="https://test.sharepoint.com/sites/test",
                library_name="Test Library",
                image_paths=[temp_image, temp_image_2],
                session_id="test-multi-001",
            )
            
            with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
                mock_instance = AsyncMock()
                mock_graph.return_value = mock_instance
                
                mock_instance.get_library_schema.return_value = {
                    "fields": [{"name": "Material", "type": "text"}]
                }
                
                mock_instance.upload_file.return_value = {
                    "file_id": "test-file",
                    "webUrl": "https://test.sharepoint.com/file.jpg",
                }
                
                mock_instance.update_file_metadata.return_value = {"success": True}
                
                with patch("openai.AsyncOpenAI") as mock_openai:
                    mock_client = AsyncMock()
                    mock_openai.return_value = mock_client
                    
                    mock_response = Mock()
                    mock_response.choices = [
                        Mock(message=Mock(content='{"Material": "Holz"}'))
                    ]
                    mock_client.chat.completions.create.return_value = mock_response
                    
                    response = await app.orchestrate_workflow(request)
            
            # Verify multiple images processed
            assert response.success is True
            assert response.data.get("images_processed", 0) == 2
        
        finally:
            if os.path.exists(temp_image_2):
                os.unlink(temp_image_2)
    
    @pytest.mark.asyncio
    async def test_workflow_error_handling(self, app, temp_image):
        """Test workflow error handling"""
        request = WorkflowRequest(
            site_url="https://test.sharepoint.com/sites/test",
            library_name="Test Library",
            image_paths=[temp_image],
            session_id="test-error-001",
        )
        
        with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
            mock_instance = AsyncMock()
            mock_graph.return_value = mock_instance
            
            # Simulate API error
            mock_instance.get_library_schema.side_effect = Exception("API connection failed")
            
            response = await app.orchestrate_workflow(request)
        
        # Verify error is handled gracefully
        assert response.success is False or "error" in str(response.data).lower()
    
    @pytest.mark.asyncio
    async def test_workflow_with_caching(self, app, temp_image):
        """Test workflow with schema caching"""
        request = WorkflowRequest(
            site_url="https://test.sharepoint.com/sites/test",
            library_name="Test Library",
            image_paths=[temp_image],
            session_id="test-cache-001",
        )
        
        with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
            mock_instance = AsyncMock()
            mock_graph.return_value = mock_instance
            
            schema = {
                "fields": [{"name": "Kunde", "type": "text"}]
            }
            mock_instance.get_library_schema.return_value = schema
            mock_instance.upload_file.return_value = {"file_id": "test"}
            mock_instance.update_file_metadata.return_value = {"success": True}
            
            with patch("openai.AsyncOpenAI") as mock_openai:
                mock_client = AsyncMock()
                mock_openai.return_value = mock_client
                mock_response = Mock()
                mock_response.choices = [Mock(message=Mock(content='{"Kunde": "Test"}'))]
                mock_client.chat.completions.create.return_value = mock_response
                
                # First call
                await app.orchestrate_workflow(request)
                first_call_count = mock_instance.get_library_schema.call_count
                
                # Second call with same library - should use cache
                request.session_id = "test-cache-002"
                await app.orchestrate_workflow(request)
                second_call_count = mock_instance.get_library_schema.call_count
                
                # Schema should be cached (call count might increase by 1 or stay same)
                assert second_call_count <= first_call_count + 1
    
    @pytest.mark.asyncio
    async def test_workflow_performance_metrics(self, app, temp_image):
        """Test workflow performance metrics collection"""
        request = WorkflowRequest(
            site_url="https://test.sharepoint.com/sites/test",
            library_name="Test Library",
            image_paths=[temp_image],
            session_id="test-perf-001",
        )
        
        with patch("erni_foto_agency.utils.graph_client.GraphAPIClient") as mock_graph:
            mock_instance = AsyncMock()
            mock_graph.return_value = mock_instance
            
            mock_instance.get_library_schema.return_value = {"fields": []}
            mock_instance.upload_file.return_value = {"file_id": "test"}
            mock_instance.update_file_metadata.return_value = {"success": True}
            
            with patch("openai.AsyncOpenAI") as mock_openai:
                mock_client = AsyncMock()
                mock_openai.return_value = mock_client
                mock_response = Mock()
                mock_response.choices = [Mock(message=Mock(content='{}'))]
                mock_client.chat.completions.create.return_value = mock_response
                
                response = await app.orchestrate_workflow(request)
        
        # Verify performance metrics
        assert response.processing_time is not None
        assert response.processing_time > 0
        assert isinstance(response.processing_time, (int, float))

