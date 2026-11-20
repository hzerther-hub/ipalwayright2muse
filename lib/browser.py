# -*- coding: utf-8 -*-
"""Chromium / CDP endpoints the panel needs.

The panel drives browsers itself, so it needs to know which Chromium is
installed and where muse2api's own browser listens. Kept here so the panel
does not depend on muse2api's config module.
"""
from __future__ import annotations

import os
import shutil


def detect_chromium():
    """The Chromium/Chrome binary to drive.

    Honours MUSE2API_CHROMIUM, then falls back to probing the usual install
    locations. Same order muse2api's own config uses, so the panel and the
    service it audits agree on which browser is in play.
    """
    configured = os.environ.get("MUSE2API_CHROMIUM", "").strip()
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) or shutil.which(candidate):
            return candidate
    return configured or "chromium"


def muse_cdp_port():
    """Where muse2api's own browser exposes its CDP endpoint."""
    try:
        return int(os.environ.get("MUSE2API_CDP_PORT", "19210"))
    except (TypeError, ValueError):
        return 19210
