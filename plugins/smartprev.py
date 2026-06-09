import asyncio
import os
import requests

SMART_CONTROL_SUPPRESS_XIAOAI = os.environ.get('SMART_CONTROL_SUPPRESS_XIAOAI', '1').lower() in ('1', 'true', 'yes', 'on')

async def _cancel_xiaomusic_local_loop(reason=''):
    global xiaomusic, log
    try:
        did = xiaomusic.get_cur_did()
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return did
        await device.cancel_group_next_timer()
        device.is_playing = False
        try:
            device._last_cmd = 'smart_agent'
        except Exception:
            pass
        log.info(f'cancel_xiaomusic_local_loop ok reason:{reason}')
        return did
    except Exception as e:
        try:
            log.warning(f'cancel_xiaomusic_local_loop failed reason:{reason} err:{e}')
        except Exception:
            pass
        try:
            return xiaomusic.get_cur_did()
        except Exception:
            return None

async def _suppress_xiaoai_once(did, reason=''):
    global xiaomusic, log
    if not SMART_CONTROL_SUPPRESS_XIAOAI or not did:
        return
    try:
        device = xiaomusic.device_manager.devices.get(did)
        if not device:
            return
        device_id_list = xiaomusic.device_manager.get_group_device_id_list(device.group_name)
        async def quick_stop(device_id):
            service = device.auth_manager.mina_service
            calls = (service.player_pause(device_id), service.player_stop(device_id))
            return await asyncio.gather(
                *(asyncio.wait_for(call, timeout=0.8) for call in calls),
                return_exceptions=True,
            )
        log.info(f'{reason} pre-stop possible official XiaoAI/native playback')
        await asyncio.wait_for(
            asyncio.gather(*(quick_stop(device_id) for device_id in device_id_list), return_exceptions=True),
            timeout=1.0,
        )
    except Exception as e:
        try:
            log.warning(f'{reason} xiaoai pre-stop skipped/failed: {e}')
        except Exception:
            pass

def _call_prev():
    r = requests.get('http://127.0.0.1:8765/prev', timeout=30)
    r.raise_for_status()
    return r.text


async def smartprev():
    global log
    did = await _cancel_xiaomusic_local_loop('smartprev')
    # One-shot pre-stop prevents Xiaomi native/official playback from becoming
    # a second sound source. No delayed stop is sent after Rust playback changes.
    await _suppress_xiaoai_once(did, 'smartprev')
    text = await asyncio.to_thread(_call_prev)
    log.info(f'smartprev response:{text}')
