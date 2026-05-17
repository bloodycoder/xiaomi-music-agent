#!/usr/bin/env python3
"""Build a local weighted alias table for Netease playlists.

The generated JSON is intentionally editable. Music Agent reads it at request
runtime, so you can add ASR variants without code changes.
"""
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
PLAYLISTS_FILE = ROOT / 'runtime' / 'playlists.json'
ALIASES_FILE = ROOT / 'runtime' / 'playlist_aliases.json'


def norm_text(text):
    text = (text or '').lower()
    text = text.replace('＆', '&').replace('（', '(').replace('）', ')')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


def add_alias(items, text, weight=800, reason='manual'):
    text = (text or '').strip()
    if not text:
        return
    key = norm_text(text)
    if not key:
        return
    old = items.get(key)
    if old is None or weight > old['weight']:
        items[key] = {'text': text, 'weight': weight, 'reason': reason}


def split_name_parts(name):
    parts = [name]
    # Text inside brackets is often what the user says.
    for m in re.finditer(r'[【\[「(（]([^】\]」)）]+)[】\]」)）]', name):
        parts.append(m.group(1))
    # Text before separators is also useful.
    for sep in ['丨', '|', '/', '：', ':', '-', '—']:
        if sep in name:
            parts.extend([x.strip() for x in name.split(sep) if x.strip()])
    # Remove bracketed decorations.
    cleaned = re.sub(r'[【\[「(（][^】\]」)）]+[】\]」)）]', ' ', name).strip()
    if cleaned and cleaned != name:
        parts.append(cleaned)
    return parts


LETTER_CN = {
    'a': ['a', '诶'], 'b': ['b', '逼', '比'], 'c': ['c', '西'], 'd': ['d', '弟'],
    'e': ['e', '一', '伊'], 'f': ['f', '艾弗'], 'g': ['g', '基', '鸡'], 'h': ['h', '艾尺'],
    'i': ['i', '爱'], 'j': ['j', '杰'], 'k': ['k', '开', '凯'], 'l': ['l', '艾勒'],
    'm': ['m', '艾姆'], 'n': ['n', '恩'], 'o': ['o', '欧'], 'p': ['p', '屁'],
    'q': ['q', '扣'], 'r': ['r', '阿尔'], 's': ['s', '艾斯'], 't': ['t', '踢'],
    'u': ['u', '优'], 'v': ['v', '微', '威'], 'w': ['w', '达不溜'], 'x': ['x', '艾克斯'],
    'y': ['y', '歪'], 'z': ['z', '贼德'],
}


def add_ascii_variants(items, token, base_weight=760):
    t = (token or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9]{1,24}', t):
        return
    add_alias(items, t, base_weight, 'ascii normalized')
    if len(t) <= 8:
        add_alias(items, ' '.join(t), base_weight - 20, 'ascii letters spaced')
    # Helpful for short all-caps playlists like VG/P4.
    if 1 <= len(t) <= 4 and t.isalnum():
        cn = []
        for ch in t:
            if ch.isdigit():
                cn.append(ch)
            else:
                cn.append(LETTER_CN.get(ch, [ch, ch])[1])
        add_alias(items, ''.join(cn), base_weight - 30, 'ascii letter chinese asr')


