# Succès au cinéma en France → MDBList → Stremio

Ce dépôt maintient une liste MDBList des films ayant réalisé plus d’un million d’entrées en France, à partir des données officielles du CNC. Cette liste peut ensuite être ajoutée comme catalogue dans Stremio.

**La liste conserve tous les films, y compris ceux déjà vus.** Le filtrage éventuel des films vus se règle dans MDBList/Stremio. Le script CNC ne consulte plus l’historique de visionnage.

## Fonctionnement

1. Chaque jour, le programme vérifie si le CNC a publié une nouvelle version de son jeu de données.
2. Si la version a changé, il télécharge le fichier Excel et retrouve les films dans TMDb.
3. Il synchronise la liste MDBList avec le catalogue CNC complet : ajout des films manquants et retrait des films qui ne figurent plus dans la source.
4. Sans nouvelle version CNC, aucune lecture ou modification de la liste MDBList n’est effectuée.

La première exécution après le changement de fonctionnement effectue une remise à niveau unique pour réintégrer les films précédemment retirés parce qu’ils étaient vus. Elle réutilise le catalogue enregistré si le CNC n’a pas changé.

La synchronisation Halloween et la gestion des listes thématiques supplémentaires ont été supprimées. L’ancienne variable `MDBLIST_ADDITIONAL_LISTS` et l’ancien secret `MDBLIST_WATCHED_LIST_ID` ne sont plus utilisés et peuvent être supprimés des paramètres GitHub.

## Configuration sur GitHub

Crée une liste **statique** dans MDBList, puis renseigne les secrets suivants dans **Settings → Secrets and variables → Actions** :

| Secret | Utilisation |
| --- | --- |
| `TMDB_API_TOKEN` | Jeton de lecture TMDb ou clé API v3 pour identifier les films. |
| `MDBLIST_API_KEY` | Clé API de ton compte MDBList. |
| `MDBLIST_OUTPUT_LIST_ID` | ID numérique ou chemin `utilisateur/nom-de-liste` de la liste statique à maintenir. |
| `SMTP_USERNAME` | Compte Gmail utilisé pour les notifications. |
| `SMTP_PASSWORD` | Mot de passe d’application Gmail. |

