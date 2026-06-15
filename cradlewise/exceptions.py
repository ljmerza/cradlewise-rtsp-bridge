"""Exceptions for the Cradlewise API."""


class CradlewiseError(Exception):
    """Base exception for Cradlewise."""


class CradlewiseAuthError(CradlewiseError):
    """Authentication failed."""


class CradlewiseApiError(CradlewiseError):
    """API request failed."""
