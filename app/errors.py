from __future__ import annotations


class DownloaderCoreError(RuntimeError):
    pass


class AuthenticationRequiredError(DownloaderCoreError):
    def __init__(self, message: str, verification_url: str | None = None) -> None:
        super().__init__(message)
        self.verification_url = verification_url


class DownloadCancelledError(DownloaderCoreError):
    pass


class DiscoveryError(DownloaderCoreError):
    pass


class TemporaryAccessError(DownloaderCoreError):
    pass


class DouyinMediaRefreshRequiredError(TemporaryAccessError):
    """A verified Douyin source must be rediscovered before one safe retry."""


class MediaDownloadError(DownloaderCoreError):
    pass
