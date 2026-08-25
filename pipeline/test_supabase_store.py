"""Offline tests for PostgREST persistence and schema routing."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import store


ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-service-key",
    "SUPABASE_SCHEMA": "news_aggregator",
}


class Response:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload=None):
        self.payload = [] if payload is None else payload

    def json(self):
        return self.payload


class StoreTests(unittest.TestCase):
    def test_schema_is_required(self):
        env = {k: v for k, v in ENV.items() if k != "SUPABASE_SCHEMA"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(store.configured())
            with self.assertRaisesRegex(store.StoreError, "SUPABASE_SCHEMA"):
                store._endpoint("briefings")

    def test_select_uses_accept_profile_and_pages(self):
        responses = [Response([{"id": "one"}]), Response([])]
        with mock.patch.dict(os.environ, ENV, clear=True), \
             mock.patch.object(store.requests, "get", side_effect=responses) as get:
            rows = store.select("seen_articles", {
                "select": "id", "order": "id"
            })

        self.assertEqual(rows, [{"id": "one"}])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Accept-Profile"],
                         "news_aggregator")
        self.assertNotIn("Content-Profile",
                         get.call_args_list[0].kwargs["headers"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["offset"], 1)

    def test_writes_use_content_profile(self):
        with mock.patch.dict(os.environ, ENV, clear=True), \
             mock.patch.object(store.requests, "post", return_value=Response()) as post, \
             mock.patch.object(store.requests, "delete", return_value=Response()) as delete:
            store.upsert("briefings", [{"date": "2026-08-24", "payload": {}}])
            store.delete("briefings", {"date": "lt.2026-08-01"})

        for call in (post.call_args, delete.call_args):
            headers = call.kwargs["headers"]
            self.assertEqual(headers["Content-Profile"], "news_aggregator")
            self.assertNotIn("Accept-Profile", headers)

    def test_delete_without_filter_is_refused(self):
        with mock.patch.dict(os.environ, ENV, clear=True):
            with self.assertRaisesRegex(store.StoreError, "refusing"):
                store.delete("briefings", {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
