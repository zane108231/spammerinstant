from flask import Flask, render_template, request, Response, jsonify
import requests
import uuid
import time
import json
import random
import threading
from pathlib import Path
from queue import Queue, Empty

app = Flask(__name__)

# ============ EDIT THIS ============
TARGET_USERNAME = "csabconfessionwall"   # NGL username to send to
AUTO_START = True                # start sending as soon as app.py runs
SEND_DELAY_MIN = 0.8             # seconds between messages (min)
SEND_DELAY_MAX = 1.0             # seconds between messages (max)
# ===================================

BASE_DIR = Path(__file__).parent
MESSAGES_FILE = BASE_DIR / 'messages.txt'

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

event_queue = Queue()


class SenderState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.paused = False
        self.stop_flag = False
        self.username = ''
        self.sent = 0
        self.failed = 0
        self.attempts = 0
        self.last_message = ''
        self.last_error = ''
        self.last_http_status = None
        self.status = 'idle'
        self.thread = None


state = SenderState()


def load_messages():
    if not MESSAGES_FILE.exists():
        return ['hey?', 'how are u?']

    messages = []
    for line in MESSAGES_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            messages.append(line)
    return messages or ['hey?']


class MessageDeck:
    def __init__(self, messages):
        self.all_messages = list(messages)
        self.deck = []
        self._reshuffle()

    def _reshuffle(self):
        self.deck = self.all_messages.copy()
        random.shuffle(self.deck)

    def draw(self):
        if not self.deck:
            self._reshuffle()
        return self.deck.pop()


def generate_device_id():
    device_id = uuid.uuid4().hex
    return '-'.join([device_id[i:i + 8] for i in range(0, 32, 8)])


def send_to_ngl(username, message):
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'Origin': 'https://ngl.link',
        'Referer': f'https://ngl.link/{username}',
    }
    payload = {
        'username': username,
        'question': message,
        'deviceId': generate_device_id(),
        'gameSlug': '',
        'referrer': '',
    }

    try:
        response = requests.post(
            'https://ngl.link/api/submit',
            headers=headers,
            data=payload,
            timeout=15,
        )
        ok = response.status_code == 200
        error = None if ok else f'HTTP {response.status_code}'
        return {
            'ok': ok,
            'status_code': response.status_code,
            'error': error,
        }
    except requests.RequestException as exc:
        return {
            'ok': False,
            'status_code': None,
            'error': str(exc),
        }


def push_event(event_type, extra=None):
    payload = {'type': event_type, 'time': time.time(), **get_snapshot()}
    if extra:
        payload.update(extra)
    event_queue.put(payload)


def get_snapshot():
    with state.lock:
        return {
            'running': state.running,
            'paused': state.paused,
            'status': state.status,
            'username': state.username,
            'sent': state.sent,
            'failed': state.failed,
            'attempts': state.attempts,
            'last_message': state.last_message,
            'last_error': state.last_error,
            'last_http_status': state.last_http_status,
            'message_count': len(load_messages()),
        }


def send_loop(username):
    deck = MessageDeck(load_messages())
    push_event('started')

    while True:
        with state.lock:
            if state.stop_flag:
                state.running = False
                state.status = 'stopped'
                push_event('stopped')
                return
            paused = state.paused

        if paused:
            time.sleep(0.1)
            continue

        message = deck.draw()
        result = send_to_ngl(username, message)

        with state.lock:
            state.attempts += 1
            state.last_message = message
            state.last_http_status = result['status_code']
            if result['ok']:
                state.sent += 1
                state.last_error = ''
                state.status = 'running'
            else:
                state.failed += 1
                state.last_error = result['error'] or 'Unknown error'
                state.status = 'running'

        push_event('progress', {
            'ok': result['ok'],
            'error': result['error'],
            'status_code': result['status_code'],
        })

        time.sleep(random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX))


def start_sending(username=None):
    username = (username or TARGET_USERNAME).strip()
    if not username:
        raise ValueError('Set TARGET_USERNAME at the top of app.py')

    with state.lock:
        if state.running:
            return False

        state.running = True
        state.paused = False
        state.stop_flag = False
        state.username = username
        state.sent = 0
        state.failed = 0
        state.attempts = 0
        state.last_message = ''
        state.last_error = ''
        state.last_http_status = None
        state.status = 'running'
        state.thread = threading.Thread(target=send_loop, args=(username,), daemon=True)
        state.thread.start()

    return True


@app.route('/')
def index():
    return render_template('index.html', target=TARGET_USERNAME, auto_start=AUTO_START)


@app.route('/api/status')
def api_status():
    return jsonify(get_snapshot())


@app.route('/api/start', methods=['POST'])
def api_start():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or TARGET_USERNAME).strip()

    if not username:
        return jsonify({'error': 'Set TARGET_USERNAME at the top of app.py'}), 400

    if not start_sending(username):
        return jsonify({'error': 'Already running. Press Stop first.'}), 400

    return jsonify({'ok': True, **get_snapshot()})


@app.route('/api/pause', methods=['POST'])
def api_pause():
    with state.lock:
        if not state.running:
            return jsonify({'error': 'Not running'}), 400
        if state.paused:
            return jsonify({'ok': True, **get_snapshot()})
        state.paused = True
        state.status = 'paused'

    push_event('paused')
    return jsonify({'ok': True, **get_snapshot()})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    with state.lock:
        if not state.running:
            return jsonify({'error': 'Not running. Press Start first.'}), 400
        if not state.paused:
            return jsonify({'ok': True, **get_snapshot()})
        state.paused = False
        state.status = 'running'

    push_event('resumed')
    return jsonify({'ok': True, **get_snapshot()})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    with state.lock:
        if not state.running:
            return jsonify({'ok': True, **get_snapshot()})
        state.stop_flag = True
        state.paused = False
        state.status = 'stopping'

    push_event('stopping')
    return jsonify({'ok': True, **get_snapshot()})


@app.route('/api/stream')
def api_stream():
    def generate():
        yield f'data: {json.dumps({"type": "hello", **get_snapshot()})}\n\n'
        while True:
            try:
                event = event_queue.get(timeout=10)
                yield f'data: {json.dumps(event)}\n\n'
            except Empty:
                yield f'data: {json.dumps({"type": "ping", **get_snapshot()})}\n\n'

    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
    })


if __name__ == '__main__':
    print('NGL Basic Sender -> http://127.0.0.1:5000')
    print(f'Loaded {len(load_messages())} messages from messages.txt')
    print(f'Target: {TARGET_USERNAME}')
    print('Web controls: Start | Pause | Resume | Stop')

    if AUTO_START:
        if start_sending(TARGET_USERNAME):
            print('Auto-started — open the site for full control.')
        else:
            print('Could not auto-start (already running).')

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
