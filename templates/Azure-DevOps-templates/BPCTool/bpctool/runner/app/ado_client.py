"""Azure DevOps REST client used by apply writers."""

from __future__ import annotations

import base64
import http.client
import json
import random
import threading
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import getproxies, proxy_bypass

from .org_url import normalize_ado_org_url, parse_ado_org_url

USER_AGENT = "BPCTool-runner (python http.client)"
REQUEST_TIMEOUT_S = 120
# Azure front doors drop idle keep-alive sockets after a few minutes.
IDLE_RECONNECT_S = 60.0
# Longest Retry-After honoured: Azure DevOps measures usage over 5-minute windows.
MAX_RETRY_DELAY_S = 300.0
# How often a wait checks for Cancel.
CANCEL_POLL_S = 0.5

# Statuses where ADO did not process the request, so any method may be retried. A 503
# only counts when it carries rate-limit headers: a front door can answer 503 after a
# backend timeout on a request that was saved.
_REJECTED_STATUSES = {429, 503}
# Additional statuses retried only for idempotent calls (request may have run).
_IDEMPOTENT_RETRY_STATUSES = {500, 502, 504}

# Azure DevOps rate-limit headers (sent on 200 responses too, before delays start).
_RATE_HEADERS = (
    "Retry-After",
    "X-RateLimit-Resource",
    "X-RateLimit-Delay",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
)
# Spacing between requests (all threads) when few TSTUs remain: (remaining share, seconds).
_PACING_STEPS = ((0.10, 2.0), (0.25, 0.5))
_DELAYED_SPACING_S = 2.0
_PACING_HOLD_S = 30.0
_NOTICE_QUIET_S = 60.0

# Project creation runs as an async operation in Azure DevOps.
PROJECT_POLL_INTERVAL_S = 2.0
PROJECT_CREATE_TIMEOUT_S = 300.0
# How long a project may be missing by name before it is treated as not created.
PROJECT_MISSING_GRACE_S = 10.0
_OPERATION_DONE = {"succeeded", "failed", "cancelled"}


def _poll_count(window_s: float) -> int:
    return max(1, round(window_s / max(PROJECT_POLL_INTERVAL_S, 0.1)) + 1)


class AdoError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        transient: bool = False,
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.outcome_unknown = outcome_unknown


class RunCancelled(Exception):
    """Raised at a safe checkpoint after the user cancels a job."""


class _ConnectFailed(OSError):
    """Opening the connection failed, so none of the request was sent."""


def _proxy_for(scheme: str, host: str) -> Any:
    """Proxy URL for a target, honouring *_PROXY / NO_PROXY and macOS system settings."""
    proxy = getproxies().get(scheme)
    if not proxy or proxy_bypass(host):
        return None
    return urlsplit(proxy if "://" in proxy else f"http://{proxy}")


