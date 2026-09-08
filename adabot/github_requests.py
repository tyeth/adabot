# SPDX-FileCopyrightText: 2017 Scott Shawcroft for Adafruit Industries
#
# SPDX-License-Identifier: MIT

"""Wrapper for GitHub requests."""

from base64 import b64encode
import datetime
import functools
import logging
import os
import time
import traceback

import requests
import requests_cache

TIMEOUT = 60
MAX_ERROR_RETRIES = 100  # cap retries on transient errors (was infinite)
DEFAULT_RETRY_DELAY = 0.5  # seconds; overridden by Retry-After header if present

logger = logging.getLogger(__name__)

# Last-seen rate limit info (updated on each non-cached response)
rate_limit_remaining = None
rate_limit_reset_at = None

# Reusable session for connection pooling (keeps TCP connections alive)
_session = requests.Session()

def setup_cache(expire_after=7200):
    """Sets up a cache for requests."""
    requests_cache.install_cache(
        cache_name="github_cache",
        backend="sqlite",
        expire_after=expire_after,
        allowable_codes=(200, 404),
    )


def _fix_url(url):
    if url.startswith("/"):
        url = "https://api.github.com" + url
    return url


def _fix_kwargs(kwargs):
    api_version = (
        "application/vnd.github.scarlet-witch-preview+json;"
        "application/vnd.github.hellcat-preview+json"
    )
    if "headers" in kwargs:
        if "Accept" in kwargs["headers"]:
            kwargs["headers"]["Accept"] += ";" + api_version
        else:
            kwargs["headers"]["Accept"] = api_version
    else:
        kwargs["headers"] = {"Accept": "application/vnd.github.hellcat-preview+json"}
    if "ADABOT_GITHUB_ACCESS_TOKEN" in os.environ and "auth" not in kwargs:
        user = os.environ.get("ADABOT_GITHUB_USER", "")
        access_token = os.environ["ADABOT_GITHUB_ACCESS_TOKEN"]
        basic_encoded = b64encode(str(user + ":" + access_token).encode()).decode()
        auth_header = "Basic {}".format(basic_encoded)

        kwargs["headers"]["Authorization"] = auth_header

    return kwargs


def _adaptive_throttle():
    """Sleep proportionally when rate limit budget is running low.

    >100 remaining  → no delay (full speed)
    20–100 remaining → 1 s between requests
    <20 remaining    → 3 s between requests
    """
    global rate_limit_remaining, rate_limit_reset_at
    if rate_limit_remaining is None or rate_limit_remaining > 100:
        return  # plenty of budget — go fast
    # A low count from before the hourly reset is stale — clear it and go fast
    # (cached responses never update the count, so it can stick for hours)
    if rate_limit_reset_at:
        try:
            if datetime.datetime.now() >= datetime.datetime.fromisoformat(rate_limit_reset_at):
                rate_limit_remaining = None
                rate_limit_reset_at = None
                return
        except (ValueError, TypeError):
            pass
    if rate_limit_remaining > 20:
        delay = 1.0
    else:
        delay = 3.0
    logger.debug("Rate-limit throttle: %d remaining, sleeping %.1fs", rate_limit_remaining, delay)
    time.sleep(delay)


