#!/usr/bin/env python3
"""Offline validator and report generator for a shopping-concierge ledger.

This module deliberately performs no network access and no platform actions.
Amounts are integer minor units (``amount_cents``), grouped by currency.
"""

import argparse
import base64
import datetime as _dt
import html
import json
import pathlib
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


ACTION_STATES = {"prepared", "submitted", "verified", "unknown", "failed", "needs_user"}
QUOTE_STAGES = {"candidate", "spec_verified", "checkout_verified", "ordered", "paid"}
MATCH_STATES = {"same", "different", "unknown"}
REFUND_STATES = {"requested", "approved", "platform_refunded", "received"}
REFUND_PURPOSES = {"savings", "recovery"}
IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PUBLIC_PARCEL_STATES = {
    "prepared": "待准备", "waiting_pickup": "待取件", "picked_up": "已揽收",
    "in_transit": "运输中", "delivered": "已送达", "needs_user": "待处理", "unknown": "待核",
}
QUOTE_STAGE_LABELS = {
    "candidate": "账本记录：报价候选", "spec_verified": "账本记录：规格已核",
    "checkout_verified": "账本记录：结算已核", "ordered": "账本记录：已下单", "paid": "账本记录：已付款",
}
ACTION_STATE_LABELS = {
    "prepared": "已准备", "submitted": "已提交待回读", "verified": "已核验",
    "unknown": "结果未知待对账", "failed": "已失败", "needs_user": "待用户处理",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _money(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list(value: Any) -> bool:
    return isinstance(value, list)


def _add(errors: List[str], path: str, message: str) -> None:
    errors.append("%s: %s" % (path, message))


def _required_object(data: Dict[str, Any], key: str, errors: List[str]) -> Dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        _add(errors, key, "must be an object")
        return {}
    return value


def _required_array(data: Dict[str, Any], key: str, errors: List[str]) -> List[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        _add(errors, key, "must be an array")
        return []
    return value


def _check_ids(rows: Sequence[Any], name: str, errors: List[str]) -> Set[str]:
    seen: Set[str] = set()
    for index, row in enumerate(rows):
        path = "%s[%d]" % (name, index)
        if not isinstance(row, dict):
            _add(errors, path, "must be an object")
            continue
        row_id = row.get("id")
        if not _string(row_id):
            _add(errors, path + ".id", "must be a non-empty string")
        elif row_id in seen:
            _add(errors, path + ".id", "duplicate id %r" % row_id)
        else:
            seen.add(row_id)
    return seen


def _check_currency(value: Any, path: str, errors: List[str]) -> None:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or value != value.upper():
        _add(errors, path, "must be an uppercase 3-letter currency code")


def _check_timestamp(value: Any, path: str, errors: List[str]) -> None:
    if not _string(value):
        _add(errors, path, "must be an ISO-8601 timestamp with timezone")
        return
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError):
        _add(errors, path, "must be an ISO-8601 timestamp with timezone")


def _detect_image_mime_bytes(content: bytes) -> Optional[str]:
    """Recognize the three supported formats by content, never by extension."""
    header = content[:16]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _detect_image_mime(path: pathlib.Path) -> Optional[str]:
    with path.open("rb") as handle:
        return _detect_image_mime_bytes(handle.read(16))


def _active_quantity(item: Dict[str, Any]) -> int:
    quantity = item.get("quantity", 0)
    cancelled = item.get("cancelled_quantity", 0)
    refunded = item.get("refunded_quantity", 0)
    if not all(_money(v) for v in (quantity, cancelled, refunded)):
        return 0
    return max(0, quantity - cancelled - refunded)


def _purchased_quantity(item: Dict[str, Any]) -> int:
    """Units that could legitimately belong to a current or historic parcel.

    Refunded units remain eligible because a parcel may be the return shipment
    for an already-refunded item.  Cancelled units were never purchased.
    """
    quantity = item.get("quantity", 0)
    cancelled = item.get("cancelled_quantity", 0)
    if not all(_money(v) for v in (quantity, cancelled)):
        return 0
    return max(0, quantity - cancelled)


def _mapped_quantity(row: Dict[str, Any], item_id: str) -> int:
    """Return a validated quote/parcel quantity, including legacy implicit 1."""
    return row.get("quantities", {}).get(item_id, 1)


def validate_ledger(data: Any, input_path: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    """Return {valid, errors, warnings, summary} without mutating *data*."""
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return {"valid": False, "errors": ["root: must be an object"], "warnings": [], "summary": {}}

    scope = _required_object(data, "scope", errors)
    items = _required_array(data, "items", errors)
    quotes = _required_array(data, "quotes", errors)
    actions = _required_array(data, "actions", errors)
    parcels = _required_array(data, "parcels", errors)
    refunds = _required_array(data, "refunds", errors)

    if not _string(scope.get("ledger_id")):
        _add(errors, "scope.ledger_id", "must be a non-empty string")
    _check_timestamp(scope.get("as_of"), "scope.as_of", errors)
    budgets = scope.get("budgets", {})
    if not isinstance(budgets, dict):
        _add(errors, "scope.budgets", "must be an object mapping currencies to integer limits")
        budgets = {}
    for currency, limit in budgets.items():
        _check_currency(currency, "scope.budgets.%s" % currency, errors)
        if not _money(limit):
            _add(errors, "scope.budgets.%s" % currency, "must be a non-negative integer")

    item_ids = _check_ids(items, "items", errors)
    quote_ids = _check_ids(quotes, "quotes", errors)
    _check_ids(actions, "actions", errors)
    _check_ids(parcels, "parcels", errors)
    refund_ids = _check_ids(refunds, "refunds", errors)

    items_by_id: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        path = "items[%d]" % index
        if _string(item.get("id")):
            items_by_id[item["id"]] = item
        if not _string(item.get("title")):
            _add(errors, path + ".title", "must be a non-empty string")
        if item.get("public_name") is not None and not _string(item.get("public_name")):
            _add(errors, path + ".public_name", "must be a non-empty string when present")
        if item.get("public_image") is not None and type(item.get("public_image")) is not bool:
            _add(errors, path + ".public_image", "must be boolean when present")
        if item.get("public_spec") is not None and not isinstance(item.get("public_spec"), dict):
            _add(errors, path + ".public_spec", "must be an object when present")
        sku = item.get("sku")
        if not isinstance(sku, dict) or not _string(sku.get("product_code")) or not isinstance(sku.get("spec"), dict):
            _add(errors, path + ".sku", "must contain product_code and a spec object")
        for field in ("quantity", "cancelled_quantity", "refunded_quantity"):
            if not _money(item.get(field)):
                _add(errors, path + "." + field, "must be a non-negative integer")
        if all(_money(item.get(f)) for f in ("quantity", "cancelled_quantity", "refunded_quantity")):
            if item["cancelled_quantity"] + item["refunded_quantity"] > item["quantity"]:
                _add(errors, path, "cancelled_quantity + refunded_quantity exceeds quantity")
        paid = item.get("unit_paid")
        if not isinstance(paid, dict):
            _add(errors, path + ".unit_paid", "must be an object")
        else:
            _check_currency(paid.get("currency"), path + ".unit_paid.currency", errors)
            if not _money(paid.get("amount_cents")):
                _add(errors, path + ".unit_paid.amount_cents", "must be a non-negative integer")
        image_path = item.get("image")
        if image_path is not None:
            image_error = _image_error(image_path, input_path)
            if image_error:
                _add(errors, path + ".image", image_error)

    selected_item_use: Dict[str, str] = {}
    single_use_coupons: Dict[str, str] = {}
    selected_quote_count = 0
    for index, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            continue
        path = "quotes[%d]" % index
        qitems = quote.get("item_ids")
        if not _list(qitems) or not qitems:
            _add(errors, path + ".item_ids", "must be a non-empty array")
            qitems = []
        valid_qitems: List[str] = []
        seen_qitems: Set[str] = set()
        for item_id in qitems:
            if not _string(item_id):
                _add(errors, path + ".item_ids", "every item id must be a non-empty string")
                continue
            if item_id in seen_qitems:
                _add(errors, path + ".item_ids", "duplicate item id %r; use quantities for counts" % item_id)
            else:
                seen_qitems.add(item_id)
            valid_qitems.append(item_id)
            if item_id not in item_ids:
                _add(errors, path + ".item_ids", "unknown item id %r" % item_id)
        quantities = quote.get("quantities", {})
        if not isinstance(quantities, dict):
            _add(errors, path + ".quantities", "must map item ids to positive integer quantities")
            quantities = {}
        for quantity_item_id in quantities:
            if quantity_item_id not in valid_qitems:
                _add(errors, path + ".quantities", "quantity provided for an item outside item_ids: %r" % quantity_item_id)
        for item_id in valid_qitems:
            active = _active_quantity(items_by_id.get(item_id, {}))
            if item_id not in quantities and active == 1:
                count = 1
            elif item_id not in quantities:
                _add(errors, path + ".quantities.%s" % item_id,
                     "is required when retained quantity is not exactly one")
                continue
            else:
                count = quantities[item_id]
            if not _is_int(count) or count <= 0:
                _add(errors, path + ".quantities.%s" % item_id, "must be a positive integer")
            elif count > active:
                _add(errors, path + ".quantities.%s" % item_id,
                     "must not exceed retained quantity %d" % active)
        matches = quote.get("matches")
        if not isinstance(matches, dict):
            _add(errors, path + ".matches", "must map every item id to same/different/unknown")
            matches = {}
        for item_id in valid_qitems:
            match_state = matches.get(item_id)
            if not isinstance(match_state, str) or match_state not in MATCH_STATES:
                _add(errors, path + ".matches.%s" % item_id, "must be same, different, or unknown")
        stage = quote.get("stage")
        if not isinstance(stage, str) or stage not in QUOTE_STAGES:
            _add(errors, path + ".stage", "invalid quote stage")
        selected = quote.get("selected")
        if type(selected) is not bool:
            _add(errors, path + ".selected", "must be boolean")
        _check_currency(quote.get("currency"), path + ".currency", errors)
        if not _money(quote.get("items_total_cents")):
            _add(errors, path + ".items_total_cents", "must be a non-negative integer")
        fees = quote.get("fees_cents")
        if fees is not None and not _money(fees):
            _add(errors, path + ".fees_cents", "must be null or a non-negative integer")
        total = quote.get("total_cents")
        if total is not None and not _money(total):
            _add(errors, path + ".total_cents", "must be null or a non-negative integer")
        if _money(quote.get("items_total_cents")) and _money(fees) and _money(total):
            if quote["items_total_cents"] + fees != total:
                _add(errors, path + ".total_cents", "must equal items_total_cents + fees_cents")
        if fees is None and total is not None:
            _add(errors, path + ".total_cents", "must be null when fees_cents is unknown")
        if isinstance(stage, str) and stage in {"checkout_verified", "ordered", "paid"}:
            if any(matches.get(item_id) != "same" for item_id in valid_qitems):
                _add(errors, path + ".matches", "checkout-or-later quote requires same match for every item")
            if fees is None or total is None:
                warnings.append(path + ": checkout verified but unknown fees; no definite savings will be calculated")
            if not _string(quote.get("source")):
                _add(errors, path + ".source", "must be a non-empty evidence source for checkout-or-later stages")
        if "observed_at" in quote or (isinstance(stage, str) and stage in {"checkout_verified", "ordered", "paid"}):
            _check_timestamp(quote.get("observed_at"), path + ".observed_at", errors)

        coupons = quote.get("coupons", [])
        if not isinstance(coupons, list):
            _add(errors, path + ".coupons", "must be an array")
            coupons = []
        local_coupon_ids: Set[str] = set()
        if selected is True:
            selected_quote_count += 1
            for item_id in valid_qitems:
                if item_id in selected_item_use:
                    _add(errors, path + ".selected", "item %r is also selected by quote %r" % (item_id, selected_item_use[item_id]))
                else:
                    selected_item_use[item_id] = quote.get("id", path)
        for cindex, coupon in enumerate(coupons):
            cpath = "%s.coupons[%d]" % (path, cindex)
            if not isinstance(coupon, dict) or not _string(coupon.get("id")):
                _add(errors, cpath, "must contain a non-empty id")
                continue
            cid = coupon["id"]
            if cid in local_coupon_ids:
                _add(errors, cpath + ".id", "duplicate coupon in quote")
            local_coupon_ids.add(cid)
            if coupon.get("single_use") is not True and coupon.get("single_use") is not False:
                _add(errors, cpath + ".single_use", "must be boolean")
            if selected is True and coupon.get("single_use") is True:
                if cid in single_use_coupons:
                    _add(errors, cpath + ".id", "single-use coupon also used by selected quote %r" % single_use_coupons[cid])
                else:
                    single_use_coupons[cid] = quote.get("id", path)

    idempotency_keys: Dict[str, str] = {}
    operations: Dict[Tuple[Any, Any], List[Tuple[str, str]]] = defaultdict(list)
    budget_occupied: Dict[str, int] = defaultdict(int)
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        path = "actions[%d]" % index
        for field in ("type", "target_id", "idempotency_key"):
            if not _string(action.get(field)):
                _add(errors, path + "." + field, "must be a non-empty string")
        state = action.get("status")
        if not isinstance(state, str) or state not in ACTION_STATES:
            _add(errors, path + ".status", "invalid action status")
        key = action.get("idempotency_key")
        if _string(key):
            if key in idempotency_keys:
                _add(errors, path + ".idempotency_key", "duplicate stable key also used by %s" % idempotency_keys[key])
            else:
                idempotency_keys[key] = action.get("id", path)
        action_type = action.get("type")
        target_id = action.get("target_id")
        if _string(action_type) and _string(target_id):
            operations[(action_type, target_id)].append((state, action.get("id", path)))
        financial = action.get("financial")
        if financial is not None and type(financial) is not bool:
            _add(errors, path + ".financial", "must be boolean")
        if financial is True:
            _check_currency(action.get("currency"), path + ".currency", errors)
            if not _money(action.get("amount_cents")):
                _add(errors, path + ".amount_cents", "must be a non-negative integer for a financial action")
            elif isinstance(state, str) and state in {"submitted", "unknown", "verified"} and _string(action.get("currency")):
                # Unknown submissions may already have succeeded, so they continue to reserve budget.
                budget_occupied[action["currency"]] += action["amount_cents"]
    for operation, attempts in operations.items():
        unresolved_positions = [index for index, attempt in enumerate(attempts)
                                if isinstance(attempt[0], str) and attempt[0] in {"submitted", "unknown"}]
        verified_positions = [index for index, attempt in enumerate(attempts) if attempt[0] == "verified"]
        unsafe = (len(unresolved_positions) > 1 or
                  any(index < len(attempts) - 1 for index in unresolved_positions) or
                  any(index < len(attempts) - 1 for index in verified_positions))
        if unsafe:
            errors.append("actions: unsafe retry for operation %r/%r; unresolved or verified attempt must be reconciled before another attempt" %
                          (operation[0], operation[1]))
    for currency, occupied in budget_occupied.items():
        if currency in budgets and _money(budgets[currency]) and occupied > budgets[currency]:
            errors.append("scope.budgets.%s: occupied amount %d exceeds limit %d (submitted, unknown and verified actions count)" %
                          (currency, occupied, budgets[currency]))

    parcel_quantity_totals: Dict[str, int] = defaultdict(int)
    for index, parcel in enumerate(parcels):
        if not isinstance(parcel, dict):
            continue
        path = "parcels[%d]" % index
        contained = parcel.get("item_ids")
        valid_contained: List[str] = []
        seen_contained: Set[str] = set()
        if not isinstance(contained, list) or not contained:
            _add(errors, path + ".item_ids", "must be a non-empty array")
        else:
            for item_id in contained:
                if not _string(item_id):
                    _add(errors, path + ".item_ids", "every item id must be a non-empty string")
                    continue
                if item_id in seen_contained:
                    _add(errors, path + ".item_ids", "duplicate item id %r; use quantities for counts" % item_id)
                else:
                    seen_contained.add(item_id)
                valid_contained.append(item_id)
                if item_id not in item_ids:
                    _add(errors, path + ".item_ids", "unknown item id %r" % item_id)
        quantities = parcel.get("quantities", {})
        if not isinstance(quantities, dict):
            _add(errors, path + ".quantities", "must map item ids to positive integer quantities")
            quantities = {}
        for quantity_item_id in quantities:
            if quantity_item_id not in valid_contained:
                _add(errors, path + ".quantities",
                     "quantity provided for an item outside item_ids: %r" % quantity_item_id)
        for item_id in valid_contained:
            purchased = _purchased_quantity(items_by_id.get(item_id, {}))
            if item_id not in quantities and purchased == 1:
                count = 1
            elif item_id not in quantities:
                _add(errors, path + ".quantities.%s" % item_id,
                     "is required when purchased non-cancelled quantity is not exactly one")
                continue
            else:
                count = quantities[item_id]
            if not _is_int(count) or count <= 0:
                _add(errors, path + ".quantities.%s" % item_id, "must be a positive integer")
            elif count > purchased:
                _add(errors, path + ".quantities.%s" % item_id,
                     "must not exceed purchased non-cancelled quantity %d" % purchased)
            else:
                parcel_quantity_totals[item_id] += count
        if parcel.get("status") is not None and not _string(parcel.get("status")):
            _add(errors, path + ".status", "must be a non-empty string when present")
        tracking = parcel.get("tracking")
        if not isinstance(tracking, dict):
            _add(errors, path + ".tracking", "must be an object")
            tracking = {}
        verified = tracking.get("receipt_verified")
        if verified is not True and verified is not False:
            _add(errors, path + ".tracking.receipt_verified", "must be boolean")
        if verified is True:
            for field in ("carrier", "tracking_number", "evidence_source"):
                if not _string(tracking.get(field)):
                    _add(errors, path + ".tracking." + field, "required when receipt_verified is true")
            _check_timestamp(tracking.get("verified_at"), path + ".tracking.verified_at", errors)
        # pickup_code is intentionally not evidence that tracking is real.
        if parcel.get("pickup_code") is not None and not isinstance(parcel.get("pickup_code"), str):
            _add(errors, path + ".pickup_code", "must be a string or null")

    for item_id, packed_total in parcel_quantity_totals.items():
        purchased = _purchased_quantity(items_by_id.get(item_id, {}))
        if packed_total > purchased:
            _add(errors, "parcels.quantities.%s" % item_id,
                 "total packed quantity %d across parcels exceeds purchased non-cancelled quantity %d" %
                 (packed_total, purchased))

    for index, refund in enumerate(refunds):
        if not isinstance(refund, dict):
            continue
        path = "refunds[%d]" % index
        refund_item_id = refund.get("item_id")
        if not _string(refund_item_id) or refund_item_id not in item_ids:
            _add(errors, path + ".item_id", "must reference an item")
        _check_currency(refund.get("currency"), path + ".currency", errors)
        if not _money(refund.get("amount_cents")):
            _add(errors, path + ".amount_cents", "must be a non-negative integer")
        refund_status = refund.get("status")
        if not isinstance(refund_status, str) or refund_status not in REFUND_STATES:
            _add(errors, path + ".status", "invalid refund status")
        refund_purpose = refund.get("purpose")
        if not isinstance(refund_purpose, str) or refund_purpose not in REFUND_PURPOSES:
            _add(errors, path + ".purpose", "must be savings or recovery")
        destination = refund.get("destination")
        if not isinstance(destination, str) or destination not in {"cash", "stored_value"}:
            _add(errors, path + ".destination", "must be cash or stored_value")
        if refund_status == "received":
            _check_timestamp(refund.get("received_verified_at"), path + ".received_verified_at", errors)

    summary = summarize(data) if not errors else {}
    if selected_quote_count == 0 and quotes:
        warnings.append("quotes: no scenario is selected; expected savings total is intentionally empty")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute conservative, currency-separated report totals."""
    retained_cost: Dict[str, int] = defaultdict(int)
    item_by_id = {i["id"]: i for i in data.get("items", []) if isinstance(i, dict) and _string(i.get("id"))}
    for item in item_by_id.values():
        active = _active_quantity(item)
        paid = item.get("unit_paid", {})
        if active and _money(paid.get("amount_cents")) and _string(paid.get("currency")):
            retained_cost[paid["currency"]] += active * paid["amount_cents"]

    # This is deliberately not called savings: a replacement also depends on the
    # original refund, return fees, retained-set closure, and all related costs.
    new_purchase_difference: Dict[str, int] = defaultdict(int)
    expected_details: List[Dict[str, Any]] = []
    for quote in data.get("quotes", []):
        if not isinstance(quote, dict) or quote.get("selected") is not True:
            continue
        if quote.get("stage") not in {"checkout_verified", "ordered", "paid"}:
            continue
        if quote.get("fees_cents") is None or not _money(quote.get("total_cents")):
            expected_details.append({"quote_id": quote.get("id"), "status": "unknown_fees"})
            continue
        currency = quote.get("currency")
        baseline = 0
        comparable = True
        for item_id in quote.get("item_ids", []):
            item = item_by_id.get(item_id, {})
            paid = item.get("unit_paid", {})
            if paid.get("currency") != currency or quote.get("matches", {}).get(item_id) != "same":
                comparable = False
                break
            quoted_quantity = quote.get("quantities", {}).get(item_id, 1)
            baseline += quoted_quantity * paid.get("amount_cents", 0)
        if not comparable:
            expected_details.append({"quote_id": quote.get("id"), "status": "not_comparable"})
            continue
        saving = baseline - quote["total_cents"]
        new_purchase_difference[currency] += saving
        expected_details.append({"quote_id": quote.get("id"), "status": "calculated", "currency": currency,
                                 "amount_cents": saving})

    received: Dict[str, Dict[str, Dict[str, int]]] = {
        "savings_related": {"cash": defaultdict(int), "stored_value": defaultdict(int)},
        "recovery": {"cash": defaultdict(int), "stored_value": defaultdict(int)},
    }
    pending: Dict[str, int] = defaultdict(int)
    for refund in data.get("refunds", []):
        if not isinstance(refund, dict) or not _money(refund.get("amount_cents")):
            continue
        currency = refund.get("currency")
        if refund.get("status") == "received" and _string(refund.get("received_verified_at")):
            purpose = "savings_related" if refund.get("purpose") == "savings" else "recovery"
            received[purpose][refund.get("destination")][currency] += refund["amount_cents"]
        else:
            pending[currency] += refund["amount_cents"]

    return {
        "retained_quantity": sum(_active_quantity(i) for i in item_by_id.values()),
        "retained_cost": dict(sorted(retained_cost.items())),
        "implemented_savings": {},
        "expected_savings": {},
        "savings_status": "not_calculated_without_closed_loop_evidence",
        "new_purchase_price_difference_not_net_savings": dict(sorted(new_purchase_difference.items())),
        "expected_savings_details": expected_details,
        "received_refunds": {
            purpose: {destination: dict(sorted(amounts.items())) for destination, amounts in destinations.items()}
            for purpose, destinations in received.items()
        },
        "pending_refunds_not_counted": dict(sorted(pending.items())),
    }


def _image_error(value: Any, input_path: Optional[pathlib.Path]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty relative path"
    if "://" in value or pathlib.PurePath(value).is_absolute():
        return "must be a relative local path, not a URL or absolute path"
    if ".." in pathlib.PurePath(value).parts:
        return "path traversal is not allowed"
    if input_path is None:
        return None
    base = input_path.resolve().parent
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return "resolved path escapes the input directory"
    if not candidate.is_file():
        return "file does not exist"
    mime = _detect_image_mime(candidate)
    if mime not in IMAGE_MIME:
        return "unsupported image content; only PNG, JPEG, and WebP are allowed"
    if candidate.stat().st_size > MAX_IMAGE_BYTES:
        return "image exceeds 5 MiB"
    return None


def _image_data(item: Dict[str, Any], input_path: pathlib.Path) -> Optional[str]:
    relative = item.get("image")
    if not relative or _image_error(relative, input_path):
        return None
    base = input_path.resolve().parent
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError("image path escaped the input directory during report generation")
    with candidate.open("rb") as handle:
        content = handle.read(MAX_IMAGE_BYTES + 1)
    # Re-check after reading. This narrows path-replacement races but is not a
    # claim of isolation from a concurrently malicious filesystem writer.
    after_read = (base / relative).resolve()
    if after_read != candidate:
        raise ValueError("image path changed during report generation")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds 5 MiB during report generation")
    mime = _detect_image_mime_bytes(content)
    if mime not in IMAGE_MIME:
        raise ValueError("image content changed or is not PNG, JPEG, or WebP")
    return "data:%s;base64,%s" % (mime, base64.b64encode(content).decode("ascii"))


def _fmt(amount: int, currency: str) -> str:
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)
    return "%s%s %d.%02d" % (sign, currency, absolute // 100, absolute % 100)


def _money_lines(values: Dict[str, int]) -> str:
    return " / ".join(_fmt(amount, currency) for currency, amount in sorted(values.items())) or "暂无可确认金额"


def _received_lines(summary: Dict[str, Any], purpose: str) -> str:
    received = summary["received_refunds"][purpose]
    return "现金 %s；储值 %s" % (_money_lines(received["cash"]), _money_lines(received["stored_value"]))


def _md_escape(value: str) -> str:
    for character in "\\`*_{}[]()#+-.!<>|":
        value = value.replace(character, "\\" + character)
    return value


def _public_parcel_status(value: Any) -> str:
    return PUBLIC_PARCEL_STATES.get(value, "待核") if isinstance(value, str) else "待核"


def _display_item_name(item: Dict[str, Any], index: int, public: bool) -> str:
    if public:
        return item.get("public_name") or "商品-%02d" % index
    return "%s [%s]" % (item.get("title", ""), item.get("id", ""))


def _display_spec(item: Dict[str, Any], public: bool) -> str:
    """Render only explicitly approved specs in a public report."""
    spec = item.get("public_spec", {}) if public else item.get("sku", {}).get("spec", {})
    if not spec:
        return "未公开" if public else "未记录"
    return json.dumps(spec, ensure_ascii=False, sort_keys=True)


def _item_indexes(data: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    items = {item["id"]: item for item in data.get("items", [])
             if isinstance(item, dict) and _string(item.get("id"))}
    indexes = {item["id"]: index for index, item in enumerate(data.get("items", []), 1)
               if isinstance(item, dict) and _string(item.get("id"))}
    return items, indexes


def _aftersales_refs(data: Dict[str, Any]) -> Dict[str, List[str]]:
    refs: Dict[str, List[str]] = defaultdict(list)
    for refund in data.get("refunds", []):
        if isinstance(refund, dict) and _string(refund.get("item_id")) and _string(refund.get("id")):
            refs[refund["item_id"]].append(refund["id"])
    return refs


def render_html(data: Dict[str, Any], input_path: pathlib.Path, public: bool = False) -> str:
    result = validate_ledger(data, input_path)
    if not result["valid"]:
        raise ValueError("invalid ledger: " + "; ".join(result["errors"]))
    summary = result["summary"]
    title = "购物账本分享报告" if public else "购物账本私密报告"
    item_cards: List[str] = []
    public_item_labels: Dict[str, str] = {}
    for index, item in enumerate(data["items"], 1):
        active = _active_quantity(item)
        paid = item["unit_paid"]
        public_item_labels[item["id"]] = item.get("public_name") or "商品-%02d" % index
        display_name = public_item_labels[item["id"]] if public else item["title"]
        image = _image_data(item, input_path) if (not public or item.get("public_image") is True) else None
        image_html = ("<button type='button' class='thumb' aria-label='放大商品图'><img src='{0}' alt='商品图'></button>".format(image)
                      if image else "<div class='no-image'>暂无图片</div>")
        details = ""
        if not public:
            details += "<p>规格：%s</p>" % html.escape(json.dumps(item["sku"]["spec"], ensure_ascii=False, sort_keys=True))
            details += "<p>SKU：%s</p>" % html.escape(item["sku"]["product_code"])
            if item.get("order_ref"):
                details += "<p>订单引用：%s</p>" % html.escape(str(item["order_ref"]))
            if item.get("notes"):
                details += "<p>备注：%s</p>" % html.escape(str(item["notes"]))
        elif item.get("public_spec"):
            details += "<p>公开规格：%s</p>" % html.escape(_display_spec(item, True))
        item_cards.append("<article class='card'>%s<div><h3>%s</h3><p>保留数量：%d（原 %d / 取消 %d / 已退 %d）</p><p>实付单价：%s</p>%s</div></article>" %
                          (image_html, html.escape(display_name), active, item["quantity"], item["cancelled_quantity"],
                           item["refunded_quantity"], html.escape(_fmt(paid["amount_cents"], paid["currency"])), details))
    if not item_cards:
        item_cards.append("<p class='empty'>本批没有商品记录。</p>")

    items_by_id, item_indexes = _item_indexes(data)
    aftersales = _aftersales_refs(data)
    quote_rows: List[str] = []
    for qindex, quote in enumerate(data["quotes"], 1):
        total = "费用未知，不计确定节省" if quote.get("total_cents") is None else _fmt(quote["total_cents"], quote["currency"])
        mapped_items: List[str] = []
        for item_id in quote.get("item_ids", []):
            item = items_by_id[item_id]
            name = _display_item_name(item, item_indexes[item_id], public)
            mapped_items.append("<li>%s；规格：%s；数量：%d</li>" % (
                html.escape(name), html.escape(_display_spec(item, public)), _mapped_quantity(quote, item_id)))
        observed = quote.get("observed_at") or "未记录"
        evidence = "<br><small>来源：%s</small>" % html.escape(quote.get("source", "未记录")) if not public else ""
        quote_rows.append("<tr><td>%s</td><td><ul>%s</ul></td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td></tr>" %
                          ("报价-%02d" % qindex, "".join(mapped_items), QUOTE_STAGE_LABELS[quote["stage"]],
                           "已选" if quote.get("selected") else "候选", html.escape(total),
                           html.escape(observed), evidence))
    if not quote_rows:
        quote_rows.append("<tr><td colspan='6'>暂无报价候选。</td></tr>")

    parcel_rows: List[str] = []
    for pindex, parcel in enumerate(data["parcels"], 1):
        alias = "包裹-%02d" % pindex
        mapped_items = []
        for item_id in parcel.get("item_ids", []):
            item = items_by_id[item_id]
            name = _display_item_name(item, item_indexes[item_id], public)
            detail = "%s；规格：%s；本包数量：%d" % (
                name, _display_spec(item, public), _mapped_quantity(parcel, item_id))
            if not public:
                order_ref = str(item.get("order_ref") or "未记录")
                refund_refs = ", ".join(aftersales.get(item_id, [])) or "未记录"
                detail += "；订单引用：%s；售后/退款引用：%s" % (order_ref, refund_refs)
            mapped_items.append("<li>%s</li>" % html.escape(detail))
        contents = "<ul>%s</ul>" % "".join(mapped_items)
        if public:
            parcel_rows.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" %
                               (alias, _public_parcel_status(parcel.get("status")), contents))
        else:
            tracking = parcel["tracking"]
            tracking_text = ("%s / %s（回执已核）" % (tracking.get("carrier"), tracking.get("tracking_number"))
                             if tracking.get("receipt_verified") else "真实运单未核验")
            private_fields = {
                "pickup_code": parcel.get("pickup_code"), "pickup_window": parcel.get("pickup_window"),
                "return_recipient": parcel.get("return_recipient"), "return_address": parcel.get("return_address"),
                "tracking": tracking_text,
            }
            parcel_rows.append("<tr><td>%s (%s)</td><td>%s</td><td>%s</td><td><pre>%s</pre></td></tr>" %
                               (alias, html.escape(str(parcel.get("id"))),
                                PUBLIC_PARCEL_STATES.get(parcel.get("status"), html.escape(str(parcel.get("status", "未标记")))),
                                contents, html.escape(json.dumps(private_fields, ensure_ascii=False, indent=2))))
    if not parcel_rows:
        parcel_rows.append("<tr><td colspan='%d'>暂无待寄包裹。</td></tr>" % (3 if public else 4))

    action_rows: List[str] = []
    for aindex, action in enumerate(data["actions"], 1):
        # Public report includes progress only; private report includes stable local identifiers.
        if public:
            action_rows.append("<li>操作-%02d：%s</li>" % (aindex, ACTION_STATE_LABELS[action["status"]]))
        else:
            ident = " / %s / %s" % (action.get("target_id"), action.get("idempotency_key"))
            action_rows.append("<li>%s：%s%s</li>" % (html.escape(str(action.get("type"))),
                                                         ACTION_STATE_LABELS[action["status"]], html.escape(ident)))
    if not action_rows:
        action_rows.append("<li>暂无操作记录。</li>")

    generated = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    private_scope = "" if public else "<p>账本：%s；截止：%s</p>" % (
        html.escape(scope_value(data, "ledger_id")), html.escape(scope_value(data, "as_of")))
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:920px;margin:auto;padding:20px;color:#202124}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.metric,.card{{border:1px solid #ddd;border-radius:12px;padding:14px}}.items{{display:grid;gap:12px}}.card{{display:flex;gap:14px}}.thumb{{border:0;padding:0;background:transparent;cursor:zoom-in}}.thumb img,.no-image{{width:112px;height:112px;object-fit:cover;border-radius:9px;background:#f1f3f4}}.no-image{{display:grid;place-items:center;color:#666}}dialog{{max-width:92vw;max-height:92vh;border:0;border-radius:12px;padding:10px}}dialog img{{display:block;max-width:88vw;max-height:80vh}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:9px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;word-break:break-word}}.muted{{color:#666;font-size:.9rem}}@media(max-width:600px){{.card{{display:block}}.thumb img,.no-image{{width:100%;height:180px}}}}</style></head><body>
<h1>{title}</h1>{private_scope}<p class='muted'>由离线账本生成，不代表已完成任何平台操作。生成时间 {generated}</p>
<section class='summary'><div class='metric'><b>已实现节省</b><p>尚不能确定</p></div><div class='metric'><b>预计节省</b><p>尚不能确定</p></div><div class='metric'><b>新购应付差额（不是净节省）</b><p>{purchase_difference}</p></div><div class='metric'><b>换购相关已到账退款</b><p>{related_refunds}</p></div><div class='metric'><b>已回收资金</b><p>{recovery}</p></div><div class='metric'><b>待到账（未计入）</b><p>{pending}</p></div></section>
<h2>商品</h2><section class='items'>{items}</section>
<h2>报价与进度</h2><table><thead><tr><th>报价</th><th>商品、规格与数量</th><th>阶段</th><th>选择</th><th>整组总成本</th><th>核验时点</th></tr></thead><tbody>{quotes}</tbody></table>
<h2>操作进度</h2><ul>{actions}</ul>
<h2>包裹</h2><table><thead><tr><th>包裹</th><th>状态</th><th>本包商品、规格与数量</th>{parcel_private_header}</tr></thead><tbody>{parcels}</tbody></table>
<dialog id='image-dialog'><button type='button' id='image-dialog-close' aria-label='关闭'>关闭</button><img alt='放大商品图'></dialog>
<script>document.addEventListener('click',function(event){{var image=event.target.closest('.thumb img');if(!image)return;var dialog=document.getElementById('image-dialog');dialog.querySelector('img').src=image.src;dialog.showModal();}});document.getElementById('image-dialog-close').addEventListener('click',function(){{document.getElementById('image-dialog').close();}});</script>
</body></html>""".format(title=html.escape(title), private_scope=private_scope, generated=html.escape(generated),
                         purchase_difference=html.escape(_money_lines(summary["new_purchase_price_difference_not_net_savings"])),
                         related_refunds=html.escape(_received_lines(summary, "savings_related")),
                         recovery=html.escape(_received_lines(summary, "recovery")),
                         pending=html.escape(_money_lines(summary["pending_refunds_not_counted"])),
                         items="".join(item_cards), quotes="".join(quote_rows), actions="".join(action_rows),
                         parcels="".join(parcel_rows), parcel_private_header="" if public else "<th>取件与运单</th>")


def scope_value(data: Dict[str, Any], key: str) -> str:
    return str(data.get("scope", {}).get(key, ""))


def render_mobile(data: Dict[str, Any], input_path: pathlib.Path, public: bool = False) -> str:
    result = validate_ledger(data, input_path)
    if not result["valid"]:
        raise ValueError("invalid ledger: " + "; ".join(result["errors"]))
    s = result["summary"]
    items_by_id, item_indexes = _item_indexes(data)
    aftersales = _aftersales_refs(data)
    lines = ["# 购物账本%s摘要" % ("分享" if public else "私密"), "",
             "- 已实现节省：尚不能确定",
             "- 预计节省：尚不能确定",
             "- 新购应付差额（未扣退货等费用，不是净节省）：" + _money_lines(s["new_purchase_price_difference_not_net_savings"]),
             "- 换购相关已到账退款：" + _received_lines(s, "savings_related"),
             "- 已回收资金：" + _received_lines(s, "recovery"),
             "- 待到账（未计入）：" + _money_lines(s["pending_refunds_not_counted"]), "", "## 商品"]
    if data["items"]:
        for index, item in enumerate(data["items"], 1):
            display_name = (item.get("public_name") or "商品-%02d" % index) if public else item["title"]
            display_name = _md_escape(display_name)
            image_allowed = bool(item.get("image")) and (not public or item.get("public_image") is True)
            image_text = "已附本地图片" if image_allowed else "暂无图片"
            lines.append("- %s：保留 %d，%s %s，%s" %
                         (display_name, _active_quantity(item), item["unit_paid"]["currency"],
                          _fmt(item["unit_paid"]["amount_cents"], item["unit_paid"]["currency"]).split(" ", 1)[1], image_text))
            if public and item.get("public_spec"):
                lines.append("  - 公开规格：%s" % _md_escape(_display_spec(item, True)))
    else:
        lines.append("本批没有商品记录。")

    lines.extend(["", "## 报价与进度"])
    if data["quotes"]:
        for qindex, quote in enumerate(data["quotes"], 1):
            total = "费用未知，不计确定节省" if quote.get("total_cents") is None else _fmt(
                quote["total_cents"], quote["currency"])
            observed = quote.get("observed_at") or "未记录"
            lines.append("- 报价-%02d：%s；%s；整组总成本 %s；核验时点 %s" % (
                qindex, QUOTE_STAGE_LABELS[quote["stage"]], "已选" if quote.get("selected") else "候选",
                total, _md_escape(observed)))
            for item_id in quote.get("item_ids", []):
                item = items_by_id[item_id]
                name = _display_item_name(item, item_indexes[item_id], public)
                lines.append("  - %s；规格：%s；数量：%d" % (
                    _md_escape(name), _md_escape(_display_spec(item, public)), _mapped_quantity(quote, item_id)))
            if not public:
                lines.append("  - 来源：%s" % _md_escape(quote.get("source", "未记录")))
    else:
        lines.append("暂无报价候选。")
    lines.extend(["", "## 进度"])
    if data["actions"]:
        if public:
            lines.extend("- 操作-%02d：%s" % (index, ACTION_STATE_LABELS[a["status"]])
                         for index, a in enumerate(data["actions"], 1))
        else:
            lines.extend("- %s：%s" % (_md_escape(str(a.get("type"))), ACTION_STATE_LABELS[a["status"]])
                         for a in data["actions"])
    else:
        lines.append("暂无操作记录。")
    lines.extend(["", "## 包裹"])
    if data["parcels"]:
        for index, parcel in enumerate(data["parcels"], 1):
            display_status = (_public_parcel_status(parcel.get("status")) if public else
                              PUBLIC_PARCEL_STATES.get(parcel.get("status"), parcel.get("status", "未标记")))
            text = "- 包裹-%02d：%s" % (index, _md_escape(str(display_status)))
            if not public:
                tracking = parcel["tracking"]
                tracking_text = (("%s/%s（回执已核）" % (tracking.get("carrier"), tracking.get("tracking_number")))
                                 if tracking.get("receipt_verified") else "真实运单未核验")
                text += "；取件码 %s；%s" % (
                    _md_escape(str(parcel.get("pickup_code") or "暂无")), _md_escape(tracking_text))
            lines.append(text)
            for item_id in parcel.get("item_ids", []):
                item = items_by_id[item_id]
                name = _display_item_name(item, item_indexes[item_id], public)
                detail = "  - %s；规格：%s；本包数量：%d" % (
                    _md_escape(name), _md_escape(_display_spec(item, public)), _mapped_quantity(parcel, item_id))
                if not public:
                    order_ref = str(item.get("order_ref") or "未记录")
                    refund_refs = ", ".join(aftersales.get(item_id, [])) or "未记录"
                    detail += "；订单引用：%s；售后/退款引用：%s" % (
                        _md_escape(order_ref), _md_escape(refund_refs))
                lines.append(detail)
    else:
        lines.append("暂无待寄包裹。")
    lines.extend(["", "> 离线账本报告；不代表已完成平台操作。"])
    return "\n".join(lines) + "\n"


def _load(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _same_file_or_resolved_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
    except OSError:
        pass
    return left.resolve() == right.resolve()


def _report_path_errors(data: Dict[str, Any], input_path: pathlib.Path,
                        html_path: pathlib.Path, mobile_path: pathlib.Path) -> List[str]:
    """Reject output aliases before report rendering creates or writes anything."""
    errors: List[str] = []
    protected: List[Tuple[str, pathlib.Path]] = [("input", input_path)]
    base = input_path.resolve().parent
    for index, item in enumerate(data.get("items", [])):
        if isinstance(item, dict) and _string(item.get("image")):
            protected.append(("items[%d].image" % index, base / item["image"]))
    if _same_file_or_resolved_path(html_path, mobile_path):
        errors.append("report outputs: --html and --mobile resolve to the same file")
    for output_name, output_path in (("--html", html_path), ("--mobile", mobile_path)):
        for protected_name, protected_path in protected:
            if _same_file_or_resolved_path(output_path, protected_path):
                errors.append("%s: output conflicts with %s" % (output_name, protected_name))
    return errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and report an offline shopping ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("input", type=pathlib.Path)
    report_cmd = sub.add_parser("report")
    report_cmd.add_argument("input", type=pathlib.Path)
    report_cmd.add_argument("--html", required=True, type=pathlib.Path)
    report_cmd.add_argument("--mobile", required=True, type=pathlib.Path)
    report_cmd.add_argument("--public", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = _load(args.input)
        result = validate_ledger(data, args.input)
        if args.command == "validate":
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["valid"] else 1
        if not result["valid"]:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        path_errors = _report_path_errors(data, args.input, args.html, args.mobile)
        if path_errors:
            result["valid"] = False
            result["errors"].extend(path_errors)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        html_text = render_html(data, args.input, public=args.public)
        mobile_text = render_mobile(data, args.input, public=args.public)
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.mobile.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(html_text, encoding="utf-8")
        args.mobile.write_text(mobile_text, encoding="utf-8")
        print(json.dumps({"valid": True, "html": str(args.html), "mobile": str(args.mobile),
                          "public": args.public, "summary": result["summary"]}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("ledger: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
