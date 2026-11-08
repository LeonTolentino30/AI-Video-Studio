"""Explicitly enabled standard-library HTTPS boundaries for the local PoC.

No environment access, default credentials, proxies, redirects, or retries.
API authentication and unauthenticated media downloads are separate objects.
"""

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
from urllib.parse import urlsplit

from .store import SafetyError


API_HOST = "api-singapore.klingai.com"
TASK_PATH = "/v1/videos/omni-video"


def media_url(value):
    """Validate syntax without DNS or network access."""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise SafetyError("Media requires a credential-free HTTPS URL on port 443.") from None


class KlingHTTPTransport:
    """One HTTP attempt per method call; the job service controls submission claims.

    The key supplier is called only after explicit enabling and path validation.
    Credentials exist in memory only and are never included in errors or repr.
    """

    def __init__(self, key_supplier, *, enabled=False, timeout=30,
                 connection_factory=http.client.HTTPSConnection, poll_diagnostics=None):
        self._key_supplier = key_supplier
        self.enabled = enabled
        self.timeout = timeout
        self._connection_factory = connection_factory
        self._poll_diagnostics = poll_diagnostics

    def post(self, path, *, json):
        return self._request("POST", path, json)

    def get(self, path):
        return self._request("GET", path, None)

    def _request(self, method, path, payload):
        if self.enabled is not True:
            raise SafetyError("Live Kling transport is disabled.")
        if not ((method == "POST" and path == TASK_PATH)
                or (method == "GET" and re.fullmatch(re.escape(TASK_PATH) + r"/[A-Za-z0-9%_.~-]+", path))):
            raise SafetyError("Unsupported Kling API path.")
        connection = None
        key = None
        diagnostics = {"http_status": None, "kling_response_code": None,
                       "provider_task_status": None, "request_id": None}
        debug_poll = method == "GET" and self._poll_diagnostics is not None
        try:
            key = self._key_supplier()
            if not isinstance(key, str) or not key or any(c.isspace() or ord(c) < 32 for c in key):
                raise ValueError
            connection = self._connection_factory(API_HOST, timeout=self.timeout,
                                                  context=ssl.create_default_context())
            headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if type(response.status) is int and 100 <= response.status <= 599:
                diagnostics["http_status"] = response.status
            if response.status != 200 and not debug_poll:
                raise ValueError  # Includes all redirects; never replay a POST.
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            if debug_poll:
                # Select and validate individual scalars; never forward response
                # bodies, error messages, URLs, headers, or arbitrary status text.
                code = result.get("code")
                if type(code) is int and -(2**31) <= code < 2**31:
                    diagnostics["kling_response_code"] = code
                data = result.get("data")
                status = data.get("task_status") if isinstance(data, dict) else None
                if isinstance(status, str):
                    diagnostics["provider_task_status"] = (
                        status if status in {"submitted", "processing", "succeed", "failed"} else "unrecognized"
                    )
                request_id = result.get("request_id")
                if (isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                        and key not in request_id):
                    diagnostics["request_id"] = request_id
            if response.status != 200:
                raise ValueError
            return result
        except Exception:
            raise SafetyError("Kling HTTPS request failed; submission may require reconciliation.") from None
        finally:
            if connection is not None:
                connection.close()
            if debug_poll:
                try:
                    self._poll_diagnostics(diagnostics)
                except Exception:
                    pass  # Diagnostic output must not change task/retry behavior.


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated public IP while verifying the URL hostname."""

    def __init__(self, hostname, *, address, timeout, context):
        super().__init__(hostname, timeout=timeout, context=context)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class MediaDownloader:
    """Private, bounded HTTPS GETs. This object has no API-key supplier."""

    def __init__(self, *, enabled=False, timeout=30, resolver=socket.getaddrinfo,
                 connection_factory=_PinnedHTTPSConnection):
        self.enabled = enabled
        self.timeout = timeout
        self._resolver = resolver
        self._connection_factory = connection_factory

    def download(self, url, destination, *, max_bytes):
        if self.enabled is not True:
            raise SafetyError("Live media downloads are disabled.")
        parsed = media_url(url)
        destination = Path(destination)
        connection = None
        temporary = None
        created_destination = False
        try:
            answers = self._resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
            addresses = [answer[4][0] for answer in answers]
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise ValueError
            connection = self._connection_factory(parsed.hostname, address=addresses[0],
                                                  timeout=self.timeout, context=ssl.create_default_context())
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"Accept": "*/*"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError  # Redirects require manual review, never follow.
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                count = 0
                while True:
                    chunk = response.read(min(65536, max_bytes - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > max_bytes:
                        raise ValueError
                    output.write(chunk)
                if count == 0:
                    raise ValueError
                output.flush()
                os.fsync(output.fileno())
            # Exclusive creation prevents replacing an existing result or symlink.
            with destination.open("xb") as output, temporary.open("rb") as source:
                created_destination = True
                os.chmod(destination, 0o600)
                while chunk := source.read(65536):
                    output.write(chunk)
            return destination
        except Exception:
            if created_destination:
                destination.unlink(missing_ok=True)
            raise SafetyError("Media download failed; no redirect or retry was attempted.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if connection is not None:
                connection.close()
