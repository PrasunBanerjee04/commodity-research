"""Real FastAPI/Chromium regressions for complete-history CAISO panels."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import polars as pl
import uvicorn

from comm_research.dashboard.server import create_app


@unittest.skipUnless(
    importlib.util.find_spec("playwright"), "Install playwright for browser checks"
)
class CaisoServerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(
                executable_path=os.environ.get("DASHBOARD_CHROMIUM")
                or shutil.which("chromium"),
                args=["--no-sandbox"],
            )
        except Error as error:
            cls.playwright.stop()
            raise unittest.SkipTest("Install Chromium") from error
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        for market, minutes, count, column in (
            ("DAM", 60, 24 * 90, "mw"),
            ("RTM", 5, 288 * 90, "value"),
        ):
            rows = [
                {
                    "interval_start_time_gmt": start + timedelta(minutes=i * minutes),
                    "node": node,
                    "lmp_type": component,
                    column: float(30 + i % 19 + offset),
                    "market_run_id": market,
                }
                for i in range(count)
                for node in ("TH_NP15_GEN-APND", "TH_SP15_GEN-APND")
                for component, offset in (
                    ("LMP", 0),
                    ("MCE", -2),
                    ("MCC", 3),
                    ("MCL", -1),
                )
            ]
            path = (
                cls.root
                / f"power_gas/napg/caiso/{market.lower()}_lmp/year=2023/month=01/day=01"
            )
            path.mkdir(parents=True)
            pl.DataFrame(rows).write_parquet(path / "data.parquet")
        # Generic feeds exercise four-pane presets alongside the CAISO panels.
        for feed in ("oil/wti/settlements", "power_gas/napg/caiso/load_forecast"):
            path = cls.root / feed
            path.mkdir(parents=True)
            pl.DataFrame(
                {"timestamp": [start, start + timedelta(days=1)], "price": [10.0, 20.0]}
            ).write_parquet(path / "data.parquet")
        cls.socket = socket.socket()
        cls.socket.bind(("127.0.0.1", 0))
        cls.port = cls.socket.getsockname()[1]
        cls.server = uvicorn.Server(
            uvicorn.Config(create_app(cls.root), log_level="error")
        )
        cls.thread = threading.Thread(
            target=lambda: cls.server.run(sockets=[cls.socket]), daemon=True
        )
        cls.thread.start()
        deadline = time.monotonic() + 15
        while (
            not cls.server.started
            and cls.thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("FastAPI startup failed")
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(10)
        cls.socket.close()
        cls.browser.close()
        cls.playwright.stop()
        cls.temporary.cleanup()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.errors = []
        self.requests = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on(
            "request",
            lambda request: (
                self.requests.append(request.url) if "/api/" in request.url else None
            ),
        )
        self.page.add_init_script(
            """document.addEventListener('chart-view-updated',event=>{window.draws??=[];window.draws.push({...event.detail,ms:performance.now()})});"""
        )

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.page.close()

    def open(self):
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "document.querySelectorAll('.caiso-chart canvas').length===2"
        )
        self.page.wait_for_function(
            "[...document.querySelectorAll('.caiso-resolution')].every(el=>el.textContent.includes('EXACT TIMESTAMPS'))"
        )

    def test_real_http_complete_pivot_aliases_and_missing_node(self):
        with urlopen(self.url + "/api/data?feed=rt_lmp&node=missing") as response:
            data = json.load(response)
        self.assertEqual(data["node"], "TH_NP15_GEN-APND")
        self.assertEqual(len(data["timestamps"]), 25920)
        self.assertEqual(data["energy"][0], 28)
        self.assertEqual(data["congestion"][0], 33)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/api/data?feed=unknown")
        self.assertEqual(error.exception.code, 400)

    def test_both_panes_mount_and_utc_exact_tooltip(self):
        self.open()
        self.assertTrue(
            self.page.locator(".caiso-empty").evaluate_all(
                "(els)=>els.every(el=>el.hidden)"
            )
        )
        dimensions = self.page.locator(".caiso-chart").evaluate_all(
            "(els)=>els.map(el=>({w:el.clientWidth,h:el.clientHeight}))"
        )
        self.assertTrue(all(item["w"] > 250 and item["h"] > 250 for item in dimensions))
        over = self.page.locator(".caiso-panel").first.locator(".u-over")
        box = over.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        tooltip = self.page.locator(".caiso-tooltip").first
        self.assertTrue(tooltip.is_visible())
        self.assertRegex(tooltip.inner_text(), r"2023-\d\d-\d\d \d\d:\d\d UTC")
        self.assertIn("/MWh", tooltip.inner_text())
        self.assertEqual(tooltip.locator(".caiso-tooltip-row").count(), 4)
        timestamp = datetime.strptime(
            tooltip.locator(".caiso-tooltip-time").inner_text()[:16], "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
        hour = int(
            (timestamp - datetime(2023, 1, 1, tzinfo=timezone.utc)).total_seconds()
            / 3600
        )
        self.assertEqual(
            tooltip.locator(".caiso-tooltip-row strong").all_text_contents(),
            [
                f"${30 + hour % 19:.2f}/MWh",
                f"${28 + hour % 19:.2f}",
                f"${33 + hour % 19:.2f}",
                f"${29 + hour % 19:.2f}",
            ],
        )
        pixels = self.page.locator(".caiso-chart canvas").evaluate_all("""els=>els.map(canvas=>{
          const pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
          const colors=[[41,98,255],[8,153,129],[242,54,69],[255,152,0]],counts=[0,0,0,0];
          for(let i=0;i<pixels.length;i+=4) for(let c=0;c<colors.length;c++)
            if(colors[c].every((value,k)=>pixels[i+k]===value)) counts[c]++;
          return counts;
        })""")
        self.assertTrue(all(count > 10 for pane in pixels for count in pane), pixels)
        self.assertFalse(any("/api/series" in request for request in self.requests))

    def test_visibility_ranges_node_cache_and_warm_latency(self):
        self.open()
        panel = self.page.locator(".caiso-panel").nth(1)
        panel.locator("select").select_option("TH_SP15_GEN-APND")
        self.page.wait_for_function(
            "document.querySelectorAll('.caiso-panel')[1].dataset.activeNode==='TH_SP15_GEN-APND'"
        )
        panel.locator("select").select_option("TH_NP15_GEN-APND")
        self.page.wait_for_function(
            "document.querySelectorAll('.caiso-panel')[1].dataset.activeNode==='TH_NP15_GEN-APND'"
        )
        self.page.evaluate(
            "new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))"
        )
        before = len(self.requests)
        result = panel.evaluate("""async root=>{
          const canvas=root.querySelector('canvas'), times=[], paints=[];
          const buttons=[...root.querySelectorAll('.caiso-component'),...root.querySelectorAll('[data-range]')];
          for(let i=0;i<36;i++) {
            const start=performance.now(); buttons[i%buttons.length].click();
            canvas.getContext('2d').getImageData(0,0,1,1); times.push(performance.now()-start);
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))); paints.push(performance.now()-start);
          }
          return {max:Math.max(...times),paintMax:Math.max(...paints),sameCanvas:canvas===root.querySelector('canvas')};
        }""")
        print("CAISO full-history warm timings:", result)
        self.assertLess(result["max"], 50)
        self.assertLess(result["paintMax"], 50)
        self.assertTrue(result["sameCanvas"])
        panel.locator("select").select_option("TH_SP15_GEN-APND")
        self.page.wait_for_timeout(50)
        self.assertEqual(len(self.requests), before)

    def test_zoom_box_pan_wheel_and_panel_independence(self):
        self.open()
        before = len(self.requests)
        initial = self.page.evaluate(
            "window.draws.filter(item=>item.feed.endsWith('rtm_lmp')).at(-1)"
        )
        over = self.page.locator(".caiso-panel").nth(1).locator(".u-over")
        box = over.bounding_box()
        self.page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + 80)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + box["width"] * 0.7, box["y"] + 80, steps=8)
        self.page.mouse.up()
        zoomed = self.page.evaluate(
            "window.draws.filter(item=>item.feed.endsWith('rtm_lmp')).at(-1)"
        )
        self.assertLess(zoomed["max"] - zoomed["min"], initial["max"] - initial["min"])
        self.page.keyboard.down("Shift")
        self.page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + 80)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + 80, steps=5)
        self.page.mouse.up()
        self.page.keyboard.up("Shift")
        panned = self.page.evaluate(
            "window.draws.filter(item=>item.feed.endsWith('rtm_lmp')).at(-1)"
        )
        self.assertNotEqual(panned["min"], zoomed["min"])
        self.page.mouse.wheel(0, -100)
        self.page.wait_for_timeout(50)
        wheeled = self.page.evaluate(
            "window.draws.filter(item=>item.feed.endsWith('rtm_lmp')).at(-1)"
        )
        self.assertLess(wheeled["max"] - wheeled["min"], panned["max"] - panned["min"])
        self.assertEqual(len(self.requests), before)
        self.assertEqual(
            self.page.locator(".caiso-panel")
            .first.locator("[data-range=ALL]")
            .get_attribute("class"),
            "active",
        )

    def test_singleton_layout_presets_and_order(self):
        self.open()
        before = len(self.requests)
        self.page.locator("#dataset-search").fill("lmp")
        for label in ("Real-Time LMP", "Day-Ahead LMP", "Real-Time LMP"):
            self.page.get_by_role("button", name=label, exact=True).click()
        self.assertEqual(self.page.locator(".caiso-panel").count(), 2)
        self.page.locator('[data-layout="4"]').click()
        self.page.wait_for_function(
            "document.querySelectorAll('.uplot canvas').length>=4"
        )
        self.page.locator('[data-layout="2"]').click()
        self.page.wait_for_timeout(100)
        dam = self.page.locator(".caiso-panel").first.bounding_box()
        rtm = self.page.locator(".caiso-panel").nth(1).bounding_box()
        self.assertLess(dam["x"], rtm["x"])
        self.assertEqual(
            sum("/api/data?" in item for item in self.requests),
            sum("/api/data?" in item for item in self.requests[:before]),
        )

    def test_http_failure_and_missing_chart_dependency_show_errors(self):
        self.page.route(
            "**/api/data?*",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"error":"TEST_FAILURE"}',
            ),
        )
        self.page.goto(self.url)
        self.page.wait_for_function(
            "document.querySelectorAll('.caiso-empty').length===2 && [...document.querySelectorAll('.caiso-empty')].every(el=>el.textContent.includes('TEST_FAILURE'))"
        )
        self.assertTrue(self.page.locator(".caiso-empty").first.is_visible())
        self.page.unroute("**/api/data?*")
        self.page.route(
            "**/vendor/uPlot.iife.min.js",
            lambda route: route.fulfill(content_type="text/javascript", body=""),
        )
        self.page.reload()
        self.page.wait_for_function(
            "document.querySelector('.caiso-empty')?.textContent.includes('ERR_DEPENDENCY_LOAD_FAILED')"
        )


if __name__ == "__main__":
    unittest.main()
