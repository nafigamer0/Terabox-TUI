"""Thin client for the dm.terabox.com web API."""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests

APP_ID = 250528
CHUNK_SIZE = 16 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 200

ProgressCb = Callable[[int, int], None]  # done_bytes, total_bytes
CancelCb = Callable[[], bool]

ERRNO_MESSAGES = {
    0: "OK",
    1: "System error",
    2: "Server error",
    3: "Too many files at once",
    4: "Invalid file name",
    -6: "User not logged in",
    -7: "Cannot access path or name is wrong",
    -8: "File already exists",
    -9: "File has been deleted",
    -10: "Not enough space",
    -11: "Parent directory does not exist",
    -22: "Shared files cannot be renamed or moved",
    12: "Some files already exist in target directory",
    102: "Cannot access directory",
    104: "Invalid cookie",
    111: "Unfinished task in progress",
    132: "Verify your identity to operate these files",
    31034: "Too many operations, slow down",
    31060: "Illegal file",
    31066: "File does not exist",
    36001: "Invalid parameter",
    36009: "Storage exceeds your plan limit",
    36010: "File does not exist",
    4000023: "Login session expired",
    4000020: "jsToken invalid",
    400810: "jsToken invalid",
    450016: "jsToken invalid",
}

AUTH_ERRNOS = {-6, 104, 4000023, 4000020, 400810, 450016}


class TeraBoxError(RuntimeError):
    def __init__(self, errno: int, message: str):
        super().__init__(f"[errno {errno}] {message}")
        self.errno = errno
        self.message = message

    @property
    def is_auth(self) -> bool:
        return self.errno in AUTH_ERRNOS


class TransferCancelled(RuntimeError):
    pass


@dataclass
class FileEntry:
    fs_id: int
    path: str
    name: str
    isdir: bool
    size: int
    mtime: int
    category: int

    @property
    def parent(self) -> str:
        if self.path == "/":
            return "/"
        parent = str(Path(self.path).parent)
        return "/" if parent == "." else parent


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def _dp_logid() -> str:
    return str(random.randint(10**19, 10**20 - 1))


def _check(resp_json: dict) -> dict:
    if not isinstance(resp_json, dict):
        raise TeraBoxError(1, f"Unexpected response: {resp_json!r}")
    errno = resp_json.get("errno", resp_json.get("error_code", 0))
    if errno is None:
        errno = 0
    errno = int(errno)
    if errno != 0:
        msg = (
            resp_json.get("show_msg")
            or ERRNO_MESSAGES.get(errno)
            or resp_json.get("info")
            or resp_json.get("errmsg")
            or resp_json.get("error_msg")
            or "Unknown error"
        )
        raise TeraBoxError(errno, str(msg))
    return resp_json


