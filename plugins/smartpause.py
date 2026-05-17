import os
import requests

MUSIC_AGENT_URL = os.environ.get('MUSIC_AGENT_URL', 'http://127.0.0.1:8765').rstrip('/')

def smartpause():
    global log
    r = requests.get(f'{MUSIC_AGENT_URL}/pause', timeout=30)
    r.raise_for_status()
    log.info(f'smartpause response:{r.text}')
