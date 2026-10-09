"""Optional Chromium regressions for actual viewport/canvas rendering."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import polars as pl

from comm_research.dashboard.app import DashboardHandler
from comm_research.dashboard.data.loader import clear_caches

HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


class QuietHandler(DashboardHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(HAS_PLAYWRIGHT, "Install playwright to run Chromium checks")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        cls.playwright = sync_playwright().start()
        executable = os.environ.get("DASHBOARD_CHROMIUM") or shutil.which("chromium")
        try:
            cls.browser = cls.playwright.chromium.launch(
                executable_path=executable, args=["--no-sandbox"]
            )
        except Error as error:
            cls.playwright.stop()
            raise unittest.SkipTest(
                "Install Chromium or set DASHBOARD_CHROMIUM"
            ) from error
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        start = datetime(2023, 7, 8, 7, tzinfo=timezone.utc)
        for feed, column, minutes in (("dam_lmp", "mw", 60), ("rtm_lmp", "value", 5)):
            rows = [
                {
                    "interval_start_time_gmt": start + timedelta(minutes=i * minutes),
                    "node": node,
                    "lmp_type": component,
                    column: float(30 + i * 2 + offset),
                }
                for i in range(12)
                for node in ("TH_NP15_GEN-APND", "TH_SP15_GEN-APND")
                for component, offset in (
                    ("LMP", 0),
                    ("MCE", -2),
                    ("MCC", 3),
                    ("MCL", -1),
                )
            ]
            path = root / f"power_gas/napg/caiso/{feed}/year=2023/month=07/day=08"
            path.mkdir(parents=True)
            pl.DataFrame(rows).write_parquet(path / "data.parquet")
        for feed in ("oil/wti/settlements", "power_gas/napg/caiso/load_forecast"):
            path = root / feed
            path.mkdir(parents=True)
            pl.DataFrame(
                {
                    "timestamp": [start + timedelta(hours=i) for i in range(12)],
                    "value": [float(10 + i * 2) for i in range(12)],
                }
            ).write_parquet(path / "data.parquet")
        clear_caches()
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, lake_root=str(root))
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.browser.close()
        cls.playwright.stop()
        cls.temporary.cleanup()
        clear_caches()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1400, "height": 800})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.page_errors = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def open(self):
        self.page.goto(self.url, wait_until="networkidle")

    def assert_canvases(self, count):
        # Check rasterized trace pixels, not just the presence of a canvas element.
        self.page.wait_for_function(
            """count => {
          const canvases = [...document.querySelectorAll('.chart-mount canvas')]
            .filter(c => c.getBoundingClientRect().width > 0);
          return canvases.length === count && canvases.every(c => {
            const rect = c.getBoundingClientRect(), mount = c.closest('.chart-mount');
            if (rect.height < 250 || Math.abs(rect.width - mount.clientWidth) > 1
                || Math.abs(rect.height - mount.clientHeight) > 1) return false;
            const pixels = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let colors = 0;
            for (let i = 0; i < pixels.length; i += 4) {
              if (pixels[i+3] > 100 && Math.max(pixels[i], pixels[i+1], pixels[i+2])
                  - Math.min(pixels[i], pixels[i+1], pixels[i+2]) > 60) colors++;
            }
            return colors > 5;
          });
        }""",
            arg=count,
        )
        self.assertEqual(self.page_errors, [])

    def test_initial_dam_and_rtm_render_without_filter_clicks(self):
        self.open()
        self.assert_canvases(2)
        titles = self.page.locator(".panel-title").all_text_contents()
        self.assertTrue(any("Day-Ahead" in title for title in titles))
        self.assertTrue(any("Real-Time" in title for title in titles))
        self.assertEqual(
            self.page.locator('[data-date="end"]').evaluate_all(
                "els => els.map(e => e.value)"
            ),
            ["2023-07-08", "2023-07-08"],
        )

    def test_splits_window_resize_navigator_and_hidden_tab_activation(self):
        self.open()
        self.assert_canvases(2)
        for count in (1, 2, 4):
            self.page.locator(f'[data-layout="{count}"]').click()
            self.assert_canvases(count)
        self.page.set_viewport_size({"width": 900, "height": 440})
        self.assert_canvases(4)
        self.page.locator("#navigator-toggle").click()
        self.assert_canvases(4)
        self.page.set_viewport_size({"width": 1600, "height": 950})
        self.assert_canvases(4)
        self.page.locator('[data-layout="1"]').click()
        self.assert_canvases(1)
        self.page.locator(".dv-tab").filter(has_text="Day-Ahead").click()
        self.assert_canvases(1)
        self.page.locator(".dv-tab").filter(has_text="Real-Time").click()
        self.assert_canvases(1)

    def test_series_controls_legend_and_theme_still_render(self):
        self.open()
        self.assert_canvases(2)
        panel = self.page.get_by_role("tabpanel", name="CAISO: Real-Time LMP")
        panel.get_by_role("checkbox", name="Congestion", exact=True).check()
        self.page.wait_for_function(
            "() => document.querySelectorAll('.chart-legend button').length === 3"
        )
        panel.locator("[data-node-label]").click()
        panel.get_by_role("checkbox", name="TH_SP15_GEN-APND", exact=True).check()
        self.page.wait_for_function(
            "() => document.querySelectorAll('.chart-legend button').length === 5"
        )
        panel.locator("[data-node-label]").click()
        requests = []
        self.page.on("request", lambda request: requests.append(request.url))
        legend = panel.locator(".chart-legend button").first
        label = legend.inner_text()
        legend.click()
        self.assertEqual(legend.get_attribute("aria-pressed"), "false")
        self.page.locator("#theme-toggle").click()
        self.assert_canvases(2)
        self.assertEqual(
            panel.get_by_role("button", name=label, exact=True).get_attribute(
                "aria-pressed"
            ),
            "false",
        )
        self.assertFalse(any("/api/series" in url for url in requests))
        self.page.reload(wait_until="networkidle")
        self.assert_canvases(2)
        self.assertEqual(panel.locator(".chart-legend button").count(), 4)
        self.assertEqual(
            panel.get_by_role("button", name=label, exact=True).get_attribute(
                "aria-pressed"
            ),
            "false",
        )

    def test_http_json_failure_is_visible_and_update_recovers(self):
        self.page.route(
            "**/api/series",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"error":"Lake unavailable"}',
            ),
        )
        self.open()
        errors = self.page.locator(".chart-empty.is-error")
        self.assertEqual(errors.count(), 2)
        self.assertIn(
            "DATA_FETCH_ERROR: HTTP 500: Lake unavailable", errors.first.inner_text()
        )
        self.assertTrue(errors.first.is_visible())
        self.assertEqual(self.page.locator("canvas").count(), 0)
        self.page.unroute("**/api/series")
        for button in self.page.locator(".panel-update").all():
            button.click()
        self.assert_canvases(2)

    def test_non_json_http_failure_keeps_http_status(self):
        self.page.route(
            "**/api/series",
            lambda route: route.fulfill(
                status=404, content_type="text/html", body="<h1>Not Found</h1>"
            ),
        )
        self.open()
        self.assertIn(
            "DATA_FETCH_ERROR: HTTP 404",
            self.page.locator(".chart-empty.is-error").first.inner_text(),
        )
        self.assertEqual(self.page_errors, [])

    def test_metadata_failure_is_visible_inside_the_pane(self):
        self.page.route(
            "**/api/metadata?*",
            lambda route: route.fulfill(
                status=404,
                content_type="application/json",
                body='{"error":"Missing feed"}',
            ),
        )
        self.open()
        self.assertEqual(self.page.locator(".chart-empty.is-error").count(), 2)
        self.assertIn(
            "DATA_FETCH_ERROR: HTTP 404: Missing feed",
            self.page.locator(".chart-empty.is-error").first.inner_text(),
        )
        self.assertEqual(self.page_errors, [])

    def test_invalid_successful_payload_is_visible(self):
        self.page.route(
            "**/api/series",
            lambda route: route.fulfill(
                content_type="application/json", body='{"plot":null}'
            ),
        )
        self.open()
        self.assertEqual(self.page.locator(".chart-empty.is-error").count(), 2)
        self.assertIn(
            "Invalid series response",
            self.page.locator(".chart-empty.is-error").first.inner_text(),
        )
        self.assertEqual(self.page_errors, [])

    def test_chart_constructor_exception_is_visible(self):
        self.page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
          window.uPlot = function() { throw new Error('Canvas initialization failed'); };
        });""")
        self.open()
        self.assertEqual(self.page.locator(".chart-empty.is-error").count(), 2)
        self.assertIn(
            "CHART_RENDER_ERROR: Canvas initialization failed",
            self.page.locator(".chart-empty.is-error").first.inner_text(),
        )
        self.assertEqual(self.page_errors, [])

    def test_explicit_missing_horizon_shows_empty_state(self):
        self.open()
        self.assert_canvases(2)
        panel = self.page.locator(".market-panel").first
        panel.locator('[data-date="start"]').fill("2026-10-01")
        panel.locator('[data-date="end"]').fill("2026-10-09")
        panel.locator(".panel-update").click()
        self.page.wait_for_function("""() => [...document.querySelectorAll('.chart-empty')]
          .some(e => !e.hidden && e.textContent === 'NO_RECORDS_FOUND_FOR_DATE_RANGE')""")
        self.assertEqual(panel.locator("canvas").count(), 0)
        self.assertTrue(panel.locator(".chart-empty").is_visible())
        self.assertEqual(self.page_errors, [])

    def test_missing_uplot_library_is_visible_in_both_panes(self):
        self.page.route("**/vendor/uPlot.iife.min.js", lambda route: route.abort())
        self.open()
        errors = self.page.locator(".chart-empty.is-error")
        self.assertEqual(errors.count(), 2)
        self.assertIn(
            "ERR_DEPENDENCY_LOAD_FAILED: uPlot missing", errors.first.inner_text()
        )
        self.assertTrue(errors.first.is_visible())
        self.assertEqual(self.page_errors, [])

    def test_missing_dockview_library_shows_workspace_error(self):
        self.page.route("**/vendor/dockview.min.js", lambda route: route.abort())
        self.open()
        self.assertIn(
            "ERR_DEPENDENCY_LOAD_FAILED: Dockview missing",
            self.page.locator("#empty-workspace").inner_text(),
        )
        self.assertTrue(self.page.locator("#empty-workspace").is_visible())
        self.assertEqual(self.page_errors, [])

    def test_stale_saved_dates_reanchor_before_initial_fetch(self):
        settings = {
            f"power_gas/napg/caiso/{feed}": {
                "start": "2026-10-01",
                "end": "2026-10-09",
                "range": None,
            }
            for feed in ("dam_lmp", "rtm_lmp")
        }
        self.page.add_init_script(
            f"localStorage.setItem('commodity-panel-settings', {json.dumps(json.dumps(settings))});"
        )
        self.open()
        self.assert_canvases(2)
        self.assertEqual(
            self.page.locator('[data-date="start"]').evaluate_all(
                "els => els.map(e => e.value)"
            ),
            ["2023-07-08", "2023-07-08"],
        )

    def test_data_arriving_before_layout_defers_chart_until_visible(self):
        self.page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
          document.querySelector('#dockview').style.display = 'none';
        });""")
        self.open()
        self.assertEqual(self.page.locator("canvas").count(), 0)
        self.assertEqual(
            self.page.locator(".chart-empty").all_text_contents(),
            ["WAITING_FOR_VIEWPORT…", "WAITING_FOR_VIEWPORT…"],
        )
        self.page.locator("#dockview").evaluate("element => element.style.display = ''")
        self.assert_canvases(2)


if __name__ == "__main__":
    unittest.main()
