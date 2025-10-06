"""Secure secret management for API keys and sensitive data."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet
from structlog import get_logger

logger = get_logger(__name__)


class SecretManager:
    """Manages encrypted storage and retrieval of sensitive data."""
    
    def __init__(self, master_key: Optional[str] = None):
        """Initialize SecretManager with encryption key.
        
        Args:
            master_key: Optional master key. If not provided, generates one from environment.
        """
        self._cipher = self._initialize_cipher(master_key)
        self._secrets_cache: Dict[str, bytes] = {}
        self._redacted_keys = {
            "api_key", "secret", "password", "token", "key", "client_secret",
            "private_key", "cert", "credential", "auth", "pwd", "pass"
        }
    
    def _initialize_cipher(self, master_key: Optional[str] = None) -> Fernet:
        """
        Initialize Fernet cipher with master key.

        Security requirements:
        - Production: ERNI_MASTER_KEY environment variable is REQUIRED
        - Development: Random key generated with warning (not persistent)

        Args:
            master_key: Optional master key to use directly

        Returns:
            Initialized Fernet cipher

        Raises:
            ValueError: If master key is not provided in production environment
        """
        if master_key:
            # Use provided master key
            key_bytes = master_key.encode('utf-8')
        else:
            # Check environment variable
            env_key = os.getenv("ERNI_MASTER_KEY")

            if not env_key:
                # Determine environment
                environment = os.getenv("ENVIRONMENT", "development").lower()

                if environment == "production":
                    # CRITICAL: Master key is required in production
                    raise ValueError(
                        "ERNI_MASTER_KEY environment variable is required in production. "
                        "Generate a secure key with: "
                        "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
                    )

                # Development only: generate random key and warn
                logger.warning(
                    "⚠️  SECURITY WARNING: Using randomly generated master key. "
                    "Secrets will NOT persist across restarts. "
                    "Set ERNI_MASTER_KEY environment variable for production use."
                )

                # Generate cryptographically secure random key
                dev_key = Fernet.generate_key()
                return Fernet(dev_key)

            key_bytes = env_key.encode('utf-8')

        # Validate and create Fernet-compatible key
        try:
            # Create Fernet-compatible key (32 bytes, base64 encoded)
            key_hash = hashlib.sha256(key_bytes).digest()
            fernet_key = base64.urlsafe_b64encode(key_hash)

            # Validate by creating Fernet instance
            cipher = Fernet(fernet_key)

            logger.info("Fernet cipher initialized successfully")
            return cipher

        except Exception as e:
            raise ValueError(f"Invalid master key format: {e}") from e
    
    def store_secret(self, key: str, value: str) -> None:
        """Store a secret in encrypted form.
        
        Args:
            key: Secret identifier
            value: Secret value to encrypt and store
        """
        if not value:
            logger.warning(f"Attempting to store empty secret for key: {self._redact_key(key)}")
            return
        
        try:
            encrypted_value = self._cipher.encrypt(value.encode('utf-8'))
            self._secrets_cache[key] = encrypted_value
            logger.debug(f"Secret stored successfully for key: {self._redact_key(key)}")
        except Exception as e:
            logger.error(f"Failed to store secret for key {self._redact_key(key)}: {str(e)}")
            raise
    
    def get_secret(self, key: str) -> Optional[str]:
        """Retrieve and decrypt a secret.
        
        Args:
            key: Secret identifier
            
        Returns:
            Decrypted secret value or None if not found
        """
        if key not in self._secrets_cache:
            logger.warning(f"Secret not found for key: {self._redact_key(key)}")
            return None
        
        try:
            encrypted_value = self._secrets_cache[key]
            decrypted_bytes = self._cipher.decrypt(encrypted_value)
            return decrypted_bytes.decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to decrypt secret for key {self._redact_key(key)}: {str(e)}")
            return None
    
    def load_from_environment(self, env_mappings: Dict[str, str]) -> None:
        """Load secrets from environment variables.
        
        Args:
            env_mappings: Dict mapping secret keys to environment variable names
        """
        for secret_key, env_var in env_mappings.items():
            env_value = os.getenv(env_var)
            if env_value:
                self.store_secret(secret_key, env_value)
                logger.debug(f"Loaded secret from environment: {env_var} -> {self._redact_key(secret_key)}")
            else:
                logger.warning(f"Environment variable {env_var} not found for secret {self._redact_key(secret_key)}")
    
    def clear_secret(self, key: str) -> None:
        """Remove a secret from memory.
        
        Args:
            key: Secret identifier to remove
        """
        if key in self._secrets_cache:
            # Overwrite with random data before deletion
            self._secrets_cache[key] = secrets.token_bytes(64)
            del self._secrets_cache[key]
            logger.debug(f"Secret cleared for key: {self._redact_key(key)}")
    
    def clear_all_secrets(self) -> None:
        """Clear all secrets from memory."""
        keys_to_clear = list(self._secrets_cache.keys())
        for key in keys_to_clear:
            self.clear_secret(key)
        logger.info("All secrets cleared from memory")
    
    def _redact_key(self, key: str) -> str:
        """Redact sensitive parts of secret keys for logging.
        
        Args:
            key: Secret key to redact
            
        Returns:
            Redacted key safe for logging
        """
        if any(sensitive in key.lower() for sensitive in self._redacted_keys):
            if len(key) <= 4:
                return "***"
            return f"{key[:2]}***{key[-2:]}"
        return key
    
    def get_redacted_config(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Return configuration with sensitive values redacted for logging.
        
        Args:
            config_dict: Configuration dictionary
            
        Returns:
            Dictionary with sensitive values redacted
        """
        redacted = {}
        for key, value in config_dict.items():
            if any(sensitive in key.lower() for sensitive in self._redacted_keys):
                redacted[key] = "***REDACTED***"
            elif isinstance(value, dict):
                redacted[key] = self.get_redacted_config(value)
            else:
                redacted[key] = value
        return redacted
    
    def __del__(self):
        """Cleanup secrets on destruction."""
        try:
            self.clear_all_secrets()
        except Exception:
            # Ignore errors during cleanup
            pass


# Global instance for application use
_secret_manager: Optional[SecretManager] = None


def get_secret_manager() -> SecretManager:
    """Get or create global SecretManager instance."""
    global _secret_manager
    if _secret_manager is None:
        _secret_manager = SecretManager()
    return _secret_manager


def initialize_secrets_from_env() -> SecretManager:
    """Initialize SecretManager with common environment variables."""
    secret_manager = get_secret_manager()
    
    # Define mapping of secret keys to environment variables
    env_mappings = {
        "openai_api_key": "OPENAI_API_KEY",
        "microsoft_client_id": "MICROSOFT_CLIENT_ID", 
        "microsoft_client_secret": "MICROSOFT_CLIENT_SECRET",
        "microsoft_tenant_id": "MICROSOFT_TENANT_ID",
        "redis_password": "REDIS_PASSWORD",
        "postgres_password": "POSTGRES_PASSWORD",
        "jwt_secret": "JWT_SECRET_KEY",
    }
    
    secret_manager.load_from_environment(env_mappings)
    logger.info("Secrets initialized from environment variables")
    return secret_manager