def request(method, url, _retries_left=MAX_ERROR_RETRIES, **kwargs):
    """Processes request for `url`."""
    global rate_limit_remaining, rate_limit_reset_at

    # Adaptive throttle: only slows down when rate limit budget is low
    _adaptive_throttle()

    try:
        response = getattr(_session, method)(
            _fix_url(url), timeout=TIMEOUT, **_fix_kwargs(kwargs)
        )
        from_cache = getattr(response, "from_cache", False)
        remaining = int(response.headers.get("X-RateLimit-Remaining", -1))
        logger.debug(
            "GET %s %s status=%s",
            url,
            f"{'(cache)' if from_cache else '(%d remaining)' % remaining}",
            response.status_code,
        )
    except requests.RequestException as exc:
        exception_text = traceback.format_exc()
        if "ADABOT_GITHUB_ACCESS_TOKEN" in os.environ:
            exception_text = exception_text.replace(
                os.environ["ADABOT_GITHUB_ACCESS_TOKEN"], "[secure]"
            )
        logger.critical("%s", exception_text)
        if method == "get" and _retries_left > 0:
            # Try to extract a retry delay from the exception's response
            delay = DEFAULT_RETRY_DELAY
            resp = getattr(exc, "response", None)
            if resp is not None:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(float(retry_after), 0.1)
                    except (ValueError, TypeError):
                        pass
            logger.info("** Sleeping %.1fs after HTTP error, retrying (%d left)", delay, _retries_left)
            time.sleep(delay)
            return request(method, url, _retries_left=_retries_left - 1, **kwargs)
        raise RuntimeError(
            "See log for error text that has been sanitized for secrets"
        ) from None

    # Handle GitHub secondary rate limit (429 or 403 with Retry-After)
    if response.status_code in (429, 403) and response.headers.get("Retry-After") and _retries_left > 0:
        try:
            delay = max(float(response.headers["Retry-After"]), 0.1)
        except (ValueError, TypeError):
            delay = DEFAULT_RETRY_DELAY
        logger.warning("GitHub secondary rate limit (%d), sleeping %.1fs (%d retries left)",
                       response.status_code, delay, _retries_left)
        time.sleep(delay)
        return request(method, url, _retries_left=_retries_left - 1, **kwargs)

    if not from_cache and remaining >= 0:
        rate_limit_remaining = remaining
        reset_ts = response.headers.get("X-RateLimit-Reset")
        if reset_ts:
            rate_limit_reset_at = datetime.datetime.fromtimestamp(int(reset_ts)).isoformat()
        else:
            rate_limit_reset_at = None
        if remaining % 10 == 0 or remaining < 20:
            logging.info(
                "%d/%s requests remaining this hour (pool: %s)",
                remaining,
                response.headers.get("X-RateLimit-Limit", "?"),
                response.headers.get("X-RateLimit-Resource", "?"),
            )
    if not from_cache and remaining == 0:
        logger.warning(
            "GitHub API Rate Limit reached. Pausing until Rate Limit reset."
        )
        reset_header = response.headers.get("X-RateLimit-Reset")
        rate_limit_reset = datetime.datetime.fromtimestamp(
             int(reset_header) if reset_header
             else (datetime.datetime.now() + datetime.timedelta(seconds=-1) )
        )
        logging.warning(
            "GitHub API Rate Limit reached. Pausing until Rate Limit reset."
        )
        # This datetime.now() is correct, *because* `fromtimestamp` above
        # converts the timestamp into local time, same as now(). This is
        # different than the sites that use GH_INTERFACE.get_rate_limit, in
        # which the rate limit is a UTC time, so it has to be compared to
        # utcnow.
        while datetime.datetime.now() < rate_limit_reset:
            logger.warning("Rate Limit will reset at: %s", rate_limit_reset)
            reset_diff = rate_limit_reset - datetime.datetime.now()

            logger.info("Sleeping %s seconds", reset_diff.seconds)
            time.sleep(reset_diff.seconds + 1)

        if remaining % 10 == 0:
            logger.info("%d requests remaining this hour", remaining)

    if not from_cache and remaining == -1:
        # No rate limit headers — assume we're fine; clear any stale warning
        rate_limit_remaining = None
        rate_limit_reset_at = None
        if logger.level == logging.DEBUG:
            logger.debug(f"-- Github responded with no rate limit info, possible problems, printing reponse ({response.status_code}):")
            logger.debug(f"Request ({method}) - URL: {url}")
            logger.debug(f"Response text: {response.text}")
            for header in response.headers:
                logger.debug(f"Response header {header}={response.headers.get(header)}")
            logger.debug("-- Continuing as if nothings wrong 😇")
        else:
            logger.warning("GitHub responded with no rate info, continuing as if nothings wrong 😇")

    return response


get = functools.partial(request, "get")
post = functools.partial(request, "post")
put = functools.partial(request, "put")
delete = functools.partial(request, "delete")
patch = functools.partial(request, "patch")
