"""
Advanced retry engine with exponential backoff, jitter, circuit breaker pattern,
and intelligent failure classification for Playwright operations.
"""

import asyncio
import random
import time
from enum import Enum
from typing import TypeVar, Callable, Awaitable, Optional, List, Dict, Any, Tuple
from functools import wraps
from dataclasses import dataclass, field
from collections import defaultdict

from tenacity import (
    retry as tenacity_retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    retry_if_result,
    RetryError,
)
from loguru import logger

from src.core.config import settings
from src.core.exceptions import AutomationError, TransientError, PermanentError

T = TypeVar("T")

class FailureSeverity(Enum):
    TRANSIENT = "transient"  # Can retry
    PERMANENT = "permanent"  # Should not retry
    UNKNOWN = "unknown"

@dataclass
class RetryContext:
    attempt: int = 0
    last_error: Optional[Exception] = None
    total_delay: float = 0.0
    custom_data: Dict[str, Any] = field(default_factory=dict)

class CircuitBreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    """Circuit breaker pattern to prevent cascading failures."""
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.half_open_calls = 0
        self._lock = asyncio.Lock()
    
    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        """Execute function with circuit breaker protection."""
        async with self._lock:
            if self.state == CircuitBreakerState.OPEN:
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    logger.info(f"Circuit breaker '{self.name}' moving to HALF_OPEN")
                    self.state = CircuitBreakerState.HALF_OPEN
                    self.half_open_calls = 0
                else:
                    raise AutomationError(
                        f"Circuit breaker '{self.name}' is OPEN",
                        error_code="CIRCUIT_OPEN"
                    )
        
        try:
            result = await func()
            await self._record_success()
            return result
        except Exception as e:
            await self._record_failure()
            raise
    
    async def _record_success(self) -> None:
        async with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                self.half_open_calls += 1
                if self.half_open_calls >= self.half_open_max_calls:
                    logger.info(f"Circuit breaker '{self.name}' closing")
                    self.state = CircuitBreakerState.CLOSED
                    self.failure_count = 0
            elif self.state == CircuitBreakerState.CLOSED:
                self.failure_count = 0
    
    async def _record_failure(self) -> None:
        async with self._lock:
            self.last_failure_time = time.time()
            
            if self.state == CircuitBreakerState.HALF_OPEN:
                logger.warning(f"Circuit breaker '{self.name}' re-opening after half-open failure")
                self.state = CircuitBreakerState.OPEN
                self.half_open_calls = 0
            elif self.state == CircuitBreakerState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= self.failure_threshold:
                    logger.warning(f"Circuit breaker '{self.name}' opening after {self.failure_count} failures")
                    self.state = CircuitBreakerState.OPEN

