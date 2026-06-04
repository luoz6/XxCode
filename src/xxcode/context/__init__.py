"""Four-level progressive compression pipeline for context management."""

from .builder import build_system_prompt
from .pipeline import CompressionStats, ContextPipeline

__all__ = ["ContextPipeline", "CompressionStats", "build_system_prompt"]
