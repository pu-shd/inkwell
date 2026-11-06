"""inkwell: Presidio-based scrubber for sensitive records."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("inkwell")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