Les clés sont disponibles dans les paramètres [TMDb](https://www.themoviedb.org/settings/api) et [MDBList](https://mdblist.com/preferences/). Une URL de preset comme `movies/?preset=7095` ne peut pas servir de liste de sortie.

Le workflow [weekly-sync.yml](.github/workflows/weekly-sync.yml) vérifie le CNC chaque jour à **07:17 UTC**, soit 08:17 en hiver et 09:17 en été à Paris. Il peut aussi être lancé depuis **Actions → Sync CNC million-entry films to MDBList → Run workflow**. Coche `dry_run` pour simuler une mise à jour sans modifier MDBList ni enregistrer le nouvel état. Un lancement manuel conserve la vérification de version : il ne force pas une reconstruction si le CNC n’a pas changé et que la remise à niveau initiale est déjà terminée.

Après une synchronisation réussie, le workflow enregistre automatiquement la date traitée et le catalogue résolu dans le dépôt. Lorsqu’une nouvelle version CNC a été traitée, il envoie un email de confirmation à l’adresse définie dans le workflow.

## Ajouter le catalogue dans Stremio

Ouvre la liste de sortie dans MDBList et utilise l’intégration **Stremio Lists / Add to Stremio**. Le catalogue est ensuite rafraîchi par cette intégration.

Le script n’utilise aucun identifiant Stremio. Ce dépôt gère les listes de films ; il ne fournit pas de vidéos ni de sources de lecture.

## Films non reconnus ou ambigus

Si un film ne peut pas être identifié avec certitude dans TMDb, le programme s’arrête **avant toute modification de MDBList**.

1. Télécharge l’artefact `cnc-sync-report` depuis l’exécution GitHub Actions.
2. Consulte `unresolved.json` pour trouver les films concernés et les candidats proposés.
3. Complète [overrides.json](overrides.json) avec la clé du film et l’identifiant TMDb retenu.
4. Enregistre la correction dans le dépôt et relance le workflow.

Exemple de correspondance manuelle :

```json
{
  "titre normalise|2024": 123456
}
```

Lors d’une nouvelle version CNC, les films à résoudre sont également envoyés par email. Une ambiguïté bloque toute la mise à jour : aucun catalogue partiel n’est appliqué.

## Utilisation locale

```bash
git clone https://github.com/RomSom75/Stremio.git
cd Stremio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Crée un fichier `.env` à la racine du dépôt :

```dotenv
TMDB_API_TOKEN=ton_jeton_tmdb
MDBLIST_API_KEY=ta_cle_mdblist
MDBLIST_OUTPUT_LIST_ID=utilisateur/nom-de-liste
CNC_DATASET_URL=https://www.data.gouv.fr/api/1/datasets/r/8e92a471-00a9-445c-ad41-56fe484605b0
```

| Commande | Effet |
| --- | --- |
| `python sync.py --check-update` | Vérifie si une synchronisation est nécessaire, sans contacter MDBList. |
| `python sync.py --dry-run` | Calcule les changements si une synchronisation est nécessaire, sans les appliquer. |
| `python sync.py` | Met à jour la liste si une synchronisation est nécessaire. |
| `python sync.py --force --dry-run` | Force la reconstruction du catalogue et simule les changements. |
| `python sync.py --force` | Force la reconstruction et la synchronisation, même sans nouvelle version CNC. |

Les rapports sont enregistrés dans `reports/`. Le fichier `cnc-catalog.json` contient les identifiants TMDb résolus ; `cnc-state.json` conserve la date CNC traitée et le marqueur `include_watched` de la remise à niveau initiale. Le mode simulation ne modifie pas ces deux fichiers.

Les fichiers `.env`, l’environnement local et les rapports sont exclus de Git par `.gitignore`.

## Historique Trakt → MDBList

Le script indépendant [sync_trakt_mdblist.py](sync_trakt_mdblist.py) synchronise les films et épisodes vus depuis Trakt vers MDBList. Trakt fait référence : il ajoute les statuts manquants et corrige les dates différentes, même pour les titres déjà vus dans MDBList. Il utilise le dernier visionnage Trakt de chaque film ou épisode, y compris si cette date est plus ancienne que celle de MDBList. Les dates sont comparées en UTC pour éviter les faux écarts de fuseau horaire. Il ne retire aucun film de la liste CNC.

Pour l’utiliser, configure les secrets `TRAKT_USERNAME`, `TRAKT_CLIENT_ID` et `MDBLIST_API_KEY`. Pour activer son exécution quotidienne à **03:23, heure de Paris**, définis la variable GitHub Actions `TRAKT_SYNC_ENABLED` sur `true`.

Un lancement manuel depuis **Actions → Trakt vers MDBList → Run workflow** effectue une simulation par défaut. Coche `apply` pour appliquer les ajouts et corrections de dates. Les exécutions planifiées les appliquent automatiquement. Un email est prévu en cas d’échec.

Pour préparer les changements, le script lit le catalogue vu MDBList par pages de 1 000 éléments avec `extended=ids_only`, afin de limiter la consommation du quota API. Toutes les pages sont validées avant les écritures.

Après chaque écriture, le script vérifie le statut **et la date** renvoyés par MDBList. Les titres déjà alignés ne sont pas réécrits. Pour remplacer une date MDBList plus récente par celle de Trakt, il remet d’abord à zéro le statut vu du film ou de l’épisode concerné, puis le rétablit avec la date Trakt, sans demander la suppression des visionnages enregistrés. Si l’exécution s’interrompt entre ces deux étapes, relance la tâche pour rétablir le statut et terminer la correction. Les titres absents de l’historique Trakt ne sont pas modifiés.

Le workflow manuel [recuperer-cle.yml](.github/workflows/recuperer-cle.yml) permet également d’envoyer la clé MDBList par email à l’adresse définie dans ce fichier.

## Tests

```bash
python -m unittest discover -s tests_cnc -v
python -m unittest discover -s tests_trakt_mdblist -v
```

## Sources

- [Jeu de données officiel du CNC sur data.gouv.fr](https://www.data.gouv.fr/datasets/films-ayant-realise-plus-dun-million-dentrees)
- [Recherche de films TMDb](https://developer.themoviedb.org/reference/search-movie)
- [API MDBList](https://api.mdblist.com/docs/)
- [Intégrations MDBList](https://mdblist.com/apps/)
