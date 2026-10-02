import unittest
from hosting.email_setup import recipients
class RecipientTests(unittest.TestCase):
    def test_multiple_and_duplicates(self):
        self.assertEqual(recipients('a@example.com, b@example.com, a@example.com'),['a@example.com','b@example.com'])
    def test_invalid_recipients(self):
        for value in ['a@example.com,','a@example.com\nBcc:x@example.com','not-email']:
            with self.assertRaises(ValueError): recipients(value)
