[README.md](https://github.com/user-attachments/files/31553564/README.md)
# CNC +1M France → MDBList → Stremio

Ce projet maintient une liste statique MDBList de films ayant réalisé plus d'un million d'entrées en France, auxquels il retire les films de ta liste « vus ». Elle ne consomme donc aucune de tes listes dynamiques MDBList.

Le fichier de référence est le jeu de données officiel du [CNC publié sur data.gouv.fr](https://www.data.gouv.fr/datasets/films-ayant-realise-plus-dun-million-dentrees). Il est téléchargé à chaque exécution. Les recherches utilisent l'endpoint officiel [TMDb Search Movie](https://developer.themoviedb.org/reference/search-movie), puis les listes sont lues et modifiées avec l'[API MDBList](https://api.mdblist.com/docs/). MDBList propose bien l'intégration officielle **Stremio Lists** avec rafraîchissement automatique : [Apps & Integrations](https://mdblist.com/apps/).

## Installation locale

```bash
cd cnc-million-stremio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Renseigne ensuite `.env` :

- `TMDB_API_TOKEN` : crée un jeton **API Read Access Token** depuis [TMDb → API](https://www.themoviedb.org/settings/api), ou utilise une clé API v3. Le script détecte le format et utilise automatiquement `Authorization: Bearer` pour v4 ou `api_key` pour v3.
- `MDBLIST_API_KEY` : récupère la clé dans [MDBList Preferences](https://mdblist.com/preferences/).
- `MDBLIST_WATCHED_LIST_ID` : facultatif. S'il est vide, le script vérifie directement l'état **Watched** de chaque film via l'API MDBList (`POST /sync/state/movie/tmdb`). Sinon, indique l'identifiant numérique ou le chemin `utilisateur/nom-de-liste` d'une liste MDBList contenant tes films vus.
- `MDBLIST_OUTPUT_LIST_ID` : l'identifiant numérique, ou le chemin `utilisateur/nom-de-liste`, d'une liste **statique**.

Pour utiliser automatiquement l'historique Trakt à la place de MDBList pour les films vus, renseigne `TRAKT_CLIENT_ID` et `TRAKT_USERNAME`. Ce mode nécessite un abonnement **Trakt VIP**, car Trakt réserve la création d'applications API aux membres VIP. Sans VIP, laisse ces variables vides : le script vérifie alors l'état Watched MDBList. Le client ID se crée dans [Trakt → Your API Apps](https://trakt.tv/oauth/applications). `TRAKT_LIST_SLUG` est facultatif si tu préfères une liste Trakt personnalisée. Quand le mode Trakt est configuré, `MDBLIST_WATCHED_LIST_ID` n'est pas utilisé.

Pour trouver les deux valeurs, ouvre la liste dans MDBList : utilise l'ID numérique d'une liste statique ou le chemin `utilisateur/nom-de-liste` visible dans son URL. Ils doivent impérativement être différents. Une URL de preset comme `movies/?preset=7095` n'est pas une liste et ne peut pas être utilisée ici.

Lance un premier contrôle sans changer quoi que ce soit :

```bash
python sync.py --dry-run
```

Si `reports/unresolved.json` n'est pas vide, le programme s'arrête volontairement **avant tout changement MDBList**. Pour valider un candidat, crée `overrides.json` depuis `overrides.example.json`, puis ajoute une entrée avec la clé `title normalized|year` affichée dans le rapport et l'ID TMDb choisi. Relance ensuite la vérification. Il n'existe pas de mode partiel : un titre ambigu ou absent bloque toute mise à jour, afin qu'aucun film précédemment présent ne soit retiré silencieusement.

Quand le contrôle est propre :

```bash
python sync.py
```

Le script lit les listes « vus » et de sortie, calcule `CNC − vus`, puis ajoute et retire seulement la différence. Les rapports JSON restent sous `reports/` et sont ignorés par Git.

## Automatisation GitHub Actions

Le workflow [`.github/workflows/weekly-sync.yml`](.github/workflows/weekly-sync.yml) tourne chaque jour à 07:17 UTC et peut aussi être lancé à la demande. Il compare la date `last_update` du jeu de données CNC avec `cnc-state.json`. Si la date CNC change, il reconstruit le catalogue TMDb résolu dans `cnc-catalog.json`. À chaque exécution, même sans changement CNC, il relit les statuts Watched MDBList et met à jour la liste de sortie. Après une reconstruction réussie, la nouvelle date et le catalogue sont enregistrés automatiquement.

Dans le dépôt GitHub, ajoute ces secrets dans **Settings → Secrets and variables → Actions** :

- `TMDB_API_TOKEN`
- `MDBLIST_API_KEY`
- `MDBLIST_WATCHED_LIST_ID`
- `MDBLIST_OUTPUT_LIST_ID`
- `SMTP_USERNAME`
- `SMTP_PASSWORD` (mot de passe d'application Gmail, pas le mot de passe du compte)

En cas d'ambiguïté, l'exécution échoue sans toucher MDBList, envoie `reports/unresolved.json` à l'adresse email définie et publie `cnc-sync-report` comme artefact téléchargeable. Après une mise à jour CNC réussie, un email de confirmation est également envoyé à cette adresse. Télécharge le rapport, complète `overrides.json`, puis envoie ce fichier dans le dépôt avant de relancer le workflow.

## Stremio

Dans MDBList, ouvre la liste statique de sortie puis utilise son bouton d'intégration **Stremio Lists / Add to Stremio**. L'intégration officielle ajoute ce catalogue à Stremio et le rafraîchit automatiquement. Le script n'a pas besoin des identifiants Stremio et ne reçoit jamais ces données.

## Notes de sûreté

- Le format du XLSX CNC est détecté à partir de ses en-têtes. Si le CNC modifie fortement sa structure, le fichier téléchargé est conservé dans le rapport afin de faciliter l'adaptation.
- Une correspondance TMDb est acceptée automatiquement lorsqu'une recherche renvoie un seul candidat. Les recherches avec plusieurs candidats, ou sans résultat, sont signalées pour validation humaine.
- Ne versionne jamais `.env`; il est exclu par `.gitignore`.
