"""Unit tests for Circuit Breaker"""

import pytest
import asyncio
from unittest.mock import AsyncMock

from erni_foto_agency.performance.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerError,
    CircuitBreakerConfig,
)


class TestCircuitBreaker:
    """Test suite for Circuit Breaker pattern implementation"""
    
    @pytest.fixture
    def circuit_breaker(self):
        """Create CircuitBreaker instance with test configuration"""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            timeout_seconds=2,  # Short timeout for tests
            max_timeout_seconds=10,
        )
        return CircuitBreaker(name="test_breaker", config=config)
    
    @pytest.mark.asyncio
    async def test_successful_calls_keep_circuit_closed(self, circuit_breaker):
        """Test that successful calls keep circuit closed"""
        async def success_func():
            return "success"
        
        # Execute multiple successful calls
        for i in range(10):
            result = await circuit_breaker.call(success_func)
            assert result == "success"
            assert circuit_breaker.state == CircuitState.CLOSED
        
        # Verify metrics
        assert circuit_breaker.failure_count == 0
        assert circuit_breaker.total_requests == 10
        assert circuit_breaker.total_successes == 10
    
    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold_failures(self, circuit_breaker):
        """Test that circuit opens after threshold failures"""
        async def failing_func():
            raise Exception("Test failure")
        
        # First 3 failures should open circuit
        for i in range(3):
            with pytest.raises(Exception, match="Test failure"):
                await circuit_breaker.call(failing_func)
        
        assert circuit_breaker.state == CircuitState.OPEN
        assert circuit_breaker.failure_count == 3
        
        # 4th call should fail fast with CircuitBreakerError
        with pytest.raises(CircuitBreakerError, match="is OPEN"):
            await circuit_breaker.call(failing_func)
        
        # Verify fail-fast didn't execute the function
        assert circuit_breaker.total_requests == 4
        assert circuit_breaker.total_failures == 4
    
    @pytest.mark.asyncio
    async def test_circuit_transitions_to_half_open(self, circuit_breaker):
        """Test circuit transitions to half-open after timeout"""
        async def failing_func():
            raise Exception("Test failure")

        async def success_func():
            return "success"

        # Open the circuit
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)

        assert circuit_breaker.state == CircuitState.OPEN

        # Circuit breaker doubles timeout on open, so wait for doubled timeout
        # Initial: 2s, after open: 4s
        await asyncio.sleep(4.5)

        # Next call should transition to half-open and succeed
        result = await circuit_breaker.call(success_func)
        assert result == "success"
        # After one successful call in half-open, it stays half-open
        # Need multiple successes to close
        assert circuit_breaker.state == CircuitState.HALF_OPEN

        # Make more successful calls to close circuit
        for _ in range(3):
            result = await circuit_breaker.call(success_func)
            assert result == "success"

        # Now circuit should be closed
        assert circuit_breaker.state == CircuitState.CLOSED
    
    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self, circuit_breaker):
        """Test that failure in half-open state reopens circuit"""
        async def failing_func():
            raise Exception("Test failure")
        
        # Open the circuit
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)
        
        assert circuit_breaker.state == CircuitState.OPEN
        
        # Wait for timeout
        await asyncio.sleep(2.5)
        
        # Next call transitions to half-open but fails
        with pytest.raises(Exception):
            await circuit_breaker.call(failing_func)
        
        # Circuit should be open again
        assert circuit_breaker.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_exponential_backoff_timeout(self, circuit_breaker):
        """Test exponential backoff for timeout"""
        async def failing_func():
            raise Exception("Test failure")

        # Record initial timeout
        initial_timeout = circuit_breaker.config.timeout_seconds
        assert initial_timeout == 2

        # Open circuit first time - timeout doubles to 4s
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)

        first_open_timeout = circuit_breaker.current_timeout
        assert first_open_timeout == 4  # Doubled from 2s

        # Wait and fail again in half-open - timeout doubles again to 8s
        await asyncio.sleep(4.5)
        with pytest.raises(Exception):
            await circuit_breaker.call(failing_func)

        # Timeout should have doubled again
        assert circuit_breaker.current_timeout == 8
    
    @pytest.mark.asyncio
    async def test_max_timeout_cap(self, circuit_breaker):
        """Test that timeout is capped at max_timeout_seconds"""
        async def failing_func():
            raise Exception("Test failure")
        
        # Manually set current timeout near max
        circuit_breaker.current_timeout = 8
        
        # Open circuit
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)
        
        # Timeout should be capped at max (10 seconds)
        assert circuit_breaker.current_timeout <= 10
    
    @pytest.mark.asyncio
    async def test_concurrent_calls_in_closed_state(self, circuit_breaker):
        """Test concurrent calls when circuit is closed"""
        async def slow_success():
            await asyncio.sleep(0.1)
            return "success"
        
        # Execute concurrent calls
        tasks = [circuit_breaker.call(slow_success) for _ in range(5)]
        results = await asyncio.gather(*tasks)
        
        assert all(r == "success" for r in results)
        assert circuit_breaker.total_successes == 5
    
    @pytest.mark.asyncio
    async def test_reset_circuit_breaker(self, circuit_breaker):
        """Test manual reset of circuit breaker"""
        async def failing_func():
            raise Exception("Test failure")
        
        # Open the circuit
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(failing_func)
        
        assert circuit_breaker.state == CircuitState.OPEN
        
        # Reset circuit breaker
        await circuit_breaker.reset()
        
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.failure_count == 0
        assert circuit_breaker.current_timeout == circuit_breaker.config.timeout_seconds
    
    @pytest.mark.asyncio
    async def test_get_metrics(self, circuit_breaker):
        """Test getting circuit breaker metrics"""
        async def success_func():
            return "success"

        async def failing_func():
            raise Exception("Test failure")

        # Execute some calls
        await circuit_breaker.call(success_func)
        await circuit_breaker.call(success_func)

        try:
            await circuit_breaker.call(failing_func)
        except Exception:
            pass

        # Verify metrics via attributes (no get_metrics method)
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.total_requests == 3
        assert circuit_breaker.total_successes == 2
        assert circuit_breaker.total_failures == 1
        assert circuit_breaker.failure_count == 1

        # Calculate success rate
        success_rate = circuit_breaker.total_successes / circuit_breaker.total_requests
        assert success_rate == pytest.approx(0.666, rel=0.01)
    
    @pytest.mark.asyncio
    async def test_different_exception_types(self, circuit_breaker):
        """Test circuit breaker with different exception types"""
        async def value_error_func():
            raise ValueError("Value error")
        
        async def runtime_error_func():
            raise RuntimeError("Runtime error")
        
        # Both should count as failures
        with pytest.raises(ValueError):
            await circuit_breaker.call(value_error_func)
        
        with pytest.raises(RuntimeError):
            await circuit_breaker.call(runtime_error_func)
        
        assert circuit_breaker.failure_count == 2
    
    @pytest.mark.asyncio
    async def test_async_function_with_return_value(self, circuit_breaker):
        """Test circuit breaker with async function returning complex value"""
        async def complex_func():
            await asyncio.sleep(0.05)
            return {"status": "ok", "data": [1, 2, 3]}
        
        result = await circuit_breaker.call(complex_func)
        
        assert result == {"status": "ok", "data": [1, 2, 3]}
        assert circuit_breaker.total_successes == 1


