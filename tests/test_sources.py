import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sources"
HEADERS = {'service_intervals': ['part', 'vendor', 'lot', 'site', 'observed_from', 'observed_to', 'usage_hours', 'ending'], 'checks': ['part', 'inspection_day', 'result', 'known_day'], 'lot_corrections': ['part', 'lot', 'known_day', 'evidence'], 'warranty_terms': ['term', 'vendor', 'lot', 'supported_site', 'return_days', 'unit_cost', 'advance_limit'], 'interchange': ['spare', 'site', 'approval', 'known_day'], 'replacement_events': ['event', 'spare', 'site', 'returned_part', 'term', 'action', 'day']}
REFERENCES = [['checks', 'part', 'service_intervals', 'part'], ['lot_corrections', 'part', 'service_intervals', 'part'], ['replacement_events', 'returned_part', 'service_intervals', 'part'], ['replacement_events', 'term', 'warranty_terms', 'term']]
COUNTS = [['service_intervals', 'ending', 'transfer', 1], ['checks', 'result', 'failed', 4], ['replacement_events', 'action', 'return_confirmed', 1]]


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for name, header in HEADERS.items():
            with (ROOT / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, header)
                self.records[name] = list(reader)

    def test_record_shape(self):
        for name, records in self.records.items():
            with self.subTest(source=name):
                self.assertTrue(records)
                for row in records:
                    self.assertEqual(set(row), set(HEADERS[name]))
                    self.assertNotIn(None, row.values())
                    self.assertTrue(row[HEADERS[name][0]])
                self.assertEqual(len(records), len({tuple(row.values()) for row in records}))

    def test_cross_source_references(self):
        for child, fk, parent, pk in REFERENCES:
            with self.subTest(source=child, field=fk):
                keys = {row[pk] for row in self.records[parent]}
                values = {row[fk] for row in self.records[child] if row[fk]}
                self.assertTrue(values)
                self.assertFalse(values - keys)

    def test_source_has_relevant_observations(self):
        for source, field, value, minimum in COUNTS:
            with self.subTest(source=source, field=field, value=value):
                self.assertGreaterEqual(sum(row[field] == value for row in self.records[source]), minimum)
