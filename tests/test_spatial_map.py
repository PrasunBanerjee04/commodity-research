import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import polars as pl
from bokeh.models import ColorBar, HoverTool, Image, ImageRGBA, RadioButtonGroup, Slider

from comm_research.markets import weather
from comm_research.markets.weather.vis._spatial_regions import (
    REGION_ASSETS,
    load_region,
)
from comm_research.markets.weather.vis.spatial_map import SpatialMap


def grid():
    times = ["2026-11-26T03:00:00-05:00", "2026-11-26 00:00:00", "2026-11-26 18:00:00Z"]
    data = pd.MultiIndex.from_product(
        [times, [42.0, 41.0, 40.0], [263.0, 264.0]],
        names=["valid_time", "latitude", "longitude"],
    ).to_frame(index=False)
    data["temperature"] = (
        data["latitude"]
        - 40
        + 3 * (data["longitude"] - 263)
        + np.repeat([10, 0, 20], 6)
    )
    return data.sample(frac=1, random_state=7)


def raster(plot):
    return next(
        renderer for renderer in plot.renderers if isinstance(renderer.glyph, Image)
    )


def mercator(latitude):
    return 6_378_137 * math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))


def conus_grid():
    data = pd.MultiIndex.from_product(
        [
            pd.date_range("2026-11-26", periods=2, freq="6h", tz="UTC"),
            np.arange(52, 21.99, -0.25),
            np.arange(232, 296.01, 0.25),
        ],
        names=["valid_time", "latitude", "longitude"],
    ).to_frame(index=False)
    return data.assign(temperature=np.float32(50))


def rgba(layout):
    return next(
        renderer
        for renderer in layout.children[1].renderers
        if isinstance(renderer.glyph, ImageRGBA)
    )


def pixel(glyph, array, lat, lon):
    x = 6_378_137 * math.radians(lon)
    y = mercator(lat)
    col = math.floor((x - glyph.x) * array.shape[1] / glyph.dw)
    row = math.floor((y - glyph.y) * array.shape[0] / glyph.dh)
    return array[row, col]


