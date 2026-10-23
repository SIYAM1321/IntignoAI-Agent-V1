import json
import os
import tempfile
import threading
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ['GOOGLE_API_KEY'] = 'test-key'
os.environ['WORKSPACE'] = tempfile.mkdtemp(prefix='intignoai-integration-')
os.environ['MAX_TOOL_STEPS'] = '5'

from agent import engine
from agent.memory import Session


received = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length).decode('utf-8'))
        n = received.get('count', 0) + 1
        received['count'] = n
        received[f'body{n}'] = body
        if n == 1:
            events = [
                {'event': 'interaction.created', 'data': {'interaction': {'id': 'i1'}}},
                {'event': 'step.start', 'data': {'index': 0, 'step': {'type': 'function_call', 'id': 'c1', 'name': r'default_api\:run_terminal', 'arguments': {}}}},
                {'event': 'step.delta', 'data': {'index': 0, 'delta': {'type': 'arguments_delta', 'arguments': '{"command":"python --version"}'}}},
                {'event': 'interaction.completed', 'data': {'interaction': {'id': 'i1', 'status': 'requires_action'}}},
                {'event': 'done', 'data': '[DONE]'},
            ]
        else:
            assert body['previous_interaction_id'] == 'i1'
            assert body['input'][0]['type'] == 'function_result'
            assert body['input'][0]['name'] == r'default_api\:run_terminal'
            assert body['input'][0]['call_id'] == 'c1'
            received['asserted'] = True
            events = [
                {'event': 'interaction.created', 'data': {'interaction': {'id': 'i2'}}},
                {'event': 'step.start', 'data': {'index': 0, 'step': {'type': 'model_output'}}},
                {'event': 'step.delta', 'data': {'index': 0, 'delta': {'type': 'text', 'text': 'Python is installed.'}}},
                {'event': 'interaction.completed', 'data': {'interaction': {'id': 'i2', 'status': 'completed'}}},
                {'event': 'done', 'data': '[DONE]'},
            ]
        chunks = []
        for e in events:
            data = e['data'] if e['event'] == 'done' else json.dumps(e['data'])
            chunks.append(f"event: {e['event']}\ndata: {data}\n\n")
        payload = ''.join(chunks).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine.API_URL = f'http://127.0.0.1:{server.server_port}/interactions'
    session = Session()
    completed = []
    engine.start_request(session, 'What Python version is installed?', lambda text: completed.append(text))
    for _ in range(100):
        if not session.busy:
            break
        __import__('time').sleep(0.03)
    server.shutdown()
    assert received.get('asserted') is True, received
    assert completed and completed[-1] == 'Python is installed.', completed
    assert any(a.tool == 'run_terminal' and a.status == 'success' for a in session.activity.items), [a.to_dict() for a in session.activity.items]
    print('fake Gemini function-call integration: PASS')


if __name__ == '__main__':
    main()
