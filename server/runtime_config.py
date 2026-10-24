"""Validate owner-supplied runtime settings before starting the application."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TypedDict
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TaskDefinition(TypedDict):
    id: str
    title: str
    detail: str
    start: str
    end: str


@dataclass(frozen=True)
class RuntimeConfig:
    title: str
    time_zone: str
    calendar_feed_url: str
    calendar_open_url: str
    default_tasks: list[TaskDefinition]


def text_value(value: object, field: str, required: bool = False) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError(f'{field} must be a string without control characters')
    if required and not value.strip():
        raise ValueError(f'{field} must not be empty')
    return value


def https_url(value: object, field: str) -> str:
    url = text_value(value, field)
    if not url:
        return ''
    # The URL becomes a quoted Nginx directive. Reject interpolation and syntax
    # characters, rather than allowing configuration to become executable code.
    if not re.fullmatch(r"[A-Za-z0-9\-._~:/?\[\]@!&'()*+,=%]+", url):
        raise ValueError(f'{field} contains unsupported URL characters; percent-encode them')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f'{field} must be an HTTPS URL without embedded credentials')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError(f'{field} has an invalid port')
    return url


def load_config(environment: Mapping[str, str]) -> RuntimeConfig:
    file_path = environment.get('JOBTRACKER_CONFIG_FILE', '')
    if file_path and 'JOBTRACKER_CONFIG_JSON' in environment:
        raise ValueError('Set either JOBTRACKER_CONFIG_FILE or JOBTRACKER_CONFIG_JSON, not both')
    raw = Path(file_path).read_text(encoding='utf-8') if file_path else environment.get('JOBTRACKER_CONFIG_JSON', '{}')
    data = json.loads(raw)
    fields = {'title', 'timeZone', 'calendarFeedUrl', 'calendarOpenUrl', 'defaultTasks'}
    if not isinstance(data, dict) or set(data) - fields:
        raise ValueError('Runtime configuration must be an object containing only supported fields')
    title = text_value(data.get('title', 'Job Tracker'), 'title', required=True)
    zone = text_value(data.get('timeZone', ''), 'timeZone')
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('timeZone must be a valid IANA timezone') from error
    tasks = data.get('defaultTasks', [])
    if not isinstance(tasks, list):
        raise ValueError('defaultTasks must be an array')
    parsed_tasks: list[TaskDefinition] = []
    ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {'id', 'title', 'detail', 'start', 'end'}:
            raise ValueError('Each default task must be an object containing only supported fields')
        task_id = text_value(task.get('id', ''), 'task.id', required=True)
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', task_id) or task_id in ids:
            raise ValueError('Task IDs must be unique letters, digits, underscores, or hyphens')
        ids.add(task_id)
        start = text_value(task.get('start', ''), 'task.start')
        end = text_value(task.get('end', ''), 'task.end')
        if any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock) for clock in (start, end)) or start >= end:
            raise ValueError('Task times must be HH:MM with end after start on the same day')
        parsed_tasks.append(TaskDefinition(
            id=task_id, title=text_value(task.get('title', ''), 'task.title', required=True),
            detail=text_value(task.get('detail', ''), 'task.detail'), start=start, end=end,
        ))
    return RuntimeConfig(title, zone, https_url(data.get('calendarFeedUrl', ''), 'calendarFeedUrl'),
                         https_url(data.get('calendarOpenUrl', ''), 'calendarOpenUrl'), parsed_tasks)


def write_config(config: RuntimeConfig, public_dir: Path, nginx_dir: Path) -> None:
    public = {'title': config.title, 'timeZone': config.time_zone,
              'calendarOpenUrl': config.calendar_open_url, 'defaultTasks': config.default_tasks}
    calendar = 'return 204;\n'
    if config.calendar_feed_url:
        host = urlsplit(config.calendar_feed_url).netloc
        calendar = (f'proxy_pass "{config.calendar_feed_url}";\n'
                    'proxy_ssl_server_name on;\nproxy_ssl_verify on;\n'
                    'proxy_ssl_trusted_certificate /etc/ssl/cert.pem;\n'
                    f'proxy_set_header Host "{host}";\n')
    public_dir.mkdir(parents=True, exist_ok=True)
    nginx_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / 'runtime-config.js').write_text('window.JOBTRACKER_CONFIG = ' + json.dumps(public) + ';\n', encoding='utf-8')
    (nginx_dir / 'jobtracker-calendar.conf').write_text(calendar, encoding='utf-8')


def main() -> None:
    config = load_config(os.environ)
    write_config(config, Path('/usr/share/nginx/html'), Path('/etc/nginx'))


if __name__ == '__main__':
    main()
