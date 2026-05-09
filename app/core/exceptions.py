# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Custom exceptions for the middleware."""


class MiddlewareException(Exception):
    """Base exception for middleware errors."""

    pass


class AgentNotFoundError(MiddlewareException):
    """Agent configuration not found in Firestore."""

    pass


class SessionError(MiddlewareException):
    """Error creating or retrieving session."""

    pass


class VertexAIError(MiddlewareException):
    """Error communicating with Vertex AI."""

    pass


class SlackAPIError(MiddlewareException):
    """Error communicating with Slack API."""

    pass


class ResourceExhaustedError(MiddlewareException):
    """Google API rate limit exceeded (429 RESOURCE_EXHAUSTED)."""

    pass


class FileDownloadError(MiddlewareException):
    """Failed to download a user-uploaded file from the source platform."""

    pass


class FileTooLargeError(MiddlewareException):
    """User-uploaded file exceeds the configured size limit."""

    pass


class UnsupportedImageTypeError(MiddlewareException):
    """User-uploaded image MIME type is not in the configured allowlist."""

    pass


class GcsUploadError(MiddlewareException):
    """Failed to upload a file to Google Cloud Storage."""

    pass


class AgentStreamError(MiddlewareException):
    """Vertex AI streaming response broke mid-flight (not a clean empty result)."""

    pass
