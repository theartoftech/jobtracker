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

    def test_training_write_route_remains_under_browser_basic_auth(self) -> None:
        nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn('auth_basic "Job Tracker";', nginx)
        self.assertIn("location ^~ /api/", nginx)
        self.assertIn("location ^~ /jobtracker/api/", nginx)
        self.assertNotIn("location = /api/analytics/skill-training", nginx)

    def test_calendar_is_disabled_without_an_owner_configured_feed(self) -> None:
        nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
        for route in ("location = /calendar.ics", "location = /jobtracker/calendar.ics"):
            start = nginx.index(route)
            block = nginx[start:nginx.index("\n    }", start)]
            self.assertIn("include /etc/nginx/jobtracker-calendar.conf;", block)
            self.assertNotIn("proxy_pass", block)
        html = (ROOT / "src/index.html").read_text(encoding="utf-8")
        self.assertNotIn("calendar.google.com", html)

    def test_distributed_configuration_has_no_personal_addresses_or_home_paths(self) -> None:
        for relative_path in (
            "README.md", "nginx.conf", "src/index.html",
            "deployment/k8s/jobtracker.yaml",
            "deployment/scripts/deploy-jobtracker-remote-k8s.sh",
        ):
            with self.subTest(path=relative_path):
                content = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotRegex(content, r"192\.168\.\d+\.\d+")
                self.assertNotRegex(content, r"/home/[a-zA-Z0-9_-]+/")
                self.assertNotRegex(content, r"[\w.+-]+(?:@|%40)[\w.-]+\.[a-zA-Z]{2,}")


if __name__ == "__main__":
    unittest.main()
