# syno-ia

**Votre RAG privé, sur votre NAS Synology, avec *vos* permissions.**

`syno-ia` est une application web auto-hébergée pour NAS Synology : vous posez une question
en français, elle cherche la réponse dans **vos documents** et la restitue avec ses sources.

Sa particularité : **chaque utilisateur se connecte avec son compte DSM** et n'obtient de
réponses que sur les documents auxquels **DSM l'autorise réellement**. Aucune fuite entre
services, aucun document RH qui ressort dans le chat d'un stagiaire.

Indexation, recherche et rédaction se font **en local** : vos documents ne quittent jamais
le NAS.

![Interface de syno-ia : réponse avec citations et liste des partages autorisés](docs/capture-interface.png)

*Marie est connectée avec son compte DSM. La barre latérale ne liste que `documents` et
`projets` : le partage `rh`, auquel elle n'a pas accès, n'apparaît pas — et son contenu
ne peut pas remonter dans les réponses.*

---

## Sommaire

- [Pourquoi ce projet](#pourquoi-ce-projet)
- [Vos documents ne quittent jamais le NAS](#vos-documents-ne-quittent-jamais-le-nas)
- [Fonctionnalités](#fonctionnalités)
- [Configuration Synology DSM](#configuration-synology-dsm)
- [Installation sur le NAS](#installation-sur-le-nas)
  - [Rédaction déportée sur un PC (Ollama)](#variante--rédaction-déportée-sur-un-pc-ollama)
  - [Mise à jour en un clic](#mise-à-jour-en-un-clic-scriptsupdatesh)
- [Modèle de sécurité](#modèle-de-sécurité)
- [Prérequis](#prérequis)
- [Profils matériels et choix des modèles](#profils-matériels-et-choix-des-modèles)
- [Variables d'environnement](#variables-denvironnement)
- [Utilisation](#utilisation)
- [Architecture](#architecture)
- [Dépannage](#dépannage)
- [Limites connues](#limites-connues)
- [Licence](#licence)

---

## Pourquoi ce projet

Synology réserve ses fonctions d'IA locale aux modèles récents et bien dotés. Un DS218+,
lui, n'aura jamais « AI Console ». Pourtant, avec 8 Go de RAM, il est parfaitement capable
de faire tourner un RAG utile.

Les solutions existantes savent faire du RAG, mais elles gèrent leurs **propres**
utilisateurs et leur **propre** silo de documents. Aucune ne sait dire : « cet utilisateur
DSM n'a pas le droit de lire `/volume1/rh`, donc il ne doit jamais voir passer le moindre
extrait de ce partage. »

C'est exactement le problème que `syno-ia` résout.

---

## Vos documents ne quittent jamais le NAS

C'est l'intérêt principal du projet, et la raison d'accepter un matériel modeste.

Les assistants en ligne (ChatGPT, Copilot, Gemini…) sont remarquables, mais les utiliser
sur des documents personnels ou professionnels pose trois problèmes que rien ne permet de
contourner côté utilisateur :

- **Il faut envoyer le document.** Pour qu'un service distant réponde sur votre bail, vos
  bulletins de salaire ou le contrat d'un client, il faut le lui transmettre. Un fichier
  parti ne revient pas : vous ne savez ni où il est stocké, ni combien de temps, ni qui y
  a accès en interne.
- **Vous ne maîtrisez plus la conformité.** RGPD, secret professionnel, clauses de
  confidentialité, données de santé, données appartenant à un tiers : la plupart de ces
  engagements interdisent le transfert à un sous-traitant non prévu au contrat. Ce n'est
  pas une question de confiance dans le prestataire, mais de ce que vous avez le droit de
  faire.
- **Les conditions changent.** Ce qui n'est pas utilisé pour l'entraînement aujourd'hui
  peut l'être demain, et une fuite chez l'hébergeur expose un corpus entier d'un coup.

Ici, le fichier reste sur votre disque. Le NAS le lit, le découpe, l'indexe et rédige la
réponse. Une fois les modèles téléchargés, **aucune connexion sortante n'est nécessaire** :
vous pouvez couper l'accès Internet du conteneur et vérifier que tout continue de
fonctionner.

S'y ajoute un effet moins évident : comme les permissions DSM sont appliquées à chaque
question, vos collègues n'ont pas davantage accès à vos documents que d'habitude. Un RAG
classique, lui, recopie tout dans un silo commun — et c'est souvent à ce moment-là que le
cloisonnement disparaît.

Deux nuances, pour rester honnête :

- Si vous [déportez la rédaction sur un PC du réseau
  local](#variante--rédaction-déportée-sur-un-pc-ollama), les données restent chez vous,
  mais transitent **en clair** sur ce réseau.
- Le recours à un service distant (OpenAI et compatibles) reste possible. Il est
  **optionnel, désactivé par défaut**, et vous replace devant les limites ci-dessus.

---

## Fonctionnalités

- 🔐 **Authentification DSM native**, double authentification (OTP) comprise
- 🛡️ **RAG cloisonné par les permissions DSM**, *fail-closed*
- 🔎 **Recherche hybride** — BM25 + similarité vectorielle
- 📄 **Formats courants** — PDF, DOCX, PPTX, XLSX, CSV, HTML, Markdown, JSON, code source
- 🧠 **Modèles adaptés au matériel**, détecté automatiquement
- 💬 **Réponses citées, en flux**, avec liens vers les documents d'origine
- 🖥️ **Rédaction déportée** sur un PC du réseau, avec repli sur le NAS
- 🪶 **Mode extractif**, sans aucun LLM
- 🌍 **Interface français / anglais**, thème clair / sombre
- 🐳 **Un seul conteneur**, aucune base externe

---

## Configuration Synology DSM

> **Seule l'étape 1 est à réaliser systématiquement.** Les étapes 2 à 4 décrivent des
> réglages déjà corrects sur une installation DSM ordinaire : contentez-vous de les
> **vérifier**, et revenez-y si quelque chose ne fonctionne pas — chacune correspond à une
> panne précise, rappelée dans le [Dépannage](#dépannage).

### 1. Créer un compte de service *(obligatoire)*

**Panneau de configuration → Utilisateur et groupe → Créer**

- Nom : **`syno-ia-svc`** — c'est la valeur déjà inscrite dans `.env.example` ; en la
  réutilisant telle quelle, vous n'aurez que le mot de passe à renseigner.
- Mot de passe : long et aléatoire
- **Permissions de dossier partagé** : *lecture seule* sur les partages à indexer, *aucun
  accès* ailleurs. Le compte n'a pas besoin d'être administrateur.
- **Applications** : autorisez **File Station**. C'est indispensable — sans ce droit,
  toutes les requêtes échouent avec le code `160`.
- Désactivez la double authentification pour ce compte de service (sinon la connexion
  automatique est impossible).

Reportez le mot de passe dans `.env` (`DSM_SERVICE_PASSWORD`) ; `DSM_SERVICE_ACCOUNT` y
vaut déjà `syno-ia-svc`.

### 2. Autoriser les utilisateurs *(à vérifier)*

Les utilisateurs qui se connecteront à `syno-ia` doivent eux aussi avoir le droit
d'application **File Station** — c'est ce droit qui permet à `syno-ia` de vérifier leurs
permissions en leur nom. Il est **accordé par défaut** à tout nouveau compte DSM : vous
n'avez rien à faire, sauf s'il a été explicitement retiré.

*Symptôme en cas d'oubli : l'utilisateur se connecte mais ne voit aucun partage.*

### 3. Pare-feu et Auto Block *(à vérifier)*

Le conteneur joint DSM par la passerelle du bridge Docker (`172.17.0.1:5000`). **Si le
pare-feu DSM est désactivé — c'est le cas par défaut — cette étape ne vous concerne pas.**

- **Panneau de configuration → Sécurité → Pare-feu** : si un pare-feu est actif, ajoutez
  une règle d'autorisation pour la source `172.17.0.0/16` vers le port `5000`, **au-dessus**
  de toute règle de blocage.
- **Panneau de configuration → Sécurité → Protection → Blocage auto** : ajoutez
  `172.17.0.0/16` à la **liste d'autorisation**. Tous les conteneurs partagent cette
  adresse source : sans cela, un autre conteneur qui échoue ses connexions pourrait faire
  bloquer `syno-ia` — et réciproquement.

*Symptôme en cas d'oubli : erreur `407` à la connexion.*

`syno-ia` limite lui-même les dégâts : après un refus d'identifiants du compte de service,
il **cesse** de réessayer jusqu'à une reconnexion manuelle depuis le panneau
d'administration.

### 4. HTTPS *(facultatif)*

Par défaut, `DSM_URL=http://172.17.0.1:5000` : le trafic ne quitte pas la machine, et le
chiffrer n'apporterait rien. Changez-le uniquement si votre politique interne l'impose —
mettez alors `https://172.17.0.1:5001` et laissez `DSM_VERIFY_SSL=false` si le certificat
DSM est auto-signé.

Pour exposer `syno-ia` lui-même en HTTPS — une fois l'application installée — placez-le
derrière le **Reverse Proxy** de DSM (Panneau de configuration → Portail de connexion →
Reverse Proxy) en activant `WebSocket`/`HTTP/1.1` et en désactivant la mise en tampon des
réponses (le chat utilise des flux SSE).

Pour exposer `syno-ia` lui-même en HTTPS — une fois l'application installée — placez-le
derrière le **Reverse Proxy** de DSM (Panneau de configuration → Portail de connexion →
Reverse Proxy) en activant `WebSocket`/`HTTP/1.1` et en désactivant la mise en tampon des
réponses (le chat utilise des flux SSE).

---

## Installation sur le NAS

Le compte de service de l'étape précédente doit exister : notez son nom et son mot de
passe, ils sont demandés dans le fichier `.env`.

### Quels dossiers seront indexés ?

**Rien n'est indexé automatiquement.** L'application ne lit que les dossiers déclarés dans
`INDEX_ROOTS`, dont la valeur par défaut est `/volume1/homes` — les dossiers personnels.
Aucun autre partage du NAS n'est touché tant que vous ne l'ajoutez pas : un partage absent
de cette liste reste totalement invisible, pour tout le monde.

Les permissions interviennent **ensuite** : parmi les dossiers indexés, chaque utilisateur
ne voit que ce que DSM l'autorise à lire. Déclarer un partage ne le rend donc pas public,
mais ne pas le déclarer le rend définitivement absent.

**Faut-il monter ces dossiers ?** Deux modes, au choix :

| | `INDEX_MODE=mount` *(défaut)* | `INDEX_MODE=filestation` |
|---|---|---|
| Montages `-v` | un par partage à indexer | **aucun** |
| Lecture des fichiers | directe sur le disque | via l'API DSM (compte de service) |
| Vitesse d'indexation | rapide | plus lente, plus de réseau |
| Sécurité | **identique** : les ACL DSM sont rejouées dans les deux cas | |

Le mode `mount` est recommandé sur un DS218+, dont le CPU est déjà limité. Si vous
préférez ne rien monter, passez simplement `INDEX_MODE=filestation` et omettez les `-v`
de partage (celui de `/app/data` reste nécessaire).

> **Les dossiers personnels (`homes`) sont indexés par défaut.**
> Chaque utilisateur y retrouve **ses propres documents**, et uniquement les siens.
> DSM présente le dossier personnel sous l'alias `/home`, alors que le compte de service
> qui indexe voit le partage parent `/homes` ; `syno-ia` rejoue cette traduction au moment
> du contrôle d'accès, si bien qu'un seul index suffit — les dossiers partagés ne sont
> jamais indexés en double. Le dossier d'un tiers reste `/homes/<tiers>`, nom qui n'existe
> pas pour un utilisateur ordinaire : il est écarté avant même d'interroger DSM.
> Seul un **administrateur**, à qui DSM montre `/homes`, y a accès — comme dans File Station.
>
> Prérequis : le service **Dossier personnel utilisateur** doit être activé dans DSM
> (*Panneau de configuration → Utilisateur et groupe → Avancé*). En mode `filestation`, le
> compte de service doit en outre appartenir au groupe **administrators** pour parcourir
> `/homes` ; en mode `mount`, la lecture se fait sur le disque et aucun droit particulier
> n'est requis.

### Installation par SSH

```bash
ssh admin@<ip-du-nas>

sudo mkdir -p /volume1/docker/apps/syno-ia/data
cd /volume1/docker/apps/syno-ia

# 1. Configuration de départ
curl -fsSL https://raw.githubusercontent.com/flake9025/syno-ia/main/.env.example -o .env

# 2. Clé de signature des sessions (à générer avant le premier démarrage)
echo "APP_SECRET=$(openssl rand -hex 32)" >> .env

# 3. Mot de passe du compte de service créé à l'étape « Configuration DSM »
vi .env          # DSM_SERVICE_PASSWORD

# 4. Démarrage — cette commande fonctionne telle quelle
sudo docker run -d \
  --name syno-ia \
  -p 8083:8080 \
  --env-file /volume1/docker/apps/syno-ia/.env \
  -e INDEX_MODE=mount \
  -e INDEX_ROOTS=/volume1/homes \
  -v /volume1/docker/apps/syno-ia/data:/app/data \
  -v /volume1/homes:/volume1/homes:ro \
  --memory=5g \
  --restart unless-stopped \
  ghcr.io/flake9025/syno-ia:latest
```

Par défaut, `syno-ia` indexe les **dossiers personnels** : `/volume1/homes` existe sur
tout NAS dont le service *Dossier personnel utilisateur* est activé, et chaque utilisateur
n'y retrouvera que les siens. Rien à adapter.

Pour ajouter des **dossiers partagés**, repérez d'abord leurs noms exacts :

```bash
ls -1 /volume1/
```

Vous obtenez la liste réelle de vos partages — `Documents`, `Projets`, `photo`… Ajoutez
ceux qui vous intéressent à `INDEX_ROOTS` (séparés par des virgules) et montez-les chacun,
en respectant la casse :

```bash
  -e INDEX_ROOTS=/volume1/homes,/volume1/Documents,/volume1/Projets \
  -v /volume1/homes:/volume1/homes:ro \
  -v /volume1/Documents:/volume1/Documents:ro \
  -v /volume1/Projets:/volume1/Projets:ro \
```

Ouvrez ensuite `http://<ip-du-nas>:8083` et connectez-vous avec **votre compte DSM
habituel** — pas avec le compte de service, qui ne sert qu'aux vérifications internes.

> ⚠️ **Montez chaque partage au chemin identique à celui du NAS.**
> La syntaxe est `-v <chemin sur le NAS>:<chemin dans le conteneur>:ro`, et les deux
> doivent être **les mêmes** : `-v /volume1/Projets:/volume1/Projets:ro`. Cette identité
> est ce qui permet de faire correspondre un fichier indexé à son chemin DSM, donc de
> rejouer ses ACL. Un montage vers `/data/docs` casserait cette correspondance.
> Le suffixe `:ro` monte en lecture seule : l'application ne peut jamais écrire dans vos
> documents.
>
> Vous n'indexez que ce que vous montez : un partage absent de cette liste reste
> totalement invisible, pour tout le monde.

### Variante avec LLM 100 % local

Même commande, avec l'image `:latest-llm` — elle embarque `llama.cpp` compilé **sans
AVX**, seule variante qui fonctionne sur les Celeron « Apollo Lake » (DS218+, DS718+,
DS918+) :

```bash
# 4 bis. Démarrage avec LLM local
sudo docker run -d \
  --name syno-ia \
  -p 8083:8080 \
  --env-file /volume1/docker/apps/syno-ia/.env \
  -e INDEX_MODE=mount \
  -e INDEX_ROOTS=/volume1/homes \
  -v /volume1/docker/apps/syno-ia/data:/app/data \
  -v /volume1/homes:/volume1/homes:ro \
  --memory=5g \
  --restart unless-stopped \
  ghcr.io/flake9025/syno-ia:latest-llm
```

Puis, depuis le panneau d'administration, onglet **Modèles** : *Télécharger le modèle
recommandé*. Le fichier GGUF (229 Mo pour le profil `nano`, ~1 Go pour `small`) est stocké
dans `/app/data/models` et survit aux mises à jour.

Tant que le modèle n'est pas téléchargé, `LLM_BACKEND=auto` laisse l'application en mode
extractif ; elle bascule toute seule sur `llamacpp` dès que le fichier est présent.

### Variante : rédaction déportée sur un PC (Ollama)

Sur un DS218+, la recherche documentaire est instantanée, mais la **rédaction** de la
réponse demande deux à trois minutes : le processeur doit relire tout le contexte avant
d'écrire le premier mot, et un Celeron sans AVX plafonne à quelques GFLOPS. Ce n'est pas
un défaut de réglage, c'est de l'arithmétique.

D'où cette variante : le NAS garde ce qu'il fait bien — indexer, chercher, **filtrer
selon les droits** — et confie la seule rédaction à un PC du réseau local.

> **Ce qui sort du NAS** : la question et les trois extraits déjà sélectionnés
> (1 à 3 Ko), rien d'autre. Le PC n'indexe rien, ne voit pas le système de fichiers et
> ne contourne aucune permission : le filtrage ACL a déjà eu lieu, en amont.

**Sur le PC** (Windows) :

```powershell
winget install Ollama.Ollama
# Par défaut Ollama n'écoute que sur 127.0.0.1 : le NAS serait refusé.
setx OLLAMA_HOST "0.0.0.0"
# puis redémarrez Ollama pour que la variable soit prise en compte
ollama pull qwen2.5:7b-instruct
```

Pensez à autoriser le port **11434** dans le pare-feu Windows pour le réseau privé.
C'est, de loin, la cause n°1 d'échec.

**Sur le NAS** : ⚙️ → *Modèles* → **Génération déportée**. L'adresse est pré-remplie avec
l'IP de la machine depuis laquelle vous consultez l'interface. Le bouton **Tester** sonde
Ollama **depuis le NAS** — le seul point de vue qui compte, car un Ollama joignable depuis
votre navigateur ne l'est pas forcément depuis le conteneur.

Le réglage est conservé dans `data/overrides.json` : il survit aux mises à jour et à la
recréation du conteneur.

Deux conséquences utiles :

- **Repli automatique.** Le PC n'est pas toujours allumé. Avant chaque question,
  l'application vérifie si Ollama répond ; sinon elle rebascule sur le modèle local du
  NAS, sans intervention. Si la panne survient en cours de rédaction, le texte déjà
  affiché est conservé tel quel — une réponse ne se réécrit jamais sous vos yeux.
- **Contexte plus large.** Quand la rédaction part sur le PC, le NAS cesse de rationner
  les extraits (1200 → 5000 caractères, 3 → 5 passages, historique plus long) : brider le
  prompt n'aurait de sens que pour épargner un processeur qui ne travaille plus.

### Mettre à jour

Reprenez **la même étiquette qu'au démarrage** : `:latest-llm` avec le LLM local,
`:latest` sinon. Se tromper d'étiquette remplace silencieusement l'image par une
variante dépourvue de `llama.cpp`.

```bash
sudo docker pull ghcr.io/flake9025/syno-ia:latest-llm
sudo docker stop syno-ia && sudo docker rm syno-ia
# puis relancez la commande « docker run » ci-dessus, à l'identique
```

`docker rm` ne supprime que le conteneur : l'index, les appareils mémorisés et les
modèles GGUF résident dans `/volume1/docker/apps/syno-ia/data` et sont conservés.

Pour vérifier que la nouvelle image tourne réellement, ouvrez
`http://<adresse-du-nas>:8083/api/health` : `build` porte l'empreinte du commit déployé
et `uptime` repart de zéro. Le panneau ⚙️ → *Modèles* reflète lui aussi la version : si
le modèle `nano` n'y figure pas, c'est que l'ancienne image tourne encore.

### Mise à jour en un clic (`scripts/update.sh`)

Pour éviter la séquence manuelle ci-dessus, le dépôt fournit `scripts/update.sh`. Il
récupère l'image, redémarre la pile, **attend que le service réponde** et compare
l'empreinte `build` avant/après : une mise à jour qui n'a pas pris est signalée comme un
échec au lieu de passer inaperçue.

Il suppose un `docker-compose.yml` déposé à côté de lui :

```bash
sudo mkdir -p /volume1/docker/apps/syno-ia
cd /volume1/docker/apps/syno-ia
# déposez-y docker-compose.yml, .env et scripts/update.sh
sudo chmod +x update.sh
sudo ./update.sh && tail -n 30 update.log
```

Puis **DSM → Panneau de configuration → Planificateur de tâches → Créer → Tâche
planifiée → Script défini par l'utilisateur**, exécutée par `root` :

```bash
/volume1/docker/apps/syno-ia/update.sh
```

Dans l'onglet *Paramètres de la tâche*, cochez **« Envoyer les détails d'exécution par
courriel »** et **« uniquement si le script se termine anormalement »** : c'est la façon
documentée et fiable d'être prévenu, le script renvoyant un code non nul en cas d'échec.

Quelques points à connaître :

- Le script **possède la pile**. Ne créez pas en parallèle un *Projet* dans Container
  Manager pointant sur le même dossier : Compose déduirait un second nom de projet et
  vous vous retrouveriez avec deux piles concurrentes sur le même port. L'onglet
  *Conteneurs* liste de toute façon le conteneur, quelle que soit son origine.
- Container Manager installe Docker hors du `PATH` des tâches planifiées ; le script
  cherche le binaire aux emplacements connus (`SYNO_IA_DOCKER` pour forcer un chemin).
- Variables disponibles : `SYNO_IA_DIR`, `SYNO_IA_URL`, `SYNO_IA_WAIT`, `SYNO_IA_LOG`,
  `SYNO_IA_PROJET`, `SYNO_IA_DOCKER`.
- Le journal `update.log` est tourné automatiquement au-delà de 1 Mo.

### Variante sans aucun montage

Mêmes paramètres, mais `INDEX_MODE=filestation` et plus aucun montage de partage — seul
celui des données de l'application subsiste :

```bash
sudo docker run -d \
  --name syno-ia \
  -p 8083:8080 \
  --env-file /volume1/docker/apps/syno-ia/.env \
  -e INDEX_MODE=filestation \
  -e INDEX_ROOTS=/volume1/homes \
  -v /volume1/docker/apps/syno-ia/data:/app/data \
  --memory=5g \
  --restart unless-stopped \
  ghcr.io/flake9025/syno-ia:latest
```

`INDEX_ROOTS` reste obligatoire : il désigne toujours les dossiers à parcourir, qui sont
cette fois lus par l'API DSM au lieu du disque.

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

**Appareil de confiance.** Si votre compte DSM utilise la double authentification, le code à
usage unique est réclamé à chaque connexion. Cochez *Faire confiance à cet appareil* en saisissant
le code : DSM émet alors un jeton d'appareil que `syno-ia` conserve dans un second cookie signé,
valable `DEVICE_TRUST_DAYS` jours (30 par défaut).

- Le **mot de passe reste exigé** à chaque connexion : seul le second facteur est allégé.
- Le jeton est lié au compte qui l'a obtenu : il ne dispense pas un autre utilisateur du code.
- Il survit à la déconnexion (c'est tout l'intérêt) ; pour l'oublier, décochez la case lors d'une
  connexion avec code. Révoquer l'appareil depuis DSM le neutralise également.
- `DEVICE_TRUST_DAYS=0` désactive complètement la fonction.

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

**Dossiers personnels.** L'index enregistre les documents personnels sous
`/homes/<compte>`, nom que voit le compte de service. Or DSM ne montre jamais `/homes` à
un utilisateur ordinaire : son propre dossier lui apparaît sous l'alias `/home`. Les deux
étapes ci-dessus traduisent donc `/homes/<compte>` en `/home` **pour le seul propriétaire
du dossier**. Le dossier d'un tiers conserve son nom `/homes/<tiers>`, introuvable dans la
liste des partages de l'utilisateur : il est écarté à l'étape 1, sans même interroger DSM.
Un index unique suffit, et les dossiers partagés ne sont jamais dupliqués.

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
lorsque la mémoire est abondante — réservé aux processeurs dotés d'AVX ou de NEON, car
la mémoire ne compense pas un processeur lent. À l'inverse, un processeur **dépourvu de
toute instruction vectorielle** descend d'un cran supplémentaire, jusqu'au palier `nano`.

| Profil | RAM disponible | Calcul | LLM local | Prompt | Embeddings |
|---|---|---|---|---|---|
| `nano` | < 1,5 Go | sans SIMD | LFM2 350M Q4_K_M (229 Mo) | 2 000 car. | model2vec `potion-multilingual-128M` |
| `micro` | < 1,5 Go | faible | Qwen2.5 0.5B Q4_K_M | 3 500 car. | model2vec `potion-multilingual-128M` |
| `small` | 1,5 – 3 Go | modeste | Qwen2.5 1.5B Q4_K_M | 5 000 car. | model2vec `potion-multilingual-128M` |
| `medium` | 3 – 7 Go | correct | Qwen2.5 3B Q4_K_M | 7 000 car. | fastembed `paraphrase-multilingual-MiniLM-L12-v2` |
| `large` | > 7 Go | AVX2, 4 cœurs+ | Qwen2.5 7B Q4_K_M | 10 000 car. | fastembed `multilingual-e5-large` |

La colonne *Prompt* suit le profil de génération : c'est le processeur, et non la
mémoire, qui doit relire le contexte avant de produire le premier mot.

> **Le LLM ne fait pas la recherche.** Les documents sont trouvés par BM25 et les
> embeddings ; le modèle ne fait que reformuler les passages déjà sélectionnés. Un tout
> petit modèle suffit donc parfaitement, et le [mode extractif](#variables-denvironnement)
> (`LLM_BACKEND=none`) répond même sans aucun LLM.

### Cas concret : DS218+ avec 8 Go

| Mesure | Valeur |
|---|---|
| Processeur | Intel Celeron J3355, 2 cœurs, **sans AVX** |
| Niveau mémoire | `medium` |
| Niveau calcul | `micro` (score ≈ 2,0) |
| **Profil retenu** | **`nano`** — LFM2 350M Q4_K_M, 229 Mo |
| Taille du prompt | 2000 caractères, 3 extraits |
| Fils de génération | 1 — le second cœur reste au serveur web |
| Embeddings | model2vec (statiques, pas d'ONNX : trop lent sans AVX) |

LFM2 350M est conçu pour l'embarqué et reste **multilingue, français compris**. Il est
30 % plus petit que Qwen2.5 0.5B et nettement plus rapide sur un processeur sans AVX.

La mémoire abondante n'accorde **aucun** cran de tolérance ici : sans AVX, un modèle
trois fois plus gros serait trois fois plus lent sans rien apporter. Pour la même raison,
**la taille du prompt suit le processeur et non la mémoire** : le contexte doit être relu
entièrement avant le premier mot de la réponse, et 8 Go de RAM ne font pas lire plus vite
un Celeron. Pour gagner encore en réactivité, deux alternatives :

1. **Mode extractif** (`LLM_BACKEND=none`) — réponse instantanée constituée des passages
   pertinents cités. Pas de reformulation, mais immédiat et toujours exact.
2. **LLM distant** — un PC du réseau avec Ollama (`LLM_BACKEND=ollama`), ou un service
   compatible OpenAI. L'indexation et le filtrage ACL restent sur le NAS ; seuls les
   extraits déjà autorisés sont transmis.

Le profil peut être forcé : `HARDWARE_PROFILE=nano|micro|small|medium|large`. Changer de
profil ne touche qu'au LLM : le modèle d'embeddings suit la mémoire, l'index vectoriel
déjà construit reste donc valide. Si le modèle du nouveau profil n'est pas encore
téléchargé, un modèle déjà présent est utilisé en attendant.

#### Accélérer les réponses

| Levier | Effet |
|---|---|
| `HARDWARE_PROFILE=nano` | Modèle le plus petit (350M) **et** prompt le plus court : le réglage le plus rapide. |
| `LLM_MAX_TOKENS=350` | Plafonne la longueur des réponses, donc l'attente maximale. |
| `CONTEXT_MAX_CHARS=1500` | Prompt plus court à analyser avant le premier jeton. |
| `RETRIEVAL_TOP_K=3` | Moins d'extraits envoyés au modèle. |
| `LLM_TIMEOUT_SECONDS=90` | Arrête la génération plus tôt et renvoie ce qui est prêt. |
| `LLM_BACKEND=none` | Réponses extractives, instantanées. |
| Case **« Répondre avec l'IA »** décochée | Même effet que `none`, mais décidé question par question, sans redémarrage. |
| [Rédaction déportée](#variante--rédaction-déportée-sur-un-pc-ollama) | Le seul levier qui supprime vraiment l'attente. |

Chaque réponse affiche le moteur réellement utilisé et le temps passé (total, premier jeton,
jetons/seconde) : de quoi mesurer l'effet de ces réglages sans quitter l'interface.

Au-delà de `LLM_TIMEOUT_SECONDS` (600 s par défaut), la génération est **arrêtée net** et
le texte déjà produit est conservé, accompagné d'un avertissement. Le NAS ne reste donc
jamais bloqué sur une réponse interminable, et llama.cpp cesse aussitôt de consommer les
cœurs. Si le délai expire **sans le moindre mot** — modèle trop lourd pour la machine —
l'application bascule sur la réponse extractive et cite les passages trouvés : vous obtenez
toujours quelque chose d'exploitable. `0` lève la limite.

> Ce délai est volontairement large : sur un petit NAS, une réponse *finit* par arriver, et
> mieux vaut l'attendre en étant prévenu que de la voir coupée à 120 s. C'est aussi
> pourquoi la case « Répondre avec l'IA » existe — l'attente doit être choisie.

#### Faire de la place

⚙️ → *Modèles* liste le catalogue avec la taille des fichiers déjà téléchargés. Le bouton
**Supprimer** efface le GGUF du NAS. Si le modèle supprimé était en service, le moteur est
reconstruit aussitôt : il se rabat sur un autre modèle installé, ou sur le mode extractif.

---

## Variables d'environnement

Fichier complet et commenté : [`.env.example`](.env.example). L'essentiel :

| Variable | Défaut | Description |
|---|---|---|
| `APP_SECRET` | *(aléatoire)* | Clé de signature des cookies. **À fixer** en production. |
| `DSM_URL` | `http://172.17.0.1:5000` | DSM vu depuis le conteneur. |
| `DSM_SERVICE_ACCOUNT` / `DSM_SERVICE_PASSWORD` | `syno-ia-svc` / — | Compte de service pour l'indexation. |
| `ADMIN_ACCOUNTS` | *(vide)* | Comptes DSM admin de `syno-ia` en plus des admins DSM. |
| `INDEX_MODE` | `mount` | `mount` (partages montés) ou `filestation` (API DSM). |
| `INDEX_ROOTS` | `/volume1/homes` | Racines à indexer, séparées par des virgules. Par défaut les dossiers personnels ; ajoutez vos dossiers partagés (`ls -1 /volume1/`) et montez-les aux mêmes chemins. |
| `INDEX_INTERVAL_MINUTES` | `360` | Réindexation automatique (`0` = désactivée). |
| `ACL_STRICT` | `true` | Vérification fichier par fichier des ACL avancées. |
| `ACL_CACHE_TTL` | `300` | Durée de vie d'une décision d'accès (secondes). |
| `SESSION_TTL_MINUTES` | `720` | Durée d'une session web. |
| `DEVICE_TRUST_DAYS` | `30` | Durée de validité d'un appareil approuvé, dispensé du code 2FA (`0` = désactivé). Le mot de passe reste toujours exigé. |
| `EMBEDDING_BACKEND` | `auto` | `auto`, `model2vec`, `fastembed`, `none`. |
| `EMBEDDING_MODEL` | *(selon profil)* | Dépôt Hugging Face. En mode `fastembed`, le modèle **doit** figurer dans le catalogue ONNX (`TextEmbedding.list_supported_models()`), sinon l'application bascule automatiquement sur model2vec. |
| `EMBEDDING_ASYNC_LOAD` | `true` | Charge le modèle en arrière-plan (voir [Premier démarrage](#premier-démarrage)). `false` rend le démarrage bloquant. |
| `EMBEDDING_BACKFILL_BATCH` | `64` | Fragments vectorisés par lot lors du rattrapage. |
| `LLM_BACKEND` | `auto` | `auto`, `llamacpp`, `ollama`, `openai`, `none`. |
| `LLM_TIMEOUT_SECONDS` | `600` | Délai maximal d'une génération ; au-delà, la réponse est tronquée proprement, ou remplacée par les extraits trouvés si aucun mot n'a été produit (`0` = sans limite). |
| `LLM_BATCH_SIZE` | `512` | Jetons lus par passe pendant l'analyse du prompt. En dessous, la lecture dégénère en produits matrice-vecteur et devient 2 à 4 fois plus lente. À ne réduire qu'en cas de manque de mémoire. |
| `LLM_THREADS` | `0` | Fils de calcul llama.cpp. `0` = automatique : **un cœur reste toujours libre** pour que le serveur continue de répondre pendant la génération. Ne montez à `cpu_count` qu'au prix de redémarrages intempestifs. |
| `OLLAMA_URL` | `http://172.17.0.1:11434` | Ollama local ou distant. Modifiable à chaud depuis ⚙️ → *Modèles*, le réglage étant alors conservé dans `data/overrides.json`. |
| `OPENAI_API_KEY` | *(vide)* | Service compatible OpenAI. |
| `HARDWARE_PROFILE` | `auto` | Forçage du profil (`nano`…`large`). |

En mode `auto`, le LLM est choisi dans cet ordre : service compatible OpenAI (si une clé
est présente) → Ollama (s'il répond) → `llama.cpp` local (si un modèle est présent) →
**mode extractif**.

Quand Ollama **et** un modèle local sont tous deux disponibles, les deux sont chaînés :
Ollama rédige, et le modèle local prend le relais s'il ne répond plus. Les réglages
enregistrés depuis l'administration (`data/overrides.json`) l'emportent sur les variables
d'environnement correspondantes.

---

## Utilisation

1. Ouvrez `http://<ip-du-nas>:8083` et connectez-vous avec votre compte DSM.
2. La barre latérale affiche les partages indexés **que vous pouvez consulter**.
3. Posez votre question. Les réponses arrivent en flux, avec des citations `[1]`, `[2]`…
   cliquables qui ouvrent le document source. Sous chaque réponse figurent le moteur
   utilisé et les temps mesurés.
4. Les administrateurs disposent d'un panneau (⚙️) : état du matériel, avancement de
   l'indexation, téléchargement et suppression de modèles, génération déportée, sessions
   actives, reconnexion DSM.

### Répondre avec l'IA, ou simplement citer les documents

Sous la zone de saisie, une case **« Répondre avec l'IA »** décide, question par question,
de ce que l'application fait des passages trouvés :

- **Décochée** — les extraits pertinents s'affichent aussitôt, avec leurs liens. La
  recherche sémantique et le filtrage des droits ont bien eu lieu : seule la rédaction est
  sautée. Sur un DS218+, c'est une réponse en moins d'une seconde.
- **Cochée** — un modèle rédige une synthèse à partir de ces extraits. Comptez deux à trois
  minutes sur un petit NAS, quelques secondes avec une [rédaction
  déportée](#variante--rédaction-déportée-sur-un-pc-ollama).

Sur les profils `nano` et `micro`, la case est **décochée par défaut** et signalée comme
lente ; votre choix est ensuite mémorisé. Comme l'attente peut être longue, le navigateur
propose d'envoyer une **notification** lorsque la réponse est prête : inutile de garder
l'onglet sous les yeux.

La première indexation d'un corpus de quelques milliers de documents prend de 20 minutes à
plusieurs heures sur un DS218+. Elle est incrémentale : les exécutions suivantes ne
retraitent que ce qui a changé (taille ou date de modification).

### Premier démarrage

Au tout premier lancement, le modèle d'embeddings doit être téléchargé depuis Hugging
Face — quelques centaines de mégaoctets, soit plusieurs minutes sur la liaison d'un NAS
domestique. **L'application n'attend pas** : elle répond immédiatement en recherche
lexicale (BM25), pleinement utilisable, et bascule en recherche hybride dès que le moteur
est prêt.

Concrètement :

1. L'interface est accessible en quelques secondes ; le bandeau sous les réponses indique
   *« le moteur sémantique se prépare encore »*.
2. L'indexation peut démarrer en parallèle : les documents sont découpés et interrogeables
   par mots-clés tout de suite.
3. Dès que le modèle est chargé, les fragments déjà indexés sont vectorisés par lots —
   **sans réindexation**, les documents n'étant ni relus ni redécoupés.
4. Le panneau d'administration (⚙️ → *Aperçu* → *Moteur*) affiche l'état :
   `chargement du modèle…`, puis `vectorisation 320/1200`, puis `recherche hybride active`.

C'est aussi ce qui évite une panne silencieuse : un chargement bloquant pouvait dépasser le
délai du *healthcheck* Docker, qui relançait alors le conteneur — lequel recommençait le
téléchargement depuis le début, indéfiniment.

Le même mécanisme s'applique après un changement de `EMBEDDING_MODEL` : les vecteurs dont
la dimension ne correspond plus sont recalculés en arrière-plan.

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
│             (vecteurs rattrapés après coup si le moteur       │
│              n'était pas encore chargé)                        │
├───────────────────────────────────────────────────────────────┤
│ DSM         list_share (partages) │ getinfo (ACL par fichier)  │
└───────────────────────────────────────────────────────────────┘
```

Choix techniques notables :

- **Un seul fichier SQLite en WAL**, pas de base vectorielle externe *(voir ci-dessous)*.
- **RRF (k = 60)** plutôt qu'une somme pondérée : aucune calibration de scores nécessaire
  entre les deux moteurs de recherche.
- **FTS5 avec `remove_diacritics 2`** : « procedure » trouve « procédure ».
- **Aucune étape de build front-end** : pas de Node, pas de bundler, pas de CVE npm.
- **Vérification ACL par lots de 20**, avec repli automatique chemin par chemin : DSM ne
  documente pas le comportement de `getinfo` en cas d'échec partiel, et un seul fichier
  supprimé depuis l'indexation ferait échouer tout le lot.
- **Chargement différé des embeddings** : le moteur est encapsulé dans un proxy
  (`DeferredEmbedder`) que le pipeline et l'indexeur interrogent à chaque appel. Le
  remplacer à chaud suffit à faire basculer l'application de BM25 vers la recherche
  hybride, sans reconstruire aucun composant ni redémarrer le service.

### Pourquoi pas de « base vectorielle » ?

La plupart des projets de RAG installent une base dédiée (Chroma, Qdrant, Milvus…). Ici,
tout tient dans le même fichier SQLite. Ce n'est pas un raccourci : c'est que, à cette
échelle, une base vectorielle ne sert à rien.

**Ce qu'on stocke.** Le modèle d'embeddings transforme chaque extrait de document en une
liste de nombres — 256 sur un DS218+, jusqu'à 1024 avec les modèles les plus gros — qui
résume son *sens*. Deux extraits qui parlent de la même chose obtiennent des listes
proches, même sans aucun mot en commun. C'est ce qui permet de retrouver « congés payés »
en cherchant « vacances ».

**Comment on les range.** Cette liste est enregistrée telle quelle, dans une colonne
binaire de la table des extraits. SQLite ne sait pas qu'il s'agit de vecteurs, et n'a pas
besoin de le savoir : il sert ici de simple boîte de rangement. Le calcul, lui, se fait en
mémoire.

**Comment on cherche.** Chercher revient à comparer la question à chaque extrait et à
garder les plus proches. C'est une comparaison exhaustive, sans malice. Sur un corpus réel
de 25 000 extraits, cela représente environ six millions de multiplications : NumPy les
effectue en quelques millisecondes, même sur le processeur modeste d'un DS218+ — et la
table complète ne pèse qu'une trentaine de mégaoctets, chargée une fois pour toutes.

**À quoi sert alors une base vectorielle ?** À éviter cette comparaison exhaustive quand
elle devient trop coûteuse — à partir de quelques millions d'extraits. Elle emploie pour
cela des index *approximatifs* : plus rapides, mais qui peuvent manquer le bon résultat.
En dessous de ce seuil, on paierait un service supplémentaire à installer, à sauvegarder
et à maintenir, en échange d'une recherche à la fois **moins exacte** et **pas plus
rapide**.

Conséquence pratique : une sauvegarde de `syno-ia`, c'est la copie d'un dossier.

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
| Les réponses ignorent le sens des mots | Le moteur sémantique n'est pas encore prêt (BM25 seul) | Normal au premier démarrage : voir [Premier démarrage](#premier-démarrage). Suivez l'état dans ⚙️ → *Aperçu* → *Moteur* |
| `État : indisponible` dans le panneau *Moteur* | Téléchargement du modèle impossible (réseau, DNS, quota Hugging Face) | L'application reste utilisable en BM25. Rétablissez l'accès sortant du conteneur, puis relancez le chargement sans redémarrer : `POST /api/admin/embeddings/reload` |
| Réponses très lentes | Prompt trop long pour le CPU, ou LLM trop gros | Mettez l'image à jour : le prompt suit désormais le processeur. Sinon `CONTEXT_MAX_CHARS=1500`, `LLM_MAX_TOKENS=350`, ou `LLM_BACKEND=none` (extractif) |
| « Le serveur est injoignable » après une longue attente | La génération n'aboutissait pas et bloquait la requête | Mettez l'image à jour : le prompt est raccourci sur les CPU lents, et la génération s'arrête d'elle-même à `LLM_TIMEOUT_SECONDS` |
| « Le serveur est injoignable » au bout d'une minute, le conteneur a redémarré | La génération occupait **tous** les cœurs : la sonde de santé n'était plus servie | Mettez l'image à jour (`docker pull ghcr.io/flake9025/syno-ia:latest-llm`) : un cœur est désormais réservé au serveur web |
| Le modèle `nano` n'apparaît pas dans ⚙️ → *Modèles* | L'ancienne image tourne encore | Voir [Mettre à jour](#mettre-à-jour) — vérifiez l'étiquette `:latest-llm` et le champ `build` de `/api/health` |
| Le bouton *Tester* ne joint pas Ollama, alors que `http://localhost:11434` marche sur le PC | Ollama n'écoute que sur `127.0.0.1` | `setx OLLAMA_HOST "0.0.0.0"`, **redémarrez Ollama**, puis ouvrez le port 11434 dans le pare-feu Windows pour le réseau privé |
| La rédaction repart sur le NAS alors qu'Ollama est configuré | Le PC était éteint ou injoignable au moment de la question | C'est le repli voulu. Le moteur réellement utilisé est affiché sous chaque réponse et dans le bandeau |
| Le chat n'affiche rien derrière un reverse proxy | Mise en tampon des réponses SSE | Désactivez le *buffering* dans la configuration du proxy |

Journaux : `docker logs -f syno-ia`. État du service : `GET /api/health` (public, sans
aucune statistique d'index). Diagnostic complet, dont les compteurs d'indexation :
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
- **La génération de texte sur un NAS d'entrée de gamme est lente, par construction.** Le
  processeur doit relire tout le prompt avant d'écrire le premier mot : sur un Celeron sans
  AVX, cela représente plusieurs centaines de milliards d'opérations, soit deux à trois
  minutes. Aucun réglage ne contourne cette limite — seules la [rédaction
  déportée](#variante--rédaction-déportée-sur-un-pc-ollama) ou les réponses extractives y
  parviennent. La recherche, elle, reste instantanée sur ces machines.
- Un Ollama distant est interrogé **en clair sur le réseau local** : question et extraits
  transitent sans chiffrement. À réserver à un réseau de confiance.

---

## Licence

MIT — voir [`LICENSE`](LICENSE).

Ce projet n'est ni affilié à, ni approuvé par Synology Inc. « Synology » et « DSM » sont des
marques de leurs propriétaires respectifs.
