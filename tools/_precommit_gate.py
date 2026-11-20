# -*- coding: utf-8 -*-
"""Final pre-commit gate: scan the ACTUAL staged git blobs for credentials.

Reads secrets from local private files, then checks every staged blob via
`git show :<path>` so the verdict applies to exactly what will be committed.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The panel is a separate project: the muse2api deployment it drives keeps its
# own credentials, and those must never leak into this repo either.
def _muse_dir():
    value = os.environ.get("MUSE2API_DIR", "").strip()
    if not value:
        try:
            with open(os.path.join(ROOT, "panel.env"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("MUSE2API_DIR="):
                        value = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except OSError:
            pass
    if not value:
        value = os.path.join(os.path.dirname(ROOT), "muse2api")
    return os.path.abspath(value)


MUSE_DIR = _muse_dir()

SECRET_FILES = ["data/chain.env", "data/muse.env", ".env"]
# Same relative names, but inside the muse2api deployment.
MUSE_SECRET_FILES = [os.path.join(MUSE_DIR, ".env"),
                     os.path.join(MUSE_DIR, "data", "muse.env"),
                     os.path.join(MUSE_DIR, "data", "chain.env"),
                     os.path.join(MUSE_DIR, "data", "PROXY-NOTES.md")]
ALLOW = {
    # Publicly routable addresses that appear in documentation are not
    # secrets. Operator-specific ones belong here, not in the source.
}
BENIGN_KEYS = {
    "HOST", "PORT", "CHAIN_LISTEN", "PROTOCOL", "MODE", "UPSTREAM_HOST",
    "UPSTREAM_PORT", "UPSTREAM_USER", "JUMP_URL", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "ALL_PROXY", "MUSE2API_HOST", "MUSE2API_PORT", "LOG_LEVEL",
    "MUSE2API_BASE_URL", "BASE_URL", "CHAIN_LOG",
}
BENIGN_VALUE = re.compile(
    r"^(?:\d{1,5}|localhost|127\.0\.0\.1(?::\d+)?|0\.0\.0\.0(?::\d+)?"
    r"|https?://(?:localhost|127\.0\.0\.1)(?::\d+)?)$"
)


def git(*args):
    out = subprocess.run(
        ["git", "-C", ROOT] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return out.stdout.decode("utf-8", "replace"), out.returncode


def _is_placeholder(value):
    """True when a token is a reference to a secret, not the secret itself."""
    v = value.strip()
    if not v:
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", v):      # MUSE2API_KEY, CHAIN_SOCKS_HOST
        return True
    if re.fullmatch(r"[\w.-]*(your|my|the)[-_][\w.-]*", v, re.I):
        return True
    if v.startswith(("http://", "https://", "127.0.0.1", "localhost")):
        return True
    return False


def _secret_files():
    """Every file that may hold credentials, not just the fixed list.

    New services bring their own .env, so a fixed path list would miss
    them - a key staged from services/<name>/.env has to be caught too.
    """
    files = list(SECRET_FILES)
    for root, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules", ".pytest_cache")]
        for name in names:
            if name == ".env" or name.endswith(".env"):
                rel = os.path.relpath(os.path.join(root, name), ROOT)
                if rel not in files:
                    files.append(rel)
    return files


def load_secrets():
    secrets = []
    for rel in _secret_files():
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().upper()
                val = val.strip().strip('"').strip("'")
                if len(val) < 4 or key in BENIGN_KEYS or BENIGN_VALUE.match(val):
                    continue
                secrets.append((rel + ":" + key, val))
    # Credentials living in the muse2api deployment are just as secret.
    for path in MUSE_SECRET_FILES:
        if not os.path.isfile(path):
            continue
        label = os.path.relpath(path, MUSE_DIR)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().upper()
            val = val.strip().strip('"').strip("'")
            if len(val) < 4 or key in BENIGN_KEYS or BENIGN_VALUE.match(val):
                continue
            secrets.append((label + ":" + key, val))
        # Free-form notes files hold credentials in two shapes: a markdown
        # table cell with the value in backticks, or a "pass : value" line
        # inside a code block. Only those are credentials - scanning prose
        # would flag product names like "iKuuuVPN" or "muse2api".
        if path.endswith(".md"):
            for line in body.splitlines():
                row = re.match(r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|", line)
                if row and re.search(r"密码|口令|账号|凭证|pass|secret|token|key|user",
                                     row.group(1), re.I):
                    value = row.group(2).strip()
                    # Placeholders are not secrets: "<MUSE2API_KEY>", "MUSE2API_KEY",
                    # "your-api-key" and friends.
                    if not _is_placeholder(value):
                        secrets.append((label + ":credential", value))
                    continue
                pair = re.match(r"^\s*(pass(?:word)?|user(?:name)?|secret|token|api[_ -]?key)"
                                r"\s*[:=]\s*(\S+)\s*$", line, re.I)
                if pair:
                    value = pair.group(2).strip().strip("`")
                    if len(value) >= 6 and not value.startswith("http"):
                        secrets.append((label + ":credential", value))
    return secrets


def main():
    staged, _ = git("diff", "--cached", "--name-only")
    files = [f for f in staged.splitlines() if f.strip()]
    secrets = load_secrets()
    print("staged files: %d" % len(files))
    print("secret literals checked: %d" % len(secrets))

    leaks = []
    for rel in files:
        blob, rc = git("show", ":" + rel)
        if rc != 0:
            continue
        for label, val in secrets:
            if val in ALLOW:
                continue
            if val in blob:
                leaks.append((rel, label, val))

    if leaks:
        print("\n*** BLOCKED: credentials found in staged blobs ***")
        for rel, label, val in leaks:
            m = val[:2] + "*" * max(0, len(val) - 4) + val[-2:] if len(val) > 4 else "*" * len(val)
            print("  %-40s <- %-28s %s" % (rel, label, m))
        return 1

    print("\nPASS: staged blobs contain no credential literals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