class RetryEngine:
    """
    Enterprise retry engine with multiple strategies, circuit breakers,
    and intelligent failure classification.
    """
    
    def __init__(self):
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._failure_history: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        self._lock = asyncio.Lock()
    
    def get_circuit_breaker(self, name: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a specific operation."""
        if name not in self.circuit_breakers:
            self.circuit_breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=settings.RETRY_MAX_ATTEMPTS,
                recovery_timeout=settings.RETRY_MAX_DELAY
            )
        return self.circuit_breakers[name]
    
    async def execute_with_retry(
        self,
        func: Callable[[], Awaitable[T]],
        operation_name: str,
        max_attempts: Optional[int] = None,
        backoff_factor: Optional[float] = None,
        max_delay: Optional[int] = None,
        retry_on_exceptions: Optional[List[type]] = None,
        circuit_breaker_name: Optional[str] = None,
    ) -> T:
        """
        Execute an async function with intelligent retry logic.
        
        Args:
            func: Async function to execute
            operation_name: Name for logging and circuit breaker
            max_attempts: Maximum retry attempts
            backoff_factor: Exponential backoff multiplier
            max_delay: Maximum delay between retries
            retry_on_exceptions: List of exception types to retry
            circuit_breaker_name: Circuit breaker name for protection
            
        Returns:
            Function result
        """
        max_attempts = max_attempts or settings.RETRY_MAX_ATTEMPTS
        backoff_factor = backoff_factor or settings.RETRY_BACKOFF_FACTOR
        max_delay = max_delay or settings.RETRY_MAX_DELAY
        retry_on_exceptions = retry_on_exceptions or [TransientError, ConnectionError, TimeoutError]
        
        # Wrap with circuit breaker if requested
        if circuit_breaker_name:
            cb = self.get_circuit_breaker(circuit_breaker_name)
            wrapper_func = lambda: cb.call(func)
        else:
            wrapper_func = func
        
        context = RetryContext()
        
        for attempt in range(1, max_attempts + 1):
            context.attempt = attempt
            try:
                logger.debug(f"Executing {operation_name} (attempt {attempt}/{max_attempts})")
                result = await wrapper_func()
                
                # Record success
                if attempt > 1:
                    logger.info(f"Successfully executed {operation_name} after {attempt} attempts")
                return result
                
            except Exception as e:
                context.last_error = e
                severity = self._classify_failure(e, retry_on_exceptions)
                
                if severity == FailureSeverity.PERMANENT or attempt == max_attempts:
                    logger.error(f"Permanent failure or max attempts reached for {operation_name}: {e}")
                    await self._record_failure(operation_name, str(e))
                    if isinstance(e, AutomationError):
                        raise
                    raise AutomationError(
                        f"Operation {operation_name} failed after {attempt} attempts: {e}",
                        details={"original_error": str(e), "attempts": attempt}
                    ) from e
                
                # Calculate delay with jitter
                delay = min(backoff_factor ** attempt + random.uniform(0, 1), max_delay)
                context.total_delay += delay
                
                logger.warning(
                    f"Retry {operation_name} attempt {attempt}/{max_attempts} failed: {e}. "
                    f"Retrying in {delay:.2f}s (total delay: {context.total_delay:.2f}s)"
                )
                
                await asyncio.sleep(delay)
        
        raise AutomationError(f"Unexpected exit from retry loop for {operation_name}")
    
    def _classify_failure(
        self,
        exception: Exception,
        retryable_exceptions: List[type]
    ) -> FailureSeverity:
        """Classify if failure is transient or permanent."""
        # Check for specific permanent errors
        if isinstance(exception, PermanentError):
            return FailureSeverity.PERMANENT
        
        # Check retryable exceptions
        for exc_type in retryable_exceptions:
            if isinstance(exception, exc_type):
                return FailureSeverity.TRANSIENT
        
        # Unknown - assume transient but log
        logger.debug(f"Unknown exception type {type(exception).__name__}, treating as transient")
        return FailureSeverity.TRANSIENT
    
    async def _record_failure(self, operation_name: str, error: str) -> None:
        """Record failure for analytics."""
        async with self._lock:
            self._failure_history[operation_name].append((time.time(), error))
            # Keep only last 100 failures
            if len(self._failure_history[operation_name]) > 100:
                self._failure_history[operation_name] = self._failure_history[operation_name][-100:]
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get retry engine statistics."""
        async with self._lock:
            return {
                "circuit_breakers": {
                    name: {
                        "state": cb.state.value,
                        "failure_count": cb.failure_count,
                    }
                    for name, cb in self.circuit_breakers.items()
                },
                "failure_history": {
                    op: len(failures) for op, failures in self._failure_history.items()
                }
            }
    
    def retry_decorator(
        self,
        operation_name: str,
        max_attempts: Optional[int] = None,
        use_circuit_breaker: bool = True
    ):
        """Decorator for retry logic."""
        def decorator(func: Callable[[], Awaitable[T]]):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await self.execute_with_retry(
                    lambda: func(*args, **kwargs),
                    operation_name=operation_name,
                    max_attempts=max_attempts,
                    circuit_breaker_name=operation_name if use_circuit_breaker else None
                )
            return wrapper
        return decorator

# Singleton instance
_retry_engine: Optional[RetryEngine] = None

async def get_retry_engine() -> RetryEngine:
    global _retry_engine
    if _retry_engine is None:
        _retry_engine = RetryEngine()
    return _retry_engine