# -*- coding: utf-8 -*-
"""Full IPDEEP static-residential catalogue with Muse eligibility.

The /static-residence page embeds the whole price tree in its Next.js flight
payload. Only promo nodes carry an IpSection (CIDR); every node carries a
country/city Code and a Price, which is enough to rank options.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipdeep_pricing import MUSE_OK, fetch, find_trees  # noqa: E402

PAGES = [
    "https://www.ipdeep.cn/static-residence",
    "https://www.ipdeep.cn/static-proxy-deals",
]


def walk(nodes, path, rows):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        code = node.get("Code") or ""
        name = node.get("Name") or ""
        price = node.get("Price")
        newpath = path + [(code, name)]
        if price not in (None, "None", ""):
            country = newpath[-2][0] if len(newpath) > 1 else code
            country_name = newpath[-2][1] if len(newpath) > 1 else ""
            city = newpath[-1][1] or name
            key = (country, city)
            rows.setdefault(key, {
                "country": country,
                "country_name": country_name,
                "city": city,
                "price": float(price),
                "special": str(node.get("IsSpecialOffer")) == "1",
                "cidr": node.get("IpSection") or "",
            })
        walk(node.get("Children") or [], newpath, rows)


def collect():
    rows = {}
    for url in PAGES:
        body = fetch(url)
        if body.startswith("ERR:"):
            print("skip", url, body, file=sys.stderr)
            continue
        for tree in find_trees(body):
            walk(tree, [], rows)
    return list(rows.values())


def main():
    rows = collect()
    ok = [r for r in rows if r["country"] in MUSE_OK]
    bad = [r for r in rows if r["country"] not in MUSE_OK]
    ok.sort(key=lambda r: (r["price"], r["country"]))

    print("=" * 92)
    print("IPDEEP STATIC RESIDENTIAL - Muse-eligible (US/CA/JP/SG/MX/AU)")
    print("=" * 92)
    print("%-6s %-12s %-12s %-8s %-8s %s" % ("CC", "COUNTRY", "CITY", "PRICE", "PROMO", "CIDR"))
    print("-" * 92)
    for r in ok:
        print("%-6s %-12s %-12s $%-7s %-8s %s" % (
            r["country"], r["country_name"][:11], r["city"][:11],
            r["price"], "yes" if r["special"] else "", r["cidr"][:22]))

    print("\ntotal nodes: %d   Muse-eligible: %d   ineligible: %d"
          % (len(rows), len(ok), len(bad)))
    print("eligible countries: %s" % ", ".join(sorted({r["country"] for r in ok})))
    print("ineligible countries: %s" % ", ".join(sorted({r["country"] for r in bad})))

    if "--json" in sys.argv:
        print(json.dumps({"eligible": ok, "all": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
