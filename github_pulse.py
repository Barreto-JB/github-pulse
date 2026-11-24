"""Summarize public GitHub release and activity metadata."""

import argparse
import contextlib
from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
import os
import stat
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_ROOT = "https://api.github.com/repos"
CACHE_VERSION = 1
CACHE_TTL = 15 * 60
RELEASE_LIMIT = 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REPO_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9_.-]{1,100}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


class PulseError(Exception):
    """An error that can be shown directly to the CLI user."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def get_json(url):
    request = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-pulse",
    })
    try:
        with OPENER.open(request, timeout=10) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        with error:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            secondary = False
            if error.code == 403 and remaining != "0" and retry_after is None:
                try:
                    raw_error = error.read(8193)
                    if len(raw_error) <= 8192:
                        payload = json.loads(raw_error)
                        message = payload.get("message", "") if isinstance(payload, dict) else ""
                        secondary = isinstance(message, str) and ("secondary rate limit" in message.lower()
                                      or "abuse detection" in message.lower())
                except (OSError, HTTPException, ValueError, RecursionError, AttributeError, TypeError):
                    pass
            if error.code == 429 or (error.code == 403 and (remaining == "0" or retry_after is not None or secondary)):
                delay = ""
                if isinstance(retry_after, str) and re.fullmatch(r"[0-9]{1,8}", retry_after):
                    delay = f" Retry after {int(retry_after)} seconds."
                raise PulseError("GitHub API rate limit reached." + delay + " Try later or use --offline.") from error
            if error.code == 404:
                raise PulseError("GitHub could not find that public repository.") from error
            if 300 <= error.code < 400:
                raise PulseError("GitHub redirected the request. Use the current owner/repo.") from error
            raise PulseError(f"GitHub API returned HTTP {error.code}.") from error
    except (OSError, HTTPException) as error:
        raise PulseError("Could not read the GitHub API. Try again or use --offline.") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise PulseError("GitHub API response exceeded the size limit.")
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as error:
        raise PulseError("GitHub returned invalid JSON.") from error


def select_fields(data, fields):
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    result = {}
    for field, kind in fields.items():
        value = data[field]
        if type(value) is not kind or (kind is int and value < 0):
            raise ValueError(f"Invalid {field}")
        result[field] = value
    return result


def valid_timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def normalize(repository, releases, slug):
    repo = select_fields(repository, {
        "full_name": str,
        "private": bool,
        "stargazers_count": int,
        "forks_count": int,
        "open_issues_count": int,
        "updated_at": str,
        "default_branch": str,
        "archived": bool,
    })
    if repo["private"] or repo["full_name"].lower() != slug:
        raise ValueError("Unexpected repository identity")
    pushed_at = repository["pushed_at"]
    if pushed_at is not None and (
        not valid_timestamp(pushed_at)
    ):
        raise ValueError("Invalid pushed_at")
    if not valid_timestamp(repo["updated_at"]):
        raise ValueError("Invalid updated_at")
    repo["pushed_at"] = pushed_at

    if not isinstance(releases, list) or len(releases) > RELEASE_LIMIT:
        raise ValueError("Unexpected releases page")
    published = []
    for release in releases:
        state = select_fields(release, {"draft": bool})
        if state["draft"]:
            continue
        entry = select_fields(release, {"tag_name": str, "prerelease": bool})
        timestamp = release["published_at"]
        if timestamp is None:
            continue
        if not valid_timestamp(timestamp):
            raise ValueError("Invalid published_at")
        published.append({**state, **entry, "published_at": timestamp})
    published.sort(key=lambda release: release["published_at"], reverse=True)
    return {"repository": repo, "releases": published}


@contextlib.contextmanager
def open_regular_cache(path):
    """Open only regular files; do not follow final-component links."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        get_type = kernel.GetFileType
        get_type.argtypes = [wintypes.HANDLE]
        get_type.restype = wintypes.DWORD
        class TagInfo(ctypes.Structure):
            _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]
        get_info = kernel.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        # GENERIC_READ, share read/write/delete, OPEN_EXISTING,
        # FILE_FLAG_OPEN_REPARSE_POINT: inspect link itself, never its final target.
        handle = create(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "Cache open failed")
        try:
            info = TagInfo()
            if get_type(handle) != 1 or not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError("Cache is not a disk file")
            if info.FileAttributes & (0x400 | 0x10):
                raise OSError("Reparse points and directories are not cache files")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None  # descriptor now owns the handle
        finally:
            if handle is not None:
                close(handle)
    else:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
            raise OSError("Safe cache open not supported")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Cache is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as cached:
            yield cached
    finally:
        os.close(fd)


