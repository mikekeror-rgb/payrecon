"""Keep the hosted Streamlit demo awake (run by .github/workflows/keep-awake.yml).

Streamlit Community Cloud puts an app to sleep after a period without visitors. A sleeping app shows a page with a
"Yes, get this app back up!" button, so a plain HTTP request does not wake it: this opens the app in a headless
browser, clicks that button if it is there, and waits for the app to start.

    python scripts/wake_streamlit.py https://<your-app>.streamlit.app

Once the app is up it also clicks a button inside it (Advanced -> Dry run, which changes nothing), so the visit is
real use of the app, not just a page load. Exit code 0 when the app is up and was clicked, 1 otherwise.
"""
from __future__ import annotations

import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

WAKE_TEXT = "get this app back up"  # the button reads "Yes, get this app back up!"


def _click_in_app(page, timeout_s: float) -> bool:
    """Streamlit Cloud shows the app inside an iframe; look in every frame for the Dry run button and press it."""
    deadline = timeout_s * 1000
    waited = 0
    while waited <= deadline:
        for frame in page.frames:
            dry = frame.get_by_role("button", name="Dry run")
            if dry.count() == 0:
                advanced = frame.get_by_text("Advanced", exact=True)
                if advanced.count():
                    advanced.first.click()  # open the sidebar section that holds the button
                    frame.wait_for_timeout(1_000)
                    dry = frame.get_by_role("button", name="Dry run")
            if dry.count():
                dry.first.click()
                frame.wait_for_timeout(3_000)
                return True
        page.wait_for_timeout(5_000)
        waited += 5_000
    return False


def wake(url: str, timeout_s: float = 180) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        state = "awake"
        button = page.get_by_role("button", name=WAKE_TEXT)
        if button.count():
            button.first.click()
            state = "woken"
            try:  # the button disappears once the app starts booting; give it time to come up
                page.get_by_role("button", name=WAKE_TEXT).first.wait_for(state="detached", timeout=timeout_s * 1000)
            except PlaywrightTimeout:
                browser.close()
                return "clicked wake-up, but the app did not come back within the timeout"
        clicked = _click_in_app(page, timeout_s)
        browser.close()
        return f"{state}, clicked Dry run" if clicked else f"{state}, but the Dry run button was not found"


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        print("usage: python scripts/wake_streamlit.py https://<your-app>.streamlit.app")
        sys.exit(2)
    result = wake(sys.argv[1])
    print(f"{sys.argv[1]}: {result}")
    sys.exit(0 if result.endswith("clicked Dry run") else 1)
