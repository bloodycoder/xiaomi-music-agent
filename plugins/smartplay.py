import asyncio
import os
import requests


DEFAULT_EMPTY_QUERY = os.environ.get('SMARTPLAY_DEFAULT_QUERY', 'kkecho歌单')
# With the local ffplay/mpv backend, Xiaomi Sound is used as a Bluetooth speaker.
# Sending Xiaomi cloud player_pause/player_stop after playback starts can also
# silence the Bluetooth output. Therefore smartplay suppression is ON by default, but only as a one-shot pre-stop before
# Agent playback starts; disable with SMARTPLAY_SUPPRESS_XIAOAI=0 if it ever
# interferes with Bluetooth audio.
SMARTPLAY_SUPPRESS_XIAOAI = os.environ.get('SMARTPLAY_SUPPRESS_XIAOAI', '1').lower() in ('1', 'true', 'yes', 'on')

async def _cancel_xiaomusic_local_loop(reason=''):
    global xiaomusic, log
    try:
        did = xiaomusic.get_cur_did()
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return
        await device.cancel_group_next_timer()
        device.is_playing = False
        try:
            device._last_cmd = 'smart_agent'
        except Exception:
            pass
        log.info(f'cancel_xiaomusic_local_loop ok reason:{reason}')
    except Exception as e:
        try:
            log.warning(f'cancel_xiaomusic_local_loop failed reason:{reason} err:{e}')
        except Exception:
            pass


def _call_music_agent(q):
    r = requests.get('http://127.0.0.1:8765/play', params={'q': q}, timeout=90)
    r.raise_for_status()
    return r.text


async def _suppress_xiaoai_once(did, reason=''):
    """Best-effort one-shot stop before playback starts.

    Do not schedule delayed stop calls for smartplay. Once ffplay/mpv has started
    and Mac audio is routed to Xiaomi Sound over Bluetooth, a late Xiaomi cloud
    stop can make the speaker go silent even though the Agent is playing.
    """
    global log, xiaomusic
    if not SMARTPLAY_SUPPRESS_XIAOAI:
        return
    try:
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return
        device_id_list = xiaomusic.device_manager.get_group_device_id_list(device.group_name)

        async def quick_stop(device_id):
            service = device.auth_manager.mina_service
            calls = (service.player_pause(device_id), service.player_stop(device_id))
            results = await asyncio.gather(
                *(asyncio.wait_for(call, timeout=0.8) for call in calls),
                return_exceptions=True,
            )
            log.info(f'smartplay pre-stop XiaoAI device_id:{device_id} results:{results}')

        log.info(f'smartplay suppress official XiaoAI answer once before playback: {reason}')
        await asyncio.wait_for(
            asyncio.gather(*(quick_stop(device_id) for device_id in device_id_list), return_exceptions=True),
            timeout=1.0,
        )
    except Exception as e:
        log.warning(f'smartplay suppress skipped/failed: {e}')


async def smartplay(query):
    global log, xiaomusic
    q = (query or '').strip()
    did = xiaomusic.get_cur_did()

    if (not q) or q == '{arg}':
        old_q = q
        q = DEFAULT_EMPTY_QUERY
        log.warning(f'smartplay empty/literal query:{old_q!r}, fallback to default:{q}')

    await _cancel_xiaomusic_local_loop('smartplay')

    # Optional one-shot suppression before playback request only. No delayed
    # background stop after Agent returns, otherwise it can mute Bluetooth audio.
    await _suppress_xiaoai_once(did, 'enter smartplay')

    try:
        # requests 是同步库，放到线程里，避免阻塞 xiaomusic 事件循环。
        text = await asyncio.to_thread(_call_music_agent, q)
        log.info(f'smartplay query:{q} response:{text}')
    except Exception as e:
        log.exception(f'smartplay query:{q} failed: {e}')
