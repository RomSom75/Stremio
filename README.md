[README.md](https://github.com/user-attachments/files/31553564/README.md)
# CNC +1M France → MDBList → Stremio

Ce projet maintient une liste statique MDBList de tous les films ayant réalisé plus d'un million d'entrées en France. Les films déjà vus restent dans la liste : le filtrage des visionnages est laissé à MDBList/Stremio. La synchronisation des listes thématiques supplémentaires a été supprimée.

Le fichier de référence est le jeu de données officiel du [CNC publié sur data.gouv.fr](https://www.data.gouv.fr/datasets/films-ayant-realise-plus-dun-million-dentrees). Il est téléchargé lorsqu’une nouvelle version CNC est détectée, ou avec `--force`. Les recherches utilisent l'endpoint officiel [TMDb Search Movie](https://developer.themoviedb.org/reference/search-movie), puis les listes sont lues et modifiées avec l'[API MDBList](https://api.mdblist.com/docs/). MDBList propose bien l'intégration officielle **Stremio Lists** avec rafraîchissement automatique : [Apps & Integrations](https://mdblist.com/apps/).

## Installation locale

```bash
cd cnc-million-stremio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
touch .env
```

Renseigne ensuite `.env` :

- `TMDB_API_TOKEN` : crée un jeton **API Read Access Token** depuis [TMDb → API](https://www.themoviedb.org/settings/api), ou utilise une clé API v3. Le script détecte le format et utilise automatiquement `Authorization: Bearer` pour v4 ou `api_key` pour v3.
- `MDBLIST_API_KEY` : récupère la clé dans [MDBList Preferences](https://mdblist.com/preferences/).
- `MDBLIST_OUTPUT_LIST_ID` : l'identifiant numérique, ou le chemin `utilisateur/nom-de-liste`, d'une liste **statique**.

Renseigne aussi `CNC_DATASET_URL` avec l'URL du fichier XLSX CNC (le workflow GitHub Actions la fournit déjà). Pour la liste de sortie, utilise son ID numérique ou le chemin `utilisateur/nom-de-liste` visible dans son URL MDBList. Une URL de preset comme `movies/?preset=7095` n'est pas une liste.

Lance un premier contrôle sans changer quoi que ce soit :

```bash
python sync.py --dry-run
```

Si `reports/unresolved.json` n'est pas vide, le programme s'arrête volontairement **avant tout changement MDBList**. Pour valider un candidat, complète `overrides.json` en ajoutant une entrée avec la clé `title normalized|year` affichée dans le rapport et l'ID TMDb choisi. Relance ensuite la vérification. Il n'existe pas de mode partiel : un titre ambigu ou absent bloque toute mise à jour, afin qu'aucun film précédemment présent ne soit retiré silencieusement.

Quand le contrôle est propre :

```bash
python sync.py
```

Le script compare le catalogue CNC complet à la liste de sortie, puis ajoute et retire seulement la différence. Aucun historique de visionnage n’est consulté. Les rapports JSON restent sous `reports/`. Utilise `python sync.py --force` pour reconstruire et resynchroniser le catalogue manuellement.

## Automatisation GitHub Actions

Le workflow [`.github/workflows/weekly-sync.yml`](.github/workflows/weekly-sync.yml) vérifie chaque jour à 07:17 UTC la date `last_update` du jeu de données CNC. Si elle change, il reconstruit le catalogue TMDb et met à jour la liste MDBList complète. Sans changement CNC, aucune lecture ou écriture MDBList n'est effectuée.

À la première exécution après cette modification, une remise à niveau réintègre les films déjà vus auparavant retirés, à partir du catalogue en cache si le CNC n'a pas changé. Le marqueur `include_watched` dans `cnc-state.json` permet de n'effectuer cette opération qu'une fois. La date, ce marqueur et le catalogue sont enregistrés après une synchronisation réussie. Le mode `--dry-run` ne les modifie pas.

Dans le dépôt GitHub, ajoute ces secrets dans **Settings → Secrets and variables → Actions** :

- `TMDB_API_TOKEN`
- `MDBLIST_API_KEY`
- `MDBLIST_OUTPUT_LIST_ID`
- `SMTP_USERNAME`
- `SMTP_PASSWORD` (mot de passe d'application Gmail, pas le mot de passe du compte)

En cas d'ambiguïté, l'exécution échoue sans toucher MDBList, envoie `reports/unresolved.json` à l'adresse email définie et publie `cnc-sync-report` comme artefact téléchargeable. Après une mise à jour CNC réussie, un email de confirmation est également envoyé à cette adresse. Télécharge le rapport, complète `overrides.json`, puis envoie ce fichier dans le dépôt avant de relancer le workflow.

## Stremio

Dans MDBList, ouvre la liste statique de sortie puis utilise son bouton d'intégration **Stremio Lists / Add to Stremio**. L'intégration officielle ajoute ce catalogue à Stremio et le rafraîchit automatiquement. Le script n'a pas besoin des identifiants Stremio et ne reçoit jamais ces données.

## Notes de sûreté

- Le format du XLSX CNC est détecté à partir de ses en-têtes. Si le CNC modifie fortement sa structure, le fichier téléchargé est conservé dans le rapport afin de faciliter l'adaptation.
- Une correspondance TMDb est acceptée automatiquement lorsqu'une recherche renvoie un seul candidat. Les recherches avec plusieurs candidats, ou sans résultat, sont signalées pour validation humaine.
- Ne versionne jamais `.env` ni les rapports locaux.

## Historique Trakt → MDBList

Le script indépendant `sync_trakt_mdblist.py` continue de synchroniser les films et épisodes vus depuis Trakt vers MDBList. Il nécessite `TRAKT_USERNAME`, `TRAKT_CLIENT_ID` et `MDBLIST_API_KEY`. Sa tâche planifiée à 03:23, heure de Paris, est activée si la variable GitHub Actions `TRAKT_SYNC_ENABLED` vaut `true`. Cette synchronisation ne retire aucun film de la liste CNC.
