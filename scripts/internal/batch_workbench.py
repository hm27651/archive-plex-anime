"""Material-first batch suggestions; identities always belong to individual files."""
from pathlib import Path
import re

from internal.source_grouping import season_hint


def describe_batch(inventory: dict, work: Path, branch: str, scope: dict) -> dict:
    from tv_plan import source_episode
    allowed = {row['path'] for row in scope.get('files', [])}
    targets = []
    for file in inventory['files']:
        path = Path(file['id'])
        file['season'] = season_hint(f'{work.name}/{file["id"]}')
        file['episode'] = source_episode(path)
        file['content_kind'] = 'extra' if file['season'] == 0 or re.search(r'(?:^|[ ._\[(-])(?:NCOP|NCED|PV|CM|SP|Menu)(?:\d|[ ._\])-]|$)', path.stem, re.I) else 'main'
        if file['kind'] != 'video' or (allowed and file['id'] not in allowed):
            continue
        audio = [{'source': file['id'], 'track': track['id'], **({'title': channel_title(track)} if branch == 'movie' else {})} for track in file['tracks'] if track['type'] == 'audio']
        subtitles = [{'source': file['id'], 'track': track['id'], 'group': track.get('group', ''), 'subtitle_type': track.get('subtitle_type', ''), 'title': track.get('title', ''), 'language': track['language'], 'preserve': True} for track in file['tracks'] if track['type'] == 'subtitles']
        part = re.search(r'(?:^|[ ._-])(cd\d+)(?=$|[ ._-])', path.stem, re.I)
        targets.append({'included': branch != 'tv' or file['content_kind'] != 'extra', 'id': f'item-{len(targets)+1}', 'video': file['id'], 'part': part[1].lower() if part and branch == 'movie' else '', 'episode': f'S{file["season"]:02d}E{file["episode"]:02d}' if file['episode'] is not None and branch == 'tv' else '', 'audio': audio, 'default_audio': audio[0] if audio else None, 'subtitles': subtitles})
    inventory['suggested_targets'] = targets
    return inventory


def same_track(left: dict | None, right: dict | None) -> bool:
    return bool(left and right and (left.get('source'), left.get('track')) == (right.get('source'), right.get('track')))


def channel_title(track: dict) -> str:
    return {1:'1.0ch',2:'2.0ch',6:'5.1ch',8:'7.1ch'}.get(track.get('channels'), str(track.get('channels') or '')+'ch')


def track_properties(original: dict, choice: dict, *, name: str, default: bool) -> dict:
    """Explicit overrides win; source evidence is never mutated."""
    result = {**original, 'title': choice.get('title', name), 'language': choice.get('language') or original.get('language') or 'und', 'default': bool(choice.get('default', default)), 'forced': bool(choice.get('forced', original.get('forced', False)))}
    if not isinstance(result['title'], str) or not isinstance(result['language'], str) or not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', result['language']):
        raise ValueError('轨道名称或语言格式无效。')
    return result
