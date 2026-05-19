import asyncio
import os
import requests

MUSIC_AGENT_URL = os.environ.get('MUSIC_AGENT_URL', 'http://127.0.0.1:8765').rstrip('/')


DEFAULT_EMPTY_QUERY = os.environ.get('SMARTPLAY_DEFAULT_QUERY', 'kkecho歌单')

_suppress_tasks = set()


def _call_music_agent(q):
    r = requests.get(f'{MUSIC_AGENT_URL}/play', params={'q': q}, timeout=90)
    r.raise_for_status()
    return r.text


async def _suppress_xiaoai_best_effort(did, reason=''):
    """后台尽快打断小爱官方回答，但绝不阻塞网易云播放。"""
    global log, xiaomusic
    try:
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return
        async def quick_stop(device_id):
            service = device.auth_manager.mina_service
            calls = (service.player_pause(device_id), service.player_stop(device_id))
            results = await asyncio.gather(
                *(asyncio.wait_for(call, timeout=2.0) for call in calls),
                return_exceptions=True,
            )
            log.info(f'smartplay quick stop XiaoAI device_id:{device_id} results:{results}')

        # 第一次立即打断，后面补两次，覆盖官方回答稍晚开始的情况。
        device_id_list = xiaomusic.device_manager.get_group_device_id_list(device.group_name)
        for delay in (0, 0.35, 1.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                log.info(f'smartplay suppress official XiaoAI answer: {reason} delay:{delay}')
                await asyncio.gather(
                    *(quick_stop(device_id) for device_id in device_id_list),
                    return_exceptions=True,
                )
            except Exception as e:
                log.warning(f'smartplay suppress failed: {e}')
    except Exception as e:
        log.warning(f'smartplay suppress outer failed: {e}')


async def smartplay(query):
    global log, xiaomusic
    q = (query or '').strip()
    did = xiaomusic.get_cur_did()

    # 抢占：后台掐掉小爱官方回答；不要 await，否则小米云接口慢时会卡住网易云播放。
    task = asyncio.create_task(_suppress_xiaoai_best_effort(did, 'enter smartplay'))
    _suppress_tasks.add(task)
    task.add_done_callback(_suppress_tasks.discard)

    if (not q) or q == '{arg}':
        old_q = q
        q = DEFAULT_EMPTY_QUERY
        log.warning(f'smartplay empty/literal query:{old_q!r}, fallback to default:{q}')

    try:
        # requests 是同步库，放到线程里，避免阻塞 xiaomusic 事件循环和后台打断任务。
        text = await asyncio.to_thread(_call_music_agent, q)
        log.info(f'smartplay query:{q} response:{text}')
    except Exception as e:
        log.exception(f'smartplay query:{q} failed: {e}')
