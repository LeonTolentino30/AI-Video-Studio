"""Explicitly enabled, GET-only Kling 4.0 task transport. No POST method."""

import http.client
import json
import re
import ssl

from .store import SafetyError


API_HOST = 'api-singapore.klingai.com'


class Kling4TaskHTTP:
    def __init__(self, key_supplier, *, enabled=False, timeout=30,
                 connection_factory=http.client.HTTPSConnection):
        self._key_supplier = key_supplier
        self.enabled = enabled
        self.timeout = timeout
        self._connection_factory = connection_factory

    def get(self, path):
        if self.enabled is not True:
            raise SafetyError('Live Kling 4.0 polling is disabled.')
        if not isinstance(path, str) or not re.fullmatch(r'/tasks\?task_ids=[A-Za-z0-9_-]{1,128}', path):
            raise SafetyError('Unsupported Kling 4.0 task query.')
        connection = None
        try:
            key = self._key_supplier()
            if not isinstance(key, str) or not key or any(c.isspace() or ord(c) < 32 for c in key):
                raise ValueError
            connection = self._connection_factory(API_HOST, timeout=self.timeout,
                                                  context=ssl.create_default_context())
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + key,
                                                     'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError  # No redirects or automatic retries.
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ValueError
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        except Exception:
            raise SafetyError('Kling 4.0 status request failed; no generation was submitted.') from None
        finally:
            if connection is not None:
                connection.close()
