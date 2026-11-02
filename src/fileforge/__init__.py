"""FileForge - deterministic file processing tools for chat users.

The package is split in two layers:

- ``fileforge.tools``: pure tool functions (no MCP dependency). They take
  local file paths, write results into the output directory and return a
  plain dict describing the produced file. This layer is what the self
  tests exercise directly.
- ``fileforge.server``: a thin FastMCP (streamable-http) transport layer
  that registers the tool functions as MCP tools.
"""

__version__ = "0.1.0"
