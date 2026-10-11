"""Command Code multi-card contract: one card per saved credential row.

The local store keeps one row per saved key; the fetcher must enumerate those
rows read-only and report every one as its own card, so one row's failure is
that card's typed state and never masks a sibling. Offline only: the pool and
every HTTP result are synthetic; no real credential is read.
"""

from __future__ import annotations

import unittest
from unittest import mock

from quota_providers import commandcode
from quota_providers.base import QuotaResult, QuotaWindow, build_unavailable

ROWS = [
    {"id": "r1", "label": "alpha.gmail", "access_token": "SYNTH-ONE"},
    {"id": "r2", "label": "beta.gmail", "access_token": "SYNTH-TWO"},
    {"id": "r3", "label": "gamma.gmail", "access_token": "SYNTH-THREE"},
    {"id": "r4", "label": "delta.mail", "access_token": "SYNTH-FOUR"},
]


def card(used: float, *, plan: str = "Go") -> QuotaResult:
    return QuotaResult(
        label="commandcode",
        windows=[QuotaWindow(label="5h", used_percent=used, reset_at="2099-01-01T00:00:00Z")],
        plan=plan,
    )


class CredentialRowTests(unittest.TestCase):
    def rows(self, entries):
        with mock.patch.object(commandcode, "_pool_rows", return_value=list(entries)):
            return commandcode._credential_rows()

    def test_every_saved_row_yields_id_label_and_secret(self):
        rows = self.rows(ROWS)
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [("r1", "alpha.gmail"), ("r2", "beta.gmail"),
             ("r3", "gamma.gmail"), ("r4", "delta.mail")],
        )
        self.assertEqual(
            [row[2] for row in rows],
            ["SYNTH-ONE", "SYNTH-TWO", "SYNTH-THREE", "SYNTH-FOUR"],
        )

    def test_duplicates_and_secretless_rows_are_skipped(self):
        rows = self.rows([
            ROWS[0],
            {**ROWS[1], "access_token": "SYNTH-ONE"},  # same secret -> one card
            {"label": "no secret"},
            {"id": "blank", "label": "Blank", "access_token": "   "},
        ])
        self.assertEqual([row[2] for row in rows], ["SYNTH-ONE"])

    def test_labels_drop_control_characters_and_stay_bounded(self):
        rows = self.rows([
            {"id": "a", "label": " bad\x07label ", "access_token": "S"},
            {"id": "\x00\x1f", "label": "\U0001f600" * 80, "access_token": "T"},
        ])
        self.assertEqual(rows[0][1], "badlabel")
        self.assertIsNone(rows[1][0])  # id stripped to nothing
        self.assertEqual(rows[1][1], "\U0001f600" * 64)  # bounded display label


class MultiCardFetchTests(unittest.TestCase):
    CARDS = {
        "SYNTH-ONE": card(50.0), "SYNTH-TWO": card(40.0),
        "SYNTH-THREE": card(30.0), "SYNTH-FOUR": card(20.0),
    }

    def fetch(self, rows, cards=None, calls=None):
        cards = dict(cards or self.CARDS)

        def fake_card(key, deadline):
            if calls is not None:
                calls.append(key)
            return cards[key]

        with mock.patch.object(commandcode, "_pool_rows", return_value=list(rows)), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=fake_card):
            return commandcode.fetch_commandcode_quota()

    def test_each_row_becomes_its_own_card(self):
        result = self.fetch(ROWS)
        self.assertEqual(result.label, "commandcode")
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual([a.id for a in result.accounts], ["r2", "r3", "r4"])
        self.assertEqual(
            [a.label for a in result.accounts], ["beta.gmail", "gamma.gmail", "delta.mail"]
        )
        self.assertEqual(
            [a.windows[0].used_percent for a in result.accounts], [40.0, 30.0, 20.0]
        )
        self.assertTrue(all(a.unavailable_reason is None for a in result.accounts))

    def test_every_row_is_fetched_with_its_own_secret(self):
        calls = []
        self.fetch(ROWS, calls=calls)
        self.assertEqual(
            sorted(calls), ["SYNTH-FOUR", "SYNTH-ONE", "SYNTH-THREE", "SYNTH-TWO"]
        )

    def test_one_rows_failure_is_that_cards_typed_state(self):
        cards = dict(self.CARDS)
        cards["SYNTH-THREE"] = build_unavailable("commandcode", "auth-failed")
        result = self.fetch(ROWS, cards)
        self.assertIsNone(result.unavailable_reason)
        by_id = {a.id: a for a in result.accounts}
        self.assertEqual(by_id["r3"].unavailable_reason, "auth-failed")
        self.assertEqual(by_id["r3"].windows, [])
        self.assertEqual(by_id["r2"].windows[0].used_percent, 40.0)

    def test_raising_row_does_not_sink_its_siblings(self):
        def fake_card(key, deadline):
            if key == "SYNTH-TWO":
                raise RuntimeError("boom")
            return card(50.0)

        with mock.patch.object(commandcode, "_pool_rows", return_value=list(ROWS)), \
                mock.patch.object(commandcode, "_fetch_card", side_effect=fake_card):
            result = commandcode.fetch_commandcode_quota()
        self.assertIsNone(result.unavailable_reason)
        by_id = {a.id: a for a in result.accounts}
        self.assertEqual(by_id["r2"].unavailable_reason, "fetch-error:RuntimeError")
        self.assertEqual(by_id["r3"].windows[0].used_percent, 50.0)

    def test_base_row_failure_still_keeps_the_other_cards(self):
        cards = dict(self.CARDS)
        cards["SYNTH-ONE"] = build_unavailable("commandcode", "timeout")
        result = self.fetch(ROWS, cards)
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertEqual(result.account_label, "alpha.gmail")
        self.assertEqual(len(result.accounts), 3)
        self.assertEqual(result.accounts[0].windows[0].used_percent, 40.0)

    def test_pool_read_and_rows_are_not_mutated(self):
        rows = [dict(row) for row in ROWS]
        snapshot = [dict(row) for row in rows]
        self.fetch(rows)
        self.assertEqual(rows, snapshot)


class SingleCardUnchangedTests(unittest.TestCase):
    def test_empty_pool_falls_back_to_the_cli_or_core_key(self):
        sentinel = card(50.0)
        with mock.patch.object(commandcode, "_pool_rows", return_value=[]), \
                mock.patch.object(commandcode, "_load_api_key", return_value="SYNTH-ONE"), \
                mock.patch.object(commandcode, "_fetch_card", return_value=sentinel) as single:
            result = commandcode.fetch_commandcode_quota()
        single.assert_called_once_with("SYNTH-ONE", mock.ANY)
        self.assertIs(result, sentinel)

    def test_empty_pool_and_no_key_is_still_no_credentials(self):
        with mock.patch.object(commandcode, "_pool_rows", return_value=[]), \
                mock.patch.object(commandcode, "_load_api_key", return_value=None):
            result = commandcode.fetch_commandcode_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")
        self.assertEqual(result.accounts, [])

    def test_a_single_row_keeps_the_plain_single_card(self):
        sentinel = card(50.0)
        with mock.patch.object(commandcode, "_pool_rows", return_value=[ROWS[0]]), \
                mock.patch.object(commandcode, "_load_api_key", return_value="SYNTH-ONE"), \
                mock.patch.object(commandcode, "_fetch_card", return_value=sentinel) as single:
            result = commandcode.fetch_commandcode_quota()
        self.assertIs(result, sentinel)
        self.assertEqual(result.accounts, [])
        self.assertIsNone(result.account_label)
        single.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
