"""The model sees the messages before a guest reply, so 'Pink bin' after a bin reminder is an acknowledgement."""
import json
import unittest
from unittest.mock import Mock, patch

import requests

from hosting import worker
from hosting.gateway import API_BASE

PROP = {"name": "Test property", "runtime_dir": "unused"}
REFERENCES = [{"text": "Bins are collected on Fridays.", "source": {"source": "guide.md"}}]
ACCOUNT = {"api_key_env": "TEST_HOSPITABLE_KEY"}
PAYLOAD = {"data": {"id": "m3", "reservation_id": "res-1", "created_at": "2026-10-22T19:09:36Z"}}


def row(identifier, sender_type, body, created):
    return {"id": identifier, "sender_type": sender_type, "body": body, "created_at": created}


def response(rows, status=200, **extra):
    reply = Mock(status_code=status)
    reply.json.return_value = {"data": rows, **extra}
    return reply


# Hospitable lists the newest message first.
THREAD = [
    row("m4", "host", "A later host message that must not be used.", "2026-10-22T19:10:00Z"),
    row("m3", "guest", "Pink bin", "2026-10-22T19:09:36Z"),
    row("m2", "guest", "This has been done", "2026-10-22T19:08:41Z"),
    row("m1", "host", "Hi Sam, please put the Pink lid bin out this evening. "
                      "Guide: https://example.com/guide/abc ref 123456", "2026-10-22T16:00:06Z"),
    row("m0", "host", "Your door code is 654321", "2026-10-22T14:45:04Z"),
]


class ConversationContextTests(unittest.TestCase):
    def conversation(self, reply=None, payload=PAYLOAD, **kwargs):
        get = kwargs.pop("get", Mock(return_value=reply))
        with patch("hosting.worker.secret", return_value="test-key"), \
             patch("hosting.worker.requests.get", get):
            return worker.recent_conversation(ACCOUNT, payload), get

    def test_prompt_carries_conversation_rule(self):
        text = " ".join(worker.SYSTEM.split())
        self.assertIn("When 'conversation' is supplied", text)
        self.assertIn("is an acknowledgement: return action 'ignored'", text)
        self.assertIn("The conversation never approves a booking change", text)
        self.assertIn("If 'conversation' is empty or absent, judge the message on its own", text)

    def test_conversation_is_earlier_ordered_and_stripped_of_secrets(self):
        conversation, get = self.conversation(response(THREAD))
        self.assertEqual(conversation, [
            {"role": "host", "text": "Hi Sam, please put the Pink lid bin out this evening. Guide: [link] ref [number]"},
            {"role": "guest", "text": "This has been done"},
        ])
        self.assertEqual(get.call_args.args[0], API_BASE + "/reservations/res-1/messages")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(get.call_args.kwargs["params"], {"per_page": 100})

    def test_only_recent_messages_are_kept_and_long_text_is_cut(self):
        rows = [row("m3", "guest", "Pink bin", "2026-10-22T19:09:36Z")]
        rows += [row(f"e{n}", "host", f"Earlier message {n} " + "x" * 600, f"2026-10-22T10:{n:02d}:00Z")
                 for n in range(10)]
        conversation, _ = self.conversation(response(rows))
        self.assertEqual(len(conversation), worker.CONVERSATION_LIMIT)
        self.assertEqual([item["text"][:17] for item in conversation][-1], "Earlier message 9")
        self.assertTrue(all(len(item["text"]) <= worker.CONVERSATION_TEXT_LIMIT for item in conversation))

    def test_any_problem_means_no_conversation(self):
        paged = response(THREAD, links={"next": "https://example.invalid/page2"})
        many = response(THREAD, meta={"last_page": 2})
        cases = {
            "server error": dict(reply=response([], status=500)),
            "not a list": dict(reply=response({"id": "x"})),
            "paged by link": dict(reply=paged),
            "paged by meta": dict(reply=many),
            "network error": dict(get=Mock(side_effect=requests.ConnectionError("down"))),
            "no timestamp anywhere": dict(reply=response([{"id": "m3", "body": "Pink bin"}]),
                                          payload={"data": {"id": "m3", "reservation_id": "res-1"}}),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                conversation, _ = self.conversation(**kwargs)
                self.assertEqual(conversation, [])

    def test_inquiries_have_no_history_and_make_no_request(self):
        conversation, get = self.conversation(payload={"data": {"id": "m3", "conversation_id": "inq-1",
                                                               "created_at": "2026-10-22T19:09:36Z"}})
        self.assertEqual(conversation, [])
        get.assert_not_called()

    def test_model_receives_conversation_and_ignored_outcome_is_kept(self):
        sent = {}
        history = [{"role": "host", "text": "Please put the Pink lid bin out this evening."},
                   {"role": "guest", "text": "This has been done"}]

        def fake_draft(prop, system, payload):
            sent["payload"] = json.loads(payload)
            return json.dumps({"action": "ignored", "answer": "", "reason": "Confirms the bin task"})

        with patch("hosting.worker.retrieve_references", return_value=REFERENCES), \
             patch("hosting.ai_service.draft_text", side_effect=fake_draft):
            result = worker.prepare_draft(PROP, "Pink bin", conversation_loader=lambda: history)
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(result["answer"], "")
        self.assertEqual(sent["payload"]["conversation"], history)
        self.assertEqual(sent["payload"]["guest_message"], "Pink bin")

    def test_missing_or_failing_loader_sends_an_empty_conversation(self):
        def broken():
            raise RuntimeError("history unavailable")

        draft = json.dumps({"action": "draft", "answer": "Bins go out on Thursday evening.", "reason": "Guide"})
        for loader in (None, broken, lambda: None):
            with self.subTest(loader=getattr(loader, "__name__", loader)):
                with patch("hosting.worker.retrieve_references", return_value=REFERENCES), \
                     patch("hosting.ai_service.draft_text", return_value=draft) as model:
                    result = worker.prepare_draft(PROP, "When are the bins collected?", conversation_loader=loader)
                self.assertEqual(json.loads(model.call_args.args[2])["conversation"], [])
                self.assertEqual(result["action"], "draft")

    def test_gates_and_missing_index_never_fetch_the_conversation(self):
        loader = Mock(return_value=[])
        for message in ("Thanks!", "The carbon monoxide alarm is sounding"):
            with self.subTest(message), patch("hosting.ai_service.draft_text") as model:
                worker.prepare_draft(PROP, message, conversation_loader=loader)
                model.assert_not_called()
        with self.subTest("index not ready"), patch("hosting.worker.retrieve_references", return_value=[]):
            self.assertEqual(worker.prepare_draft(PROP, "Pink bin", conversation_loader=loader)["action"], "review")
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
