"""Acquisition: the only code in this toolkit that touches the network.

Validation is deterministic and offline; nothing it runs imports this module.
Acquisition exists behind its own command and does exactly one thing: fetch
the declared official sources, parse them, and hand back what it read. It
writes nothing into the catalog. Whether a change is trustworthy is decided by
a person reading the diff and committing, which is the only path into
`approved.json`.

Every acquisition records the source URLs, retrieval time, raw byte digests
and the parser's version, so "the fabricator changed its data", "the
fabricator restyled its page", "our parser changed" and "our parser broke"
remain four distinguishable histories.
"""

from __future__ import annotations

import datetime
import hashlib

from . import jlcpcb, model
from .store import (OUTCOME_COMPLETE, OUTCOME_INCOMPLETE,
                    OUTCOME_PARSE_FAILED)

#: Sent because some CDNs refuse the default urllib agent outright. This
#: identifies an ordinary browser fetch of public documentation pages.
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0 Safari/537.36")

DEFAULT_TIMEOUT_S = 60

class AcquisitionError(Exception):
    pass


def _fetch(url, timeout):
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def acquire(timeout=DEFAULT_TIMEOUT_S, fetcher=None):
    """Fetch and parse the declared sources.

    `fetcher` is injectable (url -> bytes) so every failure mode is testable
    without a network and without depending on the fabricator being up.

    Returns (result, problem). `result` always carries what was fetched, with
    its digests, whatever the outcome; `problem` explains why no usable
    catalog came out of it, or is None on a complete acquisition. A partial
    parse is never attempted: a catalog built from some of the sources would
    silently shrink the offer.
    """
    fetch = fetcher or (lambda url: _fetch(url, timeout))

    raw_sources, errors = {}, []
    for spec in jlcpcb.SOURCES:
        try:
            raw_sources[spec["id"]] = fetch(spec["url"])
        except Exception as exc:                      # noqa: BLE001 - report
            errors.append({"source": spec["id"], "url": spec["url"],
                           "error": "{}: {}".format(type(exc).__name__, exc)})

    result = {
        "schema_version": 3,
        "fabricator": model.FABRICATOR,
        "retrieved_utc": datetime.datetime.now(
            datetime.timezone.utc).replace(microsecond=0).isoformat(),
        "parser": {"id": "pcbqa.fabricators.jlcpcb",
                   "version": jlcpcb.PARSER_VERSION},
        "declared_source_ids": sorted(spec["id"] for spec in jlcpcb.SOURCES),
        "raw": raw_sources,
        "sources": [{"id": spec["id"], "url": spec["url"],
                     "sha256_raw": hashlib.sha256(
                         raw_sources[spec["id"]]).hexdigest()}
                    for spec in jlcpcb.SOURCES
                    if spec["id"] in raw_sources],
        "errors": errors,
        "normalized": None,
        "outcome": OUTCOME_COMPLETE,
    }

    if errors:
        result["outcome"] = OUTCOME_INCOMPLETE
        return result, "acquisition incomplete: {} source(s) failed".format(
            len(errors))

    try:
        result["normalized"] = jlcpcb.parse(raw_sources)
        result["normalized_sha256"] = model.normalized_digest(
            result["normalized"])
    except model.CatalogError as exc:
        result["outcome"] = OUTCOME_PARSE_FAILED
        result["errors"] = [{"source": "<parse>",
                             "error": "{}: {}".format(type(exc).__name__, exc)}]
        return result, "parse failed: {}".format(exc)
    return result, None
