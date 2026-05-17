import os
import requests

MUSIC_AGENT_URL = os.environ.get('MUSIC_AGENT_URL', 'http://127.0.0.1:8765').rstrip('/')

def smartnext():
    global log
    r = requests.get(f'{MUSIC_AGENT_URL}/next', timeout=30)
    r.raise_for_status()
    log.info(f'smartnext response:{r.text}')
