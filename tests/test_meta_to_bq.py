import unittest
from unittest.mock import MagicMock, patch
import meta_to_bq

class TestMetaToBqReach(unittest.TestCase):

    @patch("time.sleep", return_value=None)
    @patch.object(meta_to_bq, "ensure_reach_table_exists", return_value=None)
    def test_reach_delete_rate_limit_raises_after_5_attempts(self, mock_ensure, mock_sleep):
        """Verify that a persistent DELETE rateLimitExceeded raises RuntimeError and never falls through to append."""
        mock_client = MagicMock()
        mock_query_job = MagicMock()
        mock_query_job.result.side_effect = Exception("rateLimitExceeded: too many table update operations")
        mock_client.query.return_value = mock_query_job

        records = [{"campaign_id": "123", "campaign_name": "Test", "reach": 100, "impressions": 200}]

        with self.assertRaises(RuntimeError) as ctx:
            meta_to_bq.load_reach_into_bigquery(
                mock_client,
                window_days=7,
                window_start="2026-11-04",
                window_end="2026-11-10",
                records=records
            )

        self.assertIn("Failed to delete existing rows in", str(ctx.exception))
        self.assertIn("after 5 attempts", str(ctx.exception))
        # Ensure it attempted exactly 5 times
        self.assertEqual(mock_client.query.call_count, 5)
        # Ensure it NEVER reached the load / append step
        self.assertEqual(mock_client.load_table_from_json.call_count, 0)

    @patch("time.sleep", return_value=None)
    @patch.object(meta_to_bq, "ensure_reach_table_exists", return_value=None)
    def test_reach_load_rate_limit_raises_after_5_attempts(self, mock_ensure, mock_sleep):
        """Verify that a persistent LOAD rateLimitExceeded raises RuntimeError and never reports success."""
        mock_client = MagicMock()
        # DELETE succeeds on first attempt
        mock_del_job = MagicMock()
        mock_del_job.result.return_value = None
        mock_client.query.return_value = mock_del_job

        # LOAD fails with rateLimitExceeded on all 5 attempts
        mock_load_job = MagicMock()
        mock_load_job.result.side_effect = Exception("rateLimitExceeded: too many table update operations")
        mock_client.load_table_from_json.return_value = mock_load_job

        records = [{"campaign_id": "123", "campaign_name": "Test", "reach": 100, "impressions": 200}]

        with self.assertRaises(RuntimeError) as ctx:
            meta_to_bq.load_reach_into_bigquery(
                mock_client,
                window_days=7,
                window_start="2026-11-04",
                window_end="2026-11-10",
                records=records
            )

        self.assertIn("Failed to load reach rows into", str(ctx.exception))
        self.assertIn("after 5 attempts", str(ctx.exception))
        # DELETE was called once
        self.assertEqual(mock_client.query.call_count, 1)
        # LOAD was attempted 5 times
        self.assertEqual(mock_client.load_table_from_json.call_count, 5)

if __name__ == "__main__":
    unittest.main()

