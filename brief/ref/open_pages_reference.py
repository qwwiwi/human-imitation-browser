"""Open Ozon pages in Camoufox through the residential proxy; print status/title only, never credentials."""
import json
import os
import random
import sys
import urllib.request
from pathlib import Path

from camoufox.sync_api import Camoufox

OUT = Path(sys.argv[1])
req = urllib.request.Request("https://mobileproxy.space/api.html?command=get_my_proxy",
                             headers={"Authorization": f"Bearer {os.environ['MOBILEPROXY_API_TOKEN']}"})
px = json.load(urllib.request.urlopen(req, timeout=20))[0]
proxy = {"server": f"http://{px['proxy_hostname']}:{px['proxy_http_port']}",
         "username": px["proxy_login"], "password": px["proxy_pass"]}
URLS = [("home", "https://www.ozon.ru/"), ("search", "https://www.ozon.ru/search/?text=чайник"),
        ("seller", "https://seller.ozon.ru/")]
with Camoufox(headless="virtual", humanize=True, geoip=True, proxy=proxy, locale="ru-RU", 
              i_know_what_im_doing=True) as browser:
    page = browser.new_page()
    try:
        r = page.goto("https://ipinfo.io/json", timeout=45000)
        info = r.json()
        print("exit:", info.get("country"), "|", info.get("org"), "|", info.get("city"))
    except Exception as exc:
        print("exit check error", type(exc).__name__, str(exc)[:150])
    for name, url in URLS:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(random.randint(6000, 9000))
            page.mouse.move(random.randint(200, 900), random.randint(150, 500))
            page.mouse.wheel(0, random.randint(300, 700))
            page.wait_for_timeout(random.randint(5000, 7000)); page.mouse.wheel(0, -1000); page.wait_for_timeout(2500)
            body = " ".join(page.inner_text("body").split())
            print(name, "| status", resp.status if resp else None, "| title", page.title()[:70], "| text", len(body), "|", body[:140])
            page.screenshot(path=str(OUT / f"img-{name}.png"))
        except Exception as exc:
            print(name, "error", type(exc).__name__, str(exc)[:200])
