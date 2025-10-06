"""
Microsoft Graph API client for SharePoint operations
Handles authentication, file uploads, and metadata management
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class GraphConfig:
    """Microsoft Graph API configuration"""

    client_id: str
    client_secret: str
    tenant_id: str
    scope: str = "https://graph.microsoft.com/.default"
    authority: str = "https://login.microsoftonline.com"


class GraphAPIClient:
    """
    Microsoft Graph API client for SharePoint operations
    Handles authentication, rate limiting, and SharePoint-specific operations
    """

    def __init__(self, config: GraphConfig | None = None):
        self.config = config or self._load_config_from_env()
        self.access_token: str | None = None
        self.token_expires_at: float = 0
        self.session: aiohttp.ClientSession | None = None

        # Rate limiting (Microsoft Graph: 10,000 requests per 10 minutes per tenant)
        self.rate_limit_requests = 10000
        self.rate_limit_window = 600  # 10 minutes
        self.request_timestamps: list[float] = []

    def _load_config_from_env(self) -> GraphConfig:
        """Load configuration from environment variables"""
        import os

        return GraphConfig(
            client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
            tenant_id=os.getenv("MICROSOFT_TENANT_ID", ""),
        )

    async def initialize(self) -> None:
        """Initialize HTTP session and authenticate"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        await self._authenticate()
        logger.info("Graph API client initialized")

    async def close(self) -> None:
        """Close HTTP session"""
        if self.session:
            await self.session.close()
        logger.info("Graph API client closed")

    async def _authenticate(self) -> None:
        """Authenticate with Microsoft Graph API"""
        auth_url = f"{self.config.authority}/{self.config.tenant_id}/oauth2/v2.0/token"

        data = {
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "scope": self.config.scope,
            "grant_type": "client_credentials",
        }

        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.post(auth_url, data=data) as response:
            if response.status == 200:
                token_data = await response.json()
                self.access_token = token_data["access_token"]
                expires_in = token_data.get("expires_in", 3600)
                self.token_expires_at = time.time() + expires_in - 300  # 5 min buffer

                logger.info("Successfully authenticated with Microsoft Graph API")
            else:
                error_text = await response.text()
                raise Exception(f"Authentication failed: {response.status} - {error_text}")

    async def _ensure_authenticated(self) -> None:
        """Ensure we have a valid access token"""
        if not self.access_token or time.time() >= self.token_expires_at:
            await self._authenticate()

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting"""
        current_time = time.time()

        # Remove old timestamps outside the window
        self.request_timestamps = [
            ts for ts in self.request_timestamps if current_time - ts < self.rate_limit_window
        ]

        # Check if we're at the limit
        if len(self.request_timestamps) >= self.rate_limit_requests:
            sleep_time = self.rate_limit_window - (current_time - self.request_timestamps[0])
            if sleep_time > 0:
                logger.warning(f"Rate limit reached, sleeping for {sleep_time:.1f}s")
                await asyncio.sleep(sleep_time)

        # Record this request
        self.request_timestamps.append(current_time)

    async def _make_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Make authenticated request to Microsoft Graph API"""
        await self._ensure_authenticated()
        await self._check_rate_limit()

        headers = kwargs.get("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        headers["Content-Type"] = "application/json"
        kwargs["headers"] = headers

        if self.session is None:
            self.session = aiohttp.ClientSession()
        async with self.session.request(method, url, **kwargs) as response:
            response_text = await response.text()

            if response.status == 200 or response.status == 201:
                try:
                    return await response.json() if response_text else {}
                except json.JSONDecodeError:
                    return {"raw_response": response_text}
            else:
                logger.error(
                    "Graph API request failed",
                    method=method,
                    url=url,
                    status=response.status,
                    response=response_text,
                )
                raise Exception(f"Graph API request failed: {response.status} - {response_text}")

    async def get_site_id(self, site_url: str) -> str:
        """Get SharePoint site ID from URL"""
        # Extract hostname and site path from URL
        from urllib.parse import urlparse

        parsed = urlparse(site_url)
        hostname = parsed.hostname
        site_path = parsed.path.strip("/")

        url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:/{site_path}"

        response = await self._make_request("GET", url)
        return str(response["id"])

    async def get_library_id(self, site_id: str, library_name: str) -> str:
        """Get document library ID by name"""
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists"

        response = await self._make_request("GET", url)

        for library in response.get("value", []):
            if library.get("displayName") == library_name:
                return str(library["id"])

        raise Exception(f"Library '{library_name}' not found")

    async def get_library_schema(self, site_url: str, library_name: str) -> dict[str, Any]:
        """Get complete library schema with fields and choices"""
        site_id = await self.get_site_id(site_url)
        library_id = await self.get_library_id(site_id, library_name)

        # Get library columns/fields
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/lists/{library_id}/columns"

        response = await self._make_request("GET", url)

        fields = []
        for column in response.get("value", []):
            field_info = {
                "internal_name": column.get("name", ""),
                "title": column.get("displayName", ""),
                "type": self._map_column_type(column),
                "required": column.get("required", False),
                "description": column.get("description", ""),
                "choices": self._extract_choices(column),
            }
            fields.append(field_info)

        return {
            "site_id": site_id,
            "library_id": library_id,
            "library_name": library_name,
            "fields": fields,
        }

    def _map_column_type(self, column: dict[str, Any]) -> str:
        """Map Graph API column type to our internal type"""
        if "text" in column:
            return "Text"
        elif "note" in column:
            return "Note"
        elif "choice" in column:
            return "MultiChoice" if column["choice"].get("allowMultipleValues") else "Choice"
        elif "number" in column:
            return "Number"
        elif "dateTime" in column:
            return "DateTime"
        elif "boolean" in column:
            return "Boolean"
        else:
            return "Unknown"

    def _extract_choices(self, column: dict[str, Any]) -> list[str]:
        """Extract choice values from column definition"""
        if "choice" in column:
            choices = column["choice"].get("choices", [])
            return [str(choice) for choice in choices]
        return []

    async def _upload_small_file(
        self,
        site_id: str,
        library_id: str,
        file_path: str,
        file_content: bytes,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Upload small file (< 4MB) using simple upload

        Args:
            site_id: SharePoint site ID
            library_id: Library (list) ID
            file_path: Path within the library
            file_content: File content as bytes
            overwrite: Whether to overwrite existing file

        Returns:
            Upload result from Graph API
        """
        # Construct upload URL
        # Using drive API: /sites/{site-id}/lists/{list-id}/drive/root:/{file-path}:/content
        upload_url = (
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
            f"lists/{library_id}/drive/root:/{file_path}:/content"
        )

        # Add conflict behavior parameter
        if overwrite:
            upload_url += "?@microsoft.graph.conflictBehavior=replace"
        else:
            upload_url += "?@microsoft.graph.conflictBehavior=fail"

        # Make PUT request with file content
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/octet-stream",
        }

        await self._ensure_authenticated()
        await self._check_rate_limit()

        if self.session is None:
            self.session = aiohttp.ClientSession()

        async with self.session.put(upload_url, data=file_content, headers=headers) as response:
            if response.status in (200, 201):
                result = await response.json()
                return result
            else:
                error_text = await response.text()
                raise Exception(f"File upload failed: {response.status} - {error_text}")

    async def _upload_large_file(
        self,
        site_id: str,
        library_id: str,
        file_path: str,
        file_content: bytes,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Upload large file (>= 4MB) using upload session

        Args:
            site_id: SharePoint site ID
            library_id: Library (list) ID
            file_path: Path within the library
            file_content: File content as bytes
            overwrite: Whether to overwrite existing file

        Returns:
            Upload result from Graph API
        """
        # Create upload session
        session_url = (
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
            f"lists/{library_id}/drive/root:/{file_path}:/createUploadSession"
        )

        session_data = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace" if overwrite else "fail"
            }
        }

        session_response = await self._make_request("POST", session_url, json=session_data)
        upload_url = session_response["uploadUrl"]

        # Upload file in chunks (10MB chunks recommended by Microsoft)
        chunk_size = 10 * 1024 * 1024  # 10MB
        file_size = len(file_content)

        for start in range(0, file_size, chunk_size):
            end = min(start + chunk_size, file_size)
            chunk = file_content[start:end]

            # Upload chunk
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end-1}/{file_size}",
            }

            if self.session is None:
                self.session = aiohttp.ClientSession()

            async with self.session.put(upload_url, data=chunk, headers=headers) as response:
                if response.status in (200, 201, 202):
                    if end == file_size:
                        # Last chunk - return result
                        result = await response.json()
                        return result
                    # Continue with next chunk
                else:
                    error_text = await response.text()
                    raise Exception(f"Chunk upload failed: {response.status} - {error_text}")

        raise Exception("Upload session completed but no result returned")

    async def upload_file(
        self,
        library_name: str,
        file_path: str,
        folder_path: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """
        Upload file to SharePoint library using Microsoft Graph API

        Args:
            library_name: Name of the SharePoint library
            file_path: Local path to the file to upload
            folder_path: Optional folder path within the library
            overwrite: Whether to overwrite existing file

        Returns:
            Dictionary with upload result including file_id and sharepoint_url
        """
        try:
            file_name = Path(file_path).name

            # Read file content
            with open(file_path, 'rb') as f:
                file_content = f.read()

            file_size = len(file_content)

            # Get site and library IDs
            # Note: This assumes site_url is stored in config or passed separately
            # For now, we'll use a placeholder that should be configured
            site_url = "https://company.sharepoint.com/sites/erni"  # TODO: Get from config
            site_id = await self.get_site_id(site_url)
            library_id = await self.get_library_id(site_id, library_name)

            # Construct upload path
            if folder_path:
                upload_path = f"{folder_path}/{file_name}"
            else:
                upload_path = file_name

            # Use different upload method based on file size
            # Small files (< 4MB): Simple upload
            # Large files (>= 4MB): Upload session
            if file_size < 4 * 1024 * 1024:  # 4MB threshold
                result = await self._upload_small_file(
                    site_id, library_id, upload_path, file_content, overwrite
                )
            else:
                result = await self._upload_large_file(
                    site_id, library_id, upload_path, file_content, overwrite
                )

            logger.info(
                "File uploaded successfully",
                file_path=file_path,
                file_id=result["id"],
                library_name=library_name,
                size_bytes=file_size,
            )

            return {
                "success": True,
                "file_id": result["id"],
                "sharepoint_url": result["webUrl"],
                "file_name": file_name,
                "size_bytes": file_size,
            }

        except FileNotFoundError:
            logger.error("File not found", file_path=file_path)
            return {"success": False, "error": f"File not found: {file_path}"}
        except Exception as e:
            logger.error("File upload failed", file_path=file_path, error=str(e))
            return {"success": False, "error": str(e)}

    async def update_file_metadata(
        self, library_name: str, file_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Update file metadata in SharePoint using Microsoft Graph API

        Args:
            library_name: Name of the SharePoint library
            file_id: ID of the file to update
            metadata: Dictionary of metadata fields to update

        Returns:
            Dictionary with update result
        """
        try:
            # Get site ID (assuming site_url is configured)
            site_url = "https://company.sharepoint.com/sites/erni"  # TODO: Get from config
            site_id = await self.get_site_id(site_url)
            library_id = await self.get_library_id(site_id, library_name)

            # Update file metadata using listItem endpoint
            # /sites/{site-id}/lists/{list-id}/items/{item-id}/fields
            update_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
                f"lists/{library_id}/items/{file_id}/fields"
            )

            # Prepare metadata payload
            # SharePoint expects fields in specific format
            fields_payload = {}
            for field_name, value in metadata.items():
                if value is not None:
                    fields_payload[field_name] = value

            # Make PATCH request to update metadata
            result = await self._make_request("PATCH", update_url, json=fields_payload)

            logger.info(
                "File metadata updated successfully",
                file_id=file_id,
                metadata_fields=len(metadata),
                library_name=library_name,
            )

            return {
                "success": True,
                "updated_fields": list(metadata.keys()),
                "field_count": len(metadata),
            }

        except Exception as e:
            logger.error("Metadata update failed", file_id=file_id, error=str(e))
            return {"success": False, "error": str(e)}

    async def get_library_statistics(self, library_name: str, days: int = 7) -> dict[str, Any]:
        """
        Get library upload statistics from SharePoint using Microsoft Graph API

        Args:
            library_name: Name of the SharePoint library
            days: Number of days to look back for recent uploads

        Returns:
            Dictionary with library statistics
        """
        try:
            # Get site and library IDs
            site_url = "https://company.sharepoint.com/sites/erni"  # TODO: Get from config
            site_id = await self.get_site_id(site_url)
            library_id = await self.get_library_id(site_id, library_name)

            # Get total file count
            items_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
                f"lists/{library_id}/items?$count=true&$top=1"
            )
            items_response = await self._make_request("GET", items_url)
            total_files = items_response.get("@odata.count", 0)

            # Get recent uploads (files created in last N days)
            from datetime import datetime, timedelta
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            cutoff_iso = cutoff_date.isoformat() + "Z"

            recent_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
                f"lists/{library_id}/items?"
                f"$filter=fields/Created ge '{cutoff_iso}'&$count=true&$top=1"
            )
            recent_response = await self._make_request("GET", recent_url)
            recent_uploads = recent_response.get("@odata.count", 0)

            # Get storage usage from drive
            drive_url = (
                f"https://graph.microsoft.com/v1.0/sites/{site_id}/"
                f"lists/{library_id}/drive"
            )
            drive_response = await self._make_request("GET", drive_url)
            storage_used_bytes = drive_response.get("quota", {}).get("used", 0)
            storage_used_mb = storage_used_bytes / (1024 * 1024)

            stats = {
                "total_files": total_files,
                "recent_uploads": recent_uploads,
                "avg_uploads_per_day": recent_uploads / days if days > 0 else 0,
                "storage_used_mb": round(storage_used_mb, 2),
                "storage_used_bytes": storage_used_bytes,
            }

            logger.info(
                "Library statistics retrieved",
                library_name=library_name,
                total_files=total_files,
                recent_uploads=recent_uploads,
                storage_mb=stats["storage_used_mb"],
            )

            return stats

        except Exception as e:
            logger.error(
                "Failed to get library statistics", library_name=library_name, error=str(e)
            )
            return {"error": str(e), "total_files": 0, "recent_uploads": 0}

    async def health_check(self) -> dict[str, Any]:
        """Perform health check of Graph API connectivity"""
        try:
            await self._ensure_authenticated()

            # Test basic API connectivity
            url = "https://graph.microsoft.com/v1.0/me"
            await self._make_request("GET", url)

            return {
                "status": "healthy",
                "authenticated": True,
                "rate_limit_remaining": self.rate_limit_requests - len(self.request_timestamps),
                "token_expires_in": max(0, self.token_expires_at - time.time()),
            }

        except Exception as e:
            logger.error("Graph API health check failed", error=str(e))
            return {"status": "unhealthy", "authenticated": False, "error": str(e)}


# Global client instance
_graph_client: GraphAPIClient | None = None


def get_graph_client() -> GraphAPIClient:
    """Get global Graph API client instance"""
    global _graph_client
    if _graph_client is None:
        _graph_client = GraphAPIClient()
    return _graph_client


async def init_graph_client(config: GraphConfig | None = None) -> GraphAPIClient:
    """Initialize global Graph API client"""
    global _graph_client
    _graph_client = GraphAPIClient(config)
    await _graph_client.initialize()
    return _graph_client
