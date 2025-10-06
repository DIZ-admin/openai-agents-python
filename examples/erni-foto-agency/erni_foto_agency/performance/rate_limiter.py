"""
Rate Limiter for OpenAI API

Implements Token Bucket algorithm for rate limiting with:
- Requests per minute (RPM) limits
- Tokens per minute (TPM) limits
- Concurrent request limits
- Per-model rate limits
- Graceful degradation with queuing
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RateLimitType(Enum):
    """Types of rate limits"""
    
    REQUESTS_PER_MINUTE = "rpm"
    TOKENS_PER_MINUTE = "tpm"
    CONCURRENT_REQUESTS = "concurrent"


@dataclass
class RateLimitConfig:
    """
    Rate limit configuration for OpenAI API
    
    Default limits based on OpenAI Tier 1 (pay-as-you-go):
    - GPT-4o: 500 RPM, 30,000 TPM
    - GPT-4o-mini: 500 RPM, 200,000 TPM
    """
    
    # Requests per minute
    requests_per_minute: int = 500
    
    # Tokens per minute
    tokens_per_minute: int = 30000
    
    # Maximum concurrent requests
    max_concurrent_requests: int = 10
    
    # Refill rate (tokens added per second)
    refill_rate_rpm: float = field(init=False)
    refill_rate_tpm: float = field(init=False)
    
    def __post_init__(self):
        """Calculate refill rates"""
        self.refill_rate_rpm = self.requests_per_minute / 60.0
        self.refill_rate_tpm = self.tokens_per_minute / 60.0


@dataclass
class TokenBucket:
    """
    Token Bucket for rate limiting
    
    Implements classic token bucket algorithm:
    - Tokens are added at constant rate (refill_rate)
    - Each request consumes tokens
    - Request is allowed if enough tokens available
    - Bucket has maximum capacity
    """
    
    capacity: float
    refill_rate: float
    tokens: float = field(init=False)
    last_refill_time: float = field(init=False)
    
    def __post_init__(self):
        """Initialize bucket as full"""
        self.tokens = self.capacity
        self.last_refill_time = time.time()
    
    def _refill(self) -> None:
        """Refill tokens based on elapsed time"""
        now = time.time()
        elapsed = now - self.last_refill_time
        
        # Add tokens based on elapsed time
        tokens_to_add = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + tokens_to_add)
        self.last_refill_time = now
    
    def consume(self, tokens: float) -> bool:
        """
        Try to consume tokens
        
        Args:
            tokens: Number of tokens to consume
        
        Returns:
            True if tokens were consumed, False if not enough tokens
        """
        self._refill()
        
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False
    
    def get_wait_time(self, tokens: float) -> float:
        """
        Calculate wait time until enough tokens available
        
        Args:
            tokens: Number of tokens needed
        
        Returns:
            Wait time in seconds
        """
        self._refill()
        
        if self.tokens >= tokens:
            return 0.0
        
        tokens_needed = tokens - self.tokens
        wait_time = tokens_needed / self.refill_rate
        return wait_time
    
    def get_available_tokens(self) -> float:
        """Get current number of available tokens"""
        self._refill()
        return self.tokens


class RateLimiter:
    """
    Rate Limiter for OpenAI API using Token Bucket algorithm
    
    Features:
    - Separate limits for RPM and TPM
    - Concurrent request limiting with semaphore
    - Automatic token refilling
    - Wait time calculation for graceful degradation
    - Per-model rate limits
    
    Example:
        limiter = RateLimiter(config=RateLimitConfig(
            requests_per_minute=500,
            tokens_per_minute=30000,
            max_concurrent_requests=10
        ))
        
        async with limiter.acquire(estimated_tokens=1000):
            response = await openai_api_call()
    """
    
    def __init__(self, name: str = "openai", config: RateLimitConfig | None = None):
        """
        Initialize rate limiter
        
        Args:
            name: Name for logging
            config: Rate limit configuration
        """
        self.name = name
        self.config = config or RateLimitConfig()
        
        # Token buckets
        self.rpm_bucket = TokenBucket(
            capacity=self.config.requests_per_minute,
            refill_rate=self.config.refill_rate_rpm
        )
        self.tpm_bucket = TokenBucket(
            capacity=self.config.tokens_per_minute,
            refill_rate=self.config.refill_rate_tpm
        )
        
        # Concurrent request limiting
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        
        # Statistics
        self.total_requests = 0
        self.total_tokens_consumed = 0
        self.total_wait_time = 0.0
        self.requests_throttled = 0
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        logger.info(
            "Rate limiter initialized",
            name=self.name,
            rpm=self.config.requests_per_minute,
            tpm=self.config.tokens_per_minute,
            max_concurrent=self.config.max_concurrent_requests
        )
    
    def acquire(self, estimated_tokens: int = 1000) -> "RateLimiterContext":
        """
        Acquire rate limit permission

        Args:
            estimated_tokens: Estimated tokens for the request

        Returns:
            Context manager for rate limiting

        Example:
            async with limiter.acquire(estimated_tokens=1000):
                response = await api_call()
        """
        return RateLimiterContext(self, estimated_tokens)
    
    async def _wait_for_capacity(self, estimated_tokens: int) -> None:
        """
        Wait until capacity is available
        
        Args:
            estimated_tokens: Estimated tokens needed
        """
        async with self._lock:
            # Check RPM limit
            rpm_wait = self.rpm_bucket.get_wait_time(1.0)
            
            # Check TPM limit
            tpm_wait = self.tpm_bucket.get_wait_time(float(estimated_tokens))
            
            # Wait for the longer of the two
            wait_time = max(rpm_wait, tpm_wait)
            
            if wait_time > 0:
                self.requests_throttled += 1
                self.total_wait_time += wait_time
                
                logger.warning(
                    "Rate limit reached, waiting",
                    name=self.name,
                    wait_time_seconds=wait_time,
                    rpm_available=self.rpm_bucket.get_available_tokens(),
                    tpm_available=self.tpm_bucket.get_available_tokens(),
                    estimated_tokens=estimated_tokens
                )
                
                await asyncio.sleep(wait_time)
            
            # Consume tokens
            self.rpm_bucket.consume(1.0)
            self.tpm_bucket.consume(float(estimated_tokens))
            
            self.total_requests += 1
            self.total_tokens_consumed += estimated_tokens
            
            logger.debug(
                "Rate limit acquired",
                name=self.name,
                estimated_tokens=estimated_tokens,
                rpm_remaining=self.rpm_bucket.get_available_tokens(),
                tpm_remaining=self.tpm_bucket.get_available_tokens()
            )
    
    def get_stats(self) -> dict[str, Any]:
        """
        Get rate limiter statistics
        
        Returns:
            Dictionary with statistics
        """
        return {
            "name": self.name,
            "total_requests": self.total_requests,
            "total_tokens_consumed": self.total_tokens_consumed,
            "total_wait_time_seconds": self.total_wait_time,
            "requests_throttled": self.requests_throttled,
            "throttle_rate": self.requests_throttled / self.total_requests if self.total_requests > 0 else 0.0,
            "avg_wait_time_seconds": self.total_wait_time / self.requests_throttled if self.requests_throttled > 0 else 0.0,
            "rpm_available": self.rpm_bucket.get_available_tokens(),
            "tpm_available": self.tpm_bucket.get_available_tokens(),
            "concurrent_slots_available": self.semaphore._value,
        }


class RateLimiterContext:
    """Context manager for rate limiting"""
    
    def __init__(self, limiter: RateLimiter, estimated_tokens: int):
        self.limiter = limiter
        self.estimated_tokens = estimated_tokens
    
    async def __aenter__(self):
        """Acquire rate limit and concurrent slot"""
        # Wait for rate limit capacity
        await self.limiter._wait_for_capacity(self.estimated_tokens)
        
        # Acquire concurrent request slot
        await self.limiter.semaphore.acquire()
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Release concurrent slot"""
        self.limiter.semaphore.release()
        return False


# ============================================================================
# Global Rate Limiters
# ============================================================================

# Rate limiters for different OpenAI models
_rate_limiters: dict[str, RateLimiter] = {}


def get_rate_limiter(model: str = "gpt-4o") -> RateLimiter:
    """
    Get rate limiter for specific model
    
    Args:
        model: Model name (gpt-4o, gpt-4o-mini, etc.)
    
    Returns:
        RateLimiter instance
    """
    if model not in _rate_limiters:
        # Configure based on model
        if "mini" in model.lower():
            config = RateLimitConfig(
                requests_per_minute=500,
                tokens_per_minute=200000,  # Higher TPM for mini
                max_concurrent_requests=10
            )
        else:
            config = RateLimitConfig(
                requests_per_minute=500,
                tokens_per_minute=30000,
                max_concurrent_requests=10
            )
        
        _rate_limiters[model] = RateLimiter(name=f"openai_{model}", config=config)
    
    return _rate_limiters[model]

