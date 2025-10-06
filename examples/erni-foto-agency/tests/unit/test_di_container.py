"""Unit tests for Dependency Injection Container"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from erni_foto_agency.di_container import (
    DIContainer,
    get_container,
    set_container,
    reset_container,
)
from erni_foto_agency.config.settings import ErniConfig


class TestDIContainer:
    """Test suite for DI Container"""
    
    @pytest.fixture
    def container(self):
        """Create fresh DI container for each test"""
        container = DIContainer()
        yield container
        # Cleanup
        container.reset()
    
    def test_container_creation(self, container):
        """Test that container is created successfully"""
        assert container is not None
        assert container.config is not None
        assert container._initialized is False
    
    def test_lazy_initialization_cache_manager(self, container):
        """Test that cache manager is lazily initialized"""
        # First access creates instance
        cache1 = container.cache_manager
        assert cache1 is not None
        
        # Second access returns same instance (singleton)
        cache2 = container.cache_manager
        assert cache1 is cache2
    
    def test_lazy_initialization_circuit_breaker(self, container):
        """Test that circuit breaker is lazily initialized"""
        breaker1 = container.circuit_breaker
        assert breaker1 is not None
        
        breaker2 = container.circuit_breaker
        assert breaker1 is breaker2
    
    def test_lazy_initialization_agents(self, container):
        """Test that agents are lazily initialized"""
        # Schema extractor
        schema1 = container.schema_extractor
        assert schema1 is not None
        schema2 = container.schema_extractor
        assert schema1 is schema2
        
        # Vision analyzer
        vision1 = container.vision_analyzer
        assert vision1 is not None
        vision2 = container.vision_analyzer
        assert vision1 is vision2
    
    def test_workflow_orchestrator_dependencies(self, container):
        """Test that workflow orchestrator gets correct dependencies"""
        orchestrator = container.workflow_orchestrator
        assert orchestrator is not None
        
        # Verify dependencies are injected
        # (orchestrator should have references to other agents)
        assert container.schema_extractor is not None
        assert container.vision_analyzer is not None
        assert container.sharepoint_uploader is not None
        assert container.validation_reporter is not None
    
    @pytest.mark.asyncio
    async def test_initialize(self, container):
        """Test container initialization"""
        # Mock cache manager and metrics collector
        mock_cache = AsyncMock()
        mock_metrics = AsyncMock()
        
        container._cache_manager = mock_cache
        container._metrics_collector = mock_metrics
        
        await container.initialize()
        
        # Verify initialization was called
        mock_cache.initialize.assert_called_once()
        mock_metrics.start_background_tasks.assert_called_once()
        assert container._initialized is True
    
    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, container):
        """Test that initialize can be called multiple times safely"""
        mock_cache = AsyncMock()
        container._cache_manager = mock_cache
        
        await container.initialize()
        await container.initialize()  # Second call
        
        # Should only initialize once
        assert mock_cache.initialize.call_count == 1
    
    @pytest.mark.asyncio
    async def test_shutdown(self, container):
        """Test container shutdown"""
        # Initialize first
        mock_cache = AsyncMock()
        mock_metrics = AsyncMock()
        
        container._cache_manager = mock_cache
        container._metrics_collector = mock_metrics
        
        await container.initialize()
        await container.shutdown()
        
        # Verify shutdown was called
        mock_metrics.stop_background_tasks.assert_called_once()
        mock_cache.close.assert_called_once()
        assert container._initialized is False
    
    @pytest.mark.asyncio
    async def test_shutdown_without_initialize(self, container):
        """Test that shutdown handles uninitialized state"""
        # Should not raise error
        await container.shutdown()
        assert container._initialized is False
    
    def test_reset(self, container):
        """Test container reset"""
        # Access some components to create them
        _ = container.cache_manager
        _ = container.circuit_breaker
        _ = container.schema_extractor
        
        assert container._cache_manager is not None
        assert container._circuit_breaker is not None
        assert container._schema_extractor is not None
        
        # Reset
        container.reset()
        
        # All should be None
        assert container._cache_manager is None
        assert container._circuit_breaker is None
        assert container._schema_extractor is None
        assert container._initialized is False
    
    def test_setter_for_testing(self, container):
        """Test that setters work for dependency injection in tests"""
        mock_cache = Mock()
        container.cache_manager = mock_cache
        
        # Should return mock
        assert container.cache_manager is mock_cache
    
    def test_custom_config(self):
        """Test container with custom configuration"""
        # ErniConfig requires openai, microsoft_graph, and sharepoint configs
        # For testing, we can use get_config() which provides defaults
        from erni_foto_agency.config.settings import get_config
        custom_config = get_config()
        container = DIContainer(config=custom_config)

        assert container.config is custom_config


class TestGlobalContainer:
    """Test global container functions"""
    
    def teardown_method(self):
        """Reset global container after each test"""
        reset_container()
    
    def test_get_container_singleton(self):
        """Test that get_container returns singleton"""
        container1 = get_container()
        container2 = get_container()
        
        assert container1 is container2
    
    def test_set_container(self):
        """Test setting custom container"""
        custom_container = DIContainer()
        set_container(custom_container)
        
        retrieved = get_container()
        assert retrieved is custom_container
    
    def test_reset_container(self):
        """Test resetting global container"""
        container1 = get_container()
        reset_container()
        container2 = get_container()
        
        # Should be different instances
        assert container1 is not container2


class TestDIContainerIntegration:
    """Integration tests for DI container"""
    
    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Test complete container lifecycle"""
        container = DIContainer()
        
        # Initialize
        await container.initialize()
        assert container._initialized is True
        
        # Access components
        cache = container.cache_manager
        breaker = container.circuit_breaker
        orchestrator = container.workflow_orchestrator
        
        assert cache is not None
        assert breaker is not None
        assert orchestrator is not None
        
        # Shutdown
        await container.shutdown()
        assert container._initialized is False
    
    @pytest.mark.asyncio
    async def test_agent_dependencies_resolved(self):
        """Test that all agent dependencies are properly resolved"""
        container = DIContainer()
        
        # Access orchestrator (which depends on other agents)
        orchestrator = container.workflow_orchestrator
        
        # Verify all dependencies exist
        assert container.schema_extractor is not None
        assert container.vision_analyzer is not None
        assert container.sharepoint_uploader is not None
        assert container.validation_reporter is not None
        
        # Orchestrator should be created successfully
        assert orchestrator is not None
    
    def test_all_components_accessible(self):
        """Test that all components can be accessed"""
        container = DIContainer()
        
        # Infrastructure
        assert container.cache_manager is not None
        assert container.circuit_breaker is not None
        assert container.metrics_collector is not None
        assert container.health_checker is not None
        assert container.image_processor is not None
        assert container.batch_processor is not None
        assert container.cost_optimizer is not None
        
        # Agents
        assert container.schema_extractor is not None
        assert container.vision_analyzer is not None
        assert container.sharepoint_uploader is not None
        assert container.validation_reporter is not None
        assert container.workflow_orchestrator is not None


class TestDIContainerForTesting:
    """Test DI container features for testing"""
    
    def test_mock_injection(self):
        """Test injecting mocks for testing"""
        container = DIContainer()
        
        # Create mocks
        mock_cache = Mock()
        mock_breaker = Mock()
        
        # Inject mocks
        container.cache_manager = mock_cache
        container.circuit_breaker = mock_breaker
        
        # Verify mocks are used
        assert container.cache_manager is mock_cache
        assert container.circuit_breaker is mock_breaker
    
    @pytest.mark.asyncio
    async def test_isolated_test_container(self):
        """Test creating isolated container for tests"""
        # Create test container
        test_container = DIContainer()
        
        # Mock components
        test_container.cache_manager = AsyncMock()
        test_container.metrics_collector = AsyncMock()
        
        # Initialize
        await test_container.initialize()
        
        # Verify mocks were called
        test_container.cache_manager.initialize.assert_called_once()
        test_container.metrics_collector.start_background_tasks.assert_called_once()
        
        # Cleanup
        await test_container.shutdown()

