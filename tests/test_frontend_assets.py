"""Frontend graph versioning is shared across market views."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class FrontendVersionTests(unittest.TestCase):
    def test_child_module_change_versions_entire_import_graph(self):
        from comm_research.dashboard.server import frontend_document

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "styles").mkdir()
            (root / "src/main.js").write_text(
                'import "./markets/power_gas/napg/caiso/research_history.js";'
            )
            child = root / "src/markets/power_gas/napg/caiso/research_history.js"
            child.parent.mkdir(parents=True)
            child.write_text("export const schema = 1;")
            (root / "styles/theme.css").write_text("body {color:white;}")
            index = root / "index.html"
            index.write_text(
                '<link href="/styles/theme.css"><script type="module" src="/src/main.js"></script>'
            )
            with patch("comm_research.dashboard.server.WEB_ROOT", root):
                first, old = frontend_document(index)
                self.assertLess(
                    first.index('type="importmap"'), first.index('type="module"')
                )
                self.assertIn(f"/src/main.js?v={old}", first)
                self.assertIn(
                    f"/src/markets/power_gas/napg/caiso/research_history.js?v={old}",
                    first,
                )
                self.assertIn(f"/styles/theme.css?v={old}", first)
                child.write_text("export const schema = 2;")
                second, new = frontend_document(index)
                self.assertNotEqual(old, new)
                self.assertIn(f"/src/main.js?v={new}", second)
                self.assertNotIn(old, second)
                self.assertEqual(frontend_document(index), (second, new))
