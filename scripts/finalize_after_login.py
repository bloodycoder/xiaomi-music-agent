#!/usr/bin/env python3
import json, subprocess, sys, time, urllib.parse, urllib.request

BASE = 'http://127.0.0.1:8090'

def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))

def post_json(path, data):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=body, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode('utf-8', 'ignore')
        try:
            return json.loads(raw)
        except Exception:
            return raw

def main():
    data = get_json('/getsetting?need_device_list=true')
    devices = data.get('device_list') or []
    print('device_count =', len(devices))
    if not devices:
        print('ERROR: no devices. Please login in web UI first.')
        sys.exit(2)
    print(json.dumps(devices, ensure_ascii=False, indent=2))

    target = None
    for d in devices:
        txt = json.dumps(d, ensure_ascii=False)
        if 'Sound' in txt or 'sound' in txt or 'L16A' in txt:
            target = d
            break
    if not target:
        target = devices[0]

    did = target.get('did') or target.get('miotDID') or target.get('deviceID') or target.get('deviceId')
    print('chosen device =', json.dumps(target, ensure_ascii=False))
    print('chosen did =', did)
    if not did:
        print('ERROR: no did in chosen device')
        sys.exit(3)

    # First try command-based local play
    ret = post_json('/cmd', {'did': did, 'cmd': '播放歌曲test-tone'})
    print('cmd ret =', ret)
    time.sleep(4)
    try:
        status = get_json('/getplayerstatus?did=' + urllib.parse.quote(str(did)))
        print('player status =', json.dumps(status, ensure_ascii=False))
    except Exception as e:
        print('status err =', e)

    # Also try push URL as fallback if cmd path doesn't visibly start
    local_url = 'http://127.0.0.1:8090/music/test-tone.wav'
    try:
        from urllib.parse import quote
        with urllib.request.urlopen(BASE + '/playurl?did=' + quote(str(did)) + '&url=' + quote(local_url, safe=''), timeout=20) as r:
            raw = r.read().decode('utf-8', 'ignore')
            print('playurl ret =', raw)
    except Exception as e:
        print('playurl err =', e)

    time.sleep(4)
    try:
        status = get_json('/getplayerstatus?did=' + urllib.parse.quote(str(did)))
        print('final player status =', json.dumps(status, ensure_ascii=False))
    except Exception as e:
        print('final status err =', e)

if __name__ == '__main__':
    main()
