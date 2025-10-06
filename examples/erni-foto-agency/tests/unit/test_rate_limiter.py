"""Unit tests for Rate Limiter"""

import asyncio
import time

import pytest

from erni_foto_agency.performance.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
    TokenBucket,
    get_rate_limiter,
)


class TestTokenBucket:
    """Test suite for Token Bucket"""
    
    def test_bucket_creation(self):
        """Test that bucket is created with full capacity"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        
        assert bucket.capacity == 100.0
        assert bucket.refill_rate == 10.0
        assert bucket.tokens == 100.0
    
    def test_consume_tokens_success(self):
        """Test consuming tokens when enough available"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        
        result = bucket.consume(50.0)
        
        assert result is True
        assert bucket.tokens == 50.0
    
    def test_consume_tokens_failure(self):
        """Test consuming tokens when not enough available"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)

        # Consume most tokens
        bucket.consume(90.0)
        tokens_after_first = bucket.tokens

        # Try to consume more than available
        result = bucket.consume(20.0)

        assert result is False
        # Tokens should be approximately unchanged (allow for small refill during test)
        assert abs(bucket.tokens - tokens_after_first) < 0.1
    
    def test_token_refill(self):
        """Test that tokens refill over time"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)  # 10 tokens/second
        
        # Consume all tokens
        bucket.consume(100.0)
        assert bucket.tokens == 0.0
        
        # Wait 1 second
        time.sleep(1.0)
        
        # Refill should add ~10 tokens
        bucket._refill()
        assert bucket.tokens >= 9.0  # Allow for timing variance
        assert bucket.tokens <= 11.0
    
    def test_refill_cap_at_capacity(self):
        """Test that refill doesn't exceed capacity"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        
        # Wait and refill (should stay at capacity)
        time.sleep(0.5)
        bucket._refill()
        
        assert bucket.tokens == 100.0
    
    def test_get_wait_time(self):
        """Test wait time calculation"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        
        # Consume most tokens
        bucket.consume(90.0)
        
        # Need 20 tokens, have 10, need 10 more
        # At 10 tokens/second, should wait ~1 second
        wait_time = bucket.get_wait_time(20.0)
        
        assert wait_time >= 0.9
        assert wait_time <= 1.1
    
    def test_get_wait_time_zero_when_available(self):
        """Test wait time is zero when tokens available"""
        bucket = TokenBucket(capacity=100.0, refill_rate=10.0)
        
        wait_time = bucket.get_wait_time(50.0)
        
        assert wait_time == 0.0


class TestRateLimitConfig:
    """Test suite for Rate Limit Config"""
    
    def test_default_config(self):
        """Test default configuration"""
        config = RateLimitConfig()
        
        assert config.requests_per_minute == 500
        assert config.tokens_per_minute == 30000
        assert config.max_concurrent_requests == 10
        assert config.refill_rate_rpm == 500 / 60.0
        assert config.refill_rate_tpm == 30000 / 60.0
    
    def test_custom_config(self):
        """Test custom configuration"""
        config = RateLimitConfig(
            requests_per_minute=100,
            tokens_per_minute=10000,
            max_concurrent_requests=5
        )
        
        assert config.requests_per_minute == 100
        assert config.tokens_per_minute == 10000
        assert config.max_concurrent_requests == 5
        assert config.refill_rate_rpm == 100 / 60.0
        assert config.refill_rate_tpm == 10000 / 60.0


