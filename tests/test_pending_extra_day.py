"""Pending request-to-book: the model sees status/dates and the extra-day rule."""
import json
import unittest
from unittest.mock import Mock, patch

import test_hosting
from hosting import worker
from hosting.gateway import resolve_property

MESSAGE = (
    "Hi, this place looks perfect for our work trip. We are staying nearby at the "
    "moment but need an extra day to finish our work. Thanks!"
)
PENDING = {
    "check_in": "2026-10-23T16:00:00+01:00",
    "arrival_date": "2026-10-23T00:00:00+01:00",
    "departure_date": "2026-10-24T00:00:00+01:00",
    "status": "pending",
}


class PendingExtraDayTests(unittest.TestCase):
    def test_prompt_carries_pending_extra_day_rule(self):
        text = " ".join(worker.SYSTEM.split())
        self.assertIn("reservation_context.status is 'pending'", text)
        self.assertIn("trip context, not a booking extension", text)
        self.assertIn("accepted or current stays", text)

    def test_gateway_supplies_status_and_departure_from_verified_api_only(self):
        base = test_hosting.HostingTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.payload["data"]["status"] = "accepted"  # an unverified payload claim
        base.payload["data"]["departure_date"] = "2099-01-01T00:00:00+00:00"
        response = base.response()
        response.json.return_value["data"].update(
            {"status": "pending", "arrival_date": PENDING["arrival_date"],
             "departure_date": PENDING["departure_date"]})
        context = {}
        resolve_property(base.accounts["a"], base.payload, Mock(return_value=response), context=context)
        self.assertEqual(context["status"], "pending")
        self.assertEqual(context["departure_date"], PENDING["departure_date"])
        self.assertEqual(context["arrival_date"], PENDING["arrival_date"])

    def test_model_receives_pending_context_and_ignored_outcome_is_kept(self):
        sent = {}

        def fake_draft(prop, system, payload):
            sent["system"] = system
            sent["payload"] = json.loads(payload)
            return json.dumps({"action": "ignored", "answer": "",
                               "reason": "Trip context on a pending request"})

        references = [{"text": "Check-in is from 4pm.", "source": {"source": "guide.md"}}]
        with patch("hosting.worker.retrieve_references", return_value=references), \
             patch("hosting.ai_service.draft_text", side_effect=fake_draft):
            result = worker.prepare_draft({"name": "Test property", "runtime_dir": "unused"},
                                          MESSAGE, reservation_context=PENDING)
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(result["answer"], "")
        context = sent["payload"]["reservation_context"]
        self.assertEqual(context["status"], "pending")
        self.assertEqual(context["departure_date"], PENDING["departure_date"])
        self.assertIn("'pending'", sent["system"])


    def test_gate_masks_bare_extra_night_only_on_pending_requests(self):
        refs = [{"text": "Check-in is from 4pm.", "source": {"source": "guide.md"}}]
        prop = {"name": "Test property", "runtime_dir": "unused"}
        ignored = json.dumps({"action": "ignored", "answer": "", "reason": "Trip context"})
        trip = "We need an extra night to complete our works, thanks."
        cases = (
            (trip, dict(PENDING), True),
            ("We need an extra day to complete our works, thanks.", dict(PENDING), True),
            ("Could we have an extra night?", dict(PENDING), False),
            ("We would like to extend by an extra night.", dict(PENDING), False),
            (trip, {"status": "accepted"}, False),
            (trip, {}, False),
        )
        for message, context, reaches_model in cases:
            with self.subTest(message=message, status=context.get("status")):
                with patch("hosting.worker.retrieve_references", return_value=refs), \
                     patch("hosting.ai_service.draft_text", return_value=ignored) as model:
                    result = worker.prepare_draft(prop, message, reservation_context=context)
                self.assertEqual(model.called, reaches_model)
                self.assertEqual(result["action"], "ignored" if reaches_model else "review")


if __name__ == "__main__":
    unittest.main()
