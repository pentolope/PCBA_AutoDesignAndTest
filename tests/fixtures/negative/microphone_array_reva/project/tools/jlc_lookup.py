"""Query the JLCPCB assembly parts catalogue.

Used during component selection to confirm that every part on the BOM is
actually orderable for JLCPCB assembly, and to capture stock, library type
(Basic/Extended) and package for the source manifest.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

SEARCH_URL = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/"
    "shoppingCart/smtGood/selectSmtComponentList"
)

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://jlcpcb.com",
    "Referer": "https://jlcpcb.com/parts",
}

FIELDS = (
    "componentCode",
    "componentModelEn",
    "componentBrandEn",
    "componentSpecificationEn",
    "componentLibraryType",
    "stockCount",
    "describe",
)


def search(keyword, page_size=8):
    payload = {
        "currentPage": 1,
        "pageSize": page_size,
        "keyword": keyword,
        "componentLibraryType": None,
        "searchSource": "search",
    }
    request = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.loads(response.read().decode("utf-8"))
    rows = (body.get("data") or {}).get("componentPageInfo") or {}
    return rows.get("list") or []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keywords", nargs="+")
    parser.add_argument("--page-size", type=int, default=8)
    args = parser.parse_args()

    failures = 0
    for keyword in args.keywords:
        print(f"===== {keyword} =====")
        try:
            results = search(keyword, args.page_size)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"  LOOKUP FAILED: {exc}")
            failures += 1
            continue
        if not results:
            print("  no results")
            failures += 1
            continue
        for item in results:
            print("  " + " | ".join(str(item.get(field, "")) for field in FIELDS))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
