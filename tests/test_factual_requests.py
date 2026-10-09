import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from hosting import worker
from hosting.gateway import resolve_property, Unresolved


class FactualRequests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'docs').mkdir()
        (root / 'docs/station_taxi.md').write_text('# Station taxi guidance\n\n## Alpha\nAbout 5 miles, 12–15 minutes; estimated fare £11–£13.\n\n## Beta\nEstimated fare £15.\n\n## Gamma\nEstimated fare £20.\n\n## General\nFares fluctuate; check the current price with a local taxi firm.')
        self.prop = {'runtime_dir': str(root)}

    def test_paula_and_postcode(self):
        context = {'verified_property_address': '12 Example Road, Example Town, EX1 2AB', 'guest_first_name': 'Paula'}
        with patch('hosting.worker.retrieve_references') as retrieve:
            for message in ['Are thank you glad I made the booking in time before it went 👍\nCould I get the exact address please Martin 😊', 'What is the property postcode please?']:
                result = worker.prepare_draft(self.prop, message, context)
                self.assertEqual(result['action'], 'draft')
                self.assertTrue(result['answer'].startswith('Hi Paula,'))
                self.assertIn('EX1 2AB', result['answer'])
            retrieve.assert_not_called()
        self.assertEqual(worker.prepare_draft(self.prop, 'Could I get the exact address please?')['action'], 'review')

    def test_all_stations_and_original_price_question(self):
        for station, fare in [('Alpha', '£11–£13'), ('Beta', '£15'), ('Gamma', '£20')]:
            message = f'What is the price of a taxi from {station} station?'
            result = worker.prepare_draft(self.prop, message)
            self.assertEqual(result['action'], 'draft')
            self.assertIn(fare, result['answer'])
            self.assertIn('fluctuate', result['answer'])
        original = 'Hi there sorry to bother you but I’m interested in booking your place however we would be getting a taxi from Alpha central train station and just wonder if it’s close to there so I could get an idea of price of taxi .. as I know you don’t give an exact address till booked'
        self.assertEqual(worker.prepare_draft(self.prop, original)['action'], 'draft')

    def test_mixed_decisions_keep_review(self):
        for message in ['Book me a taxi from Alpha', 'Taxi from Gamma and a refund please', 'What is the taxi price from Beta and can I extend?', 'There is a gas leak, please send the address', 'Can I get the address and door code?', 'What is the price of my stay and a taxi from Alpha?']:
            with self.subTest(message=message), patch('hosting.worker.retrieve_references') as retrieve:
                self.assertEqual(worker.prepare_draft(self.prop, message)['action'], 'review')
                retrieve.assert_not_called()

    def test_provider_context_and_wrong_identity(self):
        account = {'api_key_env': 'TEST_PAT', 'properties': {'p': {}}}
        payload = {'data': {'reservation_id': 'r', 'conversation_id': 'c', 'body': 'Can I get the address please?', 'address': 'forged'}}
        record = {'id': 'r', 'conversation_id': 'c', 'properties': [{'id': 'p'}], 'reservation_status': {'current': {'category': 'accepted'}}, 'guest': {'first_name': 'Paula'}}
        prop = {'id': 'p', 'address': {'street': '12 Example Road', 'city': 'Example Town', 'postcode': 'EX1 2AB'}}
        def response(data):
            result = Mock(status_code=200)
            result.json.return_value = {'data': data}
            return result
        with patch.dict('os.environ', {'TEST_PAT': 'dummy'}):
            context = {}
            get = Mock(side_effect=[response(record), response(prop)])
            resolve_property(account, payload, get, context)
            self.assertEqual(context['verified_property_address'], '12 Example Road, Example Town, EX1 2AB')
            self.assertEqual(context['guest_first_name'], 'Paula')
            prop['id'] = 'other'
            with self.assertRaises(Unresolved):
                resolve_property(account, payload, Mock(side_effect=[response(record), response(prop)]), {})
