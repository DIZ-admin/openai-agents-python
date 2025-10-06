"""
Dependency Injection Container for Erni-Foto Agency

Provides centralized dependency management with:
- Interface-based design for testability
- Lazy initialization for performance
- Singleton pattern for shared resources
- Easy mocking for unit tests
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

import structlog

from .config.settings import ErniConfig, get_config
from .erni_agents.orchestrator import ErniWorkflowOrchestratorAgent, create_workflow_orchestrator_agent
from .erni_agents.sharepoint_schema_extractor import SharePointSchemaExtractorAgent
from .erni_agents.sharepoint_uploader import SharePointUploaderAgent
from .erni_agents.structured_vision_analyzer import StructuredVisionAnalyzerAgent
from .erni_agents.validation_report import ValidationReportAgent
from .health.health_checker import HealthChecker, get_health_checker
from .monitoring.metrics_collector import ErniMetricsCollector
from .performance.batch_processor import BatchProcessor
from .performance.cache_manager import ErniCacheManager
from .performance.circuit_breaker import CircuitBreaker
from .performance.cost_optimizer import CostBudget, CostOptimizer
from .performance.rate_limiter import RateLimiter, get_rate_limiter
from .utils.image_processor import ImageProcessor

logger = structlog.get_logger(__name__)


# ============================================================================
# Protocols (Interfaces)
# ============================================================================


class ICacheManager(Protocol):
    """Interface for cache management"""
    
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...


class ICircuitBreaker(Protocol):
    """Interface for circuit breaker"""
    
    async def call(self, func: Any, *args: Any, **kwargs: Any) -> Any: ...
    async def reset(self) -> None: ...


class IMetricsCollector(Protocol):
    """Interface for metrics collection"""
    
    async def start_background_tasks(self) -> None: ...
    async def stop_background_tasks(self) -> None: ...
    def record_request(self, agent: str, duration: float, success: bool) -> None: ...


class IImageProcessor(Protocol):
    """Interface for image processing"""

    async def process_image(self, image_path: str) -> bytes: ...
    async def validate_image(self, image_path: str) -> bool: ...


class IRateLimiter(Protocol):
    """Interface for rate limiting"""

    async def acquire(self, estimated_tokens: int) -> Any: ...
    def get_stats(self) -> dict[str, Any]: ...


# ============================================================================
# Dependency Injection Container
# ============================================================================


class DIContainer:
    """
    Dependency Injection Container
    
    Manages lifecycle and dependencies of all application components.
    Uses lazy initialization for performance and singleton pattern for shared resources.
    
    Benefits:
    - Centralized dependency management
    - Easy testing with mock implementations
    - Clear dependency graph
    - Reduced coupling between components
    
    Example:
        # Production usage
        container = DIContainer()
        await container.initialize()
        
        # Testing usage
        container = DIContainer()
        container.cache_manager = MockCacheManager()
        await container.initialize()
    """
    
    def __init__(self, config: ErniConfig | None = None):
        """
        Initialize DI container
        
        Args:
            config: Optional configuration. If None, loads from environment.
        """
        self._config = config or get_config()
        
        # Lazy-initialized components
        self._cache_manager: ErniCacheManager | None = None
        self._circuit_breaker: CircuitBreaker | None = None
        self._metrics_collector: ErniMetricsCollector | None = None
        self._health_checker: HealthChecker | None = None
        self._image_processor: ImageProcessor | None = None
        self._batch_processor: BatchProcessor | None = None
        self._cost_optimizer: CostOptimizer | None = None
        self._rate_limiter_gpt4o: RateLimiter | None = None
        self._rate_limiter_gpt4o_mini: RateLimiter | None = None
        
        # Agents (lazy-initialized)
        self._schema_extractor: SharePointSchemaExtractorAgent | None = None
        self._vision_analyzer: StructuredVisionAnalyzerAgent | None = None
        self._sharepoint_uploader: SharePointUploaderAgent | None = None
        self._validation_reporter: ValidationReportAgent | None = None
        self._workflow_orchestrator: ErniWorkflowOrchestratorAgent | None = None
        
        self._initialized = False
        
        logger.info("DI Container created")
    
    # ========================================================================
    # Configuration
    # ========================================================================
    
    @property
    def config(self) -> ErniConfig:
        """Get application configuration"""
        return self._config
    
    # ========================================================================
    # Infrastructure Components
    # ========================================================================
    
    @property
    def cache_manager(self) -> ErniCacheManager:
        """Get cache manager (singleton)"""
        if self._cache_manager is None:
            self._cache_manager = ErniCacheManager(
                redis_url=self._config.cache.redis_url
            )
            logger.debug("Cache manager created")
        return self._cache_manager
    
    @cache_manager.setter
    def cache_manager(self, value: ErniCacheManager) -> None:
        """Set cache manager (for testing)"""
        self._cache_manager = value
    
    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Get circuit breaker (singleton)"""
        if self._circuit_breaker is None:
            self._circuit_breaker = CircuitBreaker(name="erni_circuit_breaker")
            logger.debug("Circuit breaker created")
        return self._circuit_breaker
    
    @circuit_breaker.setter
    def circuit_breaker(self, value: CircuitBreaker) -> None:
        """Set circuit breaker (for testing)"""
        self._circuit_breaker = value
    
    @property
    def metrics_collector(self) -> ErniMetricsCollector:
        """Get metrics collector (singleton)"""
        if self._metrics_collector is None:
            self._metrics_collector = ErniMetricsCollector(
                prometheus_port=self._config.monitoring.prometheus_port
            )
            logger.debug("Metrics collector created")
        return self._metrics_collector
    
    @metrics_collector.setter
    def metrics_collector(self, value: ErniMetricsCollector) -> None:
        """Set metrics collector (for testing)"""
        self._metrics_collector = value
    
    @property
    def health_checker(self) -> HealthChecker:
        """Get health checker (singleton)"""
        if self._health_checker is None:
            self._health_checker = get_health_checker()
            logger.debug("Health checker created")
        return self._health_checker
    
    @health_checker.setter
    def health_checker(self, value: HealthChecker) -> None:
        """Set health checker (for testing)"""
        self._health_checker = value
    
    @property
    def image_processor(self) -> ImageProcessor:
        """Get image processor (singleton)"""
        if self._image_processor is None:
            self._image_processor = ImageProcessor()
            logger.debug("Image processor created")
        return self._image_processor
    
    @image_processor.setter
    def image_processor(self, value: ImageProcessor) -> None:
        """Set image processor (for testing)"""
        self._image_processor = value
    
    @property
    def batch_processor(self) -> BatchProcessor:
        """Get batch processor (singleton)"""
        if self._batch_processor is None:
            self._batch_processor = BatchProcessor()
            logger.debug("Batch processor created")
        return self._batch_processor
    
    @batch_processor.setter
    def batch_processor(self, value: BatchProcessor) -> None:
        """Set batch processor (for testing)"""
        self._batch_processor = value
    
    @property
    def cost_optimizer(self) -> CostOptimizer:
        """Get cost optimizer (singleton)"""
        if self._cost_optimizer is None:
            default_budget = CostBudget(
                hourly_budget_usd=10.0,
                daily_budget_usd=100.0,
                cost_per_image_target_usd=0.05
            )
            self._cost_optimizer = CostOptimizer(budget=default_budget)
            logger.debug("Cost optimizer created")
        return self._cost_optimizer
    
    @cost_optimizer.setter
    def cost_optimizer(self, value: CostOptimizer) -> None:
        """Set cost optimizer (for testing)"""
        self._cost_optimizer = value

    @property
    def rate_limiter_gpt4o(self) -> RateLimiter:
        """Get rate limiter for GPT-4o (singleton)"""
        if self._rate_limiter_gpt4o is None:
            self._rate_limiter_gpt4o = get_rate_limiter("gpt-4o")
            logger.debug("Rate limiter for gpt-4o created")
        return self._rate_limiter_gpt4o

    @rate_limiter_gpt4o.setter
    def rate_limiter_gpt4o(self, value: RateLimiter) -> None:
        """Set rate limiter for GPT-4o (for testing)"""
        self._rate_limiter_gpt4o = value

    @property
    def rate_limiter_gpt4o_mini(self) -> RateLimiter:
        """Get rate limiter for GPT-4o-mini (singleton)"""
        if self._rate_limiter_gpt4o_mini is None:
            self._rate_limiter_gpt4o_mini = get_rate_limiter("gpt-4o-mini")
            logger.debug("Rate limiter for gpt-4o-mini created")
        return self._rate_limiter_gpt4o_mini

    @rate_limiter_gpt4o_mini.setter
    def rate_limiter_gpt4o_mini(self, value: RateLimiter) -> None:
        """Set rate limiter for GPT-4o-mini (for testing)"""
        self._rate_limiter_gpt4o_mini = value

    # ========================================================================
    # AI Agents
    # ========================================================================

    @property
    def schema_extractor(self) -> SharePointSchemaExtractorAgent:
        """Get SharePoint schema extractor agent"""
        if self._schema_extractor is None:
            self._schema_extractor = SharePointSchemaExtractorAgent()
            logger.debug("Schema extractor agent created")
        return self._schema_extractor

    @schema_extractor.setter
    def schema_extractor(self, value: SharePointSchemaExtractorAgent) -> None:
        """Set schema extractor (for testing)"""
        self._schema_extractor = value

    @property
    def vision_analyzer(self) -> StructuredVisionAnalyzerAgent:
        """Get vision analyzer agent"""
        if self._vision_analyzer is None:
            self._vision_analyzer = StructuredVisionAnalyzerAgent()
            logger.debug("Vision analyzer agent created")
        return self._vision_analyzer

    @vision_analyzer.setter
    def vision_analyzer(self, value: StructuredVisionAnalyzerAgent) -> None:
        """Set vision analyzer (for testing)"""
        self._vision_analyzer = value

    @property
    def sharepoint_uploader(self) -> SharePointUploaderAgent:
        """Get SharePoint uploader agent"""
        if self._sharepoint_uploader is None:
            self._sharepoint_uploader = SharePointUploaderAgent()
            logger.debug("SharePoint uploader agent created")
        return self._sharepoint_uploader

    @sharepoint_uploader.setter
    def sharepoint_uploader(self, value: SharePointUploaderAgent) -> None:
        """Set SharePoint uploader (for testing)"""
        self._sharepoint_uploader = value

    @property
    def validation_reporter(self) -> ValidationReportAgent:
        """Get validation reporter agent"""
        if self._validation_reporter is None:
            self._validation_reporter = ValidationReportAgent()
            logger.debug("Validation reporter agent created")
        return self._validation_reporter

    @validation_reporter.setter
    def validation_reporter(self, value: ValidationReportAgent) -> None:
        """Set validation reporter (for testing)"""
        self._validation_reporter = value

    @property
    def workflow_orchestrator(self) -> ErniWorkflowOrchestratorAgent:
        """Get workflow orchestrator agent"""
        if self._workflow_orchestrator is None:
            self._workflow_orchestrator = create_workflow_orchestrator_agent(
                self.schema_extractor,
                self.vision_analyzer,
                self.sharepoint_uploader,
                self.validation_reporter,
            )
            logger.debug("Workflow orchestrator agent created")
        return self._workflow_orchestrator

    @workflow_orchestrator.setter
    def workflow_orchestrator(self, value: ErniWorkflowOrchestratorAgent) -> None:
        """Set workflow orchestrator (for testing)"""
        self._workflow_orchestrator = value

    # ========================================================================
    # Lifecycle Management
    # ========================================================================

    async def initialize(self) -> None:
        """
        Initialize all components that require async setup

        This method should be called once at application startup.
        It initializes components in the correct order to handle dependencies.
        """
        if self._initialized:
            logger.warning("DI Container already initialized")
            return

        logger.info("Initializing DI Container components...")

        # Initialize cache manager
        await self.cache_manager.initialize()

        # Initialize metrics collector
        await self.metrics_collector.start_background_tasks()

        self._initialized = True
        logger.info("DI Container initialized successfully")

    async def shutdown(self) -> None:
        """
        Shutdown all components gracefully

        This method should be called at application shutdown.
        It ensures all resources are properly released.
        """
        if not self._initialized:
            logger.warning("DI Container not initialized, skipping shutdown")
            return

        logger.info("Shutting down DI Container components...")

        # Stop metrics collector (if it has stop method)
        if self._metrics_collector:
            if hasattr(self._metrics_collector, 'stop_background_tasks'):
                await self._metrics_collector.stop_background_tasks()
            elif hasattr(self._metrics_collector, 'stop'):
                await self._metrics_collector.stop()
            else:
                logger.debug("Metrics collector has no stop method")

        # Close cache manager
        if self._cache_manager:
            await self._cache_manager.close()

        self._initialized = False
        logger.info("DI Container shutdown complete")

    def reset(self) -> None:
        """
        Reset all components (for testing)

        This method clears all cached instances, forcing recreation
        on next access. Useful for test isolation.
        """
        self._cache_manager = None
        self._circuit_breaker = None
        self._metrics_collector = None
        self._health_checker = None
        self._image_processor = None
        self._batch_processor = None
        self._cost_optimizer = None
        self._rate_limiter_gpt4o = None
        self._rate_limiter_gpt4o_mini = None
        self._schema_extractor = None
        self._vision_analyzer = None
        self._sharepoint_uploader = None
        self._validation_reporter = None
        self._workflow_orchestrator = None
        self._initialized = False

        logger.debug("DI Container reset")


# ============================================================================
# Global Container Instance
# ============================================================================

_global_container: DIContainer | None = None


def get_container() -> DIContainer:
    """
    Get global DI container instance (singleton)

    Returns:
        Global DIContainer instance

    Example:
        container = get_container()
        cache = container.cache_manager
    """
    global _global_container
    if _global_container is None:
        _global_container = DIContainer()
    return _global_container


def set_container(container: DIContainer) -> None:
    """
    Set global DI container (for testing)

    Args:
        container: DIContainer instance to use globally

    Example:
        # In tests
        test_container = DIContainer()
        test_container.cache_manager = MockCacheManager()
        set_container(test_container)
    """
    global _global_container
    _global_container = container


def reset_container() -> None:
    """
    Reset global DI container (for testing)

    Forces recreation of container on next get_container() call.
    """
    global _global_container
    _global_container = None


