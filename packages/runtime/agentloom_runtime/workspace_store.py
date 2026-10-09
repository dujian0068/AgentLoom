"""Bounded, root-relative POSIX storage; shared locks live outside the sandbox.

All writers must share the same POSIX lock directory. This filesystem boundary is
not process isolation: executable tools still require a separate sandbox.
"""

from __future__ import annotations

import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import stat as stat_module
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import regex


class PosixWorkspaceStore:
    MAX_FILE_BYTES = 2 * 1024 * 1024
    MAX_WALK_ENTRIES = 5000
    MAX_SCAN_FILES = 1000
    MAX_SCAN_BYTES = 20 * 1024 * 1024
    MAX_OUTPUT_CHARS = 100000
    MAX_DEPTH = 32
    LOCK_TIMEOUT = 5.0
    GREP_TIMEOUT = 2.0
    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    _FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def __init__(
        self, root: Path, *, lock_root: Path | None = None, reserved_paths=(), lock_key=None
    ):
        self.reserved_paths = frozenset(reserved_paths)
        if any(len(self._parts(path)) != 1 for path in self.reserved_paths):
            raise ValueError("reserved paths must be single relative components")
        self.root = self._absolute(root)
        if self.root == Path("/"):
            raise ValueError("workspace root cannot be the filesystem root")
        self.lock_root = self._absolute(lock_root or self.root.parent / ".agentloom-locks")
        if self.lock_root == self.root or self.root in self.lock_root.parents:
            raise ValueError("workspace locks must be outside the sandbox mount")
        self.prepare()
        with self._absolute_fd(self.lock_root, create=True):
            pass
        if lock_key is not None and (
            not isinstance(lock_key, str) or not lock_key or len(lock_key) > 4096
        ):
            raise ValueError("lock_key must be a nonempty stable namespace identifier")
        identity = lock_key.encode() if lock_key is not None else os.fsencode(self.root)
        self._lock_name = hashlib.sha256(identity).hexdigest() + ".lock"
        self._quarantine_name = self._lock_name + ".quarantine"

    @staticmethod
    def _absolute(path):
        raw = os.fspath(path)
        if not isinstance(raw, str) or "\x00" in raw or "\\" in raw:
            raise ValueError("invalid storage root")
        if not os.path.isabs(raw) or ".." in raw.split("/"):
            raise ValueError("storage root must be absolute without traversal")
        return Path(raw)  # Never resolve symlinks before checking them.

    @classmethod
    def _parts(cls, path, *, allow_root=False):
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise ValueError("invalid workspace path")
        if "\x00" in path or "\\" in path or path.startswith("/"):
            raise ValueError("workspace paths must be relative POSIX paths")
        parts = path.split("/")
        if ".." in parts or any(":" in part or len(part.encode()) > 255 for part in parts):
            raise ValueError("workspace path traversal or invalid component")
        parts = tuple(part for part in parts if part not in ("", "."))
        if len(parts) > cls.MAX_DEPTH or (not parts and not allow_root):
            raise ValueError("invalid workspace path depth")
        return parts

    def _path(self, path, *, allow_root=False):
        parts = self._parts(path, allow_root=allow_root)
        if parts and parts[0] in self.reserved_paths:
            raise ValueError("workspace path is reserved")
        return parts

    @staticmethod
    def _display(parts):
        return "/".join(parts) or "."

    @staticmethod
    def _bound(value, minimum, maximum, name):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _unsafe(exc):
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("symlinks and non-directory parents are not permitted") from None
        raise exc

    @classmethod
    @contextmanager
    def _absolute_fd(cls, path, *, create=False):
        fd = os.open("/", cls._DIRECTORY_FLAGS)
        try:
            for part in path.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, cls._DIRECTORY_FLAGS, dir_fd=fd)
                except OSError as exc:
                    cls._unsafe(exc)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def prepare(self):
        with self._absolute_fd(self.root, create=True):
            pass
        return self.root

    @contextmanager
    def _directory(self, parts=(), *, create=False):
        with self._absolute_fd(self.root) as root_fd:
            fd = os.dup(root_fd)
            try:
                for part in parts:
                    if create:
                        try:
                            os.mkdir(part, 0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                    try:
                        child = os.open(part, self._DIRECTORY_FLAGS, dir_fd=fd)
                    except OSError as exc:
                        self._unsafe(exc)
                    os.close(fd)
                    fd = child
                yield fd
            finally:
                os.close(fd)

    @staticmethod
    def _regular(info, *, allow_unlinked=False):
        valid_links = (0, 1) if allow_unlinked else (1,)
        if not stat_module.S_ISREG(info.st_mode) or info.st_nlink not in valid_links:
            raise ValueError("only regular files without hard links are permitted")

    def _read_at(self, parent_fd, name, max_bytes):
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        # A concurrent atomic replacement can unlink the inode that stat just
        # looked up before metadata is returned (notably on APFS). Zero links
        # still describes a regular old inode, not a hard-link escape. The
        # no-follow open and descriptor check below validate the actual read.
        self._regular(info, allow_unlinked=True)  # Never open FIFOs/devices.
        try:
            fd = os.open(name, self._FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            self._unsafe(exc)
        try:
            info = os.fstat(fd)
            # Atomic replacement may unlink an already-open old inode. Its data
            # remains safe to read; multiple hard links are still rejected.
            self._regular(info, allow_unlinked=True)
            if info.st_size > max_bytes:
                raise ValueError("workspace file exceeds the read size limit")
            result = bytearray()
            while len(result) <= max_bytes:
                chunk = os.read(fd, min(65536, max_bytes + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > max_bytes:
                raise ValueError("workspace file exceeds the read size limit")
            return bytes(result)
        finally:
            os.close(fd)

    def read_bytes(self, path, max_bytes=MAX_FILE_BYTES):
        self._bound(max_bytes, 1, self.MAX_FILE_BYTES, "max_bytes")
        parts = self._path(path)
        with self._directory(parts[:-1]) as parent_fd:
            return self._read_at(parent_fd, parts[-1], max_bytes)

    @staticmethod
    def _text(raw):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("workspace text operations require UTF-8 files") from None
        if "\x00" in text:
            raise ValueError("workspace text operations do not support binary files")
        return text

    def read(self, path, offset=1, limit=200, max_chars=20000):
        self._bound(offset, 1, 10000000, "offset")
        self._bound(limit, 1, 2000, "limit")
        self._bound(max_chars, 1, self.MAX_OUTPUT_CHARS, "max_chars")
        parts = self._path(path)
        raw = self.read_bytes(path)
        lines = self._text(raw).splitlines(keepends=True)
        selected = lines[offset - 1 : offset - 1 + limit]
        full = "".join(selected)
        content = full[:max_chars]
        return {
            "path": self._display(parts),
            "content": content,
            "start_line": offset,
            "end_line": offset + len(content.splitlines()) - 1,
            "total_lines": len(lines),
            "truncated": len(full) > max_chars or offset - 1 + len(selected) < len(lines),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }

    def stat(self, path):
        parts = self._path(path, allow_root=True)
        if not parts:
            with self._directory():
                return {"path": ".", "type": "directory"}
        with self._directory(parts[:-1]) as parent_fd:
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if stat_module.S_ISDIR(info.st_mode):
                return {"path": self._display(parts), "type": "directory"}
            self._regular(info)
            result = {"path": self._display(parts), "type": "file", "size": info.st_size}
            if info.st_size <= self.MAX_FILE_BYTES:
                raw = self._read_at(parent_fd, parts[-1], self.MAX_FILE_BYTES)
                result.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            return result

    def _check_quarantine(self, parent_fd):
        try:
            os.stat(self._quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError("工作区执行状态未知，请管理员核对沙箱已停止后解除隔离")

    @staticmethod
    def _execution_owner(container_name):
        if not isinstance(container_name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_name
        ):
            raise ValueError("invalid sandbox container identity")
        return container_name

    def begin_execution(self, container_name):
        """Persist execution ownership BEFORE launch, while the caller holds lock().

        A dead worker releases flock but leaves this marker, so another worker
        cannot write while its container may still be alive. No expiration is safe
        without reconciling the original container on its original worker.
        """
        owner = self._execution_owner(container_name)
        raw = json.dumps({"kind": "sandbox-execution/v1", "container_name": owner}).encode()
        with self._absolute_fd(self.lock_root) as parent_fd:
            try:
                fd = os.open(
                    self._quarantine_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                raise ValueError("工作区存在未确认的沙箱执行，请管理员核对") from None
            try:
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    remaining = remaining[written:]
                os.fsync(fd)
                os.fsync(parent_fd)
            finally:
                os.close(fd)

    def finish_execution(self, container_name):
        """Clear this owner's marker ONLY after confirmed container cleanup.

        Called by the execution adapter while still holding the original lock;
        this is intentionally not a model-facing tool or an automatic timeout.
        Unknown/partial/quarantine-only markers require administrator recovery.
        """
        owner = self._execution_owner(container_name)
        with self._absolute_fd(self.lock_root) as parent_fd:
            raw = self._read_at(parent_fd, self._quarantine_name, 4096)
            try:
                marker = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError("工作区执行标记不完整，请管理员核对") from None
            if marker != {"kind": "sandbox-execution/v1", "container_name": owner}:
                raise ValueError("工作区沙箱执行标记的所有者不匹配")
            os.unlink(self._quarantine_name, dir_fd=parent_fd)
            os.fsync(parent_fd)

    def quarantine(self):
        """Fail closed after uncertain sandbox termination. Caller holds lock().

        The marker is outside the sandbox and has no tool-facing clear operation.
        Administrators may remove it only after verifying the worker has stopped.
        """
        with self._absolute_fd(self.lock_root) as parent_fd:
            try:
                fd = os.open(
                    self._quarantine_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                return
            try:
                os.write(fd, b"sandbox termination requires administrator verification\n")
                os.fsync(fd)
                os.fsync(parent_fd)
            finally:
                os.close(fd)

    @contextmanager
    def lock(self):
        with self._absolute_fd(self.lock_root) as parent_fd:
            self._check_quarantine(parent_fd)
            try:
                fd = os.open(
                    self._lock_name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                info = os.stat(self._lock_name, dir_fd=parent_fd, follow_symlinks=False)
                self._regular(info)
                fd = os.open(
                    self._lock_name,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            try:
                self._regular(os.fstat(fd))
                started = time.monotonic()
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() - started >= self.LOCK_TIMEOUT:
                            raise TimeoutError("workspace write lock timed out") from None
                        time.sleep(0.02)
                try:
                    self._check_quarantine(parent_fd)
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _expected(expected_sha256):
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or (
                expected_sha256
                and (
                    len(expected_sha256) != 64
                    or any(c not in "0123456789abcdef" for c in expected_sha256)
                )
            )
        ):
            raise ValueError("expected_sha256 must be a SHA-256 digest or empty for a new file")

    def _check_expected(self, parent_fd, name, expected_sha256, *, required=False):
        self._expected(expected_sha256)
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if required or expected_sha256 not in (None, ""):
                raise
            return None
        self._regular(info)
        if expected_sha256 == "":
            raise FileExistsError("workspace file already exists")
        raw = self._read_at(parent_fd, name, self.MAX_FILE_BYTES)
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("workspace file changed: SHA-256 precondition failed")
        return raw

    def _write_at(self, parent_fd, name, raw, path):
        temporary = ".agentloom-tmp-" + uuid.uuid4().hex
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        try:
            view = memoryview(raw)
            while view:
                size = os.write(fd, view)
                view = view[size:]
            os.fsync(fd)
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        return {
            "file": path,
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }

    def _content(self, content):
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("workspace content must be UTF-8 text")
        try:
            raw = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("workspace content must be UTF-8 text") from None
        if len(raw) > self.MAX_FILE_BYTES:
            raise ValueError("workspace file exceeds the write size limit")
        return raw

    def write(self, path, content, expected_sha256=None):
        parts = self._path(path)
        raw = self._content(content)
        self._expected(expected_sha256)
        with self.lock(), self._directory(parts[:-1], create=True) as parent_fd:
            self._check_expected(parent_fd, parts[-1], expected_sha256)
            return self._write_at(parent_fd, parts[-1], raw, self._display(parts))

    def edit(self, path, old_text, new_text, replace_all=False, expected_sha256=None):
        parts = self._path(path)
        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must not be empty")
        if not isinstance(new_text, str) or not isinstance(replace_all, bool):
            raise ValueError("invalid edit arguments")
        with self.lock(), self._directory(parts[:-1]) as parent_fd:
            raw = self._check_expected(parent_fd, parts[-1], expected_sha256, required=True)
            text = self._text(raw)
            count = text.count(old_text)
            if count == 0:
                raise ValueError("edit text was not found")
            if count > 1 and not replace_all:
                raise ValueError("edit text is ambiguous; use replace_all or provide more context")
            output = self._content(text.replace(old_text, new_text, -1 if replace_all else 1))
            return self._write_at(parent_fd, parts[-1], output, self._display(parts))

    def mkdir(self, path):
        parts = self._path(path)
        with self.lock(), self._directory(parts, create=True) as fd:
            os.fsync(fd)
        return {"path": self._display(parts)}

    def delete(self, path, expected_sha256=None):
        parts = self._path(path)
        with self.lock(), self._directory(parts[:-1]) as parent_fd:
            self._check_expected(parent_fd, parts[-1], expected_sha256, required=True)
            os.unlink(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        return {"path": self._display(parts)}

    def _walk(self, parts, budget, *, recursive, include_hidden):
        with self._directory(parts) as fd:
            with os.scandir(fd) as entries:
                for entry in entries:
                    budget["entries"] += 1
                    if budget["entries"] > self.MAX_WALK_ENTRIES:
                        budget["truncated"] = True
                        return
                    if not parts and entry.name in self.reserved_paths:
                        continue
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    child = parts + (entry.name,)
                    if len(child) > self.MAX_DEPTH:
                        budget["truncated"] = True
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    is_dir = stat_module.S_ISDIR(info.st_mode)
                    if not is_dir and (not stat_module.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        continue
                    yield child, info, is_dir
                    if is_dir and recursive:
                        try:
                            yield from self._walk(
                                child, budget, recursive=True, include_hidden=include_hidden
                            )
                        except FileNotFoundError:
                            continue
                    if budget["entries"] > self.MAX_WALK_ENTRIES:
                        return

    def list(self, path=".", limit=200, include_hidden=False, recursive=False):
        self._bound(limit, 1, 1000, "limit")
        parts = self._path(path, allow_root=True)
        budget = {"entries": 0, "truncated": False}
        results = []
        iterator = self._walk(parts, budget, recursive=recursive, include_hidden=include_hidden)
        try:
            for child, info, is_dir in iterator:
                if len(results) >= limit:
                    budget["truncated"] = True
                    break
                result = {"path": self._display(child), "type": "directory" if is_dir else "file"}
                if not is_dir:
                    result["size"] = info.st_size
                results.append(result)
        finally:
            iterator.close()
        results.sort(key=lambda entry: entry["path"])
        return {"entries": results, "truncated": budget["truncated"]}

    @classmethod
    def _pattern(cls, pattern):
        parts = cls._parts(pattern)
        if len(pattern) > 256:
            raise ValueError("workspace glob pattern is too long")
        return parts

    @staticmethod
    def _matches(parts, pattern):
        cache = {}

        def match(i, j):
            key = (i, j)
            if key not in cache:
                if j == len(pattern):
                    value = i == len(parts)
                elif pattern[j] == "**":
                    value = match(i, j + 1) or (i < len(parts) and match(i + 1, j))
                else:
                    value = (
                        i < len(parts)
                        and fnmatch.fnmatchcase(parts[i], pattern[j])
                        and match(i + 1, j + 1)
                    )
                cache[key] = value
            return cache[key]

        return match(0, 0)

    def glob(self, pattern, path=".", limit=200, include_hidden=False):
        self._bound(limit, 1, 1000, "limit")
        pattern_parts = self._pattern(pattern)
        parts = self._path(path, allow_root=True)
        budget = {"entries": 0, "truncated": False}
        results = []
        iterator = self._walk(parts, budget, recursive=True, include_hidden=include_hidden)
        try:
            for child, _, _ in iterator:
                if self._matches(child[len(parts) :], pattern_parts):
                    if len(results) >= limit:
                        budget["truncated"] = True
                        break
                    results.append(self._display(child))
        finally:
            iterator.close()
        return {"paths": sorted(results), "truncated": budget["truncated"]}

    def grep(
        self,
        pattern,
        path=".",
        glob="*",
        literal=True,
        case_sensitive=True,
        limit=100,
        context_lines=0,
        include_hidden=False,
    ):
        self._bound(limit, 1, 1000, "limit")
        self._bound(context_lines, 0, 10, "context_lines")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
            raise ValueError("grep pattern must contain 1 to 1000 characters")
        if not isinstance(literal, bool) or not isinstance(case_sensitive, bool):
            raise ValueError("invalid grep flags")
        expression = None
        if not literal:
            try:
                expression = regex.compile(pattern, flags=0 if case_sensitive else regex.IGNORECASE)
            except (regex.error, RecursionError):
                raise ValueError("invalid regular expression") from None
        pattern_parts = self._pattern(glob)
        parts = self._path(path, allow_root=True)
        budget = {"entries": 0, "truncated": False}
        results, scanned, total_bytes, output_chars = [], 0, 0, 0
        needle = pattern if case_sensitive else pattern.casefold()
        started = time.monotonic()
        target = self.stat(path)
        iterator = (
            self._walk(parts, budget, recursive=True, include_hidden=include_hidden)
            if target["type"] == "directory"
            else iter([(parts, None, False)])
        )
        stopped = False
        try:
            for child, _, is_dir in iterator:
                if time.monotonic() - started >= self.GREP_TIMEOUT:
                    budget["truncated"] = True
                    break
                if is_dir:
                    continue
                candidate = child[-1:] if len(pattern_parts) == 1 else child[len(parts) :]
                if not self._matches(candidate, pattern_parts):
                    continue
                if scanned >= self.MAX_SCAN_FILES:
                    budget["truncated"] = True
                    break
                try:
                    raw = self.read_bytes(self._display(child))
                    text = self._text(raw)
                except FileNotFoundError:
                    continue
                except ValueError:
                    budget["truncated"] = True
                    continue
                scanned += 1
                total_bytes += len(raw)
                if total_bytes > self.MAX_SCAN_BYTES:
                    budget["truncated"] = True
                    break
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    if time.monotonic() - started >= self.GREP_TIMEOUT:
                        stopped = True
                        break
                    try:
                        matched = (
                            bool(expression.search(line, timeout=0.02))
                            if expression is not None
                            else needle in (line if case_sensitive else line.casefold())
                        )
                    except TimeoutError:
                        stopped = True
                        break
                    if not matched:
                        continue
                    before = lines[max(0, index - context_lines) : index]
                    after = lines[index + 1 : index + 1 + context_lines]
                    added = len(line) + sum(map(len, before)) + sum(map(len, after))
                    if len(results) >= limit or output_chars + added > self.MAX_OUTPUT_CHARS:
                        stopped = True
                        break
                    result = {"path": self._display(child), "line": index + 1, "text": line}
                    if context_lines:
                        result.update(before=before, after=after)
                    results.append(result)
                    output_chars += added
                if stopped:
                    budget["truncated"] = True
                    break
        finally:
            if hasattr(iterator, "close"):
                iterator.close()
        return {"matches": results, "truncated": budget["truncated"], "scanned_files": scanned}
