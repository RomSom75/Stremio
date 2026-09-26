#!/usr/bin/env python3
"""One-way watched-status sync. Python standard library only."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class SyncError(Exception):
    pass


class API:
    def __init__(self, name, base, headers=None, key=None):
        self.name, self.base, self.headers, self.key = name, base, headers or {}, key

    def call(self, path, params=None, body=None, retry_safe=True):
        params = dict(params or {})
        if self.key:
            params['apikey'] = self.key
        url = self.base + path + ('?' + urlencode(params) if params else '')
        data = None if body is None else json.dumps(body).encode()
        headers = {'Content-Type': 'application/json', 'User-Agent': 'personal-watched-sync/1.0', **self.headers}
        for attempt in range(4):
            try:
                with urlopen(Request(url, data=data, headers=headers), timeout=45) as response:
                    result = json.load(response)
                    return result, {k.lower(): v for k, v in response.headers.items()}
            except HTTPError as exc:
                # Never expose URLs, response bodies or credentials in workflow logs.
                if retry_safe and exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    try:
                        delay = float(exc.headers.get('Retry-After', 2 ** (attempt + 1)))
                    except ValueError:
                        delay = 2 ** (attempt + 1)
                    if delay > 60:
                        raise SyncError(f'{self.name}: quota temporairement atteint. Relancer plus tard.') from None
                    time.sleep(max(1, delay))
                    continue
                raise SyncError(f'{self.name}: erreur HTTP {exc.code}. Vérifier les accès et quotas.') from None
            except (URLError, TimeoutError, OSError):
                if retry_safe and attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise SyncError(f'{self.name}: connexion interrompue. Relancer la tâche.') from None
            except (ValueError, UnicodeError):
                raise SyncError(f'{self.name}: réponse JSON invalide.') from None


def chunks(items, size=100):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def read_history(api, username, media, cutoff):
    """Read every page; honour server pagination, including clamped page sizes."""
    rows = []
    for page in range(1, 10001):
        data, headers = api.call(f'/users/{quote(username, safe="")}/history/{media}',
                                 {'page': page, 'limit': 100, 'end_at': cutoff, 'extended': 'full'})
        if not isinstance(data, list):
            raise SyncError('Trakt: format historique inattendu.')
        try:
            pages = int(headers['x-pagination-page-count'])
            actual = int(headers['x-pagination-page'])
        except (KeyError, ValueError):
            raise SyncError('Trakt: pagination absente ou invalide; synchronisation arrêtée.') from None
        if pages < 0 or actual != page or (not data and page < pages):
            raise SyncError('Trakt: pagination incohérente.')
        rows.extend(data)
        if page >= pages:
            return rows
    raise SyncError('Trakt: trop de pages; synchronisation arrêtée.')


def normalize(rows, kind):
    """Keep one latest watch per TMDB movie/episode, never a whole show."""
    items, skipped = {}, 0
    for row in rows:
        if not isinstance(row, dict):
            raise SyncError('Trakt: entrée historique invalide.')
        media = row.get(kind)
        if not isinstance(media, dict) or not isinstance(media.get('ids'), dict):
            raise SyncError('Trakt: identifiants manquants dans la réponse.')
        tmdb = media['ids'].get('tmdb')
        if not isinstance(tmdb, int) or isinstance(tmdb, bool) or tmdb <= 0:
            skipped += 1
            continue
        date = row.get('watched_at')
        try:
            parsed = datetime.fromisoformat(date.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise SyncError('Trakt: date de visionnage invalide.') from None
        canonical = parsed.astimezone(timezone.utc).isoformat()
        if tmdb not in items or canonical > items[tmdb]['watched_at']:
            items[tmdb] = {'ids': {'tmdb': tmdb}, 'watched_at': canonical}
    return list(items.values()), skipped


def states(api, kind, ids):
    data, _ = api.call(f'/sync/state/{kind}/tmdb', body={'ids': ids})
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise SyncError('MDBList: format de statuts inattendu.')
    found = {}
    for row in data['items']:
        if not isinstance(row, dict) or type(row.get('watched')) is not bool:
            raise SyncError('MDBList: statut invalide.')
        try:
            key = int(row['id'])
        except (KeyError, TypeError, ValueError):
            raise SyncError('MDBList: identifiant invalide.') from None
        if key not in ids or key in found:
            raise SyncError('MDBList: identifiants incohérents.')
        found[key] = row['watched']
    missing = data.get('not_found', [])
    try:
        unknown = {int(item) for item in missing}
    except (TypeError, ValueError):
        raise SyncError('MDBList: liste des identifiants inconnus invalide.') from None
    if set(found) | unknown != set(ids) or set(found) & unknown:
        raise SyncError('MDBList: réponse de statuts incomplète.')
    return found, unknown


def plan(api, kind, items):
    pending = []
    for batch in chunks(items):
        found, _ = states(api, kind, [i['ids']['tmdb'] for i in batch])
        pending.extend(i for i in batch if not found.get(i['ids']['tmdb'], False))
    return pending


def apply(api, kind, items):
    verified = 0
    for batch in chunks(items):
        # Do not automatically retry writes after an ambiguous network failure.
        # The next run checks the remote state again before writing.
        data, _ = api.call('/sync/watched', body={kind + 's': batch}, retry_safe=False)
        if not isinstance(data, dict) or 'updated' not in data:
            raise SyncError('MDBList: réponse d’écriture inattendue; relancer pour revérifier les statuts.')
        if data.get('errors') or any(data.get('not_found', {}).values()):
            raise SyncError(
                f'MDBList: réponse partielle pour le lot {kind}. '
                f'Identifiants TMDB envoyés : {[i["ids"]["tmdb"] for i in batch]}. '
                'Certains peuvent avoir été ajoutés; les autres restent à vérifier.'
            )
        found, unknown = states(api, kind, [i['ids']['tmdb'] for i in batch])
        if unknown or not all(found.values()):
            raise SyncError('MDBList: écriture non confirmée pour certains éléments; relancer plus tard.')
        verified += len(batch)
        print(f'{kind}: {verified}/{len(items)} ajouts confirmés.', flush=True)
    return verified


def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise SyncError(f'Configuration manquante : {name}')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Écrire les statuts (sinon simulation).')
    args = parser.parse_args()
    username = required('TRAKT_USERNAME')
    trakt = API('Trakt', 'https://api.trakt.tv', {
        'trakt-api-version': '2', 'trakt-api-key': required('TRAKT_CLIENT_ID')})
    mdb = API('MDBList', 'https://api.mdblist.com', key=required('MDBLIST_API_KEY'))
    cutoff = datetime.now(timezone.utc).isoformat()
    plans, skipped = {}, 0
    # Finish all reads and validation before making any watched-state changes.
    for kind, plural in [('movie', 'movies'), ('episode', 'episodes')]:
        source = read_history(trakt, username, plural, cutoff)
        items, omitted = normalize(source, kind)
        skipped += omitted
        plans[kind] = plan(mdb, kind, items)
        print(f'{kind}: {len(items)} éléments uniques; {len(plans[kind])} à ajouter; '
              f'{omitted} entrées sans identifiant TMDB.', flush=True)
    mode = 'Synchronisation' if args.apply else 'Simulation sans écriture'
    print(mode, flush=True)
    if args.apply:
        for kind, items in plans.items():
            apply(mdb, kind, items)
    lines = [f'## {mode}', '', *[f'- {kind}: {len(items)} statuts à ajouter' for kind, items in plans.items()],
             f'- Entrées ignorées sans identifiant TMDB : {skipped}']
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('\n'.join(lines) + '\n')
    if skipped:
        raise SyncError('Synchronisation incomplète : certaines entrées Trakt n’ont pas d’identifiant TMDB.')
    print('Terminé.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except SyncError as exc:
        print(f'ERREUR : {exc}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('ERREUR : réponse inattendue; arrêt sans afficher de données privées.', file=sys.stderr)
        sys.exit(1)
