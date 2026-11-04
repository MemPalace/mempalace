"""Optional machine runner extension."""

from .api import install
from .tools import register_tools

__all__ = ["install"]

register_tools()
