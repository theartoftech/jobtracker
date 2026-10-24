import json
import tempfile
import unittest
from pathlib import Path
from server.runtime_config import load_config, write_config


class RuntimeConfigTests(unittest.TestCase):
    def test_defaults_are_generic(self) -> None:
        config = load_config({})
        self.assertEqual(config.title, 'Job Tracker')
        self.assertEqual(config.time_zone, '')
        self.assertEqual(config.default_tasks, [])
        self.assertEqual(config.calendar_feed_url, '')

    def test_private_file_and_secret_environment_are_supported(self) -> None:
        payload = {
            'title': 'My Applications', 'timeZone': 'Europe/London',
            'calendarFeedUrl': 'https://calendar.example.com/private/feed.ics?key=secret',
            'calendarOpenUrl': 'https://calendar.example.com/view',
            'defaultTasks': [{'id': 'learning', 'title': 'Study', 'detail': 'Read', 'start': '10:00', 'end': '11:00'}],
        }
        encoded = json.dumps(payload)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            path.write_text(encoded)
            config = load_config({'JOBTRACKER_CONFIG_FILE': str(path)})
            self.assertEqual(config, load_config({'JOBTRACKER_CONFIG_JSON': encoded}))
            write_config(config, Path(directory) / 'public', Path(directory) / 'nginx')
            public = (Path(directory) / 'public/runtime-config.js').read_text()
            private = (Path(directory) / 'nginx/jobtracker-calendar.conf').read_text()
            self.assertNotIn('secret', public)
            self.assertNotIn('calendarFeedUrl', public)
            self.assertIn('Europe/London', public)
            self.assertIn('Study', public)
            self.assertIn(payload['calendarFeedUrl'], private)
            self.assertIn('proxy_ssl_verify on;', private)

    def test_defaults_disable_calendar_and_json_encoding_preserves_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config({'JOBTRACKER_CONFIG_JSON': json.dumps({'title': 'A "title" <script>'})})
            write_config(config, root / 'public', root / 'nginx')
            text = (root / 'public/runtime-config.js').read_text()
            payload = json.loads(text.removeprefix('window.JOBTRACKER_CONFIG = ').removesuffix(';\n'))
            self.assertEqual(payload['title'], 'A "title" <script>')
            self.assertEqual((root / 'nginx/jobtracker-calendar.conf').read_text(), 'return 204;\n')

    def test_invalid_configuration_fails_explicitly(self) -> None:
        bad_values = [
            [], {'unknown': True}, {'title': ''}, {'title': 'bad\nname'}, {'title': 10},
            {'timeZone': 'Not/AZone'}, {'timeZone': '../etc/passwd'},
            {'calendarFeedUrl': 'http://example.com/feed'},
            {'calendarFeedUrl': 'https://user:password@example.com/feed'},
            {'calendarFeedUrl': 'https://example.com/";return 200;#'},
            {'calendarFeedUrl': 'https://example.com/$host'},
            {'calendarFeedUrl': 'https://'}, {'calendarOpenUrl': 'javascript:alert(1)'},
            {'defaultTasks': {}}, {'defaultTasks': [None]},
            {'defaultTasks': [{'id': 'bad id', 'title': 'T', 'start': '09:00', 'end': '10:00'}]},
            {'defaultTasks': [{'id': 'a', 'title': 'T', 'start': '25:00', 'end': '26:00'}]},
            {'defaultTasks': [{'id': 'a', 'title': 'T', 'start': '10:00', 'end': '09:00'}]},
            {'defaultTasks': [{'id': 'a', 'title': 'T', 'start': '09:00', 'end': '10:00', 'extra': 1}]},
            {'defaultTasks': [{'id': 'a', 'title': 'T', 'start': '09:00', 'end': '10:00'}] * 2},
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_config({'JOBTRACKER_CONFIG_JSON': json.dumps(value)})
        for raw in ('', '{'):
            with self.assertRaises(ValueError):
                load_config({'JOBTRACKER_CONFIG_JSON': raw})
        with self.assertRaises(ValueError):
            load_config({'JOBTRACKER_CONFIG_FILE': 'file', 'JOBTRACKER_CONFIG_JSON': '{}'})
        with self.assertRaises(FileNotFoundError):
            load_config({'JOBTRACKER_CONFIG_FILE': '/does-not-exist/runtime.json'})


if __name__ == '__main__':
    unittest.main()