def read_cache(path, slug):
    try:
        with open_regular_cache(path) as cached:
            raw = cached.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(raw)
        fetched_at = data["fetched_at"]
        if (data["version"] != CACHE_VERSION or type(fetched_at) not in (int, float)
                or not math.isfinite(fetched_at) or fetched_at < 0):
            return None
        datetime.fromtimestamp(fetched_at, timezone.utc)
        snapshot = normalize(data["repository"], data["releases"], slug)
        return fetched_at, snapshot
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def write_cache(path, fetched_at, snapshot):
    encoded = json.dumps({"version": CACHE_VERSION, "fetched_at": fetched_at, **snapshot},
                         ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise PulseError("Snapshot exceeds the cache size limit; previous cache preserved.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def get_snapshot(args, slug):
    path = args.cache_dir / (slug.replace("/", "+") + ".json")
    cached = None if args.refresh else read_cache(path, slug)
    if cached is not None:
        fetched_at, snapshot = cached
        stale = not 0 <= time.time() - fetched_at < CACHE_TTL
        if args.offline or not stale:
            return format_snapshot(snapshot, fetched_at, "cache", stale)
    if args.offline:
        raise PulseError("No usable cached snapshot. Run online to populate the cache.")

    base_url = f"{API_ROOT}/{slug}"
    repository = get_json(base_url)
    releases = get_json(f"{base_url}/releases?per_page={RELEASE_LIMIT}")
    try:
        snapshot = normalize(repository, releases, slug)
    except (KeyError, ValueError) as error:
        raise PulseError("GitHub returned unexpected repository or release metadata.") from error
    fetched_at = time.time()
    try:
        write_cache(path, fetched_at, snapshot)
    except OSError as error:
        raise PulseError("Could not write the snapshot cache. Check --cache-dir.") from error
    return format_snapshot(snapshot, fetched_at, "live", False)


def format_snapshot(snapshot, fetched_at, source, stale):
    timestamp = datetime.fromtimestamp(fetched_at, timezone.utc).isoformat(timespec="seconds")
    return {**snapshot, "fetched_at": timestamp, "source": source, "stale": stale}


def print_summary(snapshot):
    repo = snapshot["repository"]
    source = "stale cache" if snapshot["stale"] else snapshot["source"]
    print(f'{json.dumps(repo["full_name"])} ({source}, fetched {snapshot["fetched_at"]})')
    print(f'  Stars {repo["stargazers_count"]} | Forks {repo["forks_count"]} | '
          f'Open issues and pull requests {repo["open_issues_count"]}')
    print(f'  Default branch {json.dumps(repo["default_branch"])} | Archived {repo["archived"]}')
    print(f'  Last push {repo["pushed_at"] or "unknown"} | Last update {repo["updated_at"]}')
    print("Published releases on the first page:")
    if not snapshot["releases"]:
        print("  None")
    for release in snapshot["releases"]:
        suffix = " (prerelease)" if release["prerelease"] else ""
        print(f'  {release["published_at"]}  {json.dumps(release["tag_name"])}{suffix}')


def default_cache_dir():
    """Use a user-specific cache without requiring a home for absolute overrides."""
    if os.name == "nt":
        value = os.environ.get("LOCALAPPDATA")
        if value and Path(value).is_absolute():
            return Path(value) / "GitHubPulse" / "Cache"
        return Path.home() / "AppData" / "Local" / "GitHubPulse" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "github-pulse"
    value = os.environ.get("XDG_CACHE_HOME")
    if value and Path(value).is_absolute():
        return Path(value) / "github-pulse"
    return Path.home() / ".cache" / "github-pulse"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="github-pulse", description="Summarize public GitHub repository metadata."
    )
    parser.add_argument("repository", metavar="OWNER/REPO")
    parser.add_argument("--json", action="store_true", help="print a JSON snapshot")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="snapshot directory (default: user-specific cache)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use only the local cache")
    mode.add_argument("--refresh", action="store_true", help="fetch a new snapshot")
    args = parser.parse_args(argv)
    if (not REPO_PATTERN.fullmatch(args.repository)
            or args.repository.split("/")[1] in (".", "..")):
        parser.error("repository must be a GitHub owner/repo identifier")
    if args.cache_dir is None:
        try:
            args.cache_dir = default_cache_dir()
        except (RuntimeError, OSError):
            parser.error("Cannot locate user cache; supply --cache-dir PATH")
    try:
        snapshot = get_snapshot(args, args.repository.lower())
    except PulseError as error:
        print(f"github-pulse: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=True))
    else:
        print_summary(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