class _ConnectionPool:
    """Per-thread keep-alive HTTP(S) connections keyed by scheme + host."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._local = threading.local()

    def _conns(self) -> dict[tuple[str, str], list[Any]]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        return conns

    def get(
        self, scheme: str, netloc: str
    ) -> tuple[http.client.HTTPConnection, dict[str, str] | None]:
        """Return (connection, proxy headers when requests go via a plain-HTTP proxy).

        A non-None second value means the request target must be the absolute URL.
        """
        conns = self._conns()
        key = (scheme, netloc)
        entry = conns.get(key)
        now = time.monotonic()
        if entry is not None and now - entry[2] > IDLE_RECONNECT_S:
            self.discard(scheme, netloc)
            entry = None
        if entry is None:
            conn, http_proxy_headers = self._open(scheme, netloc)
            entry = [conn, http_proxy_headers, now]
            conns[key] = entry
        entry[2] = now
        return entry[0], entry[1]

    def _open(
        self, scheme: str, netloc: str
    ) -> tuple[http.client.HTTPConnection, dict[str, str] | None]:
        target = urlsplit(f"{scheme}://{netloc}")
        proxy = _proxy_for(scheme, target.hostname or netloc)
        if proxy is None or not proxy.hostname:
            cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            return cls(netloc, timeout=self._timeout), None
        proxy_port = proxy.port or (443 if proxy.scheme == "https" else 80)
        proxy_headers: dict[str, str] = {}
        if proxy.username:
            credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
            proxy_headers["Proxy-Authorization"] = (
                "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            )
        if scheme == "https":
            conn = http.client.HTTPSConnection(proxy.hostname, proxy_port, timeout=self._timeout)
            conn.set_tunnel(target.hostname or netloc, target.port or 443, headers=proxy_headers)
            return conn, None
        conn = http.client.HTTPConnection(proxy.hostname, proxy_port, timeout=self._timeout)
        return conn, proxy_headers

    def discard(self, scheme: str, netloc: str) -> None:
        entry = self._conns().pop((scheme, netloc), None)
        if entry is not None:
            try:
                entry[0].close()
            except OSError:
                pass


def _retry_after_seconds(retry_after: str | None) -> float | None:
    """Seconds a Retry-After header asks for (seconds or HTTP-date); None when unreadable."""
    if not retry_after:
        return None
    value = retry_after.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay_seconds(retry_after: str | None, attempt: int) -> float:
    """Honor Retry-After (up to MAX_RETRY_DELAY_S); else jittered exponential backoff."""
    requested = _retry_after_seconds(retry_after)
    if requested is not None:
        return min(MAX_RETRY_DELAY_S, requested)
    base = min(30.0, 1.5 * (2**attempt))
    return base * (0.75 + random.random() * 0.5)


def _header_float(headers: Mapping[str, str], name: str) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


class _ThrottleGate:
    """Pauses and spaces requests from every worker thread that shares one client.

    Azure DevOps delays (rather than rejects) requests once a user's usage passes
    its limit, and warns ahead of that with ``X-RateLimit-Remaining`` and
    ``Retry-After`` on successful responses.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._not_before = 0.0
        self._next_slot = 0.0
        self._spacing = 0.0
        self._spacing_until = 0.0
        self._last_notice = float("-inf")

    def wait(self, sleep: Callable[[float], None] | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._not_before)
            if now < self._spacing_until:
                start = max(start, self._next_slot)
                self._next_slot = start + self._spacing
        if start > now:
            (sleep or time.sleep)(start - now)

    def pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._not_before = max(self._not_before, time.monotonic() + seconds)

    def observe(self, status: int, headers: Mapping[str, str]) -> str | None:
        """Adjust pacing from one response; returns a notice at most once a minute."""
        retry_after = headers.get("Retry-After")
        delayed = bool(_header_float(headers, "X-RateLimit-Delay"))
        limit = _header_float(headers, "X-RateLimit-Limit")
        remaining = _header_float(headers, "X-RateLimit-Remaining")
        reports_budget = bool(limit) and remaining is not None
        pause = retry_delay_seconds(retry_after, 0) if retry_after else 0.0
        spacing = _DELAYED_SPACING_S if delayed else 0.0
        if reports_budget:
            share = max(0.0, remaining) / limit
            for threshold, seconds in _PACING_STEPS:
                if share <= threshold:
                    spacing = max(spacing, seconds)
                    break

        now = time.monotonic()
        with self._lock:
            if pause:
                self._not_before = max(self._not_before, now + pause)
            if spacing:
                self._spacing = spacing
                self._spacing_until = now + _PACING_HOLD_S
            elif reports_budget:
                self._spacing_until = 0.0
            if not (pause or spacing or status == 429):
                return None
            if now - self._last_notice < _NOTICE_QUIET_S:
                return None
            self._last_notice = now

        resource = headers.get("X-RateLimit-Resource")
        what = "Azure DevOps is slowing this PAT's requests (rate limit"
        if reports_budget and not delayed and status != 429:
            what = f"Azure DevOps rate limit nearly reached ({remaining:g} of {limit:g} left"
        what += f": {resource})" if resource else ")"
        if pause:
            action = f"pausing all requests for {pause:.0f} s"
            requested = _retry_after_seconds(retry_after)
            if requested is not None and requested > pause:
                action += f" (Azure DevOps asked for {requested:.0f} s)"
        elif spacing:
            action = f"spacing requests {spacing:g} s apart for a while"
        else:
            action = "pausing all requests before retrying"
        return (
            f"{what}; {action}. The run continues. If this keeps appearing, "
            "lower Catalog workers next time."
        )


def _wiql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# The work item $batch endpoint is documented for this API version.
BATCH_API_VERSION = "4.1"
WORK_ITEM_BATCH_LIMIT = 200
PARENT_LINK = "System.LinkTypes.Hierarchy-Reverse"


