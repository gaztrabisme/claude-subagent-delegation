"""Additional platform privacy checks for files containing caller credentials."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

# Darwin sys/acl.h. Metadata-only permissions do not expose token contents.
_ACL_TYPE_EXTENDED = 0x100
_ACL_EXTENDED_ALLOW = 1
_ACL_EXTENDED_DENY = 2
_ACL_MAX_ENTRIES = 128
_PRIVATE_PERMISSIONS = (1 << 1) | (1 << 2) | (1 << 5) | (1 << 12) | (1 << 13)
_CHECK_ERROR = "epi token_file ACL privacy could not be verified"
_GRANT_ERROR = "epi token_file ACL grants non-owner read or write access"


def _darwin_acl_api() -> ctypes.CDLL:
    """Bind the native ABI explicitly, especially pointer-returning functions."""
    api = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    pointer = ctypes.c_void_p
    signatures = {
        "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], pointer),
        "acl_valid": ([pointer], ctypes.c_int),
        "acl_get_entry": ([pointer, ctypes.c_int, ctypes.POINTER(pointer)], ctypes.c_int),
        "acl_get_tag_type": ([pointer, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "acl_get_permset_mask_np": ([pointer, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
        "acl_get_qualifier": ([pointer], pointer),
        "mbr_uuid_to_id": ([pointer, ctypes.POINTER(ctypes.c_uint32),
                            ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "acl_free": ([pointer], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = result
    return api


def _darwin_privacy_error(path: Path) -> str | None:
    api = _darwin_acl_api()
    # Opening metadata without following a symlink pins the inode during ACL
    # inspection. This does not read the token value.
    descriptor = os.open(path, os.O_EVTONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return _CHECK_ERROR
        ctypes.set_errno(0)
        acl = api.acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
        # Darwin FILESEC_ACL reports ENOENT when the existing inode has no ACL.
        # The open descriptor makes this distinct from a missing token path.
        if not acl:
            return None if ctypes.get_errno() == errno.ENOENT else _CHECK_ERROR
        return _acl_privacy_error(api, acl, info.st_uid)
    finally:
        os.close(descriptor)


def _acl_privacy_error(api: ctypes.CDLL, acl: int, owner: int) -> str | None:
    try:
        if api.acl_valid(acl) != 0:
            return _CHECK_ERROR
        # macOS uses -1/EINVAL for end-of-list, rather than the Linux convention.
        # The ACL was validated above; errors with any other errno fail closed.
        for index in range(_ACL_MAX_ENTRIES + 1):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = api.acl_get_entry(acl, index, ctypes.byref(entry))
            if result == -1 and ctypes.get_errno() == errno.EINVAL:
                return None
            if result != 0 or not entry.value or index == _ACL_MAX_ENTRIES:
                return _CHECK_ERROR
            tag = ctypes.c_int()
            if api.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                return _CHECK_ERROR
            if tag.value == _ACL_EXTENDED_DENY:
                continue
            if tag.value != _ACL_EXTENDED_ALLOW:
                return _CHECK_ERROR
            permissions = ctypes.c_uint64()
            if api.acl_get_permset_mask_np(entry, ctypes.byref(permissions)) != 0:
                return _CHECK_ERROR
            if not permissions.value & _PRIVATE_PERMISSIONS:
                continue
            # Inherited allow entries are checked exactly like explicit ones.
            qualifier = api.acl_get_qualifier(entry)
            if not qualifier:
                return _CHECK_ERROR
            try:
                identity = ctypes.c_uint32()
                kind = ctypes.c_int()
                if api.mbr_uuid_to_id(qualifier, ctypes.byref(identity),
                                      ctypes.byref(kind)) != 0:
                    return _CHECK_ERROR
                # membership.h: ID_TYPE_UID is 0; a group is never owner-only.
                if kind.value != 0 or identity.value != owner:
                    return _GRANT_ERROR
            finally:
                api.acl_free(qualifier)
    finally:
        api.acl_free(acl)
    return _CHECK_ERROR


def token_privacy_error(path: Path) -> str | None:
    """Check ACL privacy after the caller's regular-file/mode/readability checks.

    On macOS, reject non-owner data or credential-control grants, including
    inherited grants. A deny entry does not make a later allow entry safe: do
    not attempt to infer the effective permissions of every possible identity.
    Other platforms retain the caller's mode-only checks.
    """
    if sys.platform != "darwin":
        return None
    try:
        return _darwin_privacy_error(path)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return _CHECK_ERROR
