"""Unit tests for the pure logic (normalization, matching, diff, state).
Stdlib-only: run with  python -m unittest discover tests  (PYTHONPATH=src)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scout.diff import apply_run_to_state, find_new_arrivals, find_wishlist_restocks
from scout.search import (
    extract_products,
    matches_brands,
    matches_wishlist,
    normalize_product,
)
from scout.state import empty_state, load_state, save_state


def product(pid, title, in_stock=True, **extra):
    return {"id": pid, "title": title, "brand": "", "price": 199.0,
            "image": None, "in_stock": in_stock, "raw_id_key": "product_id", **extra}


class TestNormalization(unittest.TestCase):
    def test_probes_common_field_names(self):
        raw = {"productId": 42, "display_name": "Hot Wheels '67 Camaro",
               "offer_price": 199, "imageUrl": "https://x/y.png", "in_stock": True}
        p = normalize_product(raw)
        self.assertEqual(p["id"], "42")
        self.assertEqual(p["title"], "Hot Wheels '67 Camaro")
        self.assertEqual(p["price"], 199)
        self.assertEqual(p["image"], "https://x/y.png")
        self.assertTrue(p["in_stock"])

    def test_variant_price_and_stock(self):
        raw = {"id": "a1", "name": "Hot Wheels Batmobile",
               "variations": [{"price": 249, "in_stock": False},
                              {"price": 259, "in_stock": True}]}
        p = normalize_product(raw)
        self.assertTrue(p["in_stock"])
        self.assertEqual(p["price"], 259)

    def test_real_shape_captures_spin_id_and_nested_price(self):
        # mirrors live recon_output/search_raw.json structure (13 Jul 2026)
        raw = {"productId": "WZKU25NPNK", "displayName": "HW Datsun 240Z",
               "brand": "Hot Wheels", "inStock": True,
               "variations": [{"spinId": "A3EX75TUFS", "skuId": "ICK7Z4J43J",
                               "price": {"mrp": 167, "offerPrice": 159},
                               "isInStockAndAvailable": True}]}
        p = normalize_product(raw)
        self.assertEqual(p["id"], "WZKU25NPNK")
        self.assertEqual(p["spin_id"], "A3EX75TUFS")
        self.assertEqual(p["sku_id"], "ICK7Z4J43J")
        self.assertEqual(p["price"], 159)
        self.assertTrue(p["in_stock"])

    def test_all_variants_out_of_stock(self):
        raw = {"id": "a2", "name": "Hot Wheels RX-7",
               "variations": [{"price": 249, "in_stock": False}]}
        self.assertFalse(normalize_product(raw)["in_stock"])

    def test_unusable_product_returns_none(self):
        self.assertIsNone(normalize_product({"foo": "bar"}))

    def test_bare_image_id_gets_cdn_prefix(self):
        raw = {"id": 1, "name": "HW", "image_id": "abc/def123"}
        self.assertTrue(normalize_product(raw)["image"].startswith("https://"))

    def test_extract_products_nested(self):
        payload = {"data": {"widgets": [{"items": [
            {"id": 1, "name": "Hot Wheels A"}, {"id": 2, "name": "Hot Wheels B"}]}]}}
        self.assertEqual(len(extract_products(payload)), 2)

    def test_extract_products_empty(self):
        self.assertEqual(extract_products({"status": "ok", "data": []}), [])


class TestMatching(unittest.TestCase):
    def test_brand_filter_rejects_matchbox(self):
        hw = product("1", "Hot Wheels Nissan Skyline GTR")
        mb = product("2", "Matchbox Tesla Model Y")
        self.assertTrue(matches_brands(hw, ["hot wheels"]))
        self.assertFalse(matches_brands(mb, ["hot wheels"]))

    def test_wishlist_token_subset_ignores_punctuation(self):
        title = "Hot Wheels Premium '67 Camaro SS Blue"
        self.assertEqual(matches_wishlist(title, ["hot wheels '67 camaro"]),
                         "hot wheels '67 camaro")
        self.assertIsNone(matches_wishlist(title, ["hot wheels nissan skyline gtr"]))


class TestDiff(unittest.TestCase):
    def test_new_arrival_is_unseen_id(self):
        seen = {"1": {"in_stock": True}}
        current = {"1": product("1", "HW Old"), "2": product("2", "HW New")}
        arrivals = find_new_arrivals(current, seen)
        self.assertEqual([p["id"] for p in arrivals], ["2"])

    def test_restock_requires_false_to_true_and_wishlist_match(self):
        seen = {"1": {"in_stock": False, "alerted_instock": False},
                "2": {"in_stock": False, "alerted_instock": False},
                "3": {"in_stock": True}}
        current = {"1": product("1", "Hot Wheels '67 Camaro"),
                   "2": product("2", "Hot Wheels Random Car"),
                   "3": product("3", "Hot Wheels '67 Camaro Special")}
        restocks = find_wishlist_restocks(current, seen, ["hot wheels '67 camaro"])
        # 1 restocked+wishlisted; 2 restocked but not wishlisted; 3 never left stock
        self.assertEqual([p["id"] for p in restocks], ["1"])

    def test_absence_from_good_search_marks_out_of_stock(self):
        state = empty_state()
        state["seen_products"] = {"9": {"in_stock": True, "alerted_instock": True}}
        apply_run_to_state(state, {}, "2026-07-10T00:00:00Z", search_ok=True)
        self.assertFalse(state["seen_products"]["9"]["in_stock"])
        self.assertFalse(state["seen_products"]["9"]["alerted_instock"])

    def test_bad_search_flips_nothing(self):
        state = empty_state()
        state["seen_products"] = {"9": {"in_stock": True, "alerted_instock": True}}
        apply_run_to_state(state, {}, "2026-07-10T00:00:00Z", search_ok=False)
        self.assertTrue(state["seen_products"]["9"]["in_stock"])

    def test_idempotent_second_run(self):
        state = empty_state()
        current = {"1": product("1", "Hot Wheels A")}
        apply_run_to_state(state, current, "t1", search_ok=True)
        state["seen_products"]["1"]["alerted_instock"] = True
        self.assertEqual(find_new_arrivals(current, state["seen_products"]), [])
        self.assertEqual(
            find_wishlist_restocks(current, state["seen_products"], ["hot wheels a"]), [])


class TestState(unittest.TestCase):
    def test_missing_state_seeds(self):
        state, seeded = load_state(Path(tempfile.gettempdir()) / "does-not-exist-xyz.json")
        self.assertTrue(seeded)
        self.assertEqual(state["seen_products"], {})

    def test_corrupt_state_seeds(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            _, seeded = load_state(path)
            self.assertTrue(seeded)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = empty_state()
            state["seen_products"]["1"] = {"title": "HW", "in_stock": True}
            save_state(path, state)
            loaded, seeded = load_state(path)
            self.assertFalse(seeded)
            self.assertEqual(loaded["seen_products"]["1"]["title"], "HW")
            self.assertIsNotNone(loaded["last_run_utc"])


class TestNoCheckoutAnywhere(unittest.TestCase):
    def test_checkout_not_in_allowlist(self):
        from scout.mcp_client import TOOL_ALLOWLIST
        for forbidden in ("checkout", "place_order", "order"):
            self.assertNotIn(forbidden, TOOL_ALLOWLIST)

    def test_existing_cart_reduced_to_spin_sku_quantity(self):
        from scout.cart import _existing_to_update_items
        existing = [{"spinId": "S1", "skuId": "K1", "quantity": 2},
                    {"spinId": "S2", "skuId": "K2", "quantity": 1}]
        self.assertEqual(
            _existing_to_update_items(existing),
            [{"spinId": "S1", "skuId": "K1", "quantity": 2},
             {"spinId": "S2", "skuId": "K2", "quantity": 1}])

    def test_existing_cart_line_missing_ids_skips(self):
        from scout.cart import CartSkipped, _existing_to_update_items
        with self.assertRaises(CartSkipped):
            _existing_to_update_items([{"name": "mystery", "quantity": 1}])
        with self.assertRaises(CartSkipped):  # spinId alone is not enough
            _existing_to_update_items([{"spinId": "S1", "quantity": 1}])

    def test_empty_cart_error_recognized(self):
        from scout.cart import ToolCallError, _is_empty_cart_error
        err = ToolCallError("get_cart failed: Cart not found or session expired. "
                            "Please add items to your cart again using update_cart.")
        self.assertTrue(_is_empty_cart_error(err))
        self.assertFalse(_is_empty_cart_error(ToolCallError("get_cart failed: 500 boom")))


if __name__ == "__main__":
    unittest.main()