class TeraBoxClient:
    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self.session = requests.Session()
        self._apply_headers()

    def update_config(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        self._apply_headers()

    def _apply_headers(self) -> None:
        base = self.cfg.get("base_url", "https://dm.terabox.com")
        self.session.headers.update(
            {
                "User-Agent": self.cfg.get("user_agent") or "",
                "Cookie": self.cfg.get("cookie") or "",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{base}/main?category=all",
                "Origin": base,
            }
        )

    def _params(self, extra: dict | None = None) -> dict:
        params = {
            "app_id": APP_ID,
            "web": 1,
            "channel": "dubox",
            "clienttype": 0,
            "jsToken": self.cfg.get("js_token", ""),
            "dp-logid": _dp_logid(),
        }
        if extra:
            params.update(extra)
        return params

    def _url(self, path: str) -> str:
        return self.cfg["base_url"].rstrip("/") + path

    def refresh_tokens(self) -> bool:
        """Re-fetch rotating tokens (jsToken) from the web app page.

        The page embeds ``var templateData = {...}`` whose ``jsToken`` field is
        a URL-encoded snippet ``function fn(a){window.jsToken = a};fn("<hex>")``.
        Sensitive endpoints like /api/filemanager validate the *current*
        jsToken strictly (errno 450016 "need verify" otherwise).
        """
        try:
            resp = self.session.get(
                self._url("/main"),
                params={"category": "all"},
                timeout=20,
            )
            match = re.search(
                r"var templateData = (\{.*?\});?\s*</script>", resp.text, re.S
            )
            if not match:
                return False
            data = json.loads(match.group(1))
        except (requests.RequestException, ValueError, re.error):
            return False
        snippet = urllib.parse.unquote(str(data.get("jsToken") or ""))
        token = re.search(r'fn\("([0-9A-Fa-f]+)"\)', snippet)
        if not token:
            return False
        self.cfg["js_token"] = token.group(1)
        for field in ("bdstoken", "csrf", "pcftoken"):
            value = data.get(field)
            if isinstance(value, str) and value:
                self.cfg[field] = value
        return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        full_url: str | None = None,
        timeout: tuple = (15, 60),
        stream: bool = False,
    ) -> requests.Response:
        url = full_url or self._url(path)
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                data=data,
                files=files,
                timeout=timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise TeraBoxError(0, f"Network error: {exc}") from exc
        return resp

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        _retried: bool = False,
    ) -> dict:
        resp = self._request(
            method, path, params=self._params(params), data=data
        )
        try:
            payload = resp.json()
        except ValueError:
            head = resp.text[:200] if resp.text else ""
            raise TeraBoxError(
                1, f"HTTP {resp.status_code} from {path}: {head}"
            ) from None
        try:
            return _check(payload)
        except TeraBoxError as exc:
            if exc.is_auth and not _retried and self.refresh_tokens():
                return self._api(
                    method, path, params=params, data=data, _retried=True
                )
            raise

    # ---------------------------------------------------------------- reads

    def quota(self) -> dict:
        payload = self._api(
            "GET",
            "/api/quota",
            params={"checkexpire": 1, "checkfree": 1},
        )
        total = int(payload.get("total") or 0)
        used = int(payload.get("used") or 0)
        free = int(payload.get("free") or max(total - used, 0))
        return {"total": total, "used": used, "free": free}

    def list_dir(self, path: str) -> list[FileEntry]:
        entries: list[FileEntry] = []
        page = 1
        while page <= MAX_PAGES:
            payload = self._api(
                "GET",
                "/api/list",
                params={
                    "dir": path,
                    "num": PAGE_SIZE,
                    "page": page,
                    "order": "time",
                    "desc": 1,
                    "showempty": 0,
                },
            )
            items = payload.get("list") or []
            for item in items:
                entries.append(
                    FileEntry(
                        fs_id=int(item.get("fs_id") or 0),
                        path=item.get("path") or "",
                        name=item.get("server_filename") or item.get("filename") or "",
                        isdir=bool(int(item.get("isdir") or 0)),
                        size=int(item.get("size") or 0),
                        mtime=int(
                            item.get("server_mtime") or item.get("server_ctime") or 0
                        ),
                        category=int(item.get("category") or 0),
                    )
                )
            if len(items) < PAGE_SIZE:
                break
            page += 1
        return entries

    # --------------------------------------------------------------- writes

    def mkdir(self, path: str) -> None:
        self._api(
            "POST",
            "/api/create",
            params={"a": "commit", "bdstoken": self.cfg.get("bdstoken", "")},
            data={"path": path, "isdir": 1, "block_list": "[]"},
        )

    def rename(self, path: str, new_name: str) -> None:
        payload = self._filemanager(
            "rename",
            [{"path": path, "newname": new_name}],
        )
        taskid = payload.get("taskid")
        if taskid:
            self._wait_task(str(taskid), [path])

    def delete(self, paths: Iterable[str]) -> None:
        path_list = list(paths)
        if not path_list:
            return
        payload = self._filemanager("delete", path_list)
        taskid = payload.get("taskid")
        if taskid:
            self._wait_task(str(taskid), path_list)

    def _filemanager(self, opera: str, filelist) -> dict:
        return self._api(
            "POST",
            "/api/filemanager",
            params={
                "async": 2,
                "onnest": "fail",
                "bdstoken": self.cfg.get("bdstoken", ""),
                "opera": opera,
            },
            data={"filelist": json.dumps(filelist)},
        )

    def _wait_task(self, taskid: str, paths: list[str]) -> None:
        for _ in range(60):
            try:
                payload = self._api(
                    "GET", "/share/taskquery", params={"taskid": taskid}
                )
            except TeraBoxError:
                try:
                    payload = self._api(
                        "POST",
                        "/share/taskquery",
                        params={"taskid": taskid},
                        data={"filelist": json.dumps(paths)},
                    )
                except TeraBoxError:
                    return
            status = payload.get("status")
            if status in ("success", None) and not payload.get("task_errno"):
                return
            if status == "failed" or payload.get("task_errno"):
                code = int(payload.get("task_errno") or 0)
                raise TeraBoxError(code, ERRNO_MESSAGES.get(code, "Task failed"))
            time.sleep(1)
        raise TeraBoxError(111, "Task did not finish in time")

    # ------------------------------------------------------------ downloads

    def dlink(self, path: str) -> str:
        payload = self._api(
            "GET",
            "/api/filemetas",
            params={"target": json.dumps([path]), "dlink": 1},
        )
        info = payload.get("info") or []
        if not info or not info[0].get("dlink"):
            raise TeraBoxError(31066, f"No download link for {path}")
        return info[0]["dlink"]

    def download(
        self,
        path: str,
        dest: Path,
        progress: ProgressCb | None = None,
        should_cancel: CancelCb | None = None,
    ) -> Path:
        url = self.dlink(path)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        resp = self._request("GET", "", full_url=url, stream=True, timeout=(15, 120))
        if resp.status_code >= 400:
            resp.close()
            raise TeraBoxError(2, f"Download failed: HTTP {resp.status_code}")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            body = resp.json()
            resp.close()
            _check(body)
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        try:
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if should_cancel and should_cancel():
                        raise TransferCancelled(path)
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total or done)
        except TransferCancelled:
            part.unlink(missing_ok=True)
            raise
        except requests.RequestException as exc:
            part.unlink(missing_ok=True)
            raise TeraBoxError(0, f"Download interrupted: {exc}") from exc
        finally:
            resp.close()
        part.replace(dest)
        return dest

    # -------------------------------------------------------------- uploads

    def upload(
        self,
        local: Path,
        target_dir: str,
        progress: ProgressCb | None = None,
        should_cancel: CancelCb | None = None,
        on_file: Callable[[str, int, int], None] | None = None,
    ) -> None:
        local = Path(local)
        if local.is_dir():
            files = sorted(p for p in local.rglob("*") if p.is_file())
            for index, file_path in enumerate(files, start=1):
                rel = file_path.relative_to(local)
                dest_dir = _join(target_dir, str(rel.parent))
                if str(rel.parent) == ".":
                    dest_dir = target_dir
                if on_file:
                    on_file(file_path.name, index, len(files))
                self._upload_file(
                    file_path, dest_dir, progress, should_cancel
                )
        else:
            if on_file:
                on_file(local.name, 1, 1)
            self._upload_file(local, target_dir, progress, should_cancel)

    def _upload_file(
        self,
        local: Path,
        target_dir: str,
        progress: ProgressCb | None,
        should_cancel: CancelCb | None,
    ) -> None:
        size = local.stat().st_size
        target_path = _join(target_dir, local.name)
        block_list: list[str] = []
        with open(local, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                block_list.append(hashlib.md5(chunk).hexdigest())
        if not block_list:
            block_list = [hashlib.md5(b"").hexdigest()]

        def cancelled() -> bool:
            return bool(should_cancel and should_cancel())

        if progress:
            progress(0, size)

        payload = self._api(
            "POST",
            "/api/precreate",
            data={
                "path": target_path,
                "autoinit": 1,
                "target_path": target_dir or "/",
                "block_list": json.dumps(block_list),
                "size": size,
                "file_limit_switch_v34": "true",
                "g_identity": "",
                "local_mtime": int(local.stat().st_mtime),
            },
        )
        uploadid = payload.get("uploadid")
        return_type = payload.get("return_type", 1)
        uploaded = 0

        upload_url = (
            self.cfg.get("upload_host", "https://dm1-cdata.terabox.com").rstrip("/")
            + "/rest/2.0/pcs/superfile2"
        )
        if size and int(return_type) != 2:
            with open(local, "rb") as fh:
                for partseq in range(len(block_list)):
                    if cancelled():
                        raise TransferCancelled(str(local))
                    fh.seek(partseq * CHUNK_SIZE)
                    chunk = fh.read(CHUNK_SIZE)
                    resp = self._request(
                        "POST",
                        "",
                        full_url=upload_url,
                        params={
                            "method": "upload",
                            "app_id": APP_ID,
                            "channel": "dubox",
                            "clienttype": 0,
                            "web": 1,
                            "path": target_path,
                            "uploadid": uploadid or "",
                            "uploadsign": 0,
                            "partseq": partseq,
                        },
                        files={
                            "file": ("blob", chunk, "application/octet-stream")
                        },
                        timeout=(600, 720),
                    )
                    try:
                        result = resp.json()
                    except ValueError:
                        raise TeraBoxError(
                            1, f"Upload chunk failed: HTTP {resp.status_code}"
                        ) from None
                    _check(result)
                    server_md5 = result.get("md5")
                    if server_md5:
                        block_list[partseq] = server_md5
                    uploaded += len(chunk)
                    if progress:
                        progress(min(uploaded, size), size)
        elif size and progress:
            progress(size, size)

        if cancelled():
            raise TransferCancelled(str(local))

        self._api(
            "POST",
            "/api/create",
            params={"isdir": 0, "rtype": 1, "bdstoken": self.cfg.get("bdstoken", "")},
            data={
                "path": target_path,
                "size": size,
                "uploadid": uploadid or "",
                "target_path": target_dir or "/",
                "block_list": json.dumps(block_list),
                "local_mtime": int(local.stat().st_mtime),
            },
        )
        if progress:
            progress(size, size)


def _join(directory: str, name: str) -> str:
    directory = directory.rstrip("/") or ""
    return f"{directory}/{name}"
