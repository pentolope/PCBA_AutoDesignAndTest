"""Acquisition: the only code in this toolkit that touches the network.

Validation and release are deterministic and offline; nothing they run
imports this module. Acquisition exists behind its own command, does exactly
one thing - fetch the adapter's declared official sources, parse them, and
record the result as the *observed* snapshot - and is built so its failures
are inert:

  * a fetch failure or timeout records an INCOMPLETE attempt with the
    per-source error; a parse failure (page redesign, implausibly small
    result, unexpected unit) records a PARSE-FAILED attempt carrying the
    exact raw bytes that defeated the parser. Either way the attempt
    becomes the newest observed state - superseding any older successful
    observation as "latest", so a failed acquisition can never leave an
    old observation looking current - and neither can ever be promoted;
  * nothing here reads, writes, or even opens the approved snapshot.

Every acquisition records the source URLs, retrieval time, raw byte digests
and the parser's identity, so "the fabricator changed its data", "the
fabricator restyled its page", "our parser changed" and "our parser broke"
remain four distinguishable histories.
"""

from __future__ import annotations

from . import adapter as _adapter
from . import model, store as _store

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


def acquire(fabricator, root, timeout=DEFAULT_TIMEOUT_S, fetcher=None):
    """Fetch, parse and record one fabricator's sources as observed.

    `fetcher` is injectable (url -> bytes) so every failure mode is testable
    without a network and without depending on the fabricator being up.

    Returns (snapshot, problem): every attempt writes a snapshot as the
    newest observed state; `problem` is the string explaining why no
    usable catalog came out of it, or None on a complete acquisition.
    """
    adapter = _adapter(fabricator)
    catalog_store = _store.CatalogStore(root, fabricator)
    fetch = fetcher or (lambda url: _fetch(url, timeout))

    raw_sources = {}
    errors = []
    for spec in adapter.SOURCES:
        try:
            raw_sources[spec["id"]] = fetch(spec["url"])
        except Exception as exc:                      # noqa: BLE001 - report
            errors.append({"source": spec["id"], "url": spec["url"],
                           "error": "{}: {}".format(type(exc).__name__, exc)})

    parser_identity = {"id": "pcbqa.fabricators." + fabricator,
                       "version": adapter.PARSER_VERSION}

    if errors:
        # Preserve whatever was fetched as evidence, record the attempt as
        # incomplete, and do not attempt a partial parse: a catalog built
        # from part of the sources would silently shrink the offer.
        snapshot = catalog_store.record_observation(
            None, raw_sources, parser_identity, adapter.SOURCES,
            outcome=_store.OUTCOME_INCOMPLETE, errors=errors)
        return snapshot, "acquisition incomplete: {} source(s) failed".format(
            len(errors))

    try:
        normalized = adapter.parse(raw_sources)
    except model.CatalogError as exc:
        # The attempt is recorded as the newest observed state, carrying
        # the exact bytes that defeated the parser: "the current source
        # cannot be interpreted" is a fact about NOW, and it must not leave
        # an older successful observation looking like the latest.
        snapshot = catalog_store.record_observation(
            None, raw_sources, parser_identity, adapter.SOURCES,
            outcome=_store.OUTCOME_PARSE_FAILED,
            errors=[{"source": "<parse>",
                     "error": "{}: {}".format(type(exc).__name__, exc)}])
        return snapshot, "parse failed: {}".format(exc)

    snapshot = catalog_store.record_observation(
        normalized, raw_sources, parser_identity, adapter.SOURCES,
        outcome=_store.OUTCOME_COMPLETE)
    return snapshot, None
