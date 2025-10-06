"""
Session Manager for Agent Conversations

Manages lifecycle of SQLiteSession instances to prevent memory leaks:
- Session pooling and reuse
- Automatic cleanup of old sessions
- Connection management
- Memory monitoring
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from agents import SQLiteSession

logger = structlog.get_logger(__name__)


@dataclass
class SessionMetadata:
    """Metadata for tracking session usage"""
    
    session: SQLiteSession
    created_at: float
    last_accessed: float
    access_count: int
    
    def update_access(self) -> None:
        """Update access timestamp and count"""
        self.last_accessed = time.time()
        self.access_count += 1


class SessionManager:
    """
    Session Manager for SQLiteSession instances
    
    Features:
    - Session pooling with LRU eviction
    - Automatic cleanup of inactive sessions
    - Connection lifecycle management
    - Memory leak prevention
    - Statistics tracking
    
    Example:
        manager = SessionManager(
            max_sessions=100,
            session_ttl=3600,
            cleanup_interval=300
        )
        
        # Get or create session
        session = await manager.get_session("user-123")
        
        # Use session
        result = await Runner.run(agent, "Hello", session=session)
        
        # Cleanup (automatic via background task)
        await manager.cleanup_expired_sessions()
        
        # Shutdown
        await manager.shutdown()
    """
    
    def __init__(
        self,
        db_path: str | Path = "data/sessions.db",
        max_sessions: int = 100,
        session_ttl: int = 3600,  # 1 hour
        cleanup_interval: int = 300,  # 5 minutes
    ):
        """
        Initialize session manager
        
        Args:
            db_path: Path to SQLite database for sessions
            max_sessions: Maximum number of sessions to keep in memory
            session_ttl: Time-to-live for inactive sessions (seconds)
            cleanup_interval: Interval for cleanup task (seconds)
        """
        self.db_path = Path(db_path)
        self.max_sessions = max_sessions
        self.session_ttl = session_ttl
        self.cleanup_interval = cleanup_interval
        
        # Session pool (LRU cache)
        self._sessions: OrderedDict[str, SessionMetadata] = OrderedDict()
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
        
        # Background cleanup task
        self._cleanup_task: asyncio.Task | None = None
        self._running = False
        
        # Statistics
        self.stats = {
            "sessions_created": 0,
            "sessions_evicted": 0,
            "sessions_expired": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
        
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(
            "Session Manager initialized",
            db_path=str(self.db_path),
            max_sessions=max_sessions,
            session_ttl=session_ttl
        )
    
    async def start(self) -> None:
        """Start background cleanup task"""
        if self._running:
            logger.warning("Session Manager already running")
            return
        
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info("Session Manager started")
    
    async def shutdown(self) -> None:
        """Shutdown session manager and cleanup all sessions"""
        logger.info("Shutting down Session Manager...")
        
        self._running = False
        
        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Close all sessions
        async with self._lock:
            for session_id, metadata in list(self._sessions.items()):
                await self._close_session(session_id, metadata)
            
            self._sessions.clear()
        
        logger.info("Session Manager shutdown complete")
    
    async def get_session(self, session_id: str) -> SQLiteSession:
        """
        Get or create session
        
        Args:
            session_id: Unique session identifier
        
        Returns:
            SQLiteSession instance
        """
        async with self._lock:
            # Check if session exists in pool
            if session_id in self._sessions:
                metadata = self._sessions[session_id]
                metadata.update_access()
                
                # Move to end (most recently used)
                self._sessions.move_to_end(session_id)
                
                self.stats["cache_hits"] += 1
                
                logger.debug(
                    "Session cache hit",
                    session_id=session_id,
                    access_count=metadata.access_count
                )
                
                return metadata.session
            
            # Create new session
            self.stats["cache_misses"] += 1
            
            # Evict oldest session if at capacity
            if len(self._sessions) >= self.max_sessions:
                await self._evict_oldest_session()
            
            # Create new session
            session = SQLiteSession(
                session_id=session_id,
                db_path=self.db_path
            )
            
            metadata = SessionMetadata(
                session=session,
                created_at=time.time(),
                last_accessed=time.time(),
                access_count=1
            )
            
            self._sessions[session_id] = metadata
            self.stats["sessions_created"] += 1
            
            logger.info(
                "Session created",
                session_id=session_id,
                total_sessions=len(self._sessions)
            )
            
            return session
    
    async def remove_session(self, session_id: str) -> None:
        """
        Remove session from pool
        
        Args:
            session_id: Session to remove
        """
        async with self._lock:
            if session_id in self._sessions:
                metadata = self._sessions.pop(session_id)
                await self._close_session(session_id, metadata)
                
                logger.info("Session removed", session_id=session_id)
    
    async def cleanup_expired_sessions(self) -> int:
        """
        Cleanup expired sessions
        
        Returns:
            Number of sessions cleaned up
        """
        async with self._lock:
            now = time.time()
            expired_sessions = []
            
            for session_id, metadata in self._sessions.items():
                age = now - metadata.last_accessed
                if age > self.session_ttl:
                    expired_sessions.append(session_id)
            
            # Remove expired sessions
            for session_id in expired_sessions:
                metadata = self._sessions.pop(session_id)
                await self._close_session(session_id, metadata)
                self.stats["sessions_expired"] += 1
            
            if expired_sessions:
                logger.info(
                    "Expired sessions cleaned up",
                    count=len(expired_sessions),
                    remaining=len(self._sessions)
                )
            
            return len(expired_sessions)
    
    async def _evict_oldest_session(self) -> None:
        """Evict least recently used session"""
        if not self._sessions:
            return
        
        # Get oldest session (first in OrderedDict)
        session_id, metadata = self._sessions.popitem(last=False)
        await self._close_session(session_id, metadata)
        
        self.stats["sessions_evicted"] += 1
        
        logger.debug(
            "Session evicted (LRU)",
            session_id=session_id,
            age_seconds=time.time() - metadata.created_at
        )
    
    async def _close_session(self, session_id: str, metadata: SessionMetadata) -> None:
        """
        Close session and cleanup resources
        
        Args:
            session_id: Session identifier
            metadata: Session metadata
        """
        try:
            # Clear session data
            await metadata.session.clear_session()
            
            # Close database connections (if session has close method)
            if hasattr(metadata.session, 'close'):
                await metadata.session.close()
            
            logger.debug("Session closed", session_id=session_id)
            
        except Exception as e:
            logger.error(
                "Failed to close session",
                session_id=session_id,
                error=str(e)
            )
    
    async def _cleanup_loop(self) -> None:
        """Background task for periodic cleanup"""
        logger.info("Session cleanup loop started")
        
        while self._running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                
                if self._running:
                    await self.cleanup_expired_sessions()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in cleanup loop", error=str(e))
        
        logger.info("Session cleanup loop stopped")
    
    def get_stats(self) -> dict[str, Any]:
        """
        Get session manager statistics
        
        Returns:
            Dictionary with statistics
        """
        return {
            **self.stats,
            "active_sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "session_ttl": self.session_ttl,
            "cache_hit_rate": (
                self.stats["cache_hits"] / 
                (self.stats["cache_hits"] + self.stats["cache_misses"])
                if (self.stats["cache_hits"] + self.stats["cache_misses"]) > 0
                else 0.0
            )
        }

