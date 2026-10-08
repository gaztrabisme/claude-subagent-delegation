"""A POSIX-private caller token must also be private under macOS ACLs."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from subagent.providers import private_files
from subagent.providers.epi import EPI_PROVIDER

from .conftest import make_settings

CANARY = "test-token-canary"
FAKE = Path(__file__).parent / "fakes" / "fake_epi"


def _add_acl(path: Path, grant: str) -> None:
    result = subprocess.run(
        ["/bin/chmod", "+a", grant, str(path)],
        capture_output=True, timeout=5,
    )
    if result.returncode:
        pytest.skip("The temporary filesystem does not support test ACL creation")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL regression")
@pytest.mark.parametrize("permission", ["read", "write"])
@pytest.mark.parametrize("inherited", [False, True], ids=["explicit", "inherited"])
def test_boot_refuses_acl_readable_token(tmp_path, permission, inherited):
    folder = tmp_path / "callers"
    folder.mkdir()
    if inherited:
        _add_acl(folder, f"everyone allow {permission},file_inherit")
    token = folder / "caller.token"
    token.write_text(CANARY + "\n")
    token.chmod(0o600)
    if not inherited:
        _add_acl(token, f"everyone allow {permission}")
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    settings = make_settings(tmp_path, providers={"epi": {
        "driver": "epi", "binary": str(FAKE), "token_file": str(token),
        "experimental": True, "local": True,
    }})

    error, _ = EPI_PROVIDER.boot(settings, "acl-probe", settings.provider())

    assert error is not None, "A non-owner ACL grant must fail token privacy validation"
    assert "token_file" in error
    assert "non-owner" in error
    assert CANARY not in error


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL regression")
@pytest.mark.parametrize("grant", ["none", "owner", "deny", "metadata"])
def test_native_acl_accepts_private_token(tmp_path, grant):
    import pwd

    token = tmp_path / "caller.token"
    token.write_text(CANARY + "\n")
    token.chmod(0o600)
    grants = {
        "owner": f"user:{pwd.getpwuid(token.stat().st_uid).pw_name} allow read,write",
        "deny": "everyone deny write",
        "metadata": "everyone allow readattr,readsecurity",
    }
    if grant != "none":
        _add_acl(token, grants[grant])

    assert private_files.token_privacy_error(token) is None


class FakeAclApi:
    """A small Darwin-ABI fake, including its unusual end-of-list convention."""

    def __init__(self, entries=(), *, failure=None, no_acl=False):
        self.entries = entries
        self.failure = failure
        self.no_acl = no_acl
        self.freed = []

    def acl_get_fd_np(self, descriptor, acl_type):
        assert os.fstat(descriptor).st_size == len(CANARY + "\n")
        assert acl_type == 0x100
        if self.failure == "get_acl" or self.no_acl:
            ctypes.set_errno(errno.EACCES if self.failure else errno.ENOENT)
            return None
        return 42

    def acl_valid(self, acl):
        assert acl == 42
        return -1 if self.failure == "valid" else 0

    def acl_get_entry(self, acl, index, output):
        if self.failure == "get_entry":
            ctypes.set_errno(errno.EIO)
            return -1
        if index == len(self.entries):
            ctypes.set_errno(errno.EINVAL)
            return -1
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = index + 1
        return 0

    def acl_get_tag_type(self, entry, output):
        if self.failure == "tag":
            return -1
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = self.entries[entry.value - 1][0]
        return 0

    def acl_get_permset_mask_np(self, entry, output):
        if self.failure == "permissions":
            return -1
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = self.entries[entry.value - 1][1]
        return 0

    def acl_get_qualifier(self, entry):
        return None if self.failure == "qualifier" else 1000 + entry.value

    def mbr_uuid_to_id(self, qualifier, identity, kind):
        if self.failure == "identity":
            return errno.EIO
        row = self.entries[qualifier - 1001]
        ctypes.cast(identity, ctypes.POINTER(ctypes.c_uint32))[0] = row[2]
        ctypes.cast(kind, ctypes.POINTER(ctypes.c_int))[0] = row[3]
        return 0

    def acl_free(self, value):
        self.freed.append(value)
        return 0


@pytest.fixture
def token(tmp_path):
    path = tmp_path / "caller.token"
    path.write_text(CANARY + "\n")
    path.chmod(0o600)
    return path


def _mock_api(monkeypatch, api):
    monkeypatch.setattr(private_files, "sys", SimpleNamespace(platform="darwin"))
    # The native metadata flag is absent on other platforms running the fake.
    monkeypatch.setattr(private_files.os, "O_EVTONLY", getattr(os, "O_EVTONLY", os.O_RDONLY),
                        raising=False)
    monkeypatch.setattr(private_files, "_darwin_acl_api", lambda: api)


@pytest.mark.parametrize("failure", [
    "get_acl", "valid", "get_entry", "tag", "permissions", "qualifier", "identity",
])
def test_acl_check_fails_closed_on_native_error(token, monkeypatch, failure):
    api = FakeAclApi([(1, 1 << 1, token.stat().st_uid + 1, 0)], failure=failure)
    _mock_api(monkeypatch, api)

    error = private_files.token_privacy_error(token)

    assert error == "epi token_file ACL privacy could not be verified"
    assert CANARY not in error
    assert api.freed == ([] if failure == "get_acl" else
                         [1001, 42] if failure == "identity" else [42])


@pytest.mark.parametrize("permission", [1 << 1, 1 << 2, 1 << 5, 1 << 12, 1 << 13])
@pytest.mark.parametrize("principal", ["other-user", "group-with-owner-id"])
def test_acl_rejects_non_owner_credential_grants(token, monkeypatch, permission, principal):
    owner = token.stat().st_uid
    identity, kind = (owner + 1, 0) if principal == "other-user" else (owner, 1)
    api = FakeAclApi([(1, permission, identity, kind)])
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL grants non-owner read or write access"
    )
    assert api.freed == [1001, 42]


@pytest.mark.parametrize("entries", ["empty", "owner", "deny", "metadata"])
def test_acl_accepts_only_private_grants(token, monkeypatch, entries):
    owner = token.stat().st_uid
    rows = {
        "empty": [],
        "owner": [(1, (1 << 1) | (1 << 2), owner, 0)],
        "deny": [(2, (1 << 1) | (1 << 2), owner + 1, 0)],
        "metadata": [(1, (1 << 7) | (1 << 11), owner + 1, 1)],
    }
    api = FakeAclApi(rows[entries])
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) is None
    assert api.freed == ([1001, 42] if entries == "owner" else [42])


def test_acl_no_acl_is_distinct_from_check_error(token, monkeypatch):
    api = FakeAclApi(no_acl=True)
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) is None
    assert api.freed == []
    token.unlink()
    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL privacy could not be verified"
    )


@pytest.mark.parametrize("failure", [OSError(CANARY), AttributeError(CANARY)])
def test_acl_load_errors_do_not_expose_error_details(token, monkeypatch, failure):
    def unavailable():
        raise failure

    _mock_api(monkeypatch, None)
    monkeypatch.setattr(private_files, "_darwin_acl_api", unavailable)

    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL privacy could not be verified"
    )


@pytest.mark.parametrize("platform", ["linux", "win32", "freebsd"])
def test_non_macos_keeps_mode_only_validation(token, monkeypatch, platform):
    monkeypatch.setattr(private_files, "sys", SimpleNamespace(platform=platform))

    def unexpected_native_call():
        pytest.fail("Other platforms must not call macOS ACL APIs")

    monkeypatch.setattr(private_files, "_darwin_acl_api", unexpected_native_call)

    assert private_files.token_privacy_error(token) is None


def test_acl_rejects_unknown_entry_type(token, monkeypatch):
    api = FakeAclApi([(999, 1 << 1, token.stat().st_uid, 0)])
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL privacy could not be verified"
    )
    assert api.freed == [42]


def test_acl_entry_enumeration_is_bounded(token, monkeypatch):
    api = FakeAclApi([(1, 1 << 7, token.stat().st_uid + 1, 1)] * 129)
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL privacy could not be verified"
    )
    assert api.freed == [42]


def test_acl_deny_does_not_override_later_non_owner_grant(token, monkeypatch):
    owner = token.stat().st_uid
    api = FakeAclApi([(2, 1 << 1, owner + 1, 0), (1, 1 << 1, owner + 1, 0)])
    _mock_api(monkeypatch, api)

    assert private_files.token_privacy_error(token) == (
        "epi token_file ACL grants non-owner read or write access"
    )
