"""Smoke test: start Camoufox on a virtual display with humanized cursor and open a neutral page."""
from camoufox.sync_api import Camoufox

with Camoufox(headless="virtual", humanize=True) as browser:
    page = browser.new_page()
    page.goto("https://example.com", timeout=45000)
    print("title:", page.title())
    page.mouse.move(300, 200)
    page.click("a")
    page.wait_for_timeout(2000)
    print("after click url:", page.url)
