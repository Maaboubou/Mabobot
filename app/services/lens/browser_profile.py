"""The browser profile used by sbigg's Google Lens community client.

Adapted from google_lens_pro/core/config.py (MIT community core).
Copyright (c) 2026 Google Lens Scraper Contributors.
See licenses/google-lens-community-MIT.txt.
No saved sessions, personal cookies, or proprietary modules are used.
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    f"--user-agent={USER_AGENT}",
]
HEADERS = {
    "User-Agent": USER_AGENT,
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
CONSENT_COOKIES = [
    {"name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg", "domain": ".google.com", "path": "/"},
    {"name": "CONSENT", "value": "PENDING+999", "domain": ".google.com", "path": "/"},
]
