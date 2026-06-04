"""Shell tool — re-exports BashTool for backward compatibility.

The full implementation lives in tools/BashTool/ with these modules:
  - __init__.py: BashTool class (main tool)
  - security.py: 23 security checks
  - permissions.py: Multi-layer permission analysis
  - sandbox.py: Platform sandbox isolation
  - sed_validation.py: sed command whitelist validation
  - path_validation.py: Path extraction and workspace boundary checks
  - command_semantics.py: Exit code interpretation
  - background.py: Background task management
"""

from .BashTool import BashTool
from .BashTool import BashInput as RunShellInput

# Backward-compatible alias.
RunShellTool = BashTool

__all__ = ["BashTool", "RunShellTool", "RunShellInput"]
