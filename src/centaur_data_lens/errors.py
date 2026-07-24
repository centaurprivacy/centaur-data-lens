"""User-safe exception hierarchy."""


class DataLensError(Exception):
    """Base error whose message is safe to display without a traceback."""


class ArchiveSafetyError(DataLensError):
    """The input violates an archive safety boundary."""


class UnsupportedExportError(DataLensError):
    """The selected export format or category is unsupported."""


class PlatformMismatchError(DataLensError):
    """The archive does not match the platform selected by the user."""


class ModelAdapterError(DataLensError):
    """An optional model adapter failed safely."""
