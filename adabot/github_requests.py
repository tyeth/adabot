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

logger = logging.getLogger(__name__)

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


def request(method, url, **kwargs):
    """Processes request for `url`."""
    try:
        response = getattr(requests, method)(
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
    except requests.RequestException:
        exception_text = traceback.format_exc()
        if "ADABOT_GITHUB_ACCESS_TOKEN" in os.environ:
            exception_text = exception_text.replace(
                os.environ["ADABOT_GITHUB_ACCESS_TOKEN"], "[secure]"
            )
        logger.critical("%s", exception_text)
        if(method=="get"): # getting temporary errors with large number of API fetches
            logger.info("** Sleeping 3 seconds after HTTP Get Error before retrying")
            time.sleep(3)
            return request(method, url, **kwargs)
        raise RuntimeError(
            "See log for error text that has been sanitized for secrets"
        ) from None

    if not from_cache:
        if remaining % 10 == 0 or (-1 < remaining < 20):
            logging.info("%d requests remaining this hour", remaining)
    if not from_cache and remaining == 0:
        logger.warning(
            "GitHub API Rate Limit reached. Pausing until Rate Limit reset."
        )
        rate_limit_reset = datetime.datetime.fromtimestamp(
             int(response.headers["X-RateLimit-Reset"]) if hasattr(response.headers, "X-RateLimit-Reset")                 else (datetime.datetime.now() + datetime.timedelta(seconds=-1) )
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
            logger.info(remaining, "requests remaining this hour")

    if remaining == -1:
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