def build_aliases_for_playlist(pl):
    name = pl.get('name', '')
    aliases = {}
    add_alias(aliases, name, 1000, 'playlist name')
    add_alias(aliases, f'{name}歌单', 980, 'playlist name + 歌单')
    add_alias(aliases, f'播放{name}', 960, '播放 + playlist name')
    add_alias(aliases, f'放{name}', 940, '放 + playlist name')

    for part in split_name_parts(name):
        add_alias(aliases, part, 900, 'name part')
        add_alias(aliases, f'{part}歌单', 880, 'name part + 歌单')
        for tok in re.findall(r'[A-Za-z0-9]+', part):
            add_ascii_variants(aliases, tok, 780)

    low = name.lower()
    # Curated semantic aliases for this specific user's list.
    manual = {
        '液体human喜欢的音乐': ['我喜欢的音乐', '喜欢的音乐', '我喜欢', '红心歌单', '收藏的歌', '默认歌单'],
        '国语': ['中文歌', '华语', '华语歌', '国语歌', '中文', '中文歌单'],
        'cooking': ['做饭', '做饭歌单', '烧饭', '煮饭', '厨房音乐', '烹饪音乐', 'cooking歌单'],
        '助眠钢琴': ['睡前钢琴', '钢琴助眠', '下雨钢琴', '雨天钢琴', '安静钢琴', '钢琴曲'],
        '写作': ['写作音乐', '码字', '码字音乐', '工作音乐', '专注音乐', '学习音乐'],
        '助眠': ['睡觉', '睡眠', '助眠音乐', '晚安', '入睡', '下雨睡觉'],
        '运动': ['运动音乐', '跑步', '跑步音乐', '健身', '健身音乐', '锻炼'],
        '口琴': ['口琴音乐', '口琴曲'],
        'chunk Berry': ['chunk berry', 'chuck berry', '恰克贝里', '查克贝里', '强克贝里'],
        '放松BGM': ['放松', '放松音乐', '舒缓', '舒缓音乐', '背景音乐', 'bgm', '放松bgm'],
        'Memories Off': ['秋之回忆', 'memories off', 'memory off', '回忆系列'],
        'Gundam Rock': ['高达摇滚', '高达rock', 'gundam', '高达音乐'],
        'JOJO': ['jojo', 'jojo的奇妙冒险', '乔乔', '啾啾'],
        '特摄': ['特摄音乐', '奥特曼', '假面骑士'],
        '罗大佑': ['罗大佑歌单', '罗大佑的歌'],
        '小虎队': ['小虎队歌单', '小虎队的歌'],
        '怀旧': ['怀旧歌', '老歌', '经典老歌', '以前的歌'],
        'VG': ['vg', 'v g', '微基', '威基', '游戏音乐', '电子游戏音乐', '游戏原声'],
        '日漫': ['日漫音乐', '动漫', '动漫音乐', '动画音乐', '二次元', '番剧音乐'],
        '怀旧英语': ['英文老歌', '英语老歌', '怀旧英文', '怀旧英文歌', '英文歌'],
        'PERSONA4 Theme Songs 女神异闻录4 P4 P4G': ['女神异闻录4', 'p4', 'p4g', 'persona4', '女神异闻录'],
        '杀出重围3：人类革命 (O.S.T)': ['杀出重围', '杀出重围3', '人类革命'],
        '【中国功夫】豪气万丈，笑傲江湖': ['中国功夫', '功夫', '笑傲江湖', '武侠'],
        '剪辑音效3丨短音/转场/铃声/氛围/环境音': ['剪辑音效', '音效', '转场音效', '铃声', '环境音'],
        '上古卷轴5天际(游戏原声)': ['上古卷轴', '上古卷轴5', '天际', '老滚5'],
        '「死亡搁浅」音乐歌单': ['死亡搁浅', '死亡搁浅音乐'],
        '机动战士高达 闪光的哈萨维': ['闪光的哈萨维', '哈萨维'],
        '高达0093【逆袭的夏亚】': ['逆袭的夏亚', '夏亚', '高达0093'],
        '逆转裁判 全系列 询问·异议·追求曲': ['逆转裁判', '异议', '追求曲'],
        'Memories Off 秋之回忆全op': ['秋之回忆op', '秋之回忆全op'],
        '模拟人生3背景原声带（The Sims 3 OST）': ['模拟人生', '模拟人生3', 'sims3'],
        '荒野大镖客2 Red Dead Redemption（1&2）': ['荒野大镖客', '荒野大镖客2', '大表哥', 'red dead redemption'],
        '集合啦！动物森友会': ['动物森友会', '动森', '集合啦动物森友会'],
        '【辐射4】钻石城电台完全曲目': ['辐射4', '钻石城电台'],
        '写稿、码字、写代码、学习、论文专用歌单': ['写代码', '代码', '论文', '学习', '写稿', '码字歌单'],
        '辐射新维加斯广播电台': ['新维加斯', '辐射新维加斯', '新维加斯电台'],
        '辐射新维加斯莫哈维电台': ['莫哈维电台', '莫哈维', '辐射莫哈维'],
        'kkecho': ['kkecho', 'kk echo', 'k k echo', 'k歌', '开开echo', '开开一扣', '凯凯echo', 'echo', '艾克艾克echo'],
    }
    for key, vals in manual.items():
        if norm_text(key) == norm_text(name):
            for v in vals:
                add_alias(aliases, v, 860, 'curated alias')
                add_alias(aliases, f'{v}歌单', 840, 'curated alias + 歌单')

    # Add exact roman tokens from the full name.
    for tok in re.findall(r'[A-Za-z0-9]+', low):
        add_ascii_variants(aliases, tok, 760)

    return sorted(aliases.values(), key=lambda x: (-x['weight'], x['text']))


def main():
    data = json.loads(PLAYLISTS_FILE.read_text())
    playlists = data.get('playlists') or []
    out = {
        'version': 1,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'note': 'Editable weighted alias table. Higher weight wins. Music Agent normalizes punctuation/case when matching.',
        'playlists': [],
    }
    for pl in playlists:
        out['playlists'].append({
            'id': str(pl['id']),
            'name': pl.get('name',''),
            'count': pl.get('count', 0),
            'creator': pl.get('creator',''),
            'is_mine': bool(pl.get('is_mine')),
            'aliases': build_aliases_for_playlist(pl),
        })
    ALIASES_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(f'wrote {ALIASES_FILE} with {len(out["playlists"])} playlists and {sum(len(p["aliases"]) for p in out["playlists"])} aliases')


if __name__ == '__main__':
    main()