class TestCircuitBreakerConfig:
    """Test CircuitBreakerConfig"""
    
    def test_default_config(self):
        """Test default configuration values"""
        config = CircuitBreakerConfig()
        
        assert config.failure_threshold == 5
        assert config.timeout_seconds == 60
        assert config.max_timeout_seconds == 300
    
    def test_custom_config(self):
        """Test custom configuration"""
        config = CircuitBreakerConfig(
            failure_threshold=10,
            timeout_seconds=30,
            max_timeout_seconds=600,
        )
        
        assert config.failure_threshold == 10
        assert config.timeout_seconds == 30
        assert config.max_timeout_seconds == 600


class TestCircuitBreakerIntegration:
    """Integration tests for circuit breaker with real scenarios"""
    
    @pytest.mark.asyncio
    async def test_api_call_simulation(self):
        """Simulate API calls with intermittent failures"""
        circuit_breaker = CircuitBreaker(
            name="api_breaker",
            config=CircuitBreakerConfig(failure_threshold=3, timeout_seconds=1)
        )

        call_count = 0

        async def simulated_api_call():
            nonlocal call_count
            call_count += 1

            # Fail first 3 calls, then succeed
            if call_count <= 3:
                raise Exception("API unavailable")
            return "API response"

        # First 3 calls fail and open circuit
        for _ in range(3):
            with pytest.raises(Exception):
                await circuit_breaker.call(simulated_api_call)

        assert circuit_breaker.state == CircuitState.OPEN

        # Wait for circuit to try half-open (timeout doubles to 2s on open)
        await asyncio.sleep(2.5)

        # Next call should succeed and transition to half-open
        result = await circuit_breaker.call(simulated_api_call)
        assert result == "API response"
        assert circuit_breaker.state == CircuitState.HALF_OPEN

        # Make more successful calls to close circuit
        for _ in range(3):
            result = await circuit_breaker.call(simulated_api_call)
            assert result == "API response"

        # Circuit should now be closed
        assert circuit_breaker.state == CircuitState.CLOSED

