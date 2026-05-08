import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.api_resident_dashboard import (
    _history_item_matches_filters,
    _normalize_status_for_history,
    _online_method_from_payloads,
)


class DummyTx:
    def __init__(self, request_payload=None, callback_payload=None):
        self.request_payload = request_payload
        self.callback_payload = callback_payload


class PaymentHistoryUnitTests(unittest.TestCase):
    def test_normalize_status_for_history(self):
        self.assertEqual(_normalize_status_for_history("CONFIRMED"), "confirmed")
        self.assertEqual(_normalize_status_for_history("DECLINED"), "declined")
        self.assertEqual(_normalize_status_for_history("SIGNATURE_FAILED"), "signature_failed")
        self.assertEqual(_normalize_status_for_history("INITIATED"), "initiated")
        self.assertEqual(_normalize_status_for_history("SOMETHING_ELSE"), "error")

    def test_online_method_from_payloads_google_pay(self):
        tx = DummyTx(request_payload='{"GPAYTOKEN":"abc"}')
        self.assertEqual(_online_method_from_payloads(tx), "google_pay")

    def test_online_method_from_payloads_apple_pay(self):
        tx = DummyTx(request_payload='{"GPAYTOKEN":"abc","EXT_MPI_ECI":"05"}')
        self.assertEqual(_online_method_from_payloads(tx), "apple_pay")

    def test_online_method_from_payloads_bank_card_default(self):
        tx = DummyTx(request_payload='{"ORDER":"123"}')
        self.assertEqual(_online_method_from_payloads(tx), "bank_card")

    def test_history_item_matches_filters_positive(self):
        item = {
            "status": "confirmed",
            "operation_type": "invoice_payment",
            "payment_method": "bank_card",
            "category": "utility",
            "resident_id": 7,
            "order_id": "1720",
            "reference": "REF-1",
            "invoice_number": "INV-1",
            "resident_code": "A / 1",
        }
        self.assertTrue(
            _history_item_matches_filters(
                item,
                statuses={"confirmed"},
                operation_types={"invoice_payment"},
                payment_methods={"bank_card"},
                categories={"utility"},
                resident_id=7,
                q="INV-1",
            )
        )

    def test_history_item_matches_filters_negative_status(self):
        item = {
            "status": "declined",
            "operation_type": "invoice_payment",
            "payment_method": "bank_card",
            "category": "utility",
            "resident_id": 7,
        }
        self.assertFalse(
            _history_item_matches_filters(
                item,
                statuses={"confirmed"},
                operation_types=set(),
                payment_methods=set(),
                categories=set(),
                resident_id=None,
                q=None,
            )
        )

if __name__ == "__main__":
    unittest.main()
