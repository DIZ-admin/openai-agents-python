"""Unit tests for Session Manager"""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from erni_foto_agency.session.session_manager import SessionManager, SessionMetadata


class TestSessionManager:
    """Test suite for Session Manager"""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_sessions.db"
            yield db_path
    
    @pytest.fixture
    async def session_manager(self, temp_db):
        """Create session manager instance"""
        manager = SessionManager(
            db_path=temp_db,
            max_sessions=5,
            session_ttl=2,  # 2 seconds for testing
            cleanup_interval=1  # 1 second for testing
        )
        await manager.start()
        yield manager
        await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_session_manager_creation(self, temp_db):
        """Test session manager initialization"""
        manager = SessionManager(
            db_path=temp_db,
            max_sessions=10,
            session_ttl=3600
        )
        
        assert manager.db_path == temp_db
        assert manager.max_sessions == 10
        assert manager.session_ttl == 3600
        assert len(manager._sessions) == 0
    
    @pytest.mark.asyncio
    async def test_get_session_creates_new(self, session_manager):
        """Test getting a new session creates it"""
        session = await session_manager.get_session("user-123")
        
        assert session is not None
        assert session.session_id == "user-123"
        assert len(session_manager._sessions) == 1
        assert session_manager.stats["sessions_created"] == 1
        assert session_manager.stats["cache_misses"] == 1
    
    @pytest.mark.asyncio
    async def test_get_session_returns_cached(self, session_manager):
        """Test getting existing session returns cached instance"""
        # Create session
        session1 = await session_manager.get_session("user-123")
        
        # Get same session again
        session2 = await session_manager.get_session("user-123")
        
        # Should be same instance
        assert session1 is session2
        assert len(session_manager._sessions) == 1
        assert session_manager.stats["sessions_created"] == 1
        assert session_manager.stats["cache_hits"] == 1
    
    @pytest.mark.asyncio
    async def test_session_lru_eviction(self, session_manager):
        """Test LRU eviction when max sessions reached"""
        # Create max_sessions (5) sessions
        for i in range(5):
            await session_manager.get_session(f"user-{i}")
        
        assert len(session_manager._sessions) == 5
        
        # Create one more session - should evict oldest
        await session_manager.get_session("user-new")
        
        assert len(session_manager._sessions) == 5
        assert session_manager.stats["sessions_evicted"] == 1
        
        # user-0 should be evicted (oldest)
        assert "user-0" not in session_manager._sessions
        assert "user-new" in session_manager._sessions
    
    @pytest.mark.asyncio
    async def test_session_access_updates_lru(self, session_manager):
        """Test accessing session updates LRU order"""
        # Create sessions
        await session_manager.get_session("user-1")
        await session_manager.get_session("user-2")
        await session_manager.get_session("user-3")
        
        # Access user-1 again (moves to end)
        await session_manager.get_session("user-1")
        
        # Create 3 more sessions to reach max (5)
        await session_manager.get_session("user-4")
        await session_manager.get_session("user-5")
        
        # Create one more - should evict user-2 (oldest accessed)
        await session_manager.get_session("user-6")
        
        assert "user-2" not in session_manager._sessions
        assert "user-1" in session_manager._sessions  # Should still exist
    
    @pytest.mark.asyncio
    async def test_remove_session(self, session_manager):
        """Test removing session"""
        # Create session
        await session_manager.get_session("user-123")
        assert len(session_manager._sessions) == 1
        
        # Remove session
        await session_manager.remove_session("user-123")
        
        assert len(session_manager._sessions) == 0
        assert "user-123" not in session_manager._sessions
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions(self, session_manager):
        """Test cleanup of expired sessions"""
        # Create sessions
        await session_manager.get_session("user-1")
        await session_manager.get_session("user-2")
        
        assert len(session_manager._sessions) == 2
        
        # Wait for sessions to expire (TTL = 2 seconds)
        await asyncio.sleep(2.5)
        
        # Manually trigger cleanup
        expired_count = await session_manager.cleanup_expired_sessions()
        
        assert expired_count == 2
        assert len(session_manager._sessions) == 0
        assert session_manager.stats["sessions_expired"] == 2
    
    @pytest.mark.asyncio
    async def test_cleanup_keeps_active_sessions(self, session_manager):
        """Test cleanup doesn't remove active sessions"""
        # Create session
        await session_manager.get_session("user-1")
        
        # Wait a bit but not enough to expire
        await asyncio.sleep(0.5)
        
        # Access session to update timestamp
        await session_manager.get_session("user-1")
        
        # Wait more
        await asyncio.sleep(0.5)
        
        # Cleanup should not remove active session
        expired_count = await session_manager.cleanup_expired_sessions()
        
        assert expired_count == 0
        assert len(session_manager._sessions) == 1
    
    @pytest.mark.asyncio
    async def test_background_cleanup_task(self, temp_db):
        """Test background cleanup task runs automatically"""
        manager = SessionManager(
            db_path=temp_db,
            max_sessions=10,
            session_ttl=1,  # 1 second
            cleanup_interval=0.5  # 0.5 seconds
        )
        
        await manager.start()
        
        try:
            # Create session
            await manager.get_session("user-1")
            assert len(manager._sessions) == 1
            
            # Wait for session to expire and cleanup to run
            await asyncio.sleep(2)
            
            # Session should be cleaned up automatically
            assert len(manager._sessions) == 0
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_get_stats(self, session_manager):
        """Test getting statistics"""
        # Create some sessions
        await session_manager.get_session("user-1")
        await session_manager.get_session("user-2")
        await session_manager.get_session("user-1")  # Cache hit
        
        stats = session_manager.get_stats()
        
        assert stats["sessions_created"] == 2
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 2
        assert stats["active_sessions"] == 2
        assert stats["cache_hit_rate"] == pytest.approx(1/3, rel=0.01)
    
    @pytest.mark.asyncio
    async def test_shutdown_closes_all_sessions(self, temp_db):
        """Test shutdown closes all sessions"""
        manager = SessionManager(db_path=temp_db)
        await manager.start()
        
        # Create sessions
        await manager.get_session("user-1")
        await manager.get_session("user-2")
        await manager.get_session("user-3")
        
        assert len(manager._sessions) == 3
        
        # Shutdown
        await manager.shutdown()
        
        # All sessions should be closed
        assert len(manager._sessions) == 0
    
    @pytest.mark.asyncio
    async def test_concurrent_access(self, session_manager):
        """Test concurrent access to session manager"""
        # Create multiple tasks accessing sessions concurrently
        tasks = [
            session_manager.get_session(f"user-{i % 3}")
            for i in range(10)
        ]
        
        sessions = await asyncio.gather(*tasks)
        
        # Should have 3 unique sessions
        assert len(session_manager._sessions) == 3
        
        # All sessions with same ID should be same instance
        user_0_sessions = [s for s in sessions if s.session_id == "user-0"]
        assert all(s is user_0_sessions[0] for s in user_0_sessions)
    
    def test_session_metadata_update_access(self):
        """Test session metadata access tracking"""
        from agents import SQLiteSession
        
        session = SQLiteSession("test-session")
        metadata = SessionMetadata(
            session=session,
            created_at=time.time(),
            last_accessed=time.time(),
            access_count=0
        )
        
        initial_time = metadata.last_accessed
        initial_count = metadata.access_count
        
        # Wait a bit
        time.sleep(0.1)
        
        # Update access
        metadata.update_access()
        
        assert metadata.last_accessed > initial_time
        assert metadata.access_count == initial_count + 1

