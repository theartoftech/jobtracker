import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_import_route_can_use_bearer_token_without_basic_auth_collision(self) -> None:
        nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
        for route in (
            "location = /api/analytics/workflow-runs",
            "location = /jobtracker/api/analytics/workflow-runs",
        ):
            start = nginx.index(route)
            end = nginx.index("\n    }", start)
            block = nginx[start:end]
            self.assertIn("auth_basic off;", block)
            self.assertIn("proxy_pass http://127.0.0.1:8090;", block)
            self.assertIn("client_max_body_size 2m;", block)

    def test_deployment_reads_optional_scoped_token_secret(self) -> None:
        manifest = (ROOT / "deployment/k8s/jobtracker.yaml").read_text(encoding="utf-8")
        self.assertIn("name: JOBTRACKER_ANALYTICS_IMPORT_TOKEN", manifest)
        self.assertIn("name: jobtracker-analytics-import", manifest)
        self.assertIn("key: token", manifest)
        self.assertIn("optional: true", manifest)


if __name__ == "__main__":
    unittest.main()
