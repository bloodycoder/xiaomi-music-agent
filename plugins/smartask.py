import asyncio
import os
import requests


MUSIC_AGENT_URL = os.environ.get('MUSIC_AGENT_URL', 'http://127.0.0.1:8765').rstrip('/')


async def _suppress_xiaoai(did, reason=''):
    """Best-effort stop/pause of the official XiaoAI answer.

    We cannot prevent Xiaomi cloud from generating the official answer, because
    xiaomusic only sees the conversation after it has been recorded. But once
    smartask is triggered, we can immediately stop the speaker player and then
    play our own TTS answer.
    """
    global log, xiaomusic
    try:
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return
        log.info(f'smartask suppress official XiaoAI answer: {reason}')
        await device.group_force_stop_xiaoai()
    except Exception as e:
        log.warning(f'smartask suppress failed: {e}')


async def smartask(query):
    global log, xiaomusic
    q = (query or '').strip()
    did = xiaomusic.get_cur_did()

    # 抢占：尽快打断小爱官方回答，避免它先说完整句。
    await _suppress_xiaoai(did, 'enter smartask')

    if not q:
        await xiaomusic.do_tts(did, '你想问什么？')
        return

    try:
        r = requests.get(f'{MUSIC_AGENT_URL}/ask', params={'q': q}, timeout=60)
        r.raise_for_status()
        data = r.json()
        answer = (data.get('answer') or '').strip() or '我没有得到有效回答。'
    except Exception as e:
        log.exception(f'smartask query:{q} failed: {e}')
        answer = '大模型服务暂时不可用。'

    # 避免一次 TTS 太长导致体验差。
    if len(answer) > 300:
        answer = answer[:300].rstrip() + '。'

    # 播放自己的 TTS 前再压一次，防止官方回答/提示音仍在播。
    await _suppress_xiaoai(did, 'before llm tts')
    await asyncio.sleep(0.2)

    log.info(f'smartask query:{q} answer:{answer}')
    await xiaomusic.do_tts(did, answer)
