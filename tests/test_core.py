"""Unit tests for the pure logic (normalization, matching, diff, state).
Stdlib-only: run with  python -m unittest discover tests  (PYTHONPATH=src)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scout.diff import update_address
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


class TestHysteresis(unittest.TestCase):
    WISH = ["hot wheels a"]

    def _cycle(self, seen, current, seeded=False):
        return update_address(seen, current, self.WISH, "t", seeded=seeded)

    def test_new_arrival_needs_two_confirmations(self):
        seen = {}
        cur = {"1": product("1", "Hot Wheels A")}
        # cycle 1: seen once, not yet confirmed → no alert
        self.assertEqual(self._cycle(seen, cur), [])
        self.assertFalse(seen["1"]["in_stock"])
        # cycle 2: second consecutive sighting → New arrival
        hits = self._cycle(seen, cur)
        self.assertEqual([(p["id"], k) for p, k in hits], [("1", "New arrival")])
        self.assertTrue(seen["1"]["in_stock"])

    def test_single_cycle_flicker_never_alerts(self):
        seen = {}
        cur = {"1": product("1", "Hot Wheels A")}
        self._cycle(seen, cur)          # sighting 1 (pending)
        self._cycle(seen, {})           # gone before confirmation → streak resets
        self.assertEqual(self._cycle(seen, cur), [])  # sighting 1 again, still pending
        self.assertFalse(seen["1"]["in_stock"])

    def test_transient_absence_does_not_flip_out_of_stock(self):
        seen = {}
        cur = {"1": product("1", "Hot Wheels A")}
        self._cycle(seen, cur); self._cycle(seen, cur)   # confirmed in stock
        seen["1"]["alerted_instock"] = True              # simulate emit success
        self.assertTrue(seen["1"]["in_stock"])
        # two missed cycles (< miss_threshold 3) — stays in stock, no re-alert
        self._cycle(seen, {}); self._cycle(seen, {})
        self.assertTrue(seen["1"]["in_stock"])
        # reappears: already in stock and alerted → no duplicate alert
        self.assertEqual(self._cycle(seen, cur), [])

    def test_sustained_absence_confirms_out_then_restock_realerts(self):
        seen = {}
        cur = {"1": product("1", "Hot Wheels A")}
        self._cycle(seen, cur); self._cycle(seen, cur)   # in stock, alerted new arrival
        seen["1"]["alerted_instock"] = True              # simulate emit success
        for _ in range(3):                               # 3 misses → confirmed gone
            self._cycle(seen, {})
        self.assertFalse(seen["1"]["in_stock"])
        # returns for 2 cycles → Restock (wishlisted), not New arrival
        self._cycle(seen, cur)
        hits = self._cycle(seen, cur)
        self.assertEqual([(p["id"], k) for p, k in hits], [("1", "Restock")])

    def test_non_wishlist_restock_tracked_not_alerted(self):
        seen = {}
        cur = {"9": product("9", "Hot Wheels Zzz")}      # not in WISH
        self._cycle(seen, cur); h = self._cycle(seen, cur)
        self.assertEqual([(p["id"], k) for p, k in h], [("9", "New arrival")])
        seen["9"]["alerted_instock"] = True
        for _ in range(3):
            self._cycle(seen, {})
        self._cycle(seen, cur)
        self.assertEqual(self._cycle(seen, cur), [])     # non-wishlist re-stock: silent
        self.assertTrue(seen["9"]["in_stock"])

    def test_seed_sets_baseline_without_alerts(self):
        seen = {}
        cur = {"1": product("1", "Hot Wheels A"), "2": product("2", "HW B", in_stock=False)}
        self.assertEqual(self._cycle(seen, cur, seeded=True), [])
        self.assertTrue(seen["1"]["in_stock"] and seen["1"]["alerted_instock"])
        self.assertFalse(seen["2"]["in_stock"])

    def test_addresses_are_independent(self):
        seen_a, seen_b = {}, {}
        cur = {"1": product("1", "Hot Wheels A")}
        self._cycle(seen_a, cur); self._cycle(seen_a, cur)   # confirmed at A
        self.assertTrue(seen_a["1"]["in_stock"])
        self.assertNotIn("1", seen_b)                        # untouched at B
        # B confirms independently on its own two cycles.
        self._cycle(seen_b, cur)
        hits = self._cycle(seen_b, cur)
        self.assertEqual([(p["id"], k) for p, k in hits], [("1", "New arrival")])


class TestState(unittest.TestCase):
    def test_missing_state_seeds(self):
        state, seeded = load_state(Path(tempfile.gettempdir()) / "does-not-exist-xyz.json")
        self.assertTrue(seeded)
        self.assertEqual(state["seen_by_provider"], {})

    def test_corrupt_state_seeds(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            _, seeded = load_state(path)
            self.assertTrue(seeded)

    def test_v1_flat_schema_triggers_reseed(self):
        # a v1 state.json (flat seen_products) must reseed, not crash.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps(
                {"last_run_utc": "t0", "seen_products": {"1": {"in_stock": True}}, "flags": {}}
            ), encoding="utf-8")
            state, seeded = load_state(path)
            self.assertTrue(seeded)
            self.assertEqual(state["seen_by_provider"], {})

    def test_v2_address_schema_migrates_to_swiggy_without_reseed(self):
        # the live single-provider state (seen_by_address) migrates in place
        # under "swiggy" and does NOT reseed (no Swiggy alert gap).
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text(json.dumps({
                "last_run_utc": "t0", "flags": {},
                "seen_by_address": {"addrA": {"1": {"in_stock": True, "alerted_instock": True}}},
            }), encoding="utf-8")
            state, seeded = load_state(path)
            self.assertFalse(seeded)
            self.assertNotIn("seen_by_address", state)
            self.assertEqual(state["seen_by_provider"]["swiggy"]["addrA"]["1"]["in_stock"], True)

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            state = empty_state()
            state["seen_by_provider"]["swiggy"] = {"addrA": {"1": {"title": "HW", "in_stock": True}}}
            save_state(path, state)
            loaded, seeded = load_state(path)
            self.assertFalse(seeded)
            self.assertEqual(loaded["seen_by_provider"]["swiggy"]["addrA"]["1"]["title"], "HW")
            self.assertIsNotNone(loaded["last_run_utc"])


class TestNoCheckoutAnywhere(unittest.TestCase):
    def test_no_provider_allowlists_order_or_payment_tools(self):
        from scout.providers import PROVIDERS
        # exact live tool names that place orders or move money, per provider
        forbidden = {
            "checkout", "confirm_order", "get_orders", "track_order",
            "get_payment_options", "check_payment_status",
            "zepto_shop", "create_order", "create_online_payment_order",
            "create_wallet_order", "create_upi_reserve_pay_order",
            "get_payment_methods", "add_saved_address",
        }
        for p in PROVIDERS.values():
            self.assertTrue(p.tool_allowlist.isdisjoint(forbidden),
                            f"{p.name} allowlist leaks an order/payment tool")
            # sanity: every allowlisted name is a read/address/cart verb
            for tool in p.tool_allowlist:
                self.assertFalse(any(v in tool for v in ("order", "pay", "checkout", "shop")),
                                 f"{p.name} allowlists suspicious tool {tool}")

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

    def test_real_cart_line_shape_survives_merge(self):
        # mirrors live get_cart items[] (13 Jul 2026)
        from scout.cart import _existing_to_update_items
        line = {"spinId": "8LTDGOK4ZH", "skuId": "X2FUSHLPWO", "productId": "UIL3IG7NBF",
                "itemName": "HW CADILLAC CELESTIQ", "quantity": 1, "storeId": 1397057,
                "isInStockAndAvailable": True, "mrp": 167, "discountedFinalPrice": 167}
        self.assertEqual(_existing_to_update_items([line]),
                         [{"spinId": "8LTDGOK4ZH", "skuId": "X2FUSHLPWO", "quantity": 1}])

    def test_payload_json_after_preamble(self):
        import types
        from scout.mcp_client import _result_payload
        text = ('Cart retrieved successfully. Please display the cart details...\n'
                'Data:\n{"items": [{"spinId": "S1"}], "cartId": "c1"}')
        result = types.SimpleNamespace(
            structuredContent={},  # observed empty on live responses
            content=[types.SimpleNamespace(text=text)])
        self.assertEqual(_result_payload(result),
                         {"items": [{"spinId": "S1"}], "cartId": "c1"})


class TestAddressSettings(unittest.TestCase):
    ENV = ("SWIGGY_ADDRESS_IDS", "SWIGGY_ADDRESS_ID", "SWIGGY_CART_ADDRESS_ID",
           "ZEPTO_ADDRESS_IDS", "ZEPTO_CART_ADDRESS_ID")

    def setUp(self):
        import os
        for k in self.ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        self.setUp()

    def test_ids_parsed_and_ordered_per_provider(self):
        import os
        from scout.settings import load_address_ids, load_cart_address_id
        os.environ["SWIGGY_ADDRESS_IDS"] = " a1 , a2 ,a3 "
        self.assertEqual(load_address_ids("swiggy"), ["a1", "a2", "a3"])
        self.assertEqual(load_cart_address_id("swiggy"), "a1")   # defaults to first
        self.assertEqual(load_address_ids("zepto"), [])          # independent per provider

    def test_explicit_cart_address(self):
        import os
        from scout.settings import load_cart_address_id
        os.environ["ZEPTO_ADDRESS_IDS"] = "z1,z2"
        os.environ["ZEPTO_CART_ADDRESS_ID"] = "z2"
        self.assertEqual(load_cart_address_id("zepto"), "z2")

    def test_legacy_single_address_fallback(self):
        import os
        from scout.settings import load_address_ids, load_cart_address_id
        os.environ["SWIGGY_ADDRESS_ID"] = "solo"
        self.assertEqual(load_address_ids("swiggy"), ["solo"])
        self.assertEqual(load_cart_address_id("swiggy"), "solo")


class TestAlertFormat(unittest.TestCase):
    def test_lists_addresses_and_cart_label(self):
        from scout.alerts import format_alert
        text = format_alert(product("1", "HW Skyline"), "Restock", True,
                            app="Zepto", link="https://z/x",
                            address_labels=["home", "work"], cart_label="home")
        self.assertIn("Hot Wheels (Zepto)", text)
        self.assertIn("In stock at: home, work", text)
        self.assertIn("Added to cart (home)", text)
        self.assertIn("Open in Zepto", text)

    def test_no_cart_line_when_not_added(self):
        from scout.alerts import format_alert
        text = format_alert(product("1", "HW Skyline"), "New arrival", False,
                            app="Instamart", link="https://s/x",
                            address_labels=["Akshay"])
        self.assertIn("In stock at: Akshay", text)
        self.assertNotIn("Added to cart", text)


class TestZeptoProvider(unittest.TestCase):
    def _zepto(self):
        from scout.providers import PROVIDERS
        return PROVIDERS["zepto"]

    def test_normalize_paise_and_ids(self):
        raw = {"productVariantId": "PV1", "storeProductId": "SP1",
               "name": "Hot Wheels HW Gone Mad", "price": 16700, "mrp": 19900,
               "imageUrl": "https://cdn.zeptonow.com/x.jpg", "availableQuantity": 1}
        p = self._zepto()._normalize(raw)
        self.assertEqual(p["id"], "PV1")
        self.assertEqual(p["spin_id"], "PV1")     # cart productVariantId
        self.assertEqual(p["sku_id"], "SP1")      # cart storeProductId
        self.assertEqual(p["price"], 167.0)       # paise -> rupees
        self.assertTrue(p["in_stock"])

    def test_normalize_out_of_stock_when_zero_qty(self):
        raw = {"productVariantId": "PV2", "storeProductId": "SP2",
               "name": "Hot Wheels X", "price": 16700, "availableQuantity": 0}
        self.assertFalse(self._zepto()._normalize(raw)["in_stock"])

    def test_cart_pvids_extraction(self):
        z = self._zepto()
        cart = {"cartItems": [{"productVariantId": "A"}, {"productVariantId": "B"}]}
        self.assertEqual(z._cart_pvids(cart), {"A", "B"})

    def test_zepto_is_alert_only_swiggy_is_not(self):
        from scout.providers import PROVIDERS
        self.assertFalse(PROVIDERS["zepto"].supports_cart)  # agent cart != app cart
        self.assertTrue(PROVIDERS["swiggy"].supports_cart)

    def test_zepto_confirms_on_first_sighting(self):
        # alert-only Zepto confirms fast (1); Swiggy keeps the config default.
        from scout.providers import PROVIDERS
        self.assertEqual(PROVIDERS["zepto"].confirm_threshold, 1)
        self.assertIsNone(PROVIDERS["swiggy"].confirm_threshold)

    def test_confirm_threshold_one_alerts_immediately(self):
        # with confirm_threshold=1, one sighting confirms a new arrival.
        seen = {}
        cur = {"1": product("1", "Hot Wheels A")}
        hits = update_address(seen, cur, ["hot wheels a"], "t", seeded=False,
                              confirm_threshold=1)
        self.assertEqual([(p["id"], k) for p, k in hits], [("1", "New arrival")])


if __name__ == "__main__":
    unittest.main()
