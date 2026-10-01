# syno-ia

**Votre RAG privé, sur votre NAS Synology, avec *vos* permissions.**

`syno-ia` est une application web auto-hébergée, dans l'esprit d'AnythingLLM, mais conçue
pour un NAS Synology : vous posez une question en français, elle cherche la réponse dans
**vos documents** et la restitue avec ses sources.

Sa particularité : **chaque utilisateur se connecte avec son compte DSM** et n'obtient de
réponses que sur les documents auxquels **DSM l'autorise réellement**. Aucune fuite entre
services, aucun document RH qui ressort dans le chat d'un stagiaire.

Rien ne sort du NAS : indexation, recherche et génération se font en local (un service
distant reste optionnel si vous le souhaitez).

---

## Sommaire

- [Pourquoi ce projet](#pourquoi-ce-projet)
- [Fonctionnalités](#fonctionnalités)
- [Modèle de sécurité](#modèle-de-sécurité)
- [Prérequis](#prérequis)
- [Profils matériels et choix des modèles](#profils-matériels-et-choix-des-modèles)
- [Configuration DSM](#configuration-dsm)
- [Installation sur le NAS](#installation-sur-le-nas)
- [Variables d'environnement](#variables-denvironnement)
- [Utilisation](#utilisation)
- [Architecture](#architecture)
- [Développement](#développement)
- [Déploiement continu](#déploiement-continu)
- [Dépannage](#dépannage)
- [Limites connues](#limites-connues)
- [Licence](#licence)

---

## Pourquoi ce projet

Synology réserve ses fonctions d'IA locale aux modèles récents et bien dotés. Un DS218+,
lui, n'aura jamais « AI Console ». Pourtant, avec 8 Go de RAM, il est parfaitement capable
de faire tourner un RAG utile.

Les solutions existantes (AnythingLLM, Open WebUI, Dify…) savent faire du RAG, mais elles
gèrent leurs **propres** utilisateurs et leur **propre** silo de documents. Aucune ne sait
dire : « cet utilisateur DSM n'a pas le droit de lire `/volume1/rh`, donc il ne doit jamais
voir passer le moindre extrait de ce partage. »

C'est exactement le problème que `syno-ia` résout.

---

## Fonctionnalités

- 🔐 **Authentification DSM native** — les comptes du NAS, y compris la double
  authentification (OTP). Aucun compte à créer, aucun mot de passe stocké.
- 🛡️ **RAG cloisonné par les permissions DSM** — filtrage à deux niveaux (partages puis
  fichiers), systématiquement *fail-closed*.
- 🔎 **Recherche hybride** — BM25 (SQLite FTS5) + similarité vectorielle, fusionnées par
  *Reciprocal Rank Fusion*. Fonctionne bien même sur de petits corpus.
- 📄 **Formats courants** — PDF, DOCX, PPTX, XLSX, CSV, HTML, Markdown, JSON, code source
  et ~30 extensions texte, avec localisation de la source (page, diapositive, onglet).
- 🧠 **Modèles adaptés au matériel** — détection de la RAM, des cœurs et des jeux
  d'instructions SIMD, puis sélection automatique des embeddings et du LLM.
- 💬 **Réponses citées, en flux** — chaque affirmation renvoie à `[1]`, `[2]`… cliquables
  et ouvrant le document d'origine.
- 🪶 **Mode extractif** — sans aucun LLM, l'application répond en citant les passages
  pertinents. Utile sur un NAS vraiment modeste.
- 🌍 **Interface français / anglais**, thème clair / sombre, sans étape de build.
- 🐳 **Un seul conteneur**, aucune base externe, aucun service tiers obligatoire.

---

## Modèle de sécurité

C'est le cœur du projet, donc détaillons.

### 1. L'utilisateur s'authentifie auprès de DSM, pas auprès de `syno-ia`

```
Navigateur ──(compte + mot de passe DSM)──> syno-ia ──> SYNO.API.Auth (session=FileStation)
                                                   <── sid personnel
```

- `syno-ia` ne stocke **aucun** mot de passe et n'a **aucune** base d'utilisateurs.
- Le `sid` DSM **ne quitte jamais le serveur**. Le navigateur reçoit un jeton opaque signé
  (HMAC-SHA256) dans un cookie `HttpOnly`, `SameSite=Lax`.
- Les sessions vivent **en mémoire uniquement** : un redémarrage du conteneur déconnecte
  tout le monde. C'est volontaire — aucun identifiant ne touche le disque.
- Le statut administrateur est lu depuis DSM (`SYNO.FileStation.Info` → `is_manager`).

### 2. Chaque réponse est filtrée avec le `sid` de l'utilisateur

```
Question ──> recherche hybride sur TOUT l'index (rapide, local)
         ──> 1) pré-filtre : partages visibles par l'utilisateur (list_share avec SON sid)
         ──> 2) vérification : getinfo par lots avec SON sid → applique les ACL avancées
         ──> seuls les extraits survivants alimentent le contexte du LLM
```

Le compte de service (utilisé pour l'indexation) **ne sert jamais** à répondre à un
utilisateur. Il ne sert qu'à lire les fichiers pendant l'indexation et à cartographier les
chemins réels des partages.

### 3. *Fail-closed*, toujours

Un document est masqué dès qu'il y a le moindre doute :

| Situation | Décision |
|---|---|
| Partage absent de `list_share` | refusé |
| Chemin absent de la réponse `getinfo` | refusé |
| Entrée `getinfo` portant un code d'erreur | refusé |
| `perm.acl.read = false` ou `adv_right.disable_list` | refusé |
| Erreur réseau ou API pendant la vérification | refusé |
| Session DSM expirée | 401, reconnexion demandée |

Le téléchargement d'un document (`/api/document`) **revalide** les droits au moment de la
requête : un fichier déjà cité dans une réponse ancienne ne sera pas servi si les
permissions ont changé entre-temps.

### 4. Ce que ce modèle ne couvre pas

- L'index (`data/syno-ia.db`) contient le **texte de tous les documents indexés**, sans
  cloisonnement interne. Le cloisonnement est appliqué à la lecture, pas au stockage.
  Protégez ce fichier comme vous protégeriez les documents eux-mêmes.
- Un administrateur de `syno-ia` voit les statistiques d'indexation (nombre de documents
  par partage, chemins) via le panneau d'administration.
- Les permissions sont mises en cache pendant `ACL_CACHE_TTL` (5 min par défaut). Un
  retrait de droit dans DSM peut donc mettre jusqu'à 5 minutes à prendre effet. Réduisez
  cette valeur si votre contexte l'exige.

---

## Prérequis

| Élément | Minimum | Recommandé |
|---|---|---|
| DSM | 7.0 | 7.2+ |
| Paquet | Container Manager (ou Docker) | Container Manager |
| Architecture | x86_64 ou ARM64 | x86_64 |
| RAM totale du NAS | 2 Go | 8 Go |
| RAM libre pour le conteneur | ~1 Go | 4 Go et plus |
| Espace disque | 2 Go (image + modèles) | 5 Go |

> **NAS ARM d'entrée de gamme** (DS220j, DS120j…) : l'image ARM64 est publiée, mais avec
> 512 Mo à 1 Go de RAM, seul le **mode extractif** est réaliste. Le LLM local est hors de
> portée ; visez plutôt un service distant (`LLM_BACKEND=openai` ou `ollama`).

> **DS218+ / DS718+ / DS918+ et autres Celeron « Apollo Lake »** : ces processeurs **ne
> possèdent pas AVX**. Les paquets `llama-cpp-python` précompilés du commerce sont bâtis
> avec AVX2 et provoquent un `Illegal instruction` immédiat. L'image `:latest-llm` de ce
> dépôt contient une compilation **explicitement sans AVX** — c'est la seule qui fonctionne
> sur ces machines.

---

## Profils matériels et choix des modèles

Au démarrage, `syno-ia` lit `/proc/cpuinfo`, `/proc/meminfo` et les limites cgroup du
conteneur, puis calcule **deux** niveaux indépendants :

- **mémoire** — ce qu'il est possible de charger ;
- **calcul** — `nombre de cœurs × facteur SIMD` (AVX2 ≈ 1.6, AVX ≈ 1.15, sinon 1.0), soit
  ce qu'il est raisonnable d'exécuter sans latence insupportable.

Le profil de génération retenu est le **minimum des deux**, avec un cran de tolérance
lorsque la mémoire est abondante.

| Profil | RAM disponible | Calcul | LLM local | Embeddings |
|---|---|---|---|---|
| `micro` | < 1,5 Go | faible | Qwen2.5 0.5B Q4_K_M | model2vec `potion-multilingual-128M` |
| `small` | 1,5 – 3 Go | modeste | Qwen2.5 1.5B Q4_K_M | model2vec `potion-multilingual-128M` |
| `medium` | 3 – 7 Go | correct | Qwen2.5 3B Q4_K_M | fastembed `paraphrase-multilingual-MiniLM-L12-v2` |
| `large` | > 7 Go | AVX2, 4 cœurs+ | Qwen2.5 7B Q4_K_M | fastembed `multilingual-e5-large` |

### Cas concret : DS218+ avec 8 Go

| Mesure | Valeur |
|---|---|
| Processeur | Intel Celeron J3355, 2 cœurs, **sans AVX** |
| Niveau mémoire | `medium` |
| Niveau calcul | `micro` (score ≈ 2,0) |
| **Profil retenu** | **`small`** — Qwen2.5 1.5B Q4_K_M |
| Embeddings | model2vec (statiques, pas d'ONNX : trop lent sans AVX) |
| Débit estimé | ~3 jetons/seconde |

Concrètement : une réponse de 150 mots demande **environ une minute**. C'est utilisable
pour de la recherche documentaire, pas pour de la conversation. Deux alternatives :

1. **Mode extractif** (`LLM_BACKEND=none`) — réponse instantanée constituée des passages
   pertinents cités. Pas de reformulation, mais immédiat et toujours exact.
2. **LLM distant** — un PC du réseau avec Ollama (`LLM_BACKEND=ollama`), ou un service
   compatible OpenAI. L'indexation et le filtrage ACL restent sur le NAS ; seuls les
   extraits déjà autorisés sont transmis.

Le profil peut être forcé : `HARDWARE_PROFILE=micro|small|medium|large`.

---

## Configuration DSM

> Ces quatre étapes sont à réaliser **avant** l'installation : le conteneur a besoin
> du compte de service dès son premier démarrage, et refusera de dialoguer avec DSM
> tant que les droits et le pare-feu ne sont pas en place.

### 1. Créer un compte de service

**Panneau de configuration → Utilisateur et groupe → Créer**

- Nom : `syno-ia-svc` (par exemple)
- Mot de passe : long et aléatoire
- **Permissions de dossier partagé** : *lecture seule* sur les partages à indexer, *aucun
  accès* ailleurs. Le compte n'a pas besoin d'être administrateur.
- **Applications** : autorisez **File Station**. C'est indispensable — sans ce droit,
  toutes les requêtes échouent avec le code `160`.
- Désactivez la double authentification pour ce compte de service (sinon la connexion
  automatique est impossible).

Reportez ces identifiants dans `.env` (`DSM_SERVICE_ACCOUNT`, `DSM_SERVICE_PASSWORD`).

### 2. Autoriser les utilisateurs

Les utilisateurs qui se connecteront à `syno-ia` doivent eux aussi avoir le droit
d'application **File Station** — c'est ce droit qui permet à `syno-ia` de vérifier leurs
permissions en leur nom.

### 3. Pare-feu et Auto Block

Le conteneur joint DSM par la passerelle du bridge Docker (`172.17.0.1:5000`).

- **Panneau de configuration → Sécurité → Pare-feu** : si un pare-feu est actif, ajoutez
  une règle d'autorisation pour la source `172.17.0.0/16` vers le port `5000`, **au-dessus**
  de toute règle de blocage.
- **Panneau de configuration → Sécurité → Protection → Blocage auto** : ajoutez
  `172.17.0.0/16` à la **liste d'autorisation**. Tous les conteneurs partagent cette
  adresse source : sans cela, un autre conteneur qui échoue ses connexions pourrait faire
  bloquer `syno-ia` — et réciproquement.

`syno-ia` limite lui-même les dégâts : après un refus d'identifiants du compte de service,
il **cesse** de réessayer jusqu'à une reconnexion manuelle depuis le panneau
d'administration.

### 4. HTTPS

Par défaut, `DSM_URL=http://172.17.0.1:5000` : le trafic ne quitte pas la machine. Pour
utiliser HTTPS, mettez `https://172.17.0.1:5001` et laissez `DSM_VERIFY_SSL=false` si le
certificat DSM est auto-signé.

Pour exposer `syno-ia` lui-même en HTTPS — une fois l'application installée — placez-le
derrière le **Reverse Proxy** de DSM (Panneau de configuration → Portail de connexion →
Reverse Proxy) en activant `WebSocket`/`HTTP/1.1` et en désactivant la mise en tampon des
réponses (le chat utilise des flux SSE).

---

## Installation sur le NAS

Le compte de service de l'étape précédente doit exister : notez son nom et son mot de
passe, ils sont demandés dans le fichier `.env`.

> **Si vous déployez votre propre fork.** GitHub publie les paquets GHCR en *privé* par
> défaut, même pour un dépôt public : le NAS recevrait alors `unauthorized` au
> `docker pull`. Après la première exécution réussie du workflow, passez le paquet en
> *Public* depuis ses réglages
> (`https://github.com/users/<vous>/packages/container/syno-ia/settings`), ou exportez
> `GHCR_USER` et `GHCR_TOKEN` (portée `read:packages`) avant d'appeler
> `deploy/deploy-nas.sh`. L'image officielle de ce dépôt est déjà publique.

### Option A — Container Manager (interface graphique)

1. **Container Manager → Registre** : recherchez `ghcr.io/flake9025/syno-ia`, ou utilisez
   **Projet** avec le `docker-compose.yml` du dépôt.
2. **Container Manager → Projet → Créer** :
   - chemin : `/docker/apps/syno-ia`
   - source : *Créer docker-compose.yml* et collez le contenu du fichier du dépôt.
3. Créez le fichier `.env` à côté, à partir de [`.env.example`](.env.example), en y
   reportant `APP_SECRET`, `DSM_SERVICE_ACCOUNT` et `DSM_SERVICE_PASSWORD`.
4. Démarrez, puis ouvrez `http://<ip-du-nas>:8083` et connectez-vous avec **votre compte
   DSM habituel**.

### Option B — SSH (recommandé)

```bash
ssh admin@<ip-du-nas>

sudo mkdir -p /volume1/docker/apps/syno-ia/data
cd /volume1/docker/apps/syno-ia

# 1. Configuration de départ
curl -fsSL https://raw.githubusercontent.com/flake9025/syno-ia/main/.env.example -o .env

# 2. Clé de signature des sessions (à générer avant le premier démarrage)
echo "APP_SECRET=$(openssl rand -hex 32)" >> .env

# 3. Compte de service créé à l'étape « Configuration DSM »
vi .env          # DSM_SERVICE_ACCOUNT et DSM_SERVICE_PASSWORD

# 4. Démarrage
sudo docker run -d \
  --name syno-ia \
  -p 8083:8080 \
  --env-file /volume1/docker/apps/syno-ia/.env \
  -e INDEX_MODE=mount \
  -e INDEX_ROOTS=/volume1/documents \
  -v /volume1/docker/apps/syno-ia/data:/app/data \
  -v /volume1/documents:/volume1/documents:ro \
  --memory=5g \
  --restart unless-stopped \
  ghcr.io/flake9025/syno-ia:latest
```

Ouvrez ensuite `http://<ip-du-nas>:8083` et connectez-vous avec **votre compte DSM
habituel** — pas avec le compte de service, qui ne sert qu'aux vérifications internes.

> ⚠️ **Montez les partages au même chemin que sur le NAS**
> (`-v /volume1/documents:/volume1/documents:ro`). L'identité des chemins est ce qui permet
> de faire correspondre un fichier indexé à son chemin DSM, et donc de rejouer ses ACL.
> Un montage vers `/data/docs` casserait cette correspondance.

### Variante avec LLM 100 % local

```bash
# image contenant llama.cpp compilé sans AVX
ghcr.io/flake9025/syno-ia:latest-llm
```

Puis, depuis le panneau d'administration, onglet **Modèles** : *Télécharger le modèle
recommandé*. Le fichier GGUF (~1 Go pour le profil `small`) est stocké dans
`/app/data/models` et survit aux mises à jour.

### Variante sans aucun montage

Si vous préférez ne monter aucun partage, `INDEX_MODE=filestation` fait lire les fichiers
par l'API DSM avec le compte de service. C'est plus lent et plus gourmand en réseau, mais
strictement équivalent côté sécurité.

---

## Variables d'environnement

Fichier complet et commenté : [`.env.example`](.env.example). L'essentiel :

| Variable | Défaut | Description |
|---|---|---|
| `APP_SECRET` | *(aléatoire)* | Clé de signature des cookies. **À fixer** en production. |
| `DSM_URL` | `http://172.17.0.1:5000` | DSM vu depuis le conteneur. |
| `DSM_SERVICE_ACCOUNT` / `DSM_SERVICE_PASSWORD` | — | Compte de service pour l'indexation. |
| `ADMIN_ACCOUNTS` | *(vide)* | Comptes DSM admin de `syno-ia` en plus des admins DSM. |
| `INDEX_MODE` | `mount` | `mount` (partages montés) ou `filestation` (API DSM). |
| `INDEX_ROOTS` | `/volume1/documents` | Racines à indexer, séparées par des virgules. |
| `INDEX_INTERVAL_MINUTES` | `360` | Réindexation automatique (`0` = désactivée). |
| `ACL_STRICT` | `true` | Vérification fichier par fichier des ACL avancées. |
| `ACL_CACHE_TTL` | `300` | Durée de vie d'une décision d'accès (secondes). |
| `SESSION_TTL_MINUTES` | `720` | Durée d'une session web. |
| `EMBEDDING_BACKEND` | `auto` | `auto`, `model2vec`, `fastembed`, `none`. |
| `EMBEDDING_MODEL` | *(selon profil)* | Dépôt Hugging Face. En mode `fastembed`, le modèle **doit** figurer dans le catalogue ONNX (`TextEmbedding.list_supported_models()`), sinon l'application bascule automatiquement sur model2vec. |
| `LLM_BACKEND` | `auto` | `auto`, `llamacpp`, `ollama`, `openai`, `none`. |
| `OLLAMA_URL` | `http://172.17.0.1:11434` | Ollama local ou distant. |
| `OPENAI_API_KEY` | *(vide)* | Service compatible OpenAI. |
| `HARDWARE_PROFILE` | `auto` | Forçage du profil (`micro`…`large`). |

En mode `auto`, le LLM est choisi dans cet ordre : service compatible OpenAI (si une clé
est présente) → Ollama (s'il répond) → `llama.cpp` local (si un modèle est présent) →
**mode extractif**.

---

## Utilisation

1. Ouvrez `http://<ip-du-nas>:8083` et connectez-vous avec votre compte DSM.
2. La barre latérale affiche les partages indexés **que vous pouvez consulter**.
3. Posez votre question. Les réponses arrivent en flux, avec des citations `[1]`, `[2]`…
   cliquables qui ouvrent le document source.
4. Les administrateurs disposent d'un panneau (⚙️) : état du matériel, avancement de
   l'indexation, téléchargement de modèles, sessions actives, reconnexion DSM.

La première indexation d'un corpus de quelques milliers de documents prend de 20 minutes à
plusieurs heures sur un DS218+. Elle est incrémentale : les exécutions suivantes ne
retraitent que ce qui a changé (taille ou date de modification).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Navigateur — HTML/CSS/JS sans build, SSE pour le flux         │
└───────────────────────────┬──────────────────────────────────┘
                            │ cookie de session signé (HttpOnly)
┌───────────────────────────▼──────────────────────────────────┐
│ FastAPI                                                       │
│  ├─ auth        SYNO.API.Auth → sid conservé côté serveur     │
│  ├─ chat/search pipeline de récupération + génération          │
│  ├─ admin       indexation, modèles, diagnostics               │
│  └─ health      sonde Docker / CI                              │
├───────────────────────────────────────────────────────────────┤
│ Pipeline    BM25 (FTS5) ─┐                                    │
│                          ├─ RRF ─→ filtre ACL ─→ contexte      │
│             vecteurs ────┘                                     │
├───────────────────────────────────────────────────────────────┤
│ Index SQLite (WAL)   documents │ chunks │ FTS5 │ vecteurs      │
├───────────────────────────────────────────────────────────────┤
│ Indexeur    extraction → découpage → embeddings → SQLite      │
├───────────────────────────────────────────────────────────────┤
│ DSM         list_share (partages) │ getinfo (ACL par fichier)  │
└───────────────────────────────────────────────────────────────┘
```

Choix techniques notables :

- **Un seul fichier SQLite en WAL**, pas de base vectorielle externe. Les vecteurs sont
  stockés normalisés en `float32` : le cosinus se réduit à un produit scalaire, et la
  matrice NumPy est mise en cache en mémoire, invalidée par un compteur de génération.
- **RRF (k = 60)** plutôt qu'une somme pondérée : aucune calibration de scores nécessaire
  entre BM25 et cosinus.
- **FTS5 avec `remove_diacritics 2`** : « procedure » trouve « procédure ».
- **Aucune étape de build front-end** : pas de Node, pas de bundler, pas de CVE npm.
- **Vérification ACL par lots de 20**, avec repli automatique chemin par chemin : DSM ne
  documente pas le comportement de `getinfo` en cas d'échec partiel, et un seul fichier
  supprimé depuis l'indexation ferait échouer tout le lot.

---

## Développement

```powershell
git clone https://github.com/flake9025/syno-ia.git
cd syno-ia

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt

# Tests et lint
pytest -q                  # suite complète (télécharge les modèles d'embeddings)
pytest -q -m "not network" # suite hors ligne, comme en intégration continue
ruff check app tests

# Serveur local
$env:DATA_DIR = ".\data"
$env:LLM_BACKEND = "none"
uvicorn app.main:app --reload --port 8080
```

Sous Linux/macOS, remplacez l'activation par `source .venv/bin/activate` et les
affectations par `export DATA_DIR=./data`.

La suite de tests est hermétique : aucun accès réseau, DSM simulé par `httpx.MockTransport`
et par des clients factices. Elle couvre en particulier le caractère *fail-closed* du
contrôle d'accès, qui est la garantie centrale du projet.

---

## Déploiement continu

`.github/workflows/build.yml` enchaîne :

1. **`lint_test`** — `ruff` puis `pytest -m "not network"` (les tests marqués
   `network` téléchargent de vrais modèles et ne tournent qu'en local).
2. **`docker_smoke`** — construction de l'image, démarrage, vérification de `/api/health`,
   de l'interface web, et du fait qu'une route protégée répond bien `401` sans session.
3. **`docker_image`** — publication multi-architecture (`amd64` + `arm64`) sur
   `ghcr.io/<propriétaire>/syno-ia`.
4. **`docker_image_llm`** — variante `-llm` (`amd64`), avec `llama.cpp` compilé sans AVX.
5. **`deploy_nas`** — appel du webhook `NAS_WEBHOOK_URL` (ignoré si le secret est absent).

Côté NAS, [`deploy/deploy-nas.sh`](deploy/deploy-nas.sh) récupère l'image, recrée le
conteneur avec ses montages en lecture seule et attend la sonde de santé. Installez-le dans
`/volume1/web/hooks/` et déclenchez-le depuis votre webhook, comme pour `jobs-crawler`.

Secret à créer dans le dépôt GitHub : `NAS_WEBHOOK_URL`. Aucun autre n'est requis
(`GITHUB_TOKEN` suffit pour publier sur GHCR).

---

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `NAS injoignable` à la connexion | `DSM_URL` incorrect, ou pare-feu DSM | Testez `docker exec syno-ia curl -s http://172.17.0.1:5000/webapi/query.cgi?api=SYNO.API.Info\&version=1\&method=query` |
| Erreur `160` | Droit d'application File Station manquant | Panneau de configuration → Utilisateur → onglet Applications |
| Erreur `407` | Auto Block a bloqué le sous-réseau Docker | Ajoutez `172.17.0.0/16` à la liste d'autorisation |
| Erreur `150` | L'IP de sortie du conteneur a changé | Reconnectez-vous ; évitez les réseaux Docker instables |
| `Illegal instruction` au chargement du LLM | Roue `llama-cpp-python` avec AVX2 sur un CPU sans AVX | Utilisez l'image `:latest-llm` de ce dépôt |
| Aucun résultat alors que les documents existent | Partage non visible par votre compte DSM, ou indexation non terminée | Panneau d'administration → *Indexation* ; vérifiez vos permissions DSM |
| L'indexation se fige puis redémarre | Mémoire insuffisante (OOM killer) | Augmentez `--memory`, réduisez `INDEX_BATCH_SIZE`, passez `EMBEDDING_BACKEND=model2vec` |
| Réponses très lentes | LLM local trop gros pour le CPU | `LLM_BACKEND=none` (extractif) ou LLM distant |
| Le chat n'affiche rien derrière un reverse proxy | Mise en tampon des réponses SSE | Désactivez le *buffering* dans la configuration du proxy |

Journaux : `docker logs -f syno-ia`. Diagnostic complet : `GET /api/health` (public) et
`GET /api/admin/status` (administrateurs).

---

## Limites connues

- Le comportement exact de `SYNO.FileStation.List/getinfo` en cas d'échec partiel n'est pas
  documenté par Synology. L'implémentation est défensive (repli chemin par chemin) mais
  mérite d'être validée sur votre DSM.
- DSM ne distingue pas toujours « accès refusé » de « fichier introuvable ». Dans les deux
  cas, `syno-ia` refuse — ce qui est le comportement sûr.
- Les sessions étant en mémoire, une mise à jour du conteneur déconnecte les utilisateurs.
- Le RAG ne gère pas l'OCR : un PDF scanné sans couche texte ne sera pas indexé.
- Les fichiers de plus de `INDEX_MAX_FILE_MB` (40 Mo par défaut) sont ignorés.

---

## Licence

MIT — voir [`LICENSE`](LICENSE).

Ce projet n'est ni affilié à, ni approuvé par Synology Inc. « Synology » et « DSM » sont des
marques de leurs propriétaires respectifs.