class TestRateLimiter:
    """Test suite for Rate Limiter"""
    
    @pytest.mark.asyncio
    async def test_limiter_creation(self):
        """Test that limiter is created successfully"""
        limiter = RateLimiter(name="test", config=RateLimitConfig())
        
        assert limiter.name == "test"
        assert limiter.total_requests == 0
        assert limiter.total_tokens_consumed == 0
    
    @pytest.mark.asyncio
    async def test_acquire_success(self):
        """Test acquiring rate limit when capacity available"""
        config = RateLimitConfig(
            requests_per_minute=60,  # 1 per second
            tokens_per_minute=6000,  # 100 per second
            max_concurrent_requests=10
        )
        limiter = RateLimiter(name="test", config=config)
        
        # Should succeed immediately
        async with limiter.acquire(estimated_tokens=100):
            pass
        
        assert limiter.total_requests == 1
        assert limiter.total_tokens_consumed == 100
    
    @pytest.mark.asyncio
    async def test_acquire_with_wait(self):
        """Test acquiring rate limit with wait"""
        config = RateLimitConfig(
            requests_per_minute=60,  # 1 per second
            tokens_per_minute=6000,  # 100 per second
            max_concurrent_requests=10
        )
        limiter = RateLimiter(name="test", config=config)
        
        # Consume all RPM capacity
        for _ in range(60):
            limiter.rpm_bucket.consume(1.0)
        
        # Next request should wait
        start_time = time.time()
        async with limiter.acquire(estimated_tokens=100):
            pass
        elapsed = time.time() - start_time
        
        # Should have waited ~1 second for refill
        assert elapsed >= 0.9
        assert limiter.requests_throttled == 1
    
    @pytest.mark.asyncio
    async def test_concurrent_request_limiting(self):
        """Test concurrent request limiting with semaphore"""
        config = RateLimitConfig(
            requests_per_minute=1000,  # High RPM
            tokens_per_minute=100000,  # High TPM
            max_concurrent_requests=2  # Only 2 concurrent
        )
        limiter = RateLimiter(name="test", config=config)
        
        concurrent_count = 0
        max_concurrent = 0
        
        async def task():
            nonlocal concurrent_count, max_concurrent
            async with limiter.acquire(estimated_tokens=100):
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
                await asyncio.sleep(0.1)  # Simulate work
                concurrent_count -= 1
        
        # Launch 5 tasks
        await asyncio.gather(*[task() for _ in range(5)])
        
        # Should never exceed 2 concurrent
        assert max_concurrent == 2
        assert limiter.total_requests == 5
    
    @pytest.mark.asyncio
    async def test_get_stats(self):
        """Test getting rate limiter statistics"""
        limiter = RateLimiter(name="test", config=RateLimitConfig())
        
        # Make some requests
        async with limiter.acquire(estimated_tokens=1000):
            pass
        async with limiter.acquire(estimated_tokens=2000):
            pass
        
        stats = limiter.get_stats()
        
        assert stats["name"] == "test"
        assert stats["total_requests"] == 2
        assert stats["total_tokens_consumed"] == 3000
        assert "rpm_available" in stats
        assert "tpm_available" in stats
    
    @pytest.mark.asyncio
    async def test_tpm_limiting(self):
        """Test tokens per minute limiting"""
        config = RateLimitConfig(
            requests_per_minute=1000,  # High RPM
            tokens_per_minute=1000,  # Low TPM (16.67 per second)
            max_concurrent_requests=10
        )
        limiter = RateLimiter(name="test", config=config)
        
        # Consume all TPM capacity
        limiter.tpm_bucket.consume(1000.0)
        
        # Next request should wait for token refill
        start_time = time.time()
        async with limiter.acquire(estimated_tokens=100):
            pass
        elapsed = time.time() - start_time
        
        # Should have waited for tokens to refill
        assert elapsed >= 5.0  # 100 tokens / 16.67 per second ≈ 6 seconds
        assert limiter.requests_throttled == 1


class TestGlobalRateLimiters:
    """Test global rate limiter functions"""
    
    def test_get_rate_limiter_gpt4o(self):
        """Test getting rate limiter for gpt-4o"""
        limiter = get_rate_limiter("gpt-4o")
        
        assert limiter is not None
        assert limiter.name == "openai_gpt-4o"
        assert limiter.config.tokens_per_minute == 30000
    
    def test_get_rate_limiter_gpt4o_mini(self):
        """Test getting rate limiter for gpt-4o-mini"""
        limiter = get_rate_limiter("gpt-4o-mini")
        
        assert limiter is not None
        assert limiter.name == "openai_gpt-4o-mini"
        assert limiter.config.tokens_per_minute == 200000  # Higher TPM
    
    def test_get_rate_limiter_singleton(self):
        """Test that get_rate_limiter returns singleton"""
        limiter1 = get_rate_limiter("gpt-4o")
        limiter2 = get_rate_limiter("gpt-4o")
        
        assert limiter1 is limiter2
    
    def test_get_rate_limiter_different_models(self):
        """Test that different models get different limiters"""
        limiter_4o = get_rate_limiter("gpt-4o")
        limiter_mini = get_rate_limiter("gpt-4o-mini")
        
        assert limiter_4o is not limiter_mini
        assert limiter_4o.config.tokens_per_minute != limiter_mini.config.tokens_per_minute

