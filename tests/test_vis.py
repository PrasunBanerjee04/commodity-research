"""Plotly figure structure, customization, and isolated styling contracts."""

import importlib.util
import unittest

from comm_research.infra.tools.vis import (
    DEFAULT_STYLE, plot_locations, plot_style, plot_timeseries, style_figure,
)


@unittest.skipUnless(importlib.util.find_spec("plotly"), "Install the vis extra")
class VisualizationTests(unittest.TestCase):
    def test_time_series_overlay_preserves_data_and_missing_values(self):
        from plotly.graph_objects import Figure

        dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        fig = plot_timeseries(dates, [1, None, 3], name="Observed", ylabel="°C")
        self.assertIsInstance(fig, Figure)
        self.assertEqual(list(fig.data[0].y), [1, None, 3])
        self.assertFalse(fig.data[0].connectgaps)
        self.assertFalse(fig.layout.showlegend)
        self.assertIs(plot_timeseries(dates, [2, 3, 4], fig=fig, name="Model"), fig)
        self.assertTrue(fig.layout.showlegend)
        self.assertEqual(fig.layout.yaxis.title.text, "°C")
        self.assertEqual(fig.layout.paper_bgcolor, "white")
        self.assertIn('"type":"scatter"', fig.to_json())

    def test_nested_styles_restore_after_exception_without_global_changes(self):
        import plotly.io as pio

        original_template = pio.templates.default
        with plot_style({"font": {"size": 17}}):
            fig = plot_timeseries([], [])
            self.assertEqual(fig.layout.font.size, 17)
            self.assertEqual(fig.layout.font.family, DEFAULT_STYLE["font"]["family"])
            with self.assertRaises(RuntimeError):
                with plot_style({"font": {"size": 21}}):
                    self.assertEqual(plot_timeseries([], []).layout.font.size, 21)
                    raise RuntimeError("test restoration")
            self.assertEqual(plot_timeseries([], []).layout.font.size, 17)
        self.assertEqual(plot_timeseries([], []).layout.font.size, DEFAULT_STYLE["font"]["size"])
        self.assertEqual(pio.templates.default, original_template)

    def test_geographic_coordinates_colors_and_custom_markers(self):
        fig = plot_locations([-121, -114], [35, 32.5], values=[10, 20],
                             marker={"size": 12}, title="Stations")
        trace = fig.data[0]
        self.assertEqual(trace.type, "scattergeo")
        self.assertEqual(list(trace.lon), [-121, -114])
        self.assertEqual(list(trace.lat), [35, 32.5])
        self.assertEqual(list(trace.marker.color), [10, 20])
        self.assertEqual(trace.marker.size, 12)
        self.assertTrue(trace.marker.showscale)
        self.assertEqual(fig.layout.geo.fitbounds, "locations")
        self.assertIn('"type":"scattergeo"', fig.to_json())
        with self.assertRaises(ValueError):
            plot_locations([0], [0], values=[1], marker={"color": "red"})

    def test_style_any_plotly_figure_and_export_html(self):
        from plotly.graph_objects import Bar, Figure

        fig = Figure(Bar(x=["A", "B"], y=[1, 2]))
        self.assertIs(style_figure(fig, title={"text": "Volume"}), fig)
        self.assertEqual(fig.layout.title.text, "Volume")
        self.assertEqual(fig.layout.title.x, 0.02)
        self.assertIn("Plotly.newPlot", fig.to_html(full_html=False))


if __name__ == "__main__":
    unittest.main()
