"""Pane multi-card display: per-card buttons, tone dots, and detail switching.

Runs the real desktop/plugin.js through tests/widget_multicard.cjs — the
shared harness with a stateful useState and a click step — against cache
fixtures shaped exactly like the rows the backend emits for a saved pool.
Offline: every value is synthetic, no credential and no network is touched.

The light contract mirrors the task book: remaining >40 green, 15-40 yellow,
<=15 red, a read failure grey.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = Path(__file__).resolve().parent / 'widget_multicard.cjs'

LABELS = ('alpha.gmail', 'beta.gmail', 'gamma.gmail', 'delta.mail')


def render(**options):
    p = subprocess.run(
        ['node', str(DRIVER)], input=json.dumps(options), text=True,
        capture_output=True, timeout=20,
    )
    if p.returncode:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


def card(account_label, used=None, *, account_id=None, unavailable=None):
    """One cache record in the shape quota_cache emits for an account row."""
    record = {
        'label': f'commandcode · {account_label}',
        'account_label': account_label,
        'windows': [] if used is None else [
            {'label': '5h', 'used_percent': used, 'reset_at': '2099-01-01T00:00:00Z'}],
    }
    if account_id:
        record['provider'] = 'commandcode'
        record['account_id'] = account_id
    if unavailable:
        record['unavailable_reason'] = unavailable
    return record


def pool_data(percents, labels=LABELS):
    """A four-row saved-pool provider: base row + three account rows."""
    providers = {}
    for index, (used, label) in enumerate(zip(percents, labels)):
        record = card(
            label, used,
            account_id=None if index == 0 else f'r{index + 1}',
            unavailable='fetch-error:OSError' if used is None else None,
        )
        if index == 0:
            record['label'] = 'commandcode'
        providers['commandcode' if index == 0 else f'commandcode:r{index + 1}'] = record
    return {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': providers}


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class MultiCardPaneTests(unittest.TestCase):
    def test_every_card_gets_a_button_with_its_pool_label_and_tone_dot(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)))
        self.assertEqual(out['labels'], list(LABELS))
        # remaining 50 -> good; 30 -> warn; 10 -> bad; read failure -> muted.
        self.assertEqual(out['tones'], ['good', 'warn', 'bad', 'muted'])

    def test_tone_boundaries_follow_the_shared_thresholds(self):
        # remaining 41 -> good, 40 -> warn, 16 -> warn, 15 -> bad.
        out = render(data=pool_data((59.0, 60.0, 84.0, 85.0)))
        self.assertEqual(out['tones'], ['good', 'warn', 'warn', 'bad'])

    def test_clicking_a_card_button_switches_the_detail_card(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)), click='gamma.gmail')
        self.assertIsNotNone(out['after'])
        before, after = json.dumps(out['before']), json.dumps(out['after'])
        # Default selection is the first card; the click moves the detail to
        # the clicked card (its window value appears; the first one goes).
        self.assertIn('50% left', before)
        self.assertNotIn('10% left', before)
        self.assertIn('10% left', after)
        self.assertNotIn('50% left', after)
        self.assertEqual(out['pressed'], [True, False, False, False])
        self.assertEqual(out['pressedAfter'], [False, False, True, False])

    def test_a_read_failure_stays_a_clickable_muted_card(self):
        out = render(data=pool_data((50.0, 70.0, 90.0, None)), click='delta.mail')
        after = json.dumps(out['after'])
        self.assertIn('fetch-error:OSError', after)
        # The failed card's detail is shown instead of the first card's value.
        self.assertNotIn('50% left', after)

    def test_single_card_provider_keeps_the_plain_card_path(self):
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'deepseek': {'windows': [{'label': 'Weekly', 'used_percent': 10.0}]}}}
        out = render(data=data)
        self.assertEqual(out['labels'], [])
        rendered = json.dumps(out['before'])
        self.assertIn('90% left', rendered)
        self.assertIn('DeepSeek', rendered)

    def test_grouping_is_generic_not_hard_coded_to_commandcode(self):
        data = {'fetched_at': '2026-01-02T03:04:05Z', 'age_s': 3, 'providers': {
            'anthropic': {'windows': [{'label': 'Current session', 'used_percent': 10.0}]},
            'anthropic:work': {'label': 'anthropic · Work', 'provider': 'anthropic',
                               'account_id': 'work', 'account_label': 'Work',
                               'windows': [{'label': 'Current session', 'used_percent': 60.0}]}}}
        out = render(data=data)
        # The primary row has no saved label: falls back to its display name.
        self.assertEqual(out['labels'], ['Anthropic', 'Work'])
        # remaining 90 -> good; 40 -> warn.
        self.assertEqual(out['tones'], ['good', 'warn'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
