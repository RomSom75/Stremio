#!/usr/bin/env python3
"""One-way watched-status and latest-date sync from Trakt. Standard library only."""
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
                        resume_at = datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
                        raise SyncError(f'{self.name}: limite HTTP {exc.code}; reprise possible après {resume_at} (UTC).') from None
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


def watched_date(value, service):
    """Compare instants, so UTC offsets and fractional formatting do not cause writes."""
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise SyncError(f'{service}: date de visionnage invalide.') from None


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
        parsed = watched_date(row.get('watched_at'), 'Trakt')
        if tmdb not in items or parsed > watched_date(items[tmdb]['watched_at'], 'Trakt'):
            items[tmdb] = {'ids': {'tmdb': tmdb}, 'watched_at': parsed.isoformat()}
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
        date = row.get('last_watched_at')
        found[key] = {
            'watched': row['watched'],
            'last_watched_at': watched_date(date, 'MDBList') if date is not None else None,
        }
    missing = data.get('not_found', [])
    try:
        unknown = {int(item) for item in missing}
    except (TypeError, ValueError):
        raise SyncError('MDBList: liste des identifiants inconnus invalide.') from None
    if set(found) | unknown != set(ids) or set(found) & unknown:
        raise SyncError('MDBList: réponse de statuts incomplète.')
    return found, unknown


def matches(state, item):
    return bool(state and state['watched']
                and state['last_watched_at'] == watched_date(item['watched_at'], 'Trakt'))


def watched_snapshot(api, kind):
    """Read the lightweight watched catalog in pages of 1,000, not 100 IDs."""
    found, cursors = {}, set()
    params = {'mediatype': kind, 'extended': 'ids_only', 'limit': 1000}
    for _ in range(10000):
        data, headers = api.call('/sync/watched', params=params)
        if not isinstance(data, dict) or not isinstance(data.get(kind + 's'), list):
            raise SyncError('MDBList: format du catalogue vu inattendu.')
        for row in data[kind + 's']:
            if not isinstance(row, dict):
                raise SyncError('MDBList: entrée du catalogue vu invalide.')
            key = row.get('tmdb')
            # Some MDBList titles have no TMDB mapping; they cannot match Trakt IDs.
            if key is None:
                continue
            if type(key) is not int or key <= 0:
                raise SyncError('MDBList: identifiant TMDB du catalogue invalide.')
            date = row.get('last_watched_at')
            state = {'watched': True,
                     'last_watched_at': watched_date(date, 'MDBList') if date is not None else None}
            # Concurrent watches can repeat an item across cursor pages; keep the latest.
            previous = found.get(key)
            if (previous is None or previous['last_watched_at'] is None
                    or (state['last_watched_at'] is not None
                        and state['last_watched_at'] > previous['last_watched_at'])):
                found[key] = state
        pagination = data.get('pagination')
        if isinstance(pagination, dict) and 'next_cursor' in pagination:
            cursor = pagination['next_cursor']
        elif 'x-next-cursor' in headers:
            cursor = headers['x-next-cursor'] or None
        elif isinstance(pagination, dict) and all(k in pagination for k in ('total', 'offset', 'limit')):
            # MDBList can answer the first request in legacy offset mode, with
            # no cursor at all. Validate and follow the explicit page metadata.
            total, offset, limit = (pagination[k] for k in ('total', 'offset', 'limit'))
            rows = data[kind + 's']
            if (any(type(value) is not int for value in (total, offset, limit))
                    or total < 0 or limit <= 0 or offset != params.get('offset', 0)
                    or offset < 0 or offset > total or len(rows) > limit):
                raise SyncError('MDBList: pagination par position invalide; arrêt avant écriture.')
            next_offset = offset + len(rows)
            if next_offset >= total:
                return found
            if not rows or 'cursor' in params:
                raise SyncError('MDBList: catalogue incomplet ou mode de pagination incohérent.')
            params = {**params, 'offset': next_offset}
            continue
        elif headers.get('x-has-more', '').lower() == 'false':
            return found
        elif headers.get('x-has-more', '').lower() == 'true':
            rows = data[kind + 's']
            if not rows or 'cursor' in params:
                raise SyncError('MDBList: page vide ou mode de pagination incohérent.')
            params = {**params, 'offset': params.get('offset', 0) + len(rows)}
            continue
        else:
            keys = ', '.join(sorted(pagination)) if isinstance(pagination, dict) else 'aucune'
            raise SyncError(f'MDBList: pagination du catalogue absente (champs : {keys}); arrêt avant écriture.')
        if cursor is None or cursor == '':
            return found
        if not isinstance(cursor, str) or cursor in cursors:
            raise SyncError('MDBList: curseur de pagination invalide ou répété.')
        if not data[kind + 's']:
            raise SyncError('MDBList: page vide avec une suite; catalogue incomplet.')
        cursors.add(cursor)
        params = {**params, 'cursor': cursor}
    raise SyncError('MDBList: trop de pages dans le catalogue vu.')


def plan(api, kind, items):
    if not items:
        return []
    found = watched_snapshot(api, kind)
    return [item for item in items if not matches(found.get(item['ids']['tmdb']), item)]


def has_errors(value):
    """Empty per-media buckets do not represent an API error."""
    if isinstance(value, dict):
        return any(has_errors(item) for item in value.values())
    return bool(value)


