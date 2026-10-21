"""Base memory provider interface."""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any


class BaseMemoryProvider(ABC):
    """Abstract base class for memory providers."""
    
    @abstractmethod
    def store(self, key: str, value: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Store a key-value pair with optional metadata."""
        pass
    
    @abstractmethod
    def retrieve(self, key: str) -> Optional[str]:
        """Retrieve a value by key."""
        pass
    
    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search for memories matching the query."""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a memory by key."""
        pass
    
    @abstractmethod
    def close(self) -> None:
        """Close the memory provider connection."""
        pass