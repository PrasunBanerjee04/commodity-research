"""Live HTTP and Chromium coverage for three docked WebGL research views."""

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
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import uvicorn

from comm_research.dashboard.server import create_app
from tests.test_research_lake import write_research_fixture


@unittest.skipUnless(
    importlib.util.find_spec("playwright"), "Install playwright for browser tests"
)
class ResearchBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get("DASHBOARD_CHROMIUM")
            or shutil.which("chromium"),
            args=["--no-sandbox"],
        )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        write_research_fixture(cls.root)
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
            raise RuntimeError("Server startup failed")
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
        self.page.goto(self.url, wait_until="networkidle")

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.page.close()

    def open_feed(self, alias):
        self.page.locator("#dataset-search").fill(alias)
        self.page.locator("#dataset-tree .tree-leaf").first.click()
        panel = self.page.locator(f'.research-panel[data-feed$="{alias}"]')
        panel.locator(".research-chart.js-plotly-plot").wait_for()
        self.page.wait_for_function(
            "el=>el.dataset.activeEntity", arg=panel.element_handle()
        )
        return panel

    def click_and_wait(self, panel, selector):
        return panel.evaluate(
            """async(el,selector)=>{
          const start=performance.now();
          const done=new Promise(resolve=>el.addEventListener('chart-view-updated',()=>resolve(performance.now()-start),{once:true}));
          el.querySelector(selector).click();return done;
        }""",
            selector,
        )

    def test_crr_term_tou_reset_singleton_and_cached_node_switch(self):
        panel = self.open_feed("crr_bids")
        before = len(self.requests)
        self.assertEqual(panel.get_attribute("data-trace-count"), "4")
        self.click_and_wait(panel, '[aria-label="market_term: Seasonal"]')
        self.assertEqual(panel.get_attribute("data-trace-count"), "2")
        self.click_and_wait(panel, '[aria-label="time_of_use: OFF"]')
        self.assertEqual(panel.get_attribute("data-trace-count"), "1")
        self.click_and_wait(panel, ".panel-reset")
        self.assertEqual(panel.get_attribute("data-trace-count"), "4")
        for choice in ["1D", "1W", "1M", "ALL"]:
            self.click_and_wait(panel, f'[data-range="{choice}"]')
        self.assertEqual(len(self.requests), before)
        self.page.locator("#dataset-tree .tree-leaf").first.click()
        self.assertEqual(self.page.locator(".dv-tab").count(), 3)
        panel.locator(".research-search").fill("SP15")
        panel.locator(".caiso-node").select_option("SP15")
        self.page.wait_for_function(
            'el=>el.dataset.activeEntity==="SP15"', arg=panel.element_handle()
        )
        after = len(self.requests)
        panel.locator(".research-search").fill("")
        panel.locator(".caiso-node").select_option("NP15")
        self.page.wait_for_function(
            'el=>el.dataset.activeEntity==="NP15"', arg=panel.element_handle()
        )
        self.assertEqual(len(self.requests), after)
        self.assertTrue(
            panel.evaluate(
                "el=>el.querySelector('.research-chart').data.every(trace=>trace.type==='scattergl'&&trace.x.length<=1500)"
            )
        )
        self.assertNotIn("✓", panel.inner_text())
        self.assertEqual(panel.locator("input[type=checkbox]").count(), 0)
        legend = panel.locator(".chart-legend").bounding_box()
        viewport = panel.locator(".chart-viewport").bounding_box()
        self.assertLessEqual(legend["y"] + legend["height"], viewport["y"])

    def test_transmission_overlay_market_and_direction_use_local_history(self):
        panel = self.open_feed("transmission_usage")
        before = len(self.requests)
        traces = panel.locator(".research-chart").evaluate(
            "el=>el.data.map(trace=>({name:trace.name,dash:trace.line.dash}))"
        )
        self.assertEqual(len(traces), 4)
        self.assertTrue(
            any(
                "RATING_ATC" in trace["name"] and trace["dash"] == "dash"
                for trace in traces
            )
        )
        self.click_and_wait(panel, '[aria-label="market_run_id: HASP"]')
        self.assertEqual(panel.get_attribute("data-trace-count"), "2")
        panel.locator('select[aria-label="ti_direction"]').select_option("W")
        self.page.wait_for_function(
            'el=>el.querySelector(".research-chart").data.every(trace=>trace.name.includes(" · W · "))',
            arg=panel.element_handle(),
        )
        self.assertEqual(len(self.requests), before)

    def test_as_stack_totals_legend_rebase_and_resize(self):
        panel = self.open_feed("as_req")
        before = len(self.requests)
        plot = panel.locator(".research-chart")
        self.assertEqual(
            plot.evaluate("el=>el.data.map(trace=>trace.y[0])"), [10, 30, 60, 100]
        )
        self.assertEqual(
            plot.evaluate("el=>el.data.map(trace=>trace.customdata[0])"),
            [10, 20, 30, 40],
        )
        self.assertEqual(
            plot.evaluate("el=>el.data.map(trace=>trace.fill)"),
            ["tozeroy", "tonexty", "tonexty", "tonexty"],
        )
        self.click_and_wait(panel, '[aria-label="anc_type: NR"]')
        self.assertEqual(
            plot.evaluate("el=>el.data.map(trace=>trace.y[0])"), [20, 50, 90]
        )
        self.click_and_wait(panel, ".chart-legend button")
        self.assertEqual(plot.evaluate("el=>el.data.map(trace=>trace.y[0])"), [30, 70])
        self.click_and_wait(panel, ".panel-reset")
        panel.locator(".caiso-node").select_option("AS_SP26_EXP")
        self.page.wait_for_function(
            'el=>el.dataset.activeEntity==="AS_SP26_EXP"', arg=panel.element_handle()
        )
        self.assertEqual(len(self.requests), before + 1)
        self.page.get_by_role("button", name="4-SPLIT", exact=True).click()
        self.page.wait_for_function(
            "el=>el._fullLayout.width===el.clientWidth&&el._fullLayout.height===el.clientHeight",
            arg=plot.element_handle(),
        )
        self.assertTrue(panel.locator(".caiso-empty").is_hidden())
        self.page.set_viewport_size({"width": 1200, "height": 800})
        self.page.wait_for_function(
            "el=>el._fullLayout.width===el.clientWidth&&el._fullLayout.height===el.clientHeight",
            arg=plot.element_handle(),
        )

    def test_http_date_filters_and_visible_empty_and_error_states(self):
        query = urlencode(
            {
                "feed": "transmission",
                "entity": "PATH",
                "start": "2023-01-01",
                "end": "2023-01-01T01:00:00Z",
                "filters": json.dumps(
                    {
                        "market_run_id": ["HASP"],
                        "ti_direction": ["E"],
                        "measure": ["USEAGE_MW"],
                    }
                ),
            }
        )
        with urlopen(self.url + "/api/research/data?" + query) as response:
            r = json.load(response)
        self.assertEqual(len(r["series"][0]["timestamps"]), 12)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/api/research/data?feed=crr&filters=not-json")
        self.assertEqual(error.exception.code, 400)
        self.page.route(
            "**/api/research/data?*",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body='{"error":"Unavailable"}',
            ),
        )
        self.page.locator("#dataset-search").fill("as_req")
        self.page.locator("#dataset-tree .tree-leaf").first.click()
        state = self.page.locator(".research-panel .caiso-empty")
        state.wait_for()
        self.page.wait_for_function(
            'el=>el.textContent.includes("HTTP 503")', arg=state.element_handle()
        )
        self.assertIn("Unavailable", state.inner_text())
        self.assertTrue(self.page.locator(".research-panel .panel-reset").is_visible())
        self.page.unroute("**/api/research/data?*")
        missing = {
            "status": "ok",
            "feed": "ancillary",
            "entity": "AS_CAISO",
            "unit": "MW",
            "series": [
                {
                    "key": "NR",
                    "dimensions": {"anc_type": "NR", "measure": "MINIMUM"},
                    "timestamps": ["2023-01-01T00:00:00Z"],
                    "values": [None],
                }
            ],
        }
        self.page.route(
            "**/api/research/data?*",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(missing)
            ),
        )
        self.page.locator(".research-panel .panel-reset").click()
        self.page.wait_for_function(
            'el=>el.textContent.includes("NO_RECORDS_FOUND")',
            arg=state.element_handle(),
        )
        self.assertTrue(state.is_visible())
        # RESCAN invalidates a successfully cached history with missing quantities.
        self.page.unroute("**/api/research/data?*")
        self.page.locator("#rescan-button").click()
        self.page.wait_for_function("el=>el.hidden", arg=state.element_handle())
        self.page.wait_for_function(
            'el=>el.dataset.traceCount==="1"',
            arg=self.page.locator(".research-panel").element_handle(),
        )
