import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "ledger.py"
SPEC = importlib.util.spec_from_file_location("shopping_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ledger)


def fixture():
    return {
        "scope": {"ledger_id": "local-1", "as_of": "2026-11-08T10:00:00+08:00", "notes": "PRIVATE SCOPE"},
        "items": [{
            "id": "item-1", "title": "Tee <script>alert(1)</script>",
            "sku": {"product_code": "SKU-80", "spec": {"size": "80", "color": "blue"}},
            "quantity": 2, "cancelled_quantity": 0, "refunded_quantity": 1,
            "unit_paid": {"currency": "CNY", "amount_cents": 10000},
            "order_ref": "ORDER-SECRET", "notes": "ADDRESS SECRET"
        }],
        "quotes": [{
            "id": "quote-1", "item_ids": ["item-1"], "matches": {"item-1": "same"},
            "stage": "checkout_verified", "currency": "CNY", "items_total_cents": 7000,
            "fees_cents": 1000, "total_cents": 8000, "selected": True,
            "observed_at": "2026-11-08T10:00:00+08:00", "source": "checkout receipt",
            "coupons": [{"id": "ONCE", "single_use": True}]
        }],
        "actions": [{
            "id": "action-1", "type": "return", "target_id": "item-1",
            "idempotency_key": "return:item-1:v1", "status": "verified"
        }],
        "parcels": [{
            "id": "parcel-secret", "item_ids": ["item-1"], "status": "waiting_pickup",
            "quantities": {"item-1": 1},
            "pickup_code": "PICKUP-SECRET", "return_recipient": "PRIVATE NAME",
            "return_address": "PRIVATE ADDRESS", "pickup_window": "tomorrow",
            "tracking": {"receipt_verified": True, "carrier": "carrier-secret",
                         "tracking_number": "TRACK-SECRET", "evidence_source": "receipt",
                         "verified_at": "2026-11-08T10:00:00+08:00"}
        }],
        "refunds": [
            {"id": "refund-s", "item_id": "item-1", "currency": "CNY", "amount_cents": 500,
             "status": "received", "purpose": "savings", "destination": "cash",
             "received_verified_at": "2026-11-08T10:00:00+08:00"},
            {"id": "refund-r", "item_id": "item-1", "currency": "CNY", "amount_cents": 10000,
             "status": "received", "purpose": "recovery", "destination": "cash",
             "received_verified_at": "2026-11-08T10:00:00+08:00"},
            {"id": "refund-p", "item_id": "item-1", "currency": "CNY", "amount_cents": 700,
             "status": "approved", "purpose": "savings", "destination": "stored_value"}
        ]
    }


class LedgerTests(unittest.TestCase):
    def test_money_active_quantity_and_separate_buckets(self):
        data = fixture()
        result = ledger.validate_ledger(data)
        self.assertTrue(result["valid"], result["errors"])
        summary = result["summary"]
        self.assertEqual(summary["retained_quantity"], 1)  # cancelled/refunded units are excluded
        self.assertEqual(summary["retained_cost"], {"CNY": 10000})
        self.assertEqual(summary["expected_savings"], {})
        self.assertEqual(summary["implemented_savings"], {})
        self.assertEqual(summary["new_purchase_price_difference_not_net_savings"], {"CNY": 2000})
        self.assertEqual(summary["received_refunds"]["savings_related"]["cash"], {"CNY": 500})
        self.assertEqual(summary["received_refunds"]["recovery"]["cash"], {"CNY": 10000})
        self.assertEqual(summary["pending_refunds_not_counted"], {"CNY": 700})

    def test_wrong_total_and_unknown_fees_not_counted(self):
        data = fixture()
        data["quotes"][0]["total_cents"] = 7001
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("must equal" in error for error in result["errors"]))

        data = fixture()
        data["quotes"][0]["fees_cents"] = None
        data["quotes"][0]["total_cents"] = None
        result = ledger.validate_ledger(data)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["summary"]["new_purchase_price_difference_not_net_savings"], {})
        self.assertTrue(any("unknown fees" in warning for warning in result["warnings"]))

    def test_duplicate_submitted_or_unknown_action_is_unsafe_retry(self):
        data = fixture()
        data["actions"] = [
            {"id": "a1", "type": "buy", "target_id": "item-1", "idempotency_key": "buy:v1", "status": "unknown"},
            {"id": "a2", "type": "buy", "target_id": "item-1", "idempotency_key": "buy:v2", "status": "submitted"},
        ]
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("unsafe retry" in error for error in result["errors"]))

    def test_single_use_coupon_reuse_across_selected_groups(self):
        data = fixture()
        data["items"].append({
            "id": "item-2", "title": "Other", "sku": {"product_code": "SKU-2", "spec": {"size": "M"}},
            "quantity": 1, "cancelled_quantity": 0, "refunded_quantity": 0,
            "unit_paid": {"currency": "CNY", "amount_cents": 3000}
        })
        data["quotes"].append({
            "id": "quote-2", "item_ids": ["item-2"], "matches": {"item-2": "same"},
            "stage": "checkout_verified", "currency": "CNY", "items_total_cents": 2000,
            "fees_cents": 0, "total_cents": 2000, "selected": True,
            "observed_at": "2026-11-08T10:00:00+08:00", "source": "checkout receipt",
            "coupons": [{"id": "ONCE", "single_use": True}]
        })
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("single-use coupon" in error for error in result["errors"]))

    def test_same_sku_and_currency_are_required_for_checkout_comparison(self):
        data = fixture()
        data["quotes"][0]["matches"]["item-1"] = "different"
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("requires same match" in error for error in result["errors"]))

        data = fixture()
        data["quotes"][0]["currency"] = "USD"
        result = ledger.validate_ledger(data)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["summary"]["expected_savings"], {})
        self.assertEqual(result["summary"]["expected_savings_details"][0]["status"], "not_comparable")

    def test_pickup_code_does_not_verify_tracking(self):
        data = fixture()
        data["parcels"][0]["tracking"] = {"receipt_verified": True}
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("tracking_number" in error for error in result["errors"]))

    def test_public_report_whitelist_and_html_escaping(self):
        data = fixture()
        data["items"][0]["public_name"] = "Public <script>alert(2)</script>"
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            output = ledger.render_html(data, source, public=True)
        self.assertIn("Public &lt;script&gt;alert(2)&lt;/script&gt;", output)
        self.assertNotIn("<script>alert", output)
        for secret in ("Tee &lt;script&gt;alert(1)&lt;/script&gt;", "ORDER-SECRET", "ADDRESS SECRET", "TRACK-SECRET", "PICKUP-SECRET",
                       "parcel-secret", "PRIVATE NAME", "PRIVATE ADDRESS", "return:item-1:v1", "PRIVATE SCOPE"):
            self.assertNotIn(secret, output)
        self.assertIn("包裹-01", output)

    def test_private_report_has_fields_and_empty_states(self):
        data = fixture()
        data["quotes"] = []
        data["actions"] = []
        data["parcels"] = []
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            output = ledger.render_html(data, source, public=False)
        self.assertIn("暂无报价候选", output)
        self.assertIn("暂无操作记录", output)
        self.assertIn("暂无待寄包裹", output)
        self.assertIn("暂无图片", output)

    def test_report_statuses_are_chinese(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            output = ledger.render_html(data, source, public=False)
            mobile = ledger.render_mobile(data, source, public=False)
        self.assertIn("结算已核", output)
        self.assertIn("已核验", output)
        self.assertIn("已核验", mobile)
        self.assertNotIn(">checkout_verified<", output)

    def test_image_path_traversal_rejected(self):
        data = fixture()
        data["items"][0]["image"] = "../secret.png"
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            result = ledger.validate_ledger(data, source)
        self.assertFalse(result["valid"])
        self.assertTrue(any("traversal" in error for error in result["errors"]))

    def test_unknown_financial_action_reserves_budget(self):
        data = fixture()
        data["scope"]["budgets"] = {"CNY": 10000}
        data["actions"] = [
            {"id": "a1", "type": "buy", "target_id": "item-1", "idempotency_key": "buy:v1",
             "status": "unknown", "financial": True, "currency": "CNY", "amount_cents": 10001}
        ]
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("exceeds limit" in error and "unknown" in error for error in result["errors"]))

    def test_image_is_checked_by_content_and_requires_public_approval(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            fake = pathlib.Path(directory) / "looks-like.png"
            fake.write_text("not an image", encoding="utf-8")
            data["items"][0]["image"] = fake.name
            result = ledger.validate_ledger(data, source)
            self.assertFalse(result["valid"])
            self.assertTrue(any("image content" in error for error in result["errors"]))

            # Minimal signature is sufficient for this offline embedding check.
            fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo")
            self.assertTrue(ledger.validate_ledger(data, source)["valid"])
            public = ledger.render_html(data, source, public=True)
            self.assertNotIn("data:image/png;base64,", public)
            data["items"][0]["public_image"] = True
            public = ledger.render_html(data, source, public=True)
            self.assertIn("data:image/png;base64,", public)

    def test_public_mobile_uses_only_public_name(self):
        data = fixture()
        data["items"][0]["public_name"] = "Safe [name](https://example.invalid)"
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            output = ledger.render_mobile(data, source, public=True)
        self.assertNotIn("Tee <script>", output)
        self.assertNotIn("[name](https://example.invalid)", output)
        self.assertIn("\\[name\\]\\(https://example\\.invalid\\)", output)

    def test_public_parcel_status_is_closed_and_image_uses_same_page_dialog(self):
        data = fixture()
        secret = "PRIVATE ADDRESS IN STATUS"
        data["parcels"][0]["status"] = secret
        # Add an approved local image to exercise the actual enlargement markup.
        data["items"][0]["public_image"] = True
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            image = pathlib.Path(directory) / "demo.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"demo")
            data["items"][0]["image"] = image.name
            public_html = ledger.render_html(data, source, public=True)
            public_mobile = ledger.render_mobile(data, source, public=True)
        self.assertNotIn(secret, public_html)
        self.assertNotIn(secret, public_mobile)
        self.assertIn("待核", public_html)
        self.assertIn("待核", public_mobile)
        self.assertIn("<dialog id='image-dialog'>", public_html)
        self.assertIn("dialog.querySelector('img').src=image.src", public_html)
        self.assertIn("dialog.showModal()", public_html)
        self.assertNotIn("target='_blank'", public_html)
        self.assertNotIn("href='data:image", public_html)

    def test_report_rejects_all_output_aliases_before_writing(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            image = root / "demo.png"
            image_bytes = b"\x89PNG\r\n\x1a\n" + b"unchanged-image"
            image.write_bytes(image_bytes)
            data["items"][0]["image"] = image.name
            source = root / "input.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            input_bytes = source.read_bytes()

            with contextlib.redirect_stderr(io.StringIO()):
                # Direct input overwrite.
                self.assertEqual(ledger.main(["report", str(source), "--html", str(source),
                                              "--mobile", str(root / "mobile.md")]), 1)
                self.assertEqual(source.read_bytes(), input_bytes)

                # Symlink/hardlink aliases of input and output/output collision.
                input_alias = root / "input-alias.json"
                input_alias.symlink_to(source)
                input_hardlink = root / "input-hardlink.json"
                os.link(str(source), str(input_hardlink))
                same_output = root / "same-output"
                self.assertEqual(ledger.main(["report", str(source), "--html", str(input_alias),
                                              "--mobile", str(same_output)]), 1)
                self.assertEqual(ledger.main(["report", str(source), "--html", str(input_hardlink),
                                              "--mobile", str(same_output)]), 1)
                self.assertEqual(ledger.main(["report", str(source), "--html", str(same_output),
                                              "--mobile", str(same_output)]), 1)

                # Referenced images are protected, including through a symlink alias.
                image_alias = root / "image-alias.png"
                image_alias.symlink_to(image)
                self.assertEqual(ledger.main(["report", str(source), "--html", str(image_alias),
                                              "--mobile", str(root / "other.md")]), 1)
            self.assertEqual(source.read_bytes(), input_bytes)
            self.assertEqual(image.read_bytes(), image_bytes)
            self.assertFalse((root / "mobile.md").exists())
            self.assertFalse((root / "same-output").exists())
            self.assertFalse((root / "other.md").exists())

    def test_invalid_json_field_types_return_errors_not_typeerror(self):
        mutations = [
            lambda d: d["actions"][0].update(target_id=[]),
            lambda d: d["quotes"][0].update(item_ids=[{}]),
            lambda d: d["quotes"][0].update(stage=[]),
            lambda d: d["actions"][0].update(type=[]),
            lambda d: d["refunds"][0].update(status={}),
            lambda d: d["parcels"][0].update(status=[]),
            lambda d: d["quotes"][0]["matches"].update({"item-1": []}),
            lambda d: d["refunds"][0].update(purpose=[]),
            lambda d: d["refunds"][0].update(destination={}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = fixture()
                mutate(data)
                result = ledger.validate_ledger(data)
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"])

    def test_selected_and_public_image_are_strict_booleans(self):
        for bad in ("false", 0, 1, [], {}):
            with self.subTest(field="selected", bad=bad):
                data = fixture()
                data["quotes"][0]["selected"] = bad
                self.assertFalse(ledger.validate_ledger(data)["valid"])
            with self.subTest(field="public_image", bad=bad):
                data = fixture()
                data["items"][0]["public_image"] = bad
                self.assertFalse(ledger.validate_ledger(data)["valid"])

    def test_quote_quantity_is_explicit_for_multiple_retained_units(self):
        data = fixture()
        data["items"][0]["refunded_quantity"] = 0  # two retained units
        data["quotes"][0].update(items_total_cents=9000, fees_cents=0, total_cents=9000)
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("quantities.item-1" in error for error in result["errors"]))

        data["quotes"][0]["quantities"] = {"item-1": 1}
        result = ledger.validate_ledger(data)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["summary"]["new_purchase_price_difference_not_net_savings"], {"CNY": 1000})

        data["quotes"][0]["quantities"] = {"item-1": 3}
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("must not exceed retained quantity" in error for error in result["errors"]))

    def test_quote_quantity_nested_values_are_strict(self):
        invalid_values = [None, [], {}, True, False, 0, ""]
        for bad in invalid_values:
            with self.subTest(bad=bad):
                data = fixture()
                data["quotes"][0]["quantities"] = {"item-1": bad}
                result = ledger.validate_ledger(data)
                self.assertFalse(result["valid"])
                self.assertTrue(any("quantities.item-1" in error for error in result["errors"]))

        # A literal positive integer is valid; omission remains compatible only
        # because this fixture has exactly one retained unit.
        for quantities in ({"item-1": 1}, {}):
            with self.subTest(valid=quantities):
                data = fixture()
                data["quotes"][0]["quantities"] = quantities
                result = ledger.validate_ledger(data)
                self.assertTrue(result["valid"], result["errors"])

    def test_checkout_evidence_required_and_private_only(self):
        data = fixture()
        del data["quotes"][0]["observed_at"]
        del data["quotes"][0]["source"]
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("observed_at" in error for error in result["errors"]))
        self.assertTrue(any("source" in error for error in result["errors"]))

        data = fixture()
        data["quotes"][0]["observed_at"] = "2026-11-08T10:00:00"  # no timezone
        self.assertFalse(ledger.validate_ledger(data)["valid"])

        data = fixture()
        secret_source = "PRIVATE CHECKOUT SOURCE"
        data["quotes"][0]["source"] = secret_source
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            private = ledger.render_html(data, source, public=False)
            public = ledger.render_html(data, source, public=True)
        self.assertIn(secret_source, private)
        self.assertNotIn(secret_source, public)
        self.assertIn("账本记录：结算已核", public)

    def test_action_retry_order_allows_resolved_failure_only(self):
        def actions(first, second):
            return [
                {"id": "a1", "type": "buy", "target_id": "item-1", "idempotency_key": "buy:v1", "status": first},
                {"id": "a2", "type": "buy", "target_id": "item-1", "idempotency_key": "buy:v2", "status": second},
            ]

        for first in ("failed", "needs_user"):
            with self.subTest(allowed_first=first):
                data = fixture()
                data["actions"] = actions(first, "submitted")
                self.assertTrue(ledger.validate_ledger(data)["valid"])
        for first, second in (("unknown", "failed"), ("submitted", "unknown"), ("verified", "submitted")):
            with self.subTest(rejected=(first, second)):
                data = fixture()
                data["actions"] = actions(first, second)
                result = ledger.validate_ledger(data)
                self.assertFalse(result["valid"])
                self.assertTrue(any("unsafe retry" in error for error in result["errors"]))

    def test_parcel_quantities_are_strict_and_use_purchased_history(self):
        # A fully refunded historic purchase can still be the legitimate item
        # in a return parcel; the upper bound is purchased non-cancelled units,
        # not the current retained quantity.
        data = fixture()
        data["items"][0]["refunded_quantity"] = 2
        data["quotes"] = []
        data["parcels"][0]["quantities"] = {"item-1": 2}
        self.assertTrue(ledger.validate_ledger(data)["valid"])

        del data["parcels"][0]["quantities"]
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("parcels[0].quantities.item-1" in error for error in result["errors"]))

        for bad in (None, [], {}, True, False, 0, "1"):
            with self.subTest(bad=bad):
                data = fixture()
                data["parcels"][0]["quantities"] = {"item-1": bad}
                result = ledger.validate_ledger(data)
                self.assertFalse(result["valid"])
                self.assertTrue(any("parcels[0].quantities.item-1" in error for error in result["errors"]))

        data = fixture()
        data["parcels"][0]["quantities"] = []
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("parcels[0].quantities" in error for error in result["errors"]))

        data = fixture()
        data["parcels"][0]["quantities"] = {"item-1": 3}
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("purchased non-cancelled quantity 2" in error for error in result["errors"]))

        data = fixture()
        data["items"][0].update(quantity=1, refunded_quantity=1)
        data["quotes"] = []
        del data["parcels"][0]["quantities"]
        self.assertTrue(ledger.validate_ledger(data)["valid"])

    def test_parcel_reports_map_each_package_to_items_specs_counts_and_refs(self):
        data = fixture()
        data["items"][0].update(
            title="PRIVATE BLUE TITLE", public_name="Public blue",
            public_spec={"size": "80"}, notes="PRIVATE BLUE NOTES")
        data["items"].append({
            "id": "item-2", "title": "PRIVATE RED TITLE", "public_name": "Public red",
            "public_spec": {"size": "90"},
            "sku": {"product_code": "SECRET-SKU-2", "spec": {"size": "90", "color": "PRIVATE RED"}},
            "quantity": 1, "cancelled_quantity": 0, "refunded_quantity": 0,
            "unit_paid": {"currency": "CNY", "amount_cents": 3000},
            "order_ref": "ORDER-SECRET-2", "notes": "PRIVATE RED NOTES"
        })
        data["refunds"].append({
            "id": "AFTERSALE-SECRET-2", "item_id": "item-2", "currency": "CNY", "amount_cents": 3000,
            "status": "approved", "purpose": "recovery", "destination": "cash"
        })
        data["parcels"] = [
            {"id": "p1", "item_ids": ["item-1"], "quantities": {"item-1": 1},
             "status": "prepared", "tracking": {"receipt_verified": False}},
            {"id": "p2", "item_ids": ["item-2"],
             "status": "prepared", "tracking": {"receipt_verified": False}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            private_html = ledger.render_html(data, source, public=False)
            private_mobile = ledger.render_mobile(data, source, public=False)
            public_html = ledger.render_html(data, source, public=True)
            public_mobile = ledger.render_mobile(data, source, public=True)

            swapped = json.loads(json.dumps(data))
            swapped["parcels"][0].update(item_ids=["item-2"], quantities={"item-2": 1})
            swapped["parcels"][1].update(item_ids=["item-1"], quantities={"item-1": 1})
            swapped_html = ledger.render_html(swapped, source, public=False)

        for output in (private_html, private_mobile):
            self.assertIn("PRIVATE BLUE TITLE", output)
            self.assertIn("PRIVATE RED TITLE", output)
            self.assertIn("本包数量：1", output)
        self.assertIn("ORDER-SECRET", private_html)
        self.assertIn("ORDER-SECRET-2", private_html)
        self.assertIn("AFTERSALE-SECRET-2", private_html)
        self.assertIn("ORDER\\-SECRET", private_mobile)
        self.assertIn("AFTERSALE\\-SECRET\\-2", private_mobile)
        self.assertNotEqual(private_html, swapped_html)
        for output in (public_html, public_mobile):
            self.assertIn("Public blue", output)
            self.assertIn("Public red", output)
            self.assertIn("size", output)
            self.assertNotIn("PRIVATE BLUE TITLE", output)
            self.assertNotIn("PRIVATE RED TITLE", output)
            self.assertNotIn("PRIVATE BLUE NOTES", output)
            self.assertNotIn("PRIVATE RED NOTES", output)
            self.assertNotIn("PRIVATE RED", output)
            self.assertNotIn("ORDER-SECRET", output)
            self.assertNotIn("AFTERSALE-SECRET", output)

    def test_quote_reports_include_mapping_quantity_group_total_stage_selection_and_time(self):
        data = fixture()
        data["items"][0].update(refunded_quantity=0, public_name="Public item", public_spec={"size": "80"})
        data["parcels"] = []
        data["quotes"] = [
            {"id": "one-at-80", "item_ids": ["item-1"], "quantities": {"item-1": 1},
             "matches": {"item-1": "same"}, "stage": "checkout_verified", "currency": "CNY",
             "items_total_cents": 8000, "fees_cents": 0, "total_cents": 8000, "selected": True,
             "observed_at": "2026-11-08T11:00:00+08:00", "source": "PRIVATE SOURCE ONE", "coupons": []},
            {"id": "two-at-80", "item_ids": ["item-1"], "quantities": {"item-1": 2},
             "matches": {"item-1": "same"}, "stage": "checkout_verified", "currency": "CNY",
             "items_total_cents": 8000, "fees_cents": 0, "total_cents": 8000, "selected": False,
             "observed_at": "2026-11-08T12:00:00+08:00", "source": "PRIVATE SOURCE TWO", "coupons": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            private_html = ledger.render_html(data, source, public=False)
            private_mobile = ledger.render_mobile(data, source, public=False)
            public_html = ledger.render_html(data, source, public=True)
            public_mobile = ledger.render_mobile(data, source, public=True)
        for output in (private_html, public_html):
            self.assertIn("数量：1", output)
            self.assertIn("数量：2", output)
            self.assertIn("CNY 80.00", output)
            self.assertIn("账本记录：结算已核", output)
            self.assertIn("已选", output)
            self.assertIn("候选", output)
            self.assertIn("2026-11-08T11:00:00+08:00", output)
            self.assertIn("2026-11-08T12:00:00+08:00", output)
        for output in (private_mobile, public_mobile):
            self.assertIn("数量：1", output)
            self.assertIn("数量：2", output)
            self.assertIn("CNY 80.00", output)
            self.assertIn("账本记录：结算已核", output)
            self.assertIn("已选", output)
            self.assertIn("候选", output)
            self.assertIn("2026\\-11\\-08T11:00:00\\+08:00", output)
            self.assertIn("2026\\-11\\-08T12:00:00\\+08:00", output)
        self.assertIn("item-1", private_html)
        self.assertIn(r"item\-1", private_mobile)
        self.assertIn("PRIVATE SOURCE ONE", private_html)
        self.assertIn("PRIVATE SOURCE ONE", private_mobile)
        for output in (public_html, public_mobile):
            self.assertIn("Public item", output)
            self.assertNotIn("PRIVATE SOURCE", output)
            self.assertNotIn("Tee <script>", output)

    def test_public_spec_is_explicit_and_private_spec_never_falls_through(self):
        data = fixture()
        data["items"][0]["public_name"] = "Safe item"
        data["items"][0]["sku"]["spec"] = {"size": "PRIVATE-SIZE", "color": "PRIVATE-COLOR"}
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "input.json"
            source.write_text("{}", encoding="utf-8")
            public = ledger.render_html(data, source, public=True)
        self.assertIn("未公开", public)
        self.assertNotIn("PRIVATE-SIZE", public)
        self.assertNotIn("PRIVATE-COLOR", public)

        data["items"][0]["public_spec"] = ["size"]
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("public_spec" in error for error in result["errors"]))

    def test_terminal_evidence_timestamps_require_timezone_iso8601(self):
        data = fixture()
        data["parcels"][0]["tracking"]["verified_at"] = "刚刚"
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("tracking.verified_at" in error for error in result["errors"]))

        data = fixture()
        data["refunds"][0]["received_verified_at"] = "已到账"
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("received_verified_at" in error for error in result["errors"]))

    def test_duplicate_item_ids_are_rejected_in_quotes_and_parcels(self):
        data = fixture()
        data["quotes"][0].update(
            item_ids=["item-1", "item-1"], selected=False,
            quantities={"item-1": 1})
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("quotes[0].item_ids" in error and "duplicate item id" in error
                            for error in result["errors"]))

        data = fixture()
        data["items"][0].update(quantity=1, refunded_quantity=0)
        data["parcels"][0].update(
            item_ids=["item-1", "item-1"], quantities={"item-1": 1})
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("parcels[0].item_ids" in error and "duplicate item id" in error
                            for error in result["errors"]))

    def test_optional_early_quote_observed_at_is_validated_when_present(self):
        private_text = "PRIVATE ADDRESS Shanghai ROOM 806 PHONE 13800000000"
        for stage in ("candidate", "spec_verified"):
            with self.subTest(stage=stage, missing=True):
                data = fixture()
                data["quotes"][0].update(stage=stage, selected=False)
                data["quotes"][0].pop("observed_at")
                data["quotes"][0].pop("source")
                self.assertTrue(ledger.validate_ledger(data)["valid"])

            for bad in (private_text, {}, True, False):
                with self.subTest(stage=stage, bad=bad):
                    data = fixture()
                    data["quotes"][0].update(stage=stage, selected=False, observed_at=bad)
                    data["quotes"][0].pop("source")
                    result = ledger.validate_ledger(data)
                    self.assertFalse(result["valid"])
                    self.assertTrue(any("quotes[0].observed_at" in error for error in result["errors"]))

        data = fixture()
        data["quotes"][0].update(
            stage="candidate", selected=False,
            observed_at="2026-11-08T09:00:00+08:00")
        data["quotes"][0].pop("source")
        self.assertTrue(ledger.validate_ledger(data)["valid"])

    def test_parcel_quantities_are_bounded_across_the_current_ledger(self):
        def two_single_unit_parcels(data):
            data["parcels"] = [
                {"id": "p1", "item_ids": ["item-1"], "quantities": {"item-1": 1},
                 "tracking": {"receipt_verified": False}},
                {"id": "p2", "item_ids": ["item-1"], "quantities": {"item-1": 1},
                 "tracking": {"receipt_verified": False}},
            ]

        data = fixture()
        data["items"][0].update(quantity=2, refunded_quantity=0)
        data["quotes"][0]["quantities"] = {"item-1": 1}
        two_single_unit_parcels(data)
        self.assertTrue(ledger.validate_ledger(data)["valid"])

        data = fixture()
        data["items"][0].update(quantity=1, refunded_quantity=0)
        two_single_unit_parcels(data)
        for parcel in data["parcels"]:
            parcel.pop("quantities")  # legacy implicit one must also aggregate
        result = ledger.validate_ledger(data)
        self.assertFalse(result["valid"])
        self.assertTrue(any("parcels.quantities.item-1" in error and
                            "total packed quantity 2" in error for error in result["errors"]))

        data = fixture()
        data["items"][0].update(quantity=2, refunded_quantity=2)
        data["quotes"] = []
        two_single_unit_parcels(data)
        self.assertTrue(ledger.validate_ledger(data)["valid"])


if __name__ == "__main__":
    unittest.main()
