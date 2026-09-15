"""Where a subject's export archive lives, and how it is read back.

THE DEFECT THIS NAMES
---------------------
Until 0.7.0 the staging and archive roots defaulted to ``MEDIA_ROOT/gdpr/``.
The ordinary Django/nginx shape serves MEDIA_ROOT as static files —

    location /media { alias /media; add_header Cache-Control "public, max-age=2592000"; }

— so on a deployment that followed the ordinary shape, a ZIP holding
*everything the system knows about a person* sat at a guessable URL, served
without authentication and cacheable by every intermediary between the
origin and the reader. Nothing refused, nothing warned; the default was the
whole vulnerability. It was found by tracing storage destinations on a live
fleet, not by anything in this library.

There is a second, quieter half. The archive location was stored in
``DataExportRequest.archive_path`` as an ABSOLUTE FILESYSTEM PATH, and the
download view runs in a different process from the celery task that writes
the file. Two containers off the same image both have ``/app/...``, and
neither has the other's bytes: the subject is told the export is READY and
the download answers a 500. A path is only meaningful inside the process
that wrote it.

WHAT REPLACES IT
----------------
One store, addressed by KEY, with two properties the old shape could not
have:

* **It is not a served root.** The default is :func:`default_export_root` —
  ``BASE_DIR/private/gdpr``, or a subdirectory of the system temp directory
  when a deployment sets no ``BASE_DIR``. Never MEDIA_ROOT, never
  STATIC_ROOT, never derived from either. A deployment that points it back
  at one is refused at boot by ``gdpr.E013``
  (:func:`stapel_gdpr.checks.check_export_storage`).

* **It cannot produce a URL.** :class:`PrivateFileSystemStorage` raises on
  ``url()``. That is a mechanism, not a convention: ``FileSystemStorage``
  with ``base_url=None`` silently falls back to ``settings.MEDIA_URL``
  (``Storage._value_or_setting``), so a "private" store built the obvious
  way still hands out ``/media/<key>``. The archive is streamed through
  :class:`~stapel_gdpr.views.DataExportDownloadView`, which authenticates
  the subject, spends a single-use token and deletes the object as it
  serves. There is no URL to guess and no URL to leak.

A deployment whose web and worker processes do not share a filesystem
configures a real shared store under the :data:`EXPORT_STORAGE_ALIAS` key of
``settings.STORAGES`` — S3, GCS, a shared volume — and both processes resolve
the same key::

    STORAGES = {
        ...,
        "stapel_gdpr_exports": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {"bucket_name": "acme-private", "location": "gdpr",
                        "querystring_auth": True, "default_acl": "private"},
        },
    }

The library never hands a signed object-storage URL to a subject: a signed
URL is a bearer credential to the complete dump that cannot be revoked once
issued and lands in history, ``Referer`` and proxy logs, and it cannot be
made single-use. If a deployment must use one anyway (archives too large to
stream through the app), keep the signature lifetime at **60 seconds or
less** — the time between the click and the first byte, not the time between
the email and the click.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.files.storage import FileSystemStorage, InvalidStorageError, storages

from .conf import gdpr_settings

__all__ = [
    "EXPORT_STORAGE_ALIAS",
    "PrivateFileSystemStorage",
    "archive_key",
    "archive_root",
    "default_export_root",
    "delete_archive",
    "export_root",
    "export_root_is_explicit",
    "export_storage",
    "export_storage_is_shared",
    "open_archive",
    "secure_mkdir",
    "staging_root",
    "stored_archive_exists",
]

#: The ``settings.STORAGES`` alias a deployment configures when the export
#: archive must live somewhere both the worker and the web process can read.
EXPORT_STORAGE_ALIAS = "stapel_gdpr_exports"

#: Key prefix of a finished archive inside the store.
ARCHIVE_PREFIX = "exports"

#: Directory name of the per-request staging area under the export root.
#: Staging is always local: providers write plain files into it, and it is
#: removed the moment the ZIP exists.
STAGING_DIRNAME = "staging"


class PrivateFileSystemStorage(FileSystemStorage):
    """A filesystem store that has no public URL, by construction.

    ``FileSystemStorage(base_url=None)`` is NOT private: ``base_url`` falls
    back to ``settings.MEDIA_URL``, so ``url()`` returns ``/media/<key>`` —
    exactly the address this module exists to stop existing. Overriding
    ``url()`` is the only way to make "this file is never addressable" a
    property of the code rather than of a comment.
    """

    def url(self, name):  # noqa: D102 - refusal, not an implementation
        raise ValueError(
            "stapel-gdpr export archives have no URL: a personal-data export "
            "is streamed through the authenticated download endpoint "
            "(POST /gdpr/api/v1/user/data-export/download), which spends a "
            "single-use token and deletes the archive as it serves. If you "
            "need a URL, you are about to publish somebody's data."
        )


def secure_mkdir(path: Path) -> Path:
    """``mkdir -p`` with owner-only permissions (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def default_export_root() -> Path:
    """The root used when a deployment names nothing.

    A literal shape, never derived from a setting that a web server serves:
    ``BASE_DIR/private/gdpr`` when the project declares ``BASE_DIR`` (every
    ``startproject`` layout and every fleet settings module does), and a
    subdirectory of the system temp directory otherwise. An ordinary
    ``location /media`` or ``location /static`` block cannot reach either.

    It is a working default, not a good one: see ``gdpr.W013``. A deployment
    that runs its celery worker and its web process in separate containers
    has to name a shared root or a shared store, or the download 500s.
    """
    base = getattr(settings, "BASE_DIR", "") or ""
    if base:
        return Path(os.fspath(base)) / "private" / "gdpr"
    return Path(tempfile.gettempdir()) / "stapel-gdpr"


def _configured(*names: str) -> str:
    """First non-empty value among namespaced then flat settings."""
    for name in names:
        value = getattr(gdpr_settings, name, "") or getattr(settings, f"GDPR_{name}", "")
        if value:
            return str(value)
    return ""


def export_root_is_explicit() -> bool:
    """Did the deployment name a root itself?"""
    return bool(_configured("EXPORT_ROOT") or _configured("ARCHIVE_ROOT") or _configured("STAGING_ROOT"))


def export_root() -> Path:
    """The private root holding staging directories and finished archives."""
    configured = _configured("EXPORT_ROOT")
    if configured:
        return Path(configured)
    return default_export_root()


def archive_root() -> Path:
    """Where finished ZIPs land on a filesystem-backed store.

    ``ARCHIVE_ROOT`` (and its flat twin ``GDPR_ARCHIVE_ROOT``) still win when
    set: a deployment that already moved its archives off MEDIA_ROOT keeps
    working across the bump.
    """
    configured = _configured("ARCHIVE_ROOT")
    if configured:
        return Path(configured)
    return export_root() / ARCHIVE_PREFIX


def staging_root() -> Path:
    """Where per-request staging directories are built."""
    configured = _configured("STAGING_ROOT")
    if configured:
        return Path(configured)
    return export_root() / STAGING_DIRNAME


def _alias_storage():
    try:
        return storages[EXPORT_STORAGE_ALIAS]
    except InvalidStorageError:
        return None


def export_storage_is_shared() -> bool:
    """Is the store something both processes can reach by key?"""
    return _alias_storage() is not None


def export_storage():
    """The store archives are written to and streamed from.

    ``settings.STORAGES["stapel_gdpr_exports"]`` when a deployment configured
    one, otherwise a :class:`PrivateFileSystemStorage` rooted at
    :func:`archive_root`. Either way the key stored on the request row is
    resolvable from any process that shares the store — which an absolute
    filesystem path never was.
    """
    aliased = _alias_storage()
    if aliased is not None:
        return aliased
    root = secure_mkdir(archive_root())
    return PrivateFileSystemStorage(
        location=str(root),
        file_permissions_mode=0o600,
        directory_permissions_mode=0o700,
    )


def archive_key(correlation_id: str, request_id) -> str:
    """The store key for one request's archive.

    The correlation id, not the request id, carries the entropy: the request
    id is public (it rides in ``user.export_ready`` and in the API), the
    correlation id is a uuid4 nothing hands out. The key is never a URL, so
    this is defence in depth against a deployment that later points a store
    at something addressable — not the thing keeping the archive private.
    """
    return f"{ARCHIVE_PREFIX}/{correlation_id}/export_{request_id}.zip"


def _is_legacy_path(stored: str) -> bool:
    """A value written before 0.7.0: an absolute filesystem path."""
    return bool(stored) and os.path.isabs(stored)


def stored_archive_exists(stored: Optional[str]) -> bool:
    """Is the archive named by ``DataExportRequest.archive_path`` there?"""
    if not stored:
        return False
    if _is_legacy_path(stored):
        return os.path.exists(stored)
    try:
        return export_storage().exists(stored)
    except (OSError, ValueError, NotImplementedError):
        return False


def open_archive(stored: str):
    """Open the archive for reading. Caller closes the handle."""
    if _is_legacy_path(stored):
        return open(stored, "rb")
    return export_storage().open(stored, "rb")


def delete_archive(stored: Optional[str]) -> bool:
    """Remove the archive. Returns whether anything was deleted.

    Never raises: both callers (the download view serving its last byte and
    the retention sweep) must carry on when the object is already gone.
    """
    if not stored:
        return False
    try:
        if _is_legacy_path(stored):
            if os.path.exists(stored):
                os.remove(stored)
                return True
            return False
        storage = export_storage()
        if storage.exists(stored):
            storage.delete(stored)
            return True
    except (OSError, ValueError, NotImplementedError):
        return False
    return False


def save_archive(key: str, source: Path) -> str:
    """Put *source* into the store under *key*; returns the stored key."""
    from django.core.files import File

    storage = export_storage()
    with source.open("rb") as fh:
        return storage.save(key, File(fh))