def parent_id_from_relations(relations: Any) -> int | None:
    """The Hierarchy-Reverse parent id in a work item's ``relations``, or None."""
    for relation in relations or []:
        if relation.get("rel") != PARENT_LINK:
            continue
        url = str(relation.get("url") or "").rstrip("/")
        try:
            return int(url.split("/")[-1])
        except ValueError:
            continue
    return None


def _batch_item_error(item: Any) -> str | None:
    """None when one ``$batch`` result succeeded, else its error message."""
    if not isinstance(item, dict):
        return "Azure DevOps returned no result for this update"
    try:
        code = int(item.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if 200 <= code < 300:
        return None
    body: Any = item.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    message: Any = body
    if isinstance(body, dict):
        value = body.get("value")
        message = (
            (value.get("Message") if isinstance(value, dict) else None)
            or body.get("message")
            or body
        )
    return f"ADO {code or 'error'}: {str(message)[:300]}"


class AdoClient:
    def __init__(self, org_url: str, pat: str, *, api_version: str = "7.1") -> None:
        parsed = parse_ado_org_url(org_url)
        if not parsed.ok:
            raise ValueError(parsed.error)
        if not pat or len(pat.strip()) < 20:
            raise ValueError("PAT is required and should be at least 20 characters.")
        self.org = parsed.org
        self.organization_url = normalize_ado_org_url(org_url)
        token = base64.b64encode(f":{pat.strip()}".encode("utf-8")).decode("ascii")
        self._auth = f"Basic {token}"
        self.api_version = api_version
        self._pool = _ConnectionPool(REQUEST_TIMEOUT_S)
        self._gate = _ThrottleGate()
        # Called with a plain-language notice when Azure DevOps starts rate limiting.
        self.on_throttle: Callable[[str], None] | None = None
        # Called before every request; may raise to stop a step (e.g. on Cancel).
        self.before_request: Callable[[], None] | None = None
        # True once the run is cancelled: a throttle pause or retry wait then stops with
        # RunCancelled, before the request is (re)sent.
        self.cancel_waits: Callable[[], bool] | None = None

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        cancelled = self.cancel_waits
        if cancelled is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while True:
            if cancelled():
                raise RunCancelled("Run cancelled by user")
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, CANCEL_POLL_S))

    def url(self, path: str, *, project: str | None = None, api_version: str | None = None) -> str:
        version = api_version or self.api_version
        clean = path if path.startswith("/") else f"/{path}"
        if project:
            # Allow "Project/Team" style paths used by teamsettings APIs.
            encoded_project = "/".join(quote(part, safe="") for part in project.split("/") if part)
            base = f"{self.organization_url}/{encoded_project}"
        else:
            base = self.organization_url
        sep = "&" if "?" in clean else "?"
        return f"{base}{clean}{sep}{urlencode({'api-version': version})}"

    def request(
        self,
        method: str,
        path: str,
        *,
        project: str | None = None,
        api_version: str | None = None,
        json_body: Any = None,
        content_type: str | None = None,
        retry_attempts: int = 5,
        absolute_url: str | None = None,
        idempotent: bool = True,
    ) -> Any:
        """Send a request with retries.

        ``idempotent=False`` (work item create) never retries after the request
        may have reached ADO: network errors once connected, 500/502/504 and a 503
        without rate-limit headers raise ``outcome_unknown``. Failures to connect,
        429, and 503 with rate-limit headers are retried.
        """
        if self.before_request:
            self.before_request()
        url = absolute_url or self.url(path, project=project, api_version=api_version)
        data = None
        headers = {
            "Authorization": self._auth,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"

        retry_statuses = (
            _REJECTED_STATUSES | _IDEMPOTENT_RETRY_STATUSES if idempotent else _REJECTED_STATUSES
        )
        attempts = max(1, retry_attempts)
        for attempt in range(attempts):
            last = attempt == attempts - 1
            self._gate.wait(self._sleep)
            try:
                status, reason, rate_headers, raw = self._send(method.upper(), url, data, headers)
            except (http.client.HTTPException, OSError) as exc:
                if not idempotent and not isinstance(exc, _ConnectFailed):
                    raise AdoError(
                        f"Network error talking to Azure DevOps (outcome unknown, not retried): {exc}",
                        transient=True,
                        outcome_unknown=True,
                    ) from exc
                if last:
                    raise AdoError(
                        f"Network error talking to Azure DevOps: {exc}", transient=True
                    ) from exc
                self._sleep(retry_delay_seconds(None, attempt))
                continue

            rate_headers = rate_headers or {}
            notice = self._gate.observe(status, rate_headers)
            if notice and self.on_throttle:
                try:
                    self.on_throttle(notice)
                except Exception:  # noqa: BLE001 - a notice must never fail the request
                    pass

            if status == 203:
                # ADO answers an invalid/expired PAT with a 203 HTML sign-in page.
                raise AdoError(
                    "Azure DevOps returned a sign-in page (HTTP 203): the PAT is invalid, "
                    "expired, or lacks the required scopes.",
                    status=401,
                )
            if 200 <= status < 300:
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError as exc:
                    prefix = raw[:60].decode("utf-8", errors="replace")
                    raise AdoError(
                        f"ADO {status} {reason}: the response isn't JSON (starts {prefix!r})"
                        + ("" if idempotent else "; outcome unknown, not retried: "
                           "Azure DevOps may have done it anyway"),
                        status=status,
                        transient=True,
                        outcome_unknown=not idempotent,
                    ) from exc

            body = raw.decode("utf-8", errors="replace")[:500]
            transient = status in _REJECTED_STATUSES | _IDEMPOTENT_RETRY_STATUSES
            unthrottled_503 = status == 503 and not rate_headers
            if not idempotent and (status in _IDEMPOTENT_RETRY_STATUSES or unthrottled_503):
                raise AdoError(
                    f"ADO {status} {reason}: {body} (outcome unknown, not retried: "
                    "Azure DevOps may have done it anyway)",
                    status=status,
                    transient=True,
                    outcome_unknown=True,
                )
            if status not in retry_statuses or last:
                raise AdoError(f"ADO {status} {reason}: {body}", status=status, transient=transient)
            delay = retry_delay_seconds(rate_headers.get("Retry-After"), attempt)
            if status in _REJECTED_STATUSES:
                self._gate.pause(delay)
            else:
                self._sleep(delay)
        raise AdoError("ADO request failed after retries", transient=True)

    def _send(
        self, method: str, url: str, data: bytes | None, headers: dict[str, str]
    ) -> tuple[int, str, dict[str, str], bytes]:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn, http_proxy_headers = self._pool.get(parts.scheme, parts.netloc)
        if http_proxy_headers is not None:
            target = url
            headers = {**headers, **http_proxy_headers}
        if conn.sock is None:
            try:
                conn.connect()
            except (http.client.HTTPException, OSError) as exc:
                self._pool.discard(parts.scheme, parts.netloc)
                raise _ConnectFailed(f"couldn't connect: {exc}") from exc
        try:
            conn.request(method, target, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (http.client.HTTPException, OSError):
            self._pool.discard(parts.scheme, parts.netloc)
            raise
        if response.will_close:
            self._pool.discard(parts.scheme, parts.netloc)
        rate_headers = {
            name: value
            for name in _RATE_HEADERS
            if (value := response.getheader(name)) is not None
        }
        return response.status, response.reason, rate_headers, raw

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json_body=json_body, **kwargs)

    # Microsoft DevLabs Multivalue control (Multi-select layout contribution).
    MULTIVALUE_PUBLISHER = "ms-devlabs"
    MULTIVALUE_EXTENSION = "vsts-extensions-multivalue-control"

    def extmgmt_url(self, path: str, *, api_version: str = "7.1-preview.1") -> str:
        clean = path if path.startswith("/") else f"/{path}"
        sep = "&" if "?" in clean else "?"
        return (
            f"https://extmgmt.dev.azure.com/{quote(self.org, safe='')}"
            f"{clean}{sep}{urlencode({'api-version': api_version})}"
        )

    def get_multivalue_extension_status(self) -> str:
        """Return installed | missing | disabled."""
        path = (
            "/_apis/extensionmanagement/installedextensionsbyname/"
            f"{quote(self.MULTIVALUE_PUBLISHER, safe='')}/"
            f"{quote(self.MULTIVALUE_EXTENSION, safe='')}"
        )
        try:
            data = self.get(path, absolute_url=self.extmgmt_url(path)) or {}
        except AdoError as exc:
            if exc.status == 404:
                return "missing"
            raise
        flags = str((data.get("installState") or {}).get("flags") or "none").lower()
        if "disabled" in flags:
            return "disabled"
        return "installed"

    def install_multivalue_extension(self) -> dict[str, Any]:
        path = (
            "/_apis/extensionmanagement/installedextensionsbyname/"
            f"{quote(self.MULTIVALUE_PUBLISHER, safe='')}/"
            f"{quote(self.MULTIVALUE_EXTENSION, safe='')}"
        )
        return self.post(path, absolute_url=self.extmgmt_url(path)) or {}

    def require_multivalue_extension(self) -> None:
        status = self.get_multivalue_extension_status()
        if status == "installed":
            return
        if status == "disabled":
            raise AdoError(
                "DevLabs Multivalue control extension is installed but disabled. "
                "Enable it under Organization settings → Extensions before "
                "process template and project setup.",
                status=403,
            )
        raise AdoError(
            "DevLabs Multivalue control extension "
            f"({self.MULTIVALUE_PUBLISHER}.{self.MULTIVALUE_EXTENSION}) is not "
            "installed. It is required for process template and project setup. "
            "Install from the Marketplace or approve install in the UI, then retry.",
            status=404,
        )

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json_body=json_body, **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, json_body=json_body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def list_processes(self) -> list[dict[str, Any]]:
        data = self.get("/_apis/work/processes", api_version="7.1-preview.2") or {}
        return list(data.get("value") or [])

    def get_process_id(self, process_name: str) -> str | None:
        target = process_name.strip().lower()
        for proc in self.list_processes():
            if str(proc.get("name") or "").strip().lower() == target:
                return str(proc.get("typeId"))
        return None

    def get_agile_process_id(self) -> str:
        for proc in self.list_processes():
            if str(proc.get("name") or "").strip().lower() == "agile":
                return str(proc["typeId"])
        raise AdoError("Built-in Agile process not found in organization.")

    def create_process(self, process_name: str) -> str:
        target = process_name.strip().lower()
        by_name: dict[str, dict[str, Any]] = {}
        for proc in self.list_processes():
            by_name.setdefault(str(proc.get("name") or "").strip().lower(), proc)
        if target in by_name:
            return str(by_name[target].get("typeId"))
        agile = by_name.get("agile")
        if agile is None:
            raise AdoError("Built-in Agile process not found in organization.")
        payload = {
            "name": process_name,
            "description": f"Custom process based on Agile: {process_name}",
            "parentProcessTypeId": str(agile["typeId"]),
        }
        try:
            created = self.post("/_apis/work/processes", json_body=payload, api_version="7.1-preview.2")
            return str(created["typeId"])
        except AdoError as exc:
            if exc.status == 409:
                found = self.get_process_id(process_name)
                if found:
                    return found
            raise

    def list_projects(self) -> list[dict[str, Any]]:
        data = self.get("/_apis/projects", api_version="7.1-preview.4") or {}
        return list(data.get("value") or [])

    def get_project_id(self, project_name: str) -> str | None:
        """Direct lookup by name; listing projects returns only the first 100."""
        data = self._get_project(project_name)
        return str(data["id"]) if data and data.get("id") else None

    def _get_project(self, project_name: str) -> dict[str, Any] | None:
        name = project_name.strip()
        if not name:
            return None
        try:
            return self.get(f"/_apis/projects/{quote(name, safe='')}", api_version="7.1-preview.4") or None
        except AdoError as exc:
            if exc.status == 404:
                return None
            raise

    def get_project_process_id(self, project_id: str) -> str | None:
        """Process typeId the project was created from (System.ProcessTemplateType)."""
        data = self.get(
            f"/_apis/projects/{quote(project_id, safe='')}/properties"
            "?keys=System.ProcessTemplateType",
            api_version="7.1-preview.1",
        ) or {}
        for prop in data.get("value") or []:
            if str(prop.get("name") or "") == "System.ProcessTemplateType":
                return str(prop.get("value") or "") or None
        return None

    def get_ready_project_id(
        self,
        project_name: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
        on_wait: Callable[[str], None] | None = None,
    ) -> str | None:
        """Project id, waiting while Azure DevOps is still creating the project.

        None when there is no such project (or it is being deleted). ``on_wait`` gets the
        project's state once, before waiting.
        """
        data = self._get_project(project_name)
        if not data or not data.get("id"):
            return None
        state = str(data.get("state") or "").lower()
        if state == "wellformed":
            return str(data["id"])
        if on_wait is not None:
            on_wait(str(data.get("state") or "unknown"))
        return self._wait_for_project(project_name, should_cancel=should_cancel)

    def create_project(
        self,
        project_name: str,
        process_id: str,
        *,
        description: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> str | None:
        existing = self.get_ready_project_id(project_name, should_cancel=should_cancel)
        if existing:
            return existing
        payload = {
            "name": project_name,
            "description": description or "",
            "capabilities": {
                "versioncontrol": {"sourceControlType": "Git"},
                "processTemplate": {"templateTypeId": process_id},
            },
        }
        try:
            operation = self.post(
                "/_apis/projects", json_body=payload, api_version="7.1-preview.4", idempotent=False
            ) or {}
        except AdoError as exc:
            if exc.status != 409 and not exc.outcome_unknown:
                raise
            # 409: already exists or still being created; outcome unknown: it may have been created.
            found = self._wait_for_project(project_name, should_cancel=should_cancel)
            if found:
                return found
            if exc.outcome_unknown:
                raise AdoError(
                    f"{exc}. Project '{project_name}' didn't appear in Azure DevOps afterwards. "
                    "Re-run Project Creation.",
                    outcome_unknown=True,
                ) from exc
            raise
        operation_id = str(operation.get("id") or "")
        if not operation_id:
            found = self._wait_for_project(project_name, should_cancel=should_cancel)
            if found:
                return found
            raise self._project_timeout(project_name)
        result = self._wait_for_operation(operation_id, project_name, should_cancel=should_cancel)
        status = str(result.get("status") or "").lower()
        if status == "failed":
            reason = str(result.get("resultMessage") or "").strip() or "no reason given"
            raise AdoError(f"Azure DevOps could not create project '{project_name}': {reason}")
        if status == "cancelled":
            raise AdoError(f"Creating project '{project_name}' was cancelled in Azure DevOps. Re-run Project Creation.")
        if status != "succeeded":
            raise self._project_timeout(project_name)
        return self._wait_for_project(project_name, should_cancel=should_cancel)

    def _wait_for_operation(
        self,
        operation_id: str,
        project_name: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Poll a long-running operation until it finishes; returns its last state."""
        result: dict[str, Any] = {}
        for attempt in range(_poll_count(PROJECT_CREATE_TIMEOUT_S)):
            if attempt:
                time.sleep(PROJECT_POLL_INTERVAL_S)
            self._stop_if_cancelled(should_cancel, project_name)
            result = self.get(
                f"/_apis/operations/{quote(operation_id, safe='')}", api_version="7.1"
            ) or {}
            if str(result.get("status") or "").lower() in _OPERATION_DONE:
                break
        return result

    def _wait_for_project(
        self, project_name: str, *, should_cancel: Callable[[], bool] | None = None
    ) -> str | None:
        """Project id once its state is wellFormed; None when it never shows up."""
        missing_polls = _poll_count(PROJECT_MISSING_GRACE_S)
        for attempt in range(_poll_count(PROJECT_CREATE_TIMEOUT_S)):
            if attempt:
                time.sleep(PROJECT_POLL_INTERVAL_S)
            self._stop_if_cancelled(should_cancel, project_name)
            data = self._get_project(project_name)
            state = str((data or {}).get("state") or "").lower()
            if data is None or state == "deleting":
                if attempt + 1 >= missing_polls:
                    return None
                continue
            if state == "wellformed" and data.get("id"):
                return str(data["id"])
        raise self._project_timeout(project_name)

    @staticmethod
    def _stop_if_cancelled(should_cancel: Callable[[], bool] | None, project_name: str) -> None:
        if should_cancel is not None and should_cancel():
            raise RunCancelled(
                f"Run cancelled by user while waiting for Azure DevOps to create project "
                f"'{project_name}'. Azure DevOps may still finish creating it; run again to use it."
            )

    @staticmethod
    def _project_timeout(project_name: str) -> AdoError:
        minutes = round(PROJECT_CREATE_TIMEOUT_S / 60)
        return AdoError(
            f"Azure DevOps was still creating project '{project_name}' after {minutes} minutes. "
            "Wait a few minutes, then run again; the run waits for the project and uses it once it's ready."
        )

    def update_project_description(self, project_id: str, description: str) -> None:
        self.patch(
            f"/_apis/projects/{quote(project_id, safe='')}",
            json_body={"description": description},
            api_version="7.1-preview.4",
        )

    def list_process_wits(self, process_id: str) -> list[dict[str, Any]]:
        data = (
            self.get(
                f"/_apis/work/processes/{process_id}/workitemtypes",
                api_version="7.1-preview.2",
            )
            or {}
        )
        return list(data.get("value") or [])

    def create_work_item(
        self,
        project: str,
        work_item_type: str,
        fields: dict[str, Any],
        *,
        parent_id: int | None = None,
        dry_run: bool = False,
        markdown_format_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        patch: list[dict[str, Any]] = []
        # Preserve path: mark large-text fields as Markdown so they
        # render correctly instead of as plain text inside HTML controls.
        for ref in markdown_format_fields or []:
            if ref and fields.get(ref) not in (None, ""):
                patch.append(
                    {
                        "op": "add",
                        "path": f"/multilineFieldsFormat/{ref}",
                        "value": "Markdown",
                    }
                )
        for ref, value in fields.items():
            if value is None or value == "":
                continue
            patch.append({"op": "add", "path": f"/fields/{ref}", "value": value})
        if parent_id:
            parent_url = self.url(f"/_apis/wit/workItems/{parent_id}", project=project)
            # strip query for relation URL
            parent_url = parent_url.split("?")[0]
            patch.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        "url": parent_url,
                        "attributes": {"comment": "Imported parent link"},
                    },
                }
            )
        if dry_run:
            return {"id": None, "workItemType": work_item_type, "patch": patch}
        # ADO create-by-type URLs require a literal "$" before the type name
        # (e.g. /workitems/$Bug). In Python f-strings "$" is literal; "{...}" interpolates.
        return self.request(
            "PATCH",
            f"/_apis/wit/workitems/${quote(work_item_type, safe='')}",
            project=project,
            json_body=patch,
            content_type="application/json-patch+json",
            idempotent=False,
        )

    def find_work_item_ids(
        self,
        project: str,
        work_item_type: str,
        *,
        title: str,
        field_ref: str | None = None,
        field_value: Any = None,
    ) -> list[int]:
        """WIQL lookup used to reconcile a create whose outcome is unknown."""
        clauses = [
            "[System.TeamProject] = @project",
            f"[System.WorkItemType] = {_wiql_literal(work_item_type)}",
            f"[System.Title] = {_wiql_literal(title)}",
        ]
        if field_ref and field_value not in (None, ""):
            clauses.append(f"[{field_ref}] = {_wiql_literal(field_value)}")
        query = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(clauses)
        data = self.post("/_apis/wit/wiql", {"query": query}, project=project) or {}
        return [int(item["id"]) for item in data.get("workItems") or [] if "id" in item]

    WIQL_PAGE = 10000

    def has_work_item_with_field(self, project: str, field_ref: str) -> bool:
        query = (
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
            f" AND [{field_ref}] <> ''"
        )
        data = self.post("/_apis/wit/wiql?$top=1", {"query": query}, project=project) or {}
        return bool(data.get("workItems"))

    def find_work_items_by_field_values(self, project: str, field_ref: str, values: list[str]) -> list[int]:
        """IDs of items whose ``field_ref`` is one of ``values`` (WIQL compares case-insensitively)."""
        if not values:
            return []
        query = (
            "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
            f" AND [{field_ref}] IN ({', '.join(_wiql_literal(v) for v in values)})"
        )
        data = self.post("/_apis/wit/wiql", {"query": query}, project=project) or {}
        return [int(item["id"]) for item in data.get("workItems") or [] if "id" in item]

    def find_work_items_with_field(self, project: str, work_item_type: str, field_ref: str) -> list[int]:
        """IDs of items of one type with ``field_ref`` set, read in ID order a page at a time.

        A WIQL query fails outright above 20,000 results, so each page asks for the next
        ``WIQL_PAGE`` IDs after the last one seen.
        """
        ids: list[int] = []
        last = 0
        while True:
            query = (
                "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
                f" AND [System.WorkItemType] = {_wiql_literal(work_item_type)}"
                f" AND [{field_ref}] <> ''"
                f" AND [System.Id] > {last} ORDER BY [System.Id]"
            )
            data = self.post(
                f"/_apis/wit/wiql?$top={self.WIQL_PAGE}", {"query": query}, project=project
            ) or {}
            page = [int(item["id"]) for item in data.get("workItems") or [] if "id" in item]
            ids.extend(page)
            if len(page) < self.WIQL_PAGE:
                return ids
            last = max(page)

    def update_work_item_fields(self, project: str, work_item_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        patch = [{"op": "add", "path": f"/fields/{ref}", "value": value} for ref, value in fields.items()]
        return self.request(
            "PATCH",
            f"/_apis/wit/workitems/{int(work_item_id)}",
            project=project,
            json_body=patch,
            content_type="application/json-patch+json",
            api_version="7.1",
        ) or {}

    def update_work_items_batch(self, updates: list[tuple[int, dict[str, Any]]]) -> list[str | None]:
        """Field updates for up to ``WORK_ITEM_BATCH_LIMIT`` work items in one ``$batch`` request.

        Returns one error message (None when it worked) per update, in order: each update
        succeeds or fails on its own. Setting the same values twice is harmless, so the
        request is retried like a read.
        """
        body = [
            {
                "method": "PATCH",
                "uri": f"/_apis/wit/workitems/{int(work_item_id)}?api-version={BATCH_API_VERSION}",
                "headers": {"Content-Type": "application/json-patch+json"},
                "body": [{"op": "add", "path": f"/fields/{ref}", "value": value} for ref, value in fields.items()],
            }
            for work_item_id, fields in updates
        ]
        data = self.post("/_apis/wit/$batch", body, api_version=BATCH_API_VERSION) or {}
        results = data.get("value") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(updates):
            got = len(results) if isinstance(results, list) else "no"
            raise AdoError(f"Unexpected $batch response: {got} result(s) for {len(updates)} update(s)")
        return [_batch_item_error(item) for item in results]

    def get_parent_id(self, project: str, work_item_id: int) -> int | None:
        """Return Hierarchy-Reverse parent id, or None if unlinked."""
        data = (
            self.request(
                "GET",
                f"/_apis/wit/workItems/{int(work_item_id)}?$expand=Relations",
                project=project,
                api_version="7.1",
            )
            or {}
        )
        return parent_id_from_relations(data.get("relations"))

    def add_parent_link(
        self,
        project: str,
        child_id: int,
        parent_id: int,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        parent_url = self.url(f"/_apis/wit/workItems/{int(parent_id)}", project=project)
        parent_url = parent_url.split("?")[0]
        patch = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": PARENT_LINK,
                    "url": parent_url,
                    "attributes": {"comment": "Backfilled imported parent link"},
                },
            }
        ]
        if dry_run:
            return {"id": child_id, "parentId": parent_id, "patch": patch}
        return self.request(
            "PATCH",
            f"/_apis/wit/workitems/{int(child_id)}",
            project=project,
            json_body=patch,
            content_type="application/json-patch+json",
            # A retry after a lost response fails with "already has a parent".
            idempotent=False,
        )

    def get_work_items_batch(
        self,
        project: str,
        ids: list[int],
        *,
        fields: list[str] | None = None,
        expand_relations: bool = False,
    ) -> list[dict[str, Any]]:
        """Up to 200 work items (all fields unless ``fields``); deleted or inaccessible IDs are left out.

        Azure DevOps refuses ``fields`` together with an expand, so ``expand_relations``
        returns every field as well.
        """
        body: dict[str, Any] = {"ids": [int(i) for i in ids], "errorPolicy": "omit"}
        if expand_relations:
            body["$expand"] = "relations"
        elif fields:
            body["fields"] = list(fields)
        data = self.post(
            "/_apis/wit/workitemsbatch",
            project=project,
            json_body=body,
            api_version="7.1",
        ) or {}
        return [item for item in data.get("value") or [] if item]


class ReadOnlyAdoClient(AdoClient):
    """For rehearsals: refuses every request that could change Azure DevOps."""

    _READ_POSTS = ("/_apis/wit/workitemsbatch", "/_apis/wit/wiql")

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        verb = method.upper()
        clean = path.split("?")[0]
        if verb != "GET" and not (verb == "POST" and clean.endswith(self._READ_POSTS)):
            raise AdoError(f"Rehearsal is read-only: refused {verb} {clean}")
        return super().request(method, path, **kwargs)
