"""
Enterprise Playwright Manager with multi-browser session management,
proxy integration, health checks, and resource pooling.
"""

import asyncio
import uuid
import time
from typing import Dict, Optional, List, Any, Tuple
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from playwright.async_api import (
    async_playwright, Playwright, Browser, BrowserContext, Page,
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
)
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger

from src.core.config import settings
from src.core.exceptions import AutomationError, ProxyError
from src.services.proxy.proxy_manager import ProxyManager
from src.database.models.browser_session import BrowserSessionModel
from src.repositories.session_repository import SessionRepository
from src.core.cache.redis_client import RedisClient


@dataclass
class SessionMetadata:
    """Metadata for active browser sessions."""
    session_id: str
    browser: Browser
    contexts: List[BrowserContext]
    proxy: Optional[Dict[str, str]]
    created_at: datetime
    last_used: datetime
    user_id: Optional[int]
    status: str = "active"  # active, idle, closing
    page_count: int = 0
    request_count: int = 0


class PlaywrightManager:
    """
    Manages Playwright browser instances across multiple sessions.
    Implements connection pooling, health checks, and graceful degradation.
    """
    
    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._sessions: Dict[str, SessionMetadata] = {}
        self._session_lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._proxy_manager: Optional[ProxyManager] = None
        self._session_repo = SessionRepository()
        self._redis = None
        
    async def initialize(self) -> None:
        """Initialize Playwright and start background tasks."""
        if self._initialized:
            logger.warning("PlaywrightManager already initialized")
            return
            
        try:
            self._playwright = await async_playwright().start()
            self._proxy_manager = await ProxyManager.get_instance()
            self._redis = await RedisClient.get_instance()
            
            # Start background tasks
            self._cleanup_task = asyncio.create_task(self._cleanup_idle_sessions())
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            
            self._initialized = True
            logger.info("PlaywrightManager initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize PlaywrightManager: {e}")
            raise AutomationError("Playwright initialization failed", details={"error": str(e)})
    
    async def close_all(self) -> None:
        """Close all browser sessions and shutdown Playwright."""
        if not self._initialized:
            return
            
        logger.info("Closing all browser sessions...")
        
        # Cancel background tasks
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._health_check_task:
            self._health_check_task.cancel()
        
        # Close all browsers
        async with self._session_lock:
            for session_id, metadata in list(self._sessions.items()):
                try:
                    await metadata.browser.close()
                    logger.debug(f"Closed browser session {session_id}")
                except Exception as e:
                    logger.error(f"Error closing browser {session_id}: {e}")
            self._sessions.clear()
        
        # Stop Playwright
        if self._playwright:
            await self._playwright.stop()
            
        self._initialized = False
        logger.info("All Playwright resources cleaned up")
    
    async def create_session(
        self,
        user_id: Optional[int] = None,
        use_proxy: bool = True,
        custom_proxy: Optional[Dict[str, str]] = None,
        context_count: int = 1
    ) -> str:
        """
        Create a new browser session with optional proxy.
        
        Args:
            user_id: User identifier for tracking
            use_proxy: Whether to use proxy rotation
            custom_proxy: Specific proxy configuration
            context_count: Number of browser contexts to create
            
        Returns:
            session_id: Unique session identifier
        """
        if not self._initialized:
            raise AutomationError("PlaywrightManager not initialized")
        
        if context_count > settings.BROWSER_CONTEXTS_PER_SESSION:
            raise AutomationError(
                f"Context count {context_count} exceeds maximum {settings.BROWSER_CONTEXTS_PER_SESSION}"
            )
        
        session_id = str(uuid.uuid4())
        proxy_config = None
        
        # Get proxy configuration
        if custom_proxy:
            proxy_config = custom_proxy
        elif use_proxy and settings.PROXY_ENABLED and self._proxy_manager:
            try:
                proxy_config = await self._proxy_manager.get_proxy()
                logger.info(f"Using proxy {proxy_config.get('server')} for session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to get proxy: {e}, continuing without proxy")
        
        # Launch browser with proxy if configured
        browser_args = settings.BROWSER_ARGS.copy()
        if settings.PLAYWRIGHT_HEADLESS:
            browser_args.append("--headless=new")
        
        try:
            launch_options = {
                "headless": settings.PLAYWRIGHT_HEADLESS,
                "args": browser_args,
                "timeout": settings.PLAYWRIGHT_TIMEOUT,
            }
            
            if proxy_config:
                launch_options["proxy"] = {
                    "server": proxy_config["server"],
                }
                if "username" in proxy_config and "password" in proxy_config:
                    launch_options["proxy"]["username"] = proxy_config["username"]
                    launch_options["proxy"]["password"] = proxy_config["password"]
            
            browser = await self._playwright.chromium.launch(**launch_options)
            
            # Create contexts
            contexts = []
            for i in range(context_count):
                context = await self._new_context(browser, proxy_config)
                contexts.append(context)
            
            # Store session metadata
            metadata = SessionMetadata(
                session_id=session_id,
                browser=browser,
                contexts=contexts,
                proxy=proxy_config,
                created_at=datetime.utcnow(),
                last_used=datetime.utcnow(),
                user_id=user_id,
            )
            
            async with self._session_lock:
                self._sessions[session_id] = metadata
            
            # Persist to database
            await self._session_repo.create({
                "session_id": session_id,
                "user_id": user_id,
                "proxy_server": proxy_config.get("server") if proxy_config else None,
                "status": "active",
                "contexts_count": context_count,
            })
            
            logger.info(f"Created session {session_id} with {context_count} contexts")
            return session_id
            
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise AutomationError(f"Browser session creation failed: {e}")
    
    async def _new_context(
        self,
        browser: Browser,
        proxy_config: Optional[Dict[str, str]] = None
    ) -> BrowserContext:
        """Create a new browser context with anti-detection measures."""
        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-US",
            "timezone_id": "Africa/Accra",
            "permissions": ["geolocation"],
            "geolocation": {"longitude": -0.186964, "latitude": 5.603717},
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
        }
        
        if proxy_config:
            context_options["proxy"] = proxy_config
        
        context = await browser.new_context(**context_options)
        
        # Add stealth scripts
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        
        return context
    
    @asynccontextmanager
    async def get_page(self, session_id: str, context_index: int = 0) -> Page:
        """
        Get a page from a session context for operations.
        Handles automatic cleanup and error recovery.
        """
        metadata = await self._get_session(session_id)
        if not metadata:
            raise AutomationError(f"Session {session_id} not found")
        
        if context_index >= len(metadata.contexts):
            raise AutomationError(f"Context {context_index} not available")
        
        context = metadata.contexts[context_index]
        page = None
        
        try:
            page = await context.new_page()
            metadata.page_count += 1
            metadata.last_used = datetime.utcnow()
            
            # Set default timeouts
            page.set_default_timeout(settings.NAVIGATION_TIMEOUT)
            page.set_default_navigation_timeout(settings.NAVIGATION_TIMEOUT)
            
            yield page
            
        except PlaywrightTimeoutError as e:
            logger.warning(f"Timeout in session {session_id}: {e}")
            if page:
                await page.screenshot(path=f"logs/timeout_{session_id}_{int(time.time())}.png")
            raise AutomationError(f"Page operation timeout: {e}")
            
        except PlaywrightError as e:
            logger.error(f"Playwright error in session {session_id}: {e}")
            raise AutomationError(f"Browser automation error: {e}")
            
        finally:
            if page:
                await page.close()
                metadata.page_count -= 1
    
    async def execute_script(
        self,
        session_id: str,
        script: str,
        context_index: int = 0,
        retries: int = 2
    ) -> Any:
        """Execute JavaScript in a session context with retry logic."""
        for attempt in range(retries):
            try:
                async with self.get_page(session_id, context_index) as page:
                    result = await page.evaluate(script)
                    metadata = await self._get_session(session_id)
                    if metadata:
                        metadata.request_count += 1
                    return result
            except Exception as e:
                if attempt == retries - 1:
                    raise
                logger.warning(f"Script execution failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(2 ** attempt)
    
    async def screenshot(
        self,
        session_id: str,
        context_index: int = 0,
        path: Optional[str] = None
    ) -> bytes:
        """Take screenshot of current page."""
        async with self.get_page(session_id, context_index) as page:
            if path:
                screenshot_bytes = await page.screenshot(path=path)
            else:
                screenshot_bytes = await page.screenshot()
            return screenshot_bytes
    
    async def close_session(self, session_id: str) -> bool:
        """Close and cleanup a specific browser session."""
        async with self._session_lock:
            metadata = self._sessions.pop(session_id, None)
        
        if not metadata:
            logger.warning(f"Session {session_id} not found for closing")
            return False
        
        try:
            # Close all contexts first
            for context in metadata.contexts:
                await context.close()
            # Close browser
            await metadata.browser.close()
            
            # Update database
            await self._session_repo.update_status(session_id, "closed")
            
            logger.info(f"Closed session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error closing session {session_id}: {e}")
            return False
    
    async def _get_session(self, session_id: str) -> Optional[SessionMetadata]:
        """Get session metadata with lock."""
        async with self._session_lock:
            return self._sessions.get(session_id)
    
    async def _cleanup_idle_sessions(self) -> None:
        """Background task to cleanup idle sessions (30 minutes timeout)."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                
                now = datetime.utcnow()
                idle_threshold = timedelta(minutes=30)
                
                async with self._session_lock:
                    idle_sessions = [
                        sid for sid, meta in self._sessions.items()
                        if now - meta.last_used > idle_threshold
                    ]
                
                for session_id in idle_sessions:
                    logger.info(f"Cleaning up idle session {session_id}")
                    await self.close_session(session_id)
                    
            except asyncio.CancelledError:
                logger.info("Idle session cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
    
    async def _health_check_loop(self) -> None:
        """Periodic health checks for all active sessions."""
        while True:
            try:
                await asyncio.sleep(120)  # Every 2 minutes
                
                async with self._session_lock:
                    sessions_copy = list(self._sessions.items())
                
                for session_id, metadata in sessions_copy:
                    try:
                        # Ping browser with simple evaluate
                        context = metadata.contexts[0]
                        page = await context.new_page()
                        await page.evaluate("1+1")
                        await page.close()
                        logger.debug(f"Health check passed for session {session_id}")
                    except Exception as e:
                        logger.warning(f"Health check failed for session {session_id}: {e}")
                        await self.close_session(session_id)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check loop error: {e}")
    
    def get_active_session_count(self) -> int:
        """Get number of active sessions."""
        return len(self._sessions)
    
    async def get_session_stats(self) -> Dict[str, Any]:
        """Get comprehensive session statistics."""
        async with self._session_lock:
            total_pages = sum(meta.page_count for meta in self._sessions.values())
            total_requests = sum(meta.request_count for meta in self._sessions.values())
            
        return {
            "total_sessions": len(self._sessions),
            "total_pages": total_pages,
            "total_requests": total_requests,
            "max_concurrent": settings.BROWSER_MAX_CONCURRENT,
            "headless_mode": settings.PLAYWRIGHT_HEADLESS,
            "proxy_enabled": settings.PROXY_ENABLED,
        }