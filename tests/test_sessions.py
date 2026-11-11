import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import session, server, config


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(session, 'SESSION_DIR', self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.sid = session.create()

    def test_title_preserves_records_and_auto_title(self):
        session.message(self.sid, 'user', 'Первый вопрос\nподробности')
        session.event(self.sid, {'type': 'user', 'text': 'Первый вопрос'})
        session.message(self.sid, 'assistant', 'Ответ')
        path = Path(session._path(self.sid))
        records = path.read_bytes().split(b'\n', 1)[1]
        session.set_title_if_empty(self.sid, 'Первый вопрос\nподробности')
        self.assertEqual(session.load(self.sid)['meta']['title'], 'Первый вопрос')
        session.set_title(self.sid, 'Моё название')
        session.set_title_if_empty(self.sid, 'не заменять')
        self.assertEqual(session.load(self.sid)['meta']['title'], 'Моё название')
        self.assertEqual(path.read_bytes().split(b'\n', 1)[1], records)

    def test_concurrent_rename_and_append(self):
        def writer():
            for i in range(40):
                session.message(self.sid, 'assistant', str(i))
        thread = threading.Thread(target=writer)
        thread.start()
        for i in range(15):
            session.set_title(self.sid, 'Name %s' % i)
        thread.join()
        self.assertEqual(len(session.load(self.sid)['messages']), 40)

    def test_list_search_includes_old_sessions(self):
        session.set_title(self.sid, 'Старая ПОГОДА')
        for _ in range(45):
            session.create('Новая')
        self.assertEqual(len(session.list_sessions()), 46)
        self.assertEqual(session.list_sessions(query='погода')[0]['id'], self.sid)
        self.assertEqual(session.list_sessions(query='нет совпадений'), [])

    def test_export_unicode_and_images(self):
        session.set_title(self.sid, 'Переписка')
        session.message(self.sid, 'system', 'PRIVATE SYSTEM PROMPT')
        session.message(self.sid, 'user', [{'type': 'text', 'text': 'Привет'},
                                         {'type': 'image_url', 'image_url': {'url': 'data:SECRET'}}])
        session.message(self.sid, 'assistant', 'Ответ с ```кодом```',
                        tool_calls=[{'id': '1', 'function': {'name': 'list_dir', 'arguments': '{}'}}])
        session.message(self.sid, 'tool', 'Результат')
        result = session.export_markdown(self.sid)
        for text in ['# Переписка', 'Привет', 'Ответ', 'Результат', 'list_dir']:
            self.assertIn(text, result)
        self.assertNotIn('SECRET', result)
        self.assertNotIn('PRIVATE SYSTEM', result)

    def test_invalid_ids(self):
        for sid in ['', '../config', '../../config', 'C:/x', 'a\\b', 'a.jsonl', None, ['x']]:
            self.assertIsNone(session.load(sid))
            self.assertFalse(session.set_title(sid, 'test'))
            self.assertFalse(session.delete(sid))
            self.assertIsNone(session.export_markdown(sid))

    def test_api(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:%s' % http.server_port

        def request(path, data=None, headers=None):
            payload = json.dumps(data).encode() if data is not None else None
            req = urllib.request.Request(base + path, data=payload,
                                         headers=headers or {'Content-Type': 'application/json'})
            try:
                res = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                res = exc
            with res:
                return res.status, res.headers, res.read()

        try:
            session.message(self.sid, 'user', 'Тест экспорта')
            self.assertEqual(request('/api/session/rename', {'session': self.sid, 'title': 'Новое'})[0], 200)
            for title in ['', ' ', 'a' * 121, 'a\nb', 55]:
                self.assertEqual(request('/api/session/rename', {'session': self.sid, 'title': title})[0], 400)
            code, headers, content = request('/api/session/export?session=' + self.sid)
            self.assertEqual(code, 200)
            self.assertIn('attachment', headers['Content-Disposition'])
            self.assertIn('Тест экспорта', content.decode())
            for route in ['/api/history', '/api/session/export']:
                self.assertEqual(request(route + '?session=../../config')[0], 404)
            run = server.Run(self.sid)
            server.RUNS[self.sid] = run
            self.assertEqual(request('/api/session/delete', {'session': self.sid})[0], 409)
            run.finished = True
            self.assertEqual(request('/api/session/delete', {'session': self.sid})[0], 200)
            self.assertNotIn(self.sid, server.RUNS)
            self.assertEqual(request('/api/history?session=' + self.sid)[0], 404)
            self.assertEqual(request('/api/session/delete', {'session': self.sid})[0], 404)
            with patch.object(server, 'load', return_value={}):
                self.assertEqual(request('/api/chat', {'session': '../../config', 'text': 'Hi'})[0], 404)
        finally:
            server.RUNS.pop(self.sid, None)
            http.shutdown()
            http.server_close()
            worker.join()

    def test_clean_config_workspace(self):
        with patch.object(config, 'CONFIG_PATH', str(Path(self.tmp.name) / 'absent.json')), \
                patch.object(config, 'ROOT', self.tmp.name), \
                patch.object(config, 'DATA_DIR', self.tmp.name), \
                patch.object(config, 'SESSION_DIR', self.tmp.name):
            cfg = config.load()
        self.assertEqual(cfg['workspace'], str(Path(self.tmp.name) / 'work'))
        self.assertTrue(Path(cfg['workspace']).is_dir())


if __name__ == '__main__':
    unittest.main()
