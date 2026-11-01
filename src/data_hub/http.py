"""Shared request admission for metadata providers, independent of any host."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..shared_contracts import AuthorityRef
from ..task_runtime.request_policy import RequestPolicy, source_policy_id

VERIFICATION_TITLE = re.compile(
    rb"<title>[^<]*(?:access denied|just a moment|request rate|undeclared automated)[^<]*</title>",
    re.IGNORECASE,
)


def access_verification(content: bytes) -> bool:
    sample = content[:16384].lstrip()
    return sample.startswith((b"<", b"\xef\xbb\xbf<")) and bool(
        VERIFICATION_TITLE.search(sample)
    )


class _CancelCallback:
    def __init__(self, callback):
        self.callback = callback

    def is_set(self):
        return bool(self.callback())

    def wait(self, delay):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self.is_set():
                return True
            time.sleep(min(0.05, deadline - time.monotonic()))
        return self.is_set()


class AuthorityHTTP:
    """No automatic retry; callers can inject a preconfigured HTTP boundary."""

    def __init__(self, authority: AuthorityRef, http=None, *, minimum_interval=None):
        if not isinstance(authority, AuthorityRef):
            raise TypeError("explicit_authority_required")
        self.authority, self.http = authority, http
        self.policy = RequestPolicy(authority)
        self.minimum_interval = minimum_interval
        self._owns_http = http is None

    def request(self, method, url, *, deadline=None, cancel=None, **kwargs):
        # A redirect target needs its own admission; providers must follow
        # declared hops explicitly. Automatic redirecting would bypass policy.
        if kwargs.get("allow_redirects"):
            raise ValueError("automatic_redirects_bypass_source_policy")
        kwargs["allow_redirects"] = False
        interval = self.minimum_interval
        if interval is None:
            interval = {"cninfo": 5.0, "sec": 0.2, "hkex": 1.0}.get(
                source_policy_id(url), 1.0
            )
        if callable(cancel) and not hasattr(cancel, "is_set"):
            cancel = _CancelCallback(cancel)
        self.policy.wait(url, min_interval=interval, deadline=deadline, cancel=cancel)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("source_request_deadline_exhausted")
            timeout = kwargs.get("timeout")
            if isinstance(timeout, tuple):
                kwargs["timeout"] = tuple(
                    min(float(value), remaining) if value is not None else remaining
                    for value in timeout
                )
            else:
                kwargs["timeout"] = (
                    remaining if timeout is None else min(float(timeout), remaining)
                )
        if self.http is None:
            import requests

            self.http = requests.Session()
        response = getattr(self.http, method.lower())(url, **kwargs)
        status = response.status_code
        if status in {401, 403, 429}:
            retry = response.headers.get("Retry-After", "60")
            try:
                delay = float(retry)
            except (ValueError, TypeError):
                try:
                    when = parsedate_to_datetime(str(retry))
                    delay = (when - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            self.policy.block(
                url,
                f"source_http_{status}",
                retry_after=max(60, delay) if status == 429 else None,
            )
        if (
            status == 200
            and not kwargs.get("stream")
            and access_verification(response.content)
        ):
            self.policy.block(url, "source_access_verification")
            raise ValueError("source_http_403")
        return response

    def get(self, url, **kwargs):
        return self.request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("post", url, **kwargs)

    def close(self):
        if self._owns_http and self.http is not None:
            self.http.close()
