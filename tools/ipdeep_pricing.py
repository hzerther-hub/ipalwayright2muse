"""Extract the FULL IPDEEP special-offer table (country, city, price, CIDR).

The page embeds its price tree as JSON inside the Next.js flight payload, with
`IsSpecialOffer: 1` marking special-offer entries and `IpSection` carrying the
actual network prefix for each purchasable IP.
"""

import json
import re
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/160.0.0.0 Safari/537.36"
)

URLS = [
    "https://www.ipdeep.cn/static-residence",
    "https://www.ipdeep.cn/static-proxy-deals",
    "https://www.ipdeep.cn/locations/jp-proxy",
    "https://www.ipdeep.cn/locations/ca-proxy",
]

# Muse consumer availability (2026-11-17): US, CA, JP, SG, MX, AU
MUSE_OK = {"US", "CA", "JP", "SG", "MX", "AU"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:
        return "ERR:" + str(exc)[:120]


def find_trees(body):
    """Find every price tree blob.

    The page embeds the tree twice-escaped (\\" inside a JS string), so we
    unescape first, then let json's raw_decode consume exactly one array.
    """
    trees = []
    idx = 0
    decoder = json.JSONDecoder()
    while True:
        i = body.find("priceInfos", idx)
        if i < 0:
            break
        idx = i + 1
        start = body.find("[", i)
        if start < 0:
            continue
        try:
            nodes, _end = decoder.raw_decode(unescape(body[start:]))
        except ValueError:
            continue
        trees.append(nodes)
    return trees


def unescape(text):
    return (
        text.replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\\\", "\\")
        .replace('\\u0026', "&")
    )


def walk(nodes, path, out):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("Name") or ""
        code = node.get("Code") or ""
        price = node.get("Price")
        special = node.get("IsSpecialOffer")
        ipsec = node.get("IpSection")
        newpath = path + [(code, name)]
        if price is not None and ipsec:
            out.append(
                {
                    "path": newpath,
                    "code": code,
                    "name": name,
                    "price": price,
                    "special": special,
                    "cidr": ipsec,
                }
            )
        kids = node.get("Children") or []
        if kids:
            walk(kids, newpath, out)


def main():
    rows = {}
    for url in URLS:
        body = fetch(url)
        if body.startswith("ERR:"):
            print("skip", url, body)
            continue
        for nodes in find_trees(body):
            out = []
            walk(nodes, [], out)
            for row in out:
                key = (row["cidr"], row["price"])
                rows.setdefault(key, row)

    print("=" * 96)
    print("IPDEEP STATIC RESIDENTIAL - FULL PRICE TABLE (with real CIDR)")
    print("=" * 96)
    print("%-10s %-14s %-9s %-8s %-22s %s" % ("CODE", "CITY", "PRICE", "SPECIAL", "CIDR", "MUSE_OK"))
    print("-" * 96)

    ordered = sorted(rows.values(), key=lambda r: (r["price"], r["code"]))
    for row in ordered:
        leaf = row["path"][-1]
        country_code = row["path"][-2][0] if len(row["path"]) > 1 else leaf[0]
        country_name = row["path"][-2][1] if len(row["path"]) > 1 else ""
        ok = "YES" if country_code in MUSE_OK else "no"
        print(
            "%-10s %-14s %-9s $%-8s %-8s %-22s %s"
            % (
                country_code,
                (country_name or "")[:13],
                (leaf[1] or "")[:13],
                row["price"],
                "SPECIAL" if row["special"] else "",
                row["cidr"],
                ok,
            )
        )

    best = []
    for row in ordered:
        country_code = row["path"][-2][0] if len(row["path"]) > 1 else row["path"][-1][0]
        if country_code in MUSE_OK:
            best.append((country_code, row))

    print()
    print("=" * 96)
    print("CHEAPEST Muse-eligible options (US/CA/JP/SG/MX/AU):")
    print("=" * 96)
    for country_code, row in best[:14]:
        leaf = row["path"][-1]
        country_name = row["path"][-2][1] if len(row["path"]) > 1 else ""
        print(
            "  $%-6s %-4s %-12s %-12s %-22s %s"
            % (
                row["price"],
                country_code,
                (country_name or "")[:11],
                (leaf[1] or "")[:11],
                row["cidr"],
                "SPECIAL" if row["special"] else "",
            )
        )


if __name__ == "__main__":
    main()
