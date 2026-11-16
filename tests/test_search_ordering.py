"""Ordering regressions using real Qt widgets when Maya's runtime is available."""

import importlib
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bk_maya.api import client as api
from bk_maya.core.prefs import _Prefs


class SearchOrderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        try:
            cls.ui = importlib.import_module("bk_maya.ui.asset_bar")
        except ImportError as exc:
            raise unittest.SkipTest(f"Maya and a Qt binding are required: {exc}") from exc
        from qtpy.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.prefs = _Prefs(search_order="-quality")
        self._patch(self.ui, "prefs", self.prefs)
        self.save = self._patch(self.prefs, "save")
        self.search = self._patch(self.ui.bk_search, "search")
        self._patch(self.ui, "_ensure_poller")
        self._patch(self.ui.auth, "is_logged_in", return_value=False)
        self._patch(self.ui.auth, "add_profile_listener")
        self._patch(self.ui.bk_bookmarks, "add_listener")
        self._patch(self.ui.AssetGrid, "_add_placeholders")
        self.bar = self.ui.AssetBarWidget()
        self.addCleanup(self.bar.deleteLater)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_selection_restarts_search_preserving_filters_and_saves_order(self):
        self.assertEqual(self.bar._order.currentData(), "-quality")
        self.bar._search_bar._input.setText("chair")
        self.bar._filters._free_only.setChecked(True)
        self.bar._grid.set_extra_filters({"author_id": 42})

        self.bar._order.setCurrentIndex(self.bar._order.findData("created"))

        self.search.assert_called_once()
        request = self.search.call_args.kwargs
        self.assertEqual(request["query"], "chair")
        self.assertEqual(request["order"], "created")
        self.assertTrue(request["free_only"])
        self.assertEqual(request["extra_filters"], {"author_id": 42})
        self.assertEqual(request.get("next_url", ""), "")
        self.assertEqual(self.prefs.search_order, "created")
        self.save.assert_called_once_with()
        url = api.build_search_url(
            query=request["query"],
            order=request["order"],
            extra_params={"is_free": "true", **request["extra_filters"]},
        )
        tokens = parse_qs(urlsplit(url).query)["query"][0].split()
        self.assertIn("order:-is_free,created", tokens)

    def test_order_survives_search_entry_points_and_pagination(self):
        for action in (
            self.bar._default_search,
            lambda: self.bar._on_search("wood", "material"),
            self.bar._on_filters_changed,
            lambda: self.bar.search_by_author(42),
        ):
            with self.subTest(action=action):
                action()
                self.assertEqual(self.search.call_args.kwargs["order"], "-quality")

        grid = self.bar._grid
        grid._results = [{"id": "first"}]
        grid._total = 48
        grid._loading = False
        grid._next_url = "https://www.blendkit.com/api/v1/search/?cursor=next"
        grid._load_next_page()
        request = self.search.call_args.kwargs
        self.assertEqual(request["order"], "-quality")
        self.assertEqual(request["next_url"], grid._next_url)
        self.assertEqual(api.build_search_url(order=request["order"], next_url=request["next_url"]), grid._next_url)

    def test_default_restores_smart_ordering_and_resets_pagination(self):
        self.bar._grid._next_url = "old-cursor"
        self.bar._grid._results = [{"id": "old-result"}]
        self.bar._order.setCurrentIndex(self.bar._order.findData(""))

        request = self.search.call_args.kwargs
        self.assertEqual(request["order"], "")
        self.assertEqual(self.bar._grid._next_url, "")
        self.assertEqual(self.bar._grid._results, [])
        for query, expected in (("", "-last_blend_upload,-last_zip_file_upload"), ("chair", "_score")):
            with self.subTest(query=query):
                url = api.build_search_url(query=query, order=request["order"])
                tokens = parse_qs(urlsplit(url).query)["query"][0].split()
                self.assertIn(f"order:{expected}", tokens)