class SpatialMapTests(unittest.TestCase):
    def test_plot_exports_raw_ecmwf_rows_from_both_regions(self):
        data = pl.DataFrame(
            [
                (run, cycle, member, 20261127, valid, lat, lon, value, "K")
                for run, cycle, member, value in (
                    (20261125, 1800, 1, 240),
                    (20261126, 0, 1, 250),
                    (20261126, 1200, 1, 280),
                    (20261126, 1200, 2, 310),
                )
                for valid in (30, 1230)
                for lats, lons in (([41, 42], [236, 237]), ([65, 66], [58, 59]))
                for lat in lats
                for lon in lons
            ],
            schema={
                "dataDate": pl.Int32,
                "dataTime": pl.Int32,
                "perturbationNumber": pl.Int32,
                "validityDate": pl.Int32,
                "validityTime": pl.Int32,
                "latitude": pl.Float64,
                "longitude": pl.Float64,
                "value": pl.Float64,
                "units": pl.String,
            },
            orient="row",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                SpatialMap, "save", autospec=True, side_effect=SpatialMap.save
            ) as save,
        ):
            path = weather.plot(
                data,
                "value",
                ["us48", "europe"],
                "Forecast",
                "Both regions",
                output_dir=directory,
            )
            self.assertEqual(path, Path(directory).resolve() / "us48-europe.html")
            self.assertNotIn("<script src=", path.read_text(encoding="utf-8"))
            layout = save.call_args.args[1]

        slider = layout.select_one({"type": Slider})
        self.assertEqual(slider.end, 1)
        callback = slider.js_property_callbacks["change:value"][0]
        self.assertEqual(
            callback.args["labels"],
            ("Fri 27 Nov 2026 · 00:30Z", "Fri 27 Nov 2026 · 12:30Z"),
        )
        frames = callback.args["frames"].data["image"]
        for frame in frames:
            np.testing.assert_array_equal(np.unique(frame[np.isfinite(frame)]), [280])
        self.assertEqual(layout.select_one({"type": RadioButtonGroup}).active, 2)
        legend = layout.select_one({"type": ColorBar})
        self.assertEqual(legend.title, "Temperature (K)")

    def test_plot_infers_pandas_columns_and_fits_an_empty_region_selection(self):
        data = grid().rename(
            columns={
                "valid_time": "timestamp",
                "latitude": "lat",
                "longitude": "lon",
                "temperature": "temperature_f",
            }
        )
        original = data.copy(deep=True)
        with tempfile.TemporaryDirectory() as directory:
            path = weather.plot(data, "temperature_f", regions=[], output_dir=directory)
            self.assertEqual(path.name, "weather.html")
            html = path.read_text(encoding="utf-8")
            self.assertIn("weather-units", html)
            self.assertIn("weather-region-mask", html)
        pd.testing.assert_frame_equal(data, original)

    def test_us48_with_clipping_disabled_keeps_full_raster_in_a_padded_view(self):
        data = grid()
        m = SpatialMap()
        for width, height in ((1050, 515), (570, 700)):
            with self.subTest(width=width, height=height):
                inferred = m.plot(
                    data, "temperature", width=width, height=height
                ).children[1]
                named = m.plot(
                    data,
                    "temperature",
                    region="us48",
                    clip_to_region=False,
                    width=width,
                    height=height,
                ).children[1]
                self.assertLess(named.x_range.start, 6_378_137 * math.radians(-125))
                self.assertGreater(named.x_range.end, 6_378_137 * math.radians(-66))
                self.assertLess(named.y_range.start, mercator(24))
                self.assertGreater(named.y_range.end, mercator(50))
                self.assertIsNone(named.x_range.bounds)
                self.assertIsNone(named.y_range.bounds)
                self.assertAlmostEqual(
                    (named.x_range.end - named.x_range.start)
                    / (named.y_range.end - named.y_range.start),
                    width / height,
                )
                self.assertLess(
                    inferred.x_range.end - inferred.x_range.start,
                    named.x_range.end - named.x_range.start,
                )
                image = raster(named)
                for field in ("x", "y", "dw", "dh"):
                    self.assertEqual(
                        getattr(image.glyph, field),
                        getattr(raster(inferred).glyph, field),
                    )
                np.testing.assert_array_equal(
                    image.data_source.data["image"][0],
                    raster(inferred).data_source.data["image"][0],
                )
                self.assertGreater(named.renderers.index(image), 0)
                self.assertLess(named.renderers.index(image), len(named.renderers) - 1)

    def test_us48_view_stays_fixed_for_different_grids(self):
        data = grid()
        m = SpatialMap()
        plot = m.plot(data, "temperature", region="us48")
        shifted = m.plot(
            data.assign(longitude=data.longitude + 2), "temperature", region="us48"
        )
        self.assertEqual(
            plot.children[1].x_range.start, shifted.children[1].x_range.start
        )
        self.assertEqual(plot.children[1].x_range.end, shifted.children[1].x_range.end)
        self.assertNotEqual(rgba(plot).glyph.x, rgba(shifted).glyph.x)

    def test_projected_raster_and_missing_cells(self):
        data = grid()
        data = data[
            ~(
                (data.valid_time == "2026-11-26 00:00:00")
                & (data.latitude == 40)
                & (data.longitude == 264)
            )
        ]
        layout = SpatialMap().plot(data, "temperature")
        image = raster(layout.children[1])
        cells = image.data_source.data["image"][0]
        self.assertAlmostEqual(image.glyph.x, 6_378_137 * math.radians(-97.5))
        self.assertAlmostEqual(image.glyph.y, mercator(39.5))
        self.assertAlmostEqual(image.glyph.y + image.glyph.dh, mercator(42.5))
        self.assertEqual(image.glyph.origin, "bottom_left")
        self.assertEqual(cells.dtype, np.float32)
        self.assertEqual(cells[0, 0], 0)
        self.assertTrue(np.isnan(cells[0, 1]))
        self.assertEqual(cells[-1, -1], 5)
        self.assertEqual(image.glyph.color_mapper.nan_color, "#00000000")
        row = int((mercator(41) - image.glyph.y) / image.glyph.dh * len(cells))
        self.assertEqual(cells[row, 0], 1)

    def test_time_order_fixed_colors_and_single_frame(self):
        data = grid()
        layout = SpatialMap().plot(data, "temperature", palette="anomaly")
        mapper = raster(layout.children[1]).glyph.color_mapper
        self.assertEqual((mapper.low, mapper.high), (-25, 25))
        slider = layout.select_one({"type": Slider})
        callback = slider.js_property_callbacks["change:value"][0]
        self.assertEqual(
            callback.args["labels"],
            ("Thu 26 Nov 2026 · 00Z", "Thu 26 Nov 2026 · 08Z", "Thu 26 Nov 2026 · 18Z"),
        )
        self.assertEqual(
            [frame[0, 0] for frame in callback.args["frames"].data["image"]],
            [0, 10, 20],
        )
        single = SpatialMap().plot(
            data[data.valid_time == "2026-11-26 00:00:00"].assign(temperature=5),
            "temperature",
        )
        self.assertIsNone(single.select_one({"type": Slider}))
        mapper = raster(single.children[1]).glyph.color_mapper
        self.assertLess(mapper.low, 5)
        self.assertGreater(mapper.high, 5)

    def test_invalid_inputs(self):
        data = grid()
        row = data.iloc[:1].copy()
        alternatives = [
            (data.drop(columns="latitude"), "Missing columns"),
            (data.iloc[:0], "empty"),
            (pd.concat([data, row.assign(longitude=row.longitude - 360)]), "Duplicate"),
            (data.assign(latitude=90), "Web Mercator"),
            (data.assign(valid_time="tomorrow"), "invalid timestamps"),
            (
                data.assign(latitude=data.latitude.replace({41: 40.5})),
                "regularly spaced",
            ),
            (
                data.assign(longitude=data.longitude.replace({263: 179, 264: 180})),
                "antimeridian",
            ),
            (data.assign(temperature=np.nan), "no finite values"),
        ]
        for frame, message in alternatives:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                SpatialMap().plot(frame, "temperature")
        with self.assertRaisesRegex(ValueError, "us48"):
            SpatialMap().plot(data, "temperature", region="unknown")
        with self.assertRaisesRegex(ValueError, "limit"):
            SpatialMap(max_frame_mb=0.00001).plot(data, "temperature")
        with self.assertRaisesRegex(ValueError, "Color bounds"):
            SpatialMap().plot(data, "temperature", vmin=5, vmax=5)

    def test_html_export_works_without_external_scripts(self):
        m = SpatialMap()
        layout = m.plot(grid(), "temperature", region="us48")
        with tempfile.TemporaryDirectory() as directory:
            target = m.save(layout, Path(directory) / "nested" / "test.html")
            text = target.read_text(encoding="utf-8")
        self.assertTrue(target.is_absolute())
        self.assertIn("Bokeh", text)
        self.assertIn("weather-frames", text)
        self.assertIn("Natural Earth", text)
        self.assertNotIn("<script src=", text)

    def test_conus_renders_projected_land_and_coastal_islands(self):
        data = conus_grid()
        original = data.copy(deep=True)
        clipped = SpatialMap().plot(data, "temperature", region="CONUS")
        renderer = rgba(clipped)
        pixels = renderer.data_source.data["image"][0]
        glyph = renderer.glyph
        self.assertEqual(glyph.origin, "bottom_left")
        self.assertAlmostEqual(glyph.x, 6_378_137 * math.radians(-128.125))
        self.assertAlmostEqual(glyph.y, mercator(21.875))
        self.assertAlmostEqual(glyph.y + glyph.dh, mercator(52.125))
        self.assertGreater(pixels.shape[1], data.longitude.nunique())
        for point in ((38, -121), (39, -78), (27.5, -81.6)):
            with self.subTest(land=point):
                self.assertEqual(pixel(glyph, pixels >> 24, *point), 255)
        self.assertGreater(pixel(glyph, pixels >> 24, 40.9, -72.4), 200)  # Long Island
        for point in ((50, -110), (24, -100), (35, -125.5), (44, -87), (41.7, -82)):
            with self.subTest(water_or_foreign_land=point):
                self.assertEqual(pixel(glyph, pixels >> 24, *point), 0)
        # The alpha edge includes fractional coverage along the shoreline.
        self.assertTrue(np.any((0 < (pixels >> 24)) & ((pixels >> 24) < 255)))
        outline = clipped.select_one({"name": "weather-region-outline"})
        self.assertIsNotNone(outline)
        self.assertGreater(len(outline.data_source.data["xs"]), 1)
        pd.testing.assert_frame_equal(data, original)

        unmasked = SpatialMap().plot(
            data, "temperature", region="CONUS", clip_to_region=False
        )
        unmasked_image = raster(unmasked.children[1])
        for point in ((50, -110), (24, -100), (35, -125.5), (44, -87)):
            self.assertEqual(
                pixel(
                    unmasked_image.glyph,
                    unmasked_image.data_source.data["image"][0],
                    *point,
                ),
                50,
            )
        for axis in ("x_range", "y_range"):
            masked_range = getattr(clipped.children[1], axis)
            plain_range = getattr(unmasked.children[1], axis)
            self.assertEqual(
                (masked_range.start, masked_range.end),
                (plain_range.start, plain_range.end),
            )

    def test_masked_scale_uses_native_centers_and_keeps_missing_and_zero(self):
        data = conus_grid()
        data.loc[data.latitude >= 50, "temperature"] = 1000
        data.loc[data.valid_time == data.valid_time.max(), "temperature"] += 5
        data.loc[data.latitude.eq(40) & data.longitude.eq(260), "temperature"] = np.nan
        data.loc[data.latitude.eq(37) & data.longitude.eq(260), "temperature"] = 0
        m = SpatialMap()
        layout = m.plot(data, "temperature", region="us48", units="°F")
        legend = layout.select_one({"type": ColorBar})
        self.assertEqual((legend.color_mapper.low, legend.color_mapper.high), (0, 55))
        self.assertEqual(legend.title, "Temperature (°F)")
        renderer = rgba(layout)
        pixels = renderer.data_source.data["image"][0]
        self.assertEqual(pixel(renderer.glyph, pixels >> 24, 40, -100), 0)
        self.assertEqual(pixel(renderer.glyph, pixels >> 24, 37, -100), 255)
        callbacks = layout.select_one({"type": Slider}).js_property_callbacks[
            "change:value"
        ]
        frames = callbacks[0].args["frames"].data["image"]
        self.assertTrue(
            all(np.isnan(pixel(renderer.glyph, image, 40, -100)) for image in frames)
        )
        self.assertEqual(
            [pixel(renderer.glyph, image, 37, -100) for image in frames], [0, 0]
        )
        self.assertTrue(
            all(
                image.dtype == np.float32 and image.shape < pixels.shape
                for image in frames
            )
        )

        explicit = m.plot(data, "temperature", region="CONUS", vmin=-5, vmax=60)
        for axis in ("x_range", "y_range"):
            alias = getattr(layout.children[1], axis)
            canonical = getattr(explicit.children[1], axis)
            self.assertEqual((alias.start, alias.end), (canonical.start, canonical.end))
        explicit_legend = explicit.select_one({"type": ColorBar})
        self.assertEqual(
            (explicit_legend.color_mapper.low, explicit_legend.color_mapper.high),
            (-5, 60),
        )
        alpha = layout.select_one({"name": "weather-region-mask"}).data["alpha"][0]
        self.assertIs(
            alpha, explicit.select_one({"name": "weather-region-mask"}).data["alpha"][0]
        )

    def test_clipped_single_frame_preserves_small_footprint(self):
        data = grid()
        data = data.loc[data.valid_time.eq("2026-11-26 00:00:00")]
        layout = SpatialMap().plot(data, "temperature", region="CONUS")
        self.assertIsNone(layout.select_one({"type": Slider}))
        glyph = rgba(layout).glyph
        self.assertAlmostEqual(glyph.x + glyph.dw, 6_378_137 * math.radians(-95.5))
        self.assertAlmostEqual(glyph.y + glyph.dh, mercator(42.5))

    def test_multiple_regions_share_the_view_and_keep_disjoint_grid_tiles(self):
        t = pd.Timestamp("2026-11-26", tz="UTC")
        data = pd.DataFrame(
            [
                (t, lat, lon, value)
                for ys, xs, value in (
                    (range(38, 41), range(-101, -98), 32),
                    (range(48, 51), range(4, 7), 68),
                )
                for lat in ys
                for lon in xs
            ],
            columns=["valid_time", "latitude", "longitude", "temperature"],
        )
        layout = SpatialMap().plot(data, "temperature", region=["us48", "euro"])
        renderer = rgba(layout)
        alpha = renderer.data_source.data["image"][0] >> 24
        self.assertEqual(pixel(renderer.glyph, alpha, 39, -100), 255)
        self.assertEqual(pixel(renderer.glyph, alpha, 49, 5), 255)
        self.assertEqual(pixel(renderer.glyph, alpha, 44, -50), 0)
        legend = layout.select_one({"type": ColorBar})
        self.assertEqual((legend.color_mapper.low, legend.color_mapper.high), (32, 68))
        xrange = layout.children[1].x_range
        self.assertLess(xrange.start, 6_378_137 * math.radians(-125))
        self.assertGreater(xrange.end, 6_378_137 * math.radians(45))

        with self.assertRaisesRegex(ValueError, "nonempty list"):
            SpatialMap().plot(data, "temperature", region=[])
        with self.assertRaisesRegex(ValueError, "Unknown map region 'antarctica'"):
            SpatialMap().plot(data, "temperature", region=["us48", "antarctica"])

    def test_western_europe_region_includes_nordics(self):
        data = pd.MultiIndex.from_product(
            [
                [pd.Timestamp("2026-11-26", tz="UTC")],
                [65.0, 66.0],
                [30.0, 31.0],
            ],
            names=["valid_time", "latitude", "longitude"],
        ).to_frame(index=False)
        data["temperature"] = 10

        layout = SpatialMap().plot(data, "temperature", region="western_europe")

        self.assertIsNotNone(layout.select_one({"name": "weather-region-mask"}))

    def test_europe_clips_to_country_borders_and_coastlines(self):
        data = pd.MultiIndex.from_product(
            [["2026-11-26"], np.arange(34, 73, 0.5), np.arange(-25, 61, 0.5)],
            names=["valid_time", "latitude", "longitude"],
        ).to_frame(index=False)
        data["temperature"] = np.where(data.latitude < 37, 999, 50)

        # The warmest values are on the excluded side of the Georgia–Russia border.
        georgia = data.latitude.between(41.5, 42) & data.longitude.between(44, 45)
        data.loc[georgia, "temperature"] = 2000
        layout = SpatialMap().plot(data, "temperature", region="europe", vmin=0)
        renderer = rgba(layout)
        alpha = renderer.data_source.data["image"][0] >> 24
        for lat, lon, expected in (
            (48, 3, 255),  # France
            (39, 35, 255),  # Turkey
            (52, -20, 0),  # Atlantic
            (36, 3, 0),  # Algeria
            (42, 44.5, 0),  # Georgia
            (45, 44.5, 255),  # Russia
        ):
            with self.subTest(lat=lat, lon=lon):
                self.assertEqual(pixel(renderer.glyph, alpha, lat, lon), expected)
        self.assertLess(layout.select_one({"type": ColorBar}).color_mapper.high, 2000)

    def test_temperature_conversion_controls_and_hover_share_the_selection(self):
        m = SpatialMap()
        for units, index, freezing in (("°F", 0, 32), ("°C", 1, 0), ("K", 2, 273.15)):
            with self.subTest(units=units):
                plot = m.plot(grid(), "temperature", region="us48", units=units)
                controls = plot.select_one({"type": RadioButtonGroup})
                self.assertEqual(
                    (controls.labels, controls.active), (["°F", "°C", "K"], index)
                )
                switch = controls.js_property_callbacks["change:active"][0]
                for factor, offset, expected in zip(
                    switch.args["scales"], switch.args["offsets"], (32, 0, 273.15)
                ):
                    self.assertAlmostEqual(freezing * factor + offset, expected)
                hover = plot.select_one({"type": HoverTool})
                self.assertIs(hover.formatters["@image"].args["selector"], controls)
                self.assertIs(
                    hover.formatters["@image"].args["mask"],
                    plot.select_one({"name": "weather-region-mask"}),
                )
                self.assertIsNot(
                    switch.args["source_mapper"], switch.args["legend"].color_mapper
                )

        anomaly = m.plot(grid(), "temperature", units="°F", palette="anomaly")
        callback = anomaly.select_one({"type": RadioButtonGroup}).js_property_callbacks[
            "change:active"
        ][0]
        self.assertEqual(callback.args["offsets"], [0, 0, 0])
        other = m.plot(grid(), "temperature", units="m/s")
        self.assertIsNone(other.select_one({"type": RadioButtonGroup}))
        self.assertIsNotNone(other.select_one({"type": HoverTool}))

    def test_clipping_validation_and_display_budget(self):
        m = SpatialMap()
        data = grid()
        with self.assertRaisesRegex(ValueError, "cell centers in the land mask"):
            m.plot(
                data.assign(longitude=data.longitude - 45),
                "temperature",
                clip_to_region=True,
            )
        with self.assertRaisesRegex(
            ValueError, "No finite observations have cell centers in CONUS"
        ):
            m.plot(
                data.assign(latitude=data.latitude + 12), "temperature", region="CONUS"
            )
        with self.assertRaisesRegex(ValueError, "clipped display needs"):
            SpatialMap(max_frame_mb=4).plot(conus_grid(), "temperature", region="CONUS")
        load_region.cache_clear()
        try:
            with (
                patch.dict(
                    REGION_ASSETS, {"conus": Path("missing-conus-test.json.gz")}
                ),
                self.assertRaisesRegex(
                    FileNotFoundError, "Boundary data for CONUS is missing"
                ),
            ):
                m.plot(data, "temperature", region="CONUS", vmin=0, vmax=50)
        finally:
            load_region.cache_clear()