def apply(api, kind, items, strict=True, unresolved=None):
    verified = 0
    for batch in chunks(items):
        # Re-read before writing: a concurrent sync may already have aligned a date.
        found, _ = states(api, kind, [i['ids']['tmdb'] for i in batch])
        pending = [i for i in batch if not matches(found.get(i['ids']['tmdb']), i)]
        # MDBList ignores backdated writes while a newer watched status exists.
        # Reset only that status, never delete the play history or a whole show.
        reset = []
        for item in pending:
            current = found.get(item['ids']['tmdb'])
            if (current and current['watched'] and current['last_watched_at'] is not None
                    and current['last_watched_at'] > watched_date(item['watched_at'], 'Trakt')):
                reset.append({'ids': item['ids']})
        if reset:
            data, _ = api.call('/sync/watched/remove', body={kind + 's': reset}, retry_safe=False)
            # Summary counters vary; the authoritative check is the item state below.
            if not isinstance(data, dict):
                raise SyncError('MDBList: réponse de remise à zéro invalide; relancer la synchronisation.')
            reset_state, _ = states(api, kind, [i['ids']['tmdb'] for i in reset])
            if any(i['ids']['tmdb'] not in reset_state or reset_state[i['ids']['tmdb']]['watched'] for i in reset):
                raise SyncError('MDBList: ancienne date toujours présente; relancer la synchronisation.')
        if pending:
            data, _ = api.call('/sync/watched', body={kind + 's': pending}, retry_safe=False)
            if not isinstance(data, dict) or 'updated' not in data:
                raise SyncError('MDBList: réponse d’écriture inattendue; relancer pour revérifier les dates.')
            if strict and (has_errors(data.get('errors')) or has_errors(data.get('not_found'))):
                raise SyncError('MDBList: certains éléments n’ont pas été résolus.')
            found, _ = states(api, kind, [i['ids']['tmdb'] for i in batch])
        else:
            data = {}
        remaining = [i['ids']['tmdb'] for i in batch if not matches(found.get(i['ids']['tmdb']), i)]
        verified += len(batch) - len(remaining)
        print(f'{kind}: {verified}/{len(items)} statuts et dates confirmés.', flush=True)
        if remaining:
            if strict:
                raise SyncError('MDBList: statut ou date de visionnage non confirmé pour certains éléments.')
            if unresolved is not None:
                unresolved.extend((kind, item_id) for item_id in remaining)
            print(f'::warning::{kind}: statuts ou dates non confirmés pour les identifiants TMDB '
                  f'{remaining}. Ces éléments seront retentés au prochain passage.', flush=True)
        elif has_errors(data.get('errors')):
            raise SyncError('MDBList: erreurs signalées malgré des dates confirmées; vérification nécessaire.')
    return verified


def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise SyncError(f'Configuration manquante : {name}')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Synchroniser les statuts et dates depuis Trakt (sinon simulation).')
    args = parser.parse_args()
    username = required('TRAKT_USERNAME')
    trakt = API('Trakt', 'https://api.trakt.tv', {
        'trakt-api-version': '2', 'trakt-api-key': required('TRAKT_CLIENT_ID')})
    mdb = API('MDBList', 'https://api.mdblist.com', key=required('MDBLIST_API_KEY'))
    cutoff = datetime.now(timezone.utc).isoformat()
    plans, skipped = {}, 0
    unresolved, confirmed = [], {}
    # Finish all reads and validation before making any watched-state changes.
    for kind, plural in [('movie', 'movies'), ('episode', 'episodes')]:
        source = read_history(trakt, username, plural, cutoff)
        items, omitted = normalize(source, kind)
        skipped += omitted
        plans[kind] = plan(mdb, kind, items)
        print(f'{kind}: {len(items)} éléments uniques; {len(plans[kind])} à ajouter ou corriger; '
              f'{omitted} entrées sans identifiant TMDB.', flush=True)
    mode = 'Synchronisation' if args.apply else 'Simulation sans écriture'
    print(mode, flush=True)
    if args.apply:
        for kind, items in plans.items():
            confirmed[kind] = apply(mdb, kind, items, strict=False, unresolved=unresolved)
    lines = [f'## {mode}', '', *[f'- {kind}: {len(items)} statuts/dates à ajouter ou corriger' for kind, items in plans.items()],
             f'- Entrées ignorées sans identifiant TMDB : {skipped}']
    if args.apply:
        lines.extend(f'- {kind}: {count} statuts et dates confirmés' for kind, count in confirmed.items())
    if unresolved:
        lines.append('- Non synchronisés (type, identifiant TMDB) : ' + str(unresolved))
    if skipped or unresolved:
        lines.append('\n**Résultat partiel : les éléments ci-dessus restent non synchronisés.**')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('\n'.join(lines) + '\n')
    if skipped:
        print(f'::warning::{skipped} entrées Trakt ignorées sans identifiant TMDB.', flush=True)
    print('Terminé avec avertissements : certains éléments restent non synchronisés.'
          if skipped or unresolved else 'Terminé.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except SyncError as exc:
        print(f'ERREUR : {exc}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('ERREUR : réponse inattendue; arrêt sans afficher de données privées.', file=sys.stderr)
        sys.exit(1)
