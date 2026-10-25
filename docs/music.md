# Musique dans les salons vocaux (`/play`, `/skip`, `/queue`…, version 1.12.0)

Samae peut jouer de la musique dans un salon vocal : recherche ou lien, file d'attente par
serveur, panneau à boutons, volume, historique et favoris enregistrés dans la base SQLite.

La musique est **désactivée par défaut** (`MUSIC_ENABLED=false`). Tant qu'elle l'est, ou
tant que Lavalink n'est pas joignable, les commandes de musique répondent par une
explication et **le reste du bot ne change pas**.

Depuis 1.12.0, les commandes sont **au premier niveau** (`/play`, `/skip`…, comme les autres
bots de musique) au lieu du groupe `/musique`, et elles fonctionnent sur **n'importe quel
serveur** où le bot est invité (une file, un panneau, un volume et un rôle DJ par serveur).
Le diagnostic « pas de son » est au §8 bis.

> Ce document est public : il ne contient ni adresse de serveur, ni mot de passe.
> L'installation de Lavalink sur le VPS est décrite pas à pas, pour la personne qui a les
> droits d'administration, dans [music-vps-checklist.md](music-vps-checklist.md).

## 1. Comment ça marche

Le bot **n'envoie jamais le son lui-même**. Il pilote un second programme, **Lavalink**,
qui tourne sur la même machine, cherche les titres, décode l'audio et l'envoie à Discord
(y compris le chiffrement de bout en bout « DAVE » que Discord impose aux salons vocaux).

```
Discord  ◄──── audio (UDP, DAVE) ──── Lavalink (Java, 127.0.0.1:2333)
   │                                       ▲
   │ commandes, boutons                    │ REST + WebSocket local, mot de passe
   ▼                                       │
Samae (Python) ── wavelink ────────────────┘
   │
   └── SQLite : historique, favoris, volume par serveur (schéma v13)
```

| Partie | Fichier | Rôle |
|---|---|---|
| Modèles | `src/music/models.py` | `Track`, `QueueEntry`, `SearchOutcome` (aucun import Discord) |
| Recherche | `src/music/search.py` | sources connues, filtre des liens, préfixes de recherche, conversion des réponses de Lavalink |
| File | `src/music/queue.py` | file FIFO bornée, une par serveur |
| Lecteur | `src/music/player.py` | état d'un serveur (déconnecté, en attente, lecture, pause) et transitions autorisées |
| Règles | `src/music/permissions.py` | qui peut faire quoi (§5) |
| Service | `src/music/service.py` | toute la logique : recherche, ajout, enchaînement, erreurs, historique |
| Adaptateur | `src/music/lavalink.py` | le seul fichier qui parle à wavelink / Lavalink |
| Base | `src/database/music.py` + migration v13 dans `src/database/connection.py` | historique, favoris, volume |
| Diagnostic | `src/music/diagnostics.py` | les 5 étapes du son (§8 bis), version de Lavalink, codes de fermeture vocale |
| Rôle DJ par serveur | `src/database/guild_settings.py` (migration v14) | choisi avec `/setup role-dj` |
| Discord | `src/discord_bot/cogs/music.py`, `src/discord_bot/music_ui.py` | commandes, panneau, boutons, menus |

Le service ne connaît Lavalink qu'à travers une interface (`AudioBackend`) : les tests
utilisent un faux moteur audio, et changer de moteur un jour ne toucherait qu'un fichier.

### Versions figées (vérifiées le 25/10/2026)

| Composant | Version | Pourquoi celle-ci |
|---|---|---|
| discord.py | 2.7.1 (inchangée) | déjà utilisée par le bot ; gère le chiffrement vocal DAVE |
| wavelink | 3.5.2 (`requirements.txt`) | demande discord.py ≥ 2.7.0, transmet l'identifiant de salon dont Lavalink a besoin pour DAVE |
| async-timeout | 5.0.1 | dépendance de wavelink, figée comme les autres |
| Lavalink | 4.2.2 (`Lavalink.jar`) | gère DAVE depuis 4.2.0 ; Java 17 ou plus |
| youtube-source (plugin) | 1.18.2 | plugin YouTube officiel de l'équipe Lavalink |
| LavaSrc (plugin, **facultatif**) | 4.8.3 | seulement pour Spotify / Deezer / Apple Music (§3) ; **non installé** par défaut |

Aucune version « latest » : Lavalink ne télécharge aucun plugin tout seul (les `.jar` sont
déposés à la main après vérification de leur empreinte SHA-256).

## 2. Commandes

Toutes s'utilisent sur un serveur (jamais en message privé). Les réponses sont
**éphémères** ; seul le panneau du lecteur est public, et il ne notifie personne.

| Commande | Effet |
|---|---|
| `/play query:<texte ou lien> [source:]` | un texte lance **le premier résultat** ; un lien (titre ou playlist) est ajouté directement |
| `/search query:<texte> [source:]` | propose les 5 premiers résultats dans un menu (réservé à la personne qui cherche) |
| `/pause` · `/resume` | pause et reprise |
| `/skip` | titre suivant |
| `/stop` | arrête et vide la file (le bot reste dans le salon) |
| `/leave` | arrête, vide la file et quitte le salon vocal |
| `/join` | fait venir le bot dans votre salon vocal sans rien lancer |
| `/nowplaying` | titre en cours : source, durée, progression, demandeur |
| `/queue` | titre en cours et file d'attente, par pages |
| `/remove position:<n°>` | retire un titre de la file (autocomplétion avec les titres en attente) |
| `/clear` | vide la file ; le titre en cours continue |
| `/shuffle` | mélange les titres en attente |
| `/loop mode:` | répétition : désactivée, titre en cours, ou toute la file |
| `/volume [level:0-100]` | sans valeur : affiche ; avec : règle et **retient** pour ce serveur |
| `/history` | 50 dernières lectures de ce serveur, par pages |
| `/favorite add` · `/favorite list` · `/favorite remove number:<n°>` | ajouter le titre en cours, voir et rejouer (menu), retirer un favori |
| `/audiocheck` | **pas de son ?** affiche les 5 étapes du son et la cause probable (§8 bis) |

Le **panneau** (message public dans le salon où la musique a été lancée) a six boutons :
pause/reprise, passer, arrêter, répétition, file, favori. Les boutons et les commandes
passent par **le même code** (`MusicCog._control` puis `MusicService`) : mêmes règles, mêmes
messages. Le panneau est modifié sur place (une modification par seconde au plus, jamais un
nouveau message à chaque action). Les boutons d'un ancien panneau répondent « ce panneau ne
correspond plus à la lecture en cours » : ils ne peuvent pas agir sur la mauvaise lecture.

### Correspondance avec les anciennes commandes (avant 1.12.0)

| Avant (`/musique …`) | Maintenant | Remarque |
|---|---|---|
| `jouer recherche:` (lien) | `/play query:` | identique |
| `jouer recherche:` (texte, menu de 5) | `/search query:` | `/play` avec un texte lance directement le 1er résultat |
| `pause` · `reprendre` | `/pause` · `/resume` | |
| `passer` | `/skip` | |
| `arreter` | `/stop` | |
| `quitter` | `/leave` | |
| `encours` | `/nowplaying` | |
| `file` | `/queue` | |
| `volume valeur:` | `/volume level:` | |
| `historique` | `/history` | |
| `favori` (ajoute ou retire le titre en cours) | `/favorite add` | ne retire plus : pas de suppression par erreur ; le bouton ★ du panneau garde l'ajout/retrait |
| `favori retirer:<n°>` | `/favorite remove number:` | autocomplétion |
| `favoris` | `/favorite list` | |
| (nouveau) | `/join`, `/remove`, `/clear`, `/shuffle`, `/loop`, `/audiocheck` | |

Aucune fonction n'a été retirée. Les anciennes commandes `/musique` disparaissent de Discord
à la première synchronisation de la 1.12.0 (aucun doublon). Les noms sont en anglais parce
que ce sont les noms que tout le monde connaît pour un bot de musique ; les descriptions et
les réponses restent en français.

## 3. Sources : ce qui marche vraiment

Lavalink ne lit que ce qu'il sait **décoder lui-même**. Spotify, Apple Music et Deezer
protègent leur audio (DRM) : Samae ne contourne **jamais** une protection, un abonnement ou
une connexion. Pour ces services, seules les **métadonnées** (titre, artiste) sont lues, et
le son est **cherché sur une autre source** ; le bot l'indique alors : « (son lu sur une
autre source) ».

| Source (`MUSIC_SOURCES`) | Métadonnées | Résolution (lien ou recherche) | Lecture du son | Playlists | Restrictions / remarques |
|---|---|---|---|---|---|
| `youtubemusic` | oui | recherche `ytmsearch:` ; liens `music.youtube.com` | oui, par le plugin youtube-source | oui | Conditions d'utilisation de YouTube : la lecture par un robot n'y est pas autorisée. YouTube bloque souvent les adresses de VPS (« Sign in to confirm you're not a bot »). Aucun contournement (ni compte, ni jeton) : la recherche passe alors à la source suivante. |
| `youtube` | oui | recherche `ytsearch:` ; liens `youtube.com`, `youtu.be` | oui, idem | oui (vérifié : 14 titres d'une playlist chargés sur un vrai Lavalink) | idem ; vidéos privées, réservées aux membres ou limitées par âge/région : refusées |
| `soundcloud` | oui | recherche `scsearch:` ; liens `soundcloud.com` | oui (intégré à Lavalink) | oui | titres réservés aux abonnés Go+ : seul un extrait de 30 s existe, il est **écarté** (`filterPreviewTracks`) |
| `bandcamp` | oui | recherche `bcsearch:` ; liens `*.bandcamp.com` | oui (intégré), titres en écoute libre | albums | titres non écoutables gratuitement : refusés |
| `spotify` | oui, avec LavaSrc | liens `open.spotify.com`, recherche `spsearch:` | **non** : son cherché sur YouTube/SoundCloud | oui (métadonnées) | demande LavaSrc **et** des identifiants d'application Spotify (gratuits, à créer sur developer.spotify.com) ; le titre trouvé peut différer (version, reprise) |
| `deezer` | oui, avec LavaSrc | liens `deezer.com`, recherche `dzsearch:` | **non** dans Samae : son cherché ailleurs | oui (métadonnées) | LavaSrc sait lire Deezer avec une clé de déchiffrement et un compte : **exclu** (contournement de DRM) |
| `applemusic` | oui, avec LavaSrc | liens `music.apple.com`, recherche `amsearch:` | **non** : son cherché ailleurs | oui (métadonnées) | demande un jeton de l'Apple Developer Program (**payant**, 99 $/an) : **non recommandé** |

Ce qui est **toujours refusé** :

- les directs (radios, lives) et les titres plus longs que `MUSIC_MAX_TRACK_MINUTES` ;
- tout lien qui n'est pas en `http(s)` vers un site d'une source **activée** : pas
  d'adresse IP, pas de port, pas d'identifiants dans le lien, pas de fichier du serveur.
  Lavalink a en plus les sources `http` et `local` désactivées (vérifié : un lien direct
  vers un fichier audio renvoie 0 titre) ;
- un texte n'est jamais transmis tel quel à Lavalink : il est toujours préfixé par la
  recherche d'une source activée (impossible de lui faire charger autre chose).

**Réglage conseillé** : `MUSIC_SOURCES=youtubemusic,soundcloud` (défaut). Si YouTube bloque
le VPS, `MUSIC_SOURCES=soundcloud,bandcamp` donne un bot qui marche sans dépendre de YouTube.

Spotify/Deezer/Apple Music **ne sont pas installés** : il faudrait ajouter le plugin LavaSrc
4.8.3 et sa configuration à Lavalink (hors de ce lot ; à décider plus tard). Sans lui, ces
sources sont refusées à la configuration avec un message clair.

## 4. File d'attente et lecture

- **Une file par serveur**, premier arrivé premier servi, `MUSIC_MAX_QUEUE` titres au plus
  (100). Une playlist ajoute au plus `MUSIC_MAX_PLAYLIST` titres (25) et le dit ; si la file
  n'a plus assez de place, ce qui rentre est ajouté et le reste est signalé.
- **États** d'un serveur : déconnecté → en attente (dans le salon, rien ne joue) → lecture ⇄
  pause. Toute autre transition est refusée par le code (`InvalidTransition`).
- **Enchaînement** : à la fin d'un titre (ou s'il n'a pas pu se charger), le suivant démarre.
  Un titre illisible est sauté ; après **3 échecs de suite**, la lecture s'arrête avec un
  message plutôt que de vider la file en boucle.
- **Événements périmés** : chaque titre porte un numéro transmis à Lavalink et renvoyé à la
  fin du titre. Une fin de titre qui arrive en retard (après un « passer ») est ignorée :
  la file n'avance jamais deux fois. Un verrou par serveur sérialise les changements.
- **Départ automatique** : sans musique depuis `MUSIC_IDLE_MINUTES` (5), le bot quitte le
  salon. S'il est déplacé ou expulsé du salon, la file est vidée proprement.
- **Lavalink arrêté ou redémarré** : le bot le détecte (wavelink seul ne le voit pas, voir §9),
  vide les files, quitte les salons et se reconnecte tout seul quand Lavalink revient.
- **Redémarrage du bot** : la file et le titre en cours sont perdus (ils ne sont pas en
  base) ; l'historique, les favoris et le volume restent.
- **Limites anti-abus** : une recherche toutes les 3 s par personne, 2 recherches en même
  temps par serveur, 12 s au plus par recherche, 200 caractères par recherche.

## 5. Qui peut faire quoi

Les règles sont vérifiées à **chaque** commande et à **chaque** clic, à partir de ce que
Discord dit au moment de l'action (salon vocal réel, rôles réels), jamais du contenu d'un
bouton.

| Action | Qui |
|---|---|
| ajouter un titre, pause, reprise | toute personne dans le salon vocal du bot (ou dans n'importe quel salon vocal si le bot n'est pas connecté) |
| passer **son** titre | la personne qui l'a demandé |
| passer le titre d'un autre, arrêter, volume, faire partir le bot | une personne « DJ » |
| arrêter / faire partir le bot sans être dans le salon | un administrateur du bot |
| consulter (en cours, file, historique, favoris) | tout membre du serveur |

**Est « DJ »** : un administrateur du bot (« Gérer le serveur » ou `ADMIN_ROLE_NAME`), une
personne qui a le **rôle DJ de ce serveur**, ou la personne **seule** avec le bot dans le
salon. Le rôle DJ se choisit **depuis Discord**, serveur par serveur, avec
`/setup role-dj role:@DJ` (rangé en base, migration v14). À défaut, le nom
`MUSIC_DJ_ROLE_NAME` du `.env` s'applique à tous les serveurs. **Sans rôle DJ** (défaut,
adapté à une équipe de 5), toute personne présente dans le salon du bot est DJ.

| Action | Qui (avec un rôle DJ) |
|---|---|
| `/remove` de **son** titre | la personne qui l'a demandé |
| `/remove` du titre d'un autre, `/clear`, `/shuffle`, `/loop` | une personne « DJ » |

Permissions Discord nécessaires au bot dans le salon vocal : **Se connecter** et **Parler**
(le bot vérifie et explique ce qui manque, ainsi qu'un salon plein). Il rejoint en sourdine
(il n'écoute jamais le salon). Aucune nouvelle permission privilégiée (intents) n'est demandée.

## 6. Historique, favoris, volume (base SQLite)

Migration **v13**, uniquement des créations de tables (aucune table existante modifiée) :

| Table | Contenu | Règles |
|---|---|---|
| `music_history` | une ligne par titre **effectivement lancé** : serveur, titre, artiste, source, lien, durée, demandeur, date | effacé après `MUSIC_HISTORY_RETENTION_DAYS` (90 j), au même moment que la purge existante ; index par serveur et par date |
| `music_favorites` | favoris par serveur **et** par personne | pas de doublon (clé primaire), 100 au plus par personne, un lien obligatoire (pour pouvoir le rejouer) |
| `music_guild_settings` | volume retenu par serveur | 0 à 100 (contrainte `CHECK`) |

Comme pour chaque migration : sauvegarde vérifiée avant, migration dans une transaction
(si elle échoue, la base reste en v12, intacte, et le bot réessaie au démarrage suivant). Une
base v13 est refusée par une version plus ancienne du bot ; `deploy/rollback.sh` propose
alors de restaurer la sauvegarde faite avant la mise à jour.

Données personnelles : seuls l'identifiant Discord du demandeur et les titres écoutés sont
gardés, dans la base du bot, pour la durée de rétention. Rien n'est envoyé ailleurs.

## 7. Configuration (`.env`)

Section « Musique » de `.env.example`. Une valeur invalide **désactive la musique seulement**
(le reste du bot démarre) et l'erreur est affichée par `/etat`.

| Variable | Défaut | Rôle |
|---|---|---|
| `MUSIC_ENABLED` | `false` | active la musique |
| `LAVALINK_URI` | `http://127.0.0.1:2333` | adresse de Lavalink ; `http://` accepté **seulement** vers 127.0.0.1/localhost, port obligatoire, sans chemin ni identifiants |
| `LAVALINK_PASSWORD` | (vide) | **secret** : même valeur que `LAVALINK_SERVER_PASSWORD` côté Lavalink ; 16 caractères au moins, « youshallnotpass » refusé ; masqué dans les journaux |
| `MUSIC_SOURCES` | `youtubemusic,soundcloud` | sources, dans l'ordre d'essai (§3) |
| `MUSIC_DEFAULT_VOLUME` | `50` | volume de départ (0 à 100) |
| `MUSIC_MAX_QUEUE` | `100` | taille de la file (1 à 500) |
| `MUSIC_MAX_PLAYLIST` | `25` | titres pris dans une playlist (1 à 100) |
| `MUSIC_MAX_TRACK_MINUTES` | `180` | durée maximale d'un titre (0 = sans limite) |
| `MUSIC_DJ_ROLE_NAME` | (vide) | nom exact du rôle DJ (§5) |
| `MUSIC_IDLE_MINUTES` | `5` | départ du salon après ce temps sans musique (1 à 60) |
| `MUSIC_HISTORY_RETENTION_DAYS` | `90` | rétention de l'historique (1 à 3650) |

`/etat` affiche une ligne « Musique » : désactivée, configuration invalide (avec la raison),
Lavalink injoignable, ou prête avec le nombre de salons actifs.

## 8. Installer et tester

### Sur un ordinateur (développement)

1. `pip install -r requirements.txt` (dans le `.venv` du projet).
2. Installer Java 17 ou plus, télécharger `Lavalink.jar` 4.2.2 et le plugin youtube 1.18.2
   (liens et empreintes : [music-vps-checklist.md](music-vps-checklist.md), étape 3).
3. Copier `deploy/lavalink/application.yml.example` en `application.yml` à côté du jar,
   créer `plugins/` et y mettre le plugin.
4. Lancer : `LAVALINK_SERVER_PASSWORD=<mot de passe> java -Xmx512m -jar Lavalink.jar`.
5. Dans `.env` : `MUSIC_ENABLED=true` et `LAVALINK_PASSWORD=<le même>`, puis lancer le bot.

### Tests automatiques

```bash
python -m pytest tests/test_music_*.py -q     # sans réseau ni Discord
python -m pytest -m "not integration" -q     # tests unitaires seulement (sans le faux Lavalink local)
python -m pytest -q                           # toute la suite
```

Ils couvrent la recherche et le filtre des liens, la file, le service (enchaînements,
événements périmés, échecs, permissions, verrous), la base (migration depuis v1 à v12,
migration interrompue, index et contraintes), la configuration, les commandes et boutons
Discord (simulés), et l'adaptateur wavelink face à un faux serveur Lavalink v4 local
(connexion, mauvais mot de passe, Lavalink absent, redémarrage de Lavalink).

### Test manuel sur un serveur Discord de test (à faire avant la production)

Avec le bot de test (token de test) et un Lavalink local :

1. `/etat` : ligne Musique « prête ». Arrêter Lavalink : « injoignable » ; le relancer :
   « prête » sans redémarrer le bot (≈ 10 s).
2. Hors salon vocal : `/play` explique qu'il faut rejoindre un salon.
3. Dans un salon : `/play query:daft punk` → le 1er résultat joue, le panneau apparaît.
   `/search query:daft punk` → menu de 5 résultats. **Vérifier le son** (§8 bis).
4. Coller un lien SoundCloud, puis un lien YouTube, puis une playlist : ajout, limite de
   playlist annoncée.
5. Boutons : pause, reprise, passer, répétition, file, favori, arrêter. Cliquer un bouton d'un
   ancien panneau : refus « ne correspond plus ».
6. `/queue`, `/remove` (autocomplétion), `/shuffle`, `/clear`, `/loop mode:Titre en cours`
   (le titre recommence à la fin), `/loop mode:Toute la file`.
7. Avec un second compte : rejoindre un autre salon et essayer de contrôler → refus.
   `/setup role-dj role:@DJ` et vérifier que seul le rôle peut arrêter / régler le volume.
8. `/volume level:20`, redémarrer le bot, `/volume` → 20.
9. `/history`, `/favorite add`, `/favorite list` (rejouer depuis le menu), `/favorite remove number:1`.
10. Inviter le bot sur un **second** serveur de test : lancer de la musique sur les deux en
    même temps ; files et panneaux séparés, `/stop` sur l'un ne touche pas l'autre.
11. Laisser le bot seul sans musique : il part après `MUSIC_IDLE_MINUTES`.
12. Lien interdit (`http://127.0.0.1/…`, un site inconnu) → refus clair.

## 8 bis. « Le bot est dans le salon mais on n'entend rien »

Une recherche réussie et un bot présent dans le salon **ne prouvent pas** que du son part.
Le son suit cinq étapes, et le bot vérifie chacune séparément :

| Étape | Ce qui est vérifié | D'où vient l'information |
|---|---|---|
| 1. Recherche | la source a renvoyé au moins un titre lisible | réponse de Lavalink à `loadtracks` |
| 2. Piste chargée | Lavalink a accepté la piste (requête de lecture sans erreur) | réponse de Lavalink à la mise à jour du lecteur |
| 3. Lecteur démarré | Lavalink a envoyé « TrackStartEvent » pour **cette** piste | WebSocket de Lavalink |
| 4. Connexion vocale | Lavalink est connecté au serveur vocal de Discord | `playerUpdate.connected` (toutes les 5 s) |
| 5. Audio envoyé | la position de lecture **avance** pendant que la connexion vocale est ouverte | `playerUpdate.position` |

Chaque étape est écrite **une fois par titre** dans les journaux du bot
(`Musique [serveur …] étape 3/5 : …`), sans jeton ni identifiant de session vocale (les
journaux de wavelink, qui contiennent le jeton vocal en DEBUG, sont bloqués à INFO).
20 s après le lancement d'un titre, si une étape a échoué, la **cause probable** s'affiche
sur le panneau. `/audiocheck` montre l'état des cinq étapes à tout moment, la version de
Lavalink, ce que Lavalink dit du lecteur (connecté, ping, position, salon transmis) et l'état
vocal du bot côté Discord (muet par le serveur, spectateur d'une scène).

| Ce que montre `/audiocheck` | Cause probable | Que faire |
|---|---|---|
| étape 3 en échec, erreur de la source | YouTube refuse l'adresse de la machine, vidéo indisponible | essayer `source:SoundCloud` ; mettre `soundcloud` en premier dans `MUSIC_SOURCES` |
| étape 4 en échec, « code 4017 » ou « DAVE » | Lavalink plus ancien que 4.2.0 : Discord exige le chiffrement DAVE | installer Lavalink 4.2.2 (§1) |
| étape 4 en échec sans code | Lavalink n'arrive pas à joindre le serveur vocal (UDP sortant bloqué par un pare-feu, réseau d'école ou d'entreprise) | autoriser l'UDP sortant de Java (ports 50000-65535) ; tester depuis un autre réseau |
| « Version de Lavalink » trop ancienne ou plugin YouTube absent | installation incomplète | voir [music-vps-checklist.md](music-vps-checklist.md), étape 3 |
| étape 5 en échec alors que 1 à 4 sont OK | la connexion vocale est ouverte mais ne consomme pas le son | redémarrer Lavalink ; regarder ses journaux |
| tout est ✅ mais rien ne s'entend | côté Discord : bot rendu muet **pour vous** (clic droit sur le bot > volume utilisateur), sortie audio de votre ordinateur, application Discord trop ancienne (DAVE) | vérifier avec un autre compte ou un autre appareil |
| « muet par le serveur » / « spectateur » | un modérateur a rendu le bot muet, ou salon de type scène | clic droit sur le bot > décocher « Rendre muet » ; dans une scène, l'inviter à parler |

Ce que le bot **ne peut pas savoir** : si vos oreilles entendent le son. L'étape 5 réussie
veut dire « le son quitte Lavalink, chiffré, vers Discord ». Seul le test manuel ci-dessous
le confirme.

### Procédure de test du son sur un vrai serveur Discord

À faire avec **un serveur Discord de test** et **un bot de test** (jamais le token de production) :

1. Lancer Lavalink 4.2.2 (§8), puis le bot avec `MUSIC_ENABLED=true` et `LOG_LEVEL=INFO`.
2. Dans les journaux du bot, vérifier au démarrage : `Lavalink 4.2.2 : sources youtube, soundcloud…`
   et **aucune** ligne `Musique : Lavalink … ne gère pas le chiffrement DAVE`.
3. Sur Discord : `/etat` → ligne « Musique » prête, version de Lavalink affichée.
4. Rejoindre un salon vocal **normal** (pas une scène) avec votre compte, depuis l'application
   Discord à jour (ordinateur ou téléphone).
5. `/play query:https://soundcloud.com/…` (un titre SoundCloud public : moins de blocages que YouTube).
6. Attendre 20 s, puis `/audiocheck` : les 5 étapes doivent être ✅. Noter le résultat.
7. **Écouter** : le son doit s'entendre. Si les 5 étapes sont ✅ et rien ne s'entend, vérifier le
   volume utilisateur du bot (clic droit sur le bot) et tester avec un second compte.
8. Recommencer avec `/play query:daft punk` (recherche YouTube Music) et noter si YouTube
   refuse la machine (étape 3).
9. Dans les journaux du bot, retrouver les lignes `étape 1/5` à `étape 5/5` du titre.
10. Noter dans [test-manuel.md](test-manuel.md) : version de Lavalink, système, réseau
    (maison, VPS…), résultat de `/audiocheck`, son entendu ou non.

## 9. Limites connues

- **Non vérifié ici** : la lecture du son dans un vrai salon Discord (pas de Discord dans
  l'environnement de développement) et les recherches texte réelles (pas d'accès DNS) ; à
  valider par la procédure du §8 bis. La cause exacte du silence observé en 1.11.0 n'est
  pas prouvée : le diagnostic de la 1.12.0 sert à la trouver au prochain essai.
- wavelink 3.5.2 lève une erreur interne sur un code de fermeture vocale qu'il ne connaît pas ;
  la surveillance de l'adaptateur (§4) remet alors le lecteur en état, mais la cause n'est
  écrite que dans les journaux de Lavalink.
- **YouTube** peut refuser l'adresse du VPS à tout moment ; rien n'est fait pour le
  contourner. SoundCloud reste alors la source de secours.
- La progression affichée dans le panneau n'est pas en temps réel : elle est recalculée à
  chaque mise à jour du panneau et par `/nowplaying`.
- La file est en mémoire : elle est perdue au redémarrage du bot ou de Lavalink.
- **Défaut de wavelink 3.5.2** constaté sur un vrai Lavalink : quand Lavalink s'arrête, sa
  tâche de connexion meurt sans que le nœud passe « déconnecté ». L'adaptateur le surveille
  (toutes les 10 s), nettoie et se reconnecte ; vérifié : perte vue en 2 s, reconnexion 7 s
  après le retour de Lavalink.
- Une seule instance de Lavalink (pas de répartition) : largement suffisant pour un serveur.

## 10. Coûts et capacité

- **Argent : 0 €.** Lavalink, les plugins et wavelink sont libres et gratuits ; aucun service
  payant n'est utilisé. (Apple Music demanderait un compte développeur payant : non retenu.)
- **Mémoire** mesurée sur un vrai Lavalink 4.2.2 au repos avec `-Xmx512m` : ≈ 305 Mo
  (résident). Le service systemd d'exemple plafonne à 640 Mo (souple) / 768 Mo (dur). Le
  bot lui-même ne prend que quelques Mo de plus (wavelink).
- **Processeur et réseau** : non mesurés en lecture réelle (pas de Discord ici). Ordre de
  grandeur habituel : un flux Opus ≈ 128 kbit/s par salon actif, quelques % d'un cœur.
- **Pour mesurer sur le VPS** pendant le test : `systemctl status lavalink` (lignes Memory
  et CPU), `ps -o rss,pcpu -p $(systemctl show -p MainPID --value lavalink)`, et
  `free -m` avant/après, avec 1 puis 2 salons en lecture.

Vérifier avant d'activer : le VPS doit avoir ≈ 800 Mo de mémoire libre en plus du bot.

## 11. Passage en production (résumé)

1. Relire et fusionner la PR dans `main` (après le test manuel).
2. **Propriétaire du VPS (sudo)** : installer Lavalink selon [music-vps-checklist.md](music-vps-checklist.md).
3. **Utilisateur du bot (sans sudo)** : `bash deploy/update.sh` (sauvegarde vérifiée puis
   migration v13 automatique), puis dans `.env` : `MUSIC_ENABLED=true`,
   `LAVALINK_PASSWORD=<la valeur donnée par le propriétaire>`, puis redémarrer le bot
   (`sudo systemctl restart samae`, ou le demander au propriétaire).
4. `/etat` → Musique prête ; un premier `/play` dans un salon, puis `/audiocheck` (§8 bis).

Pour désactiver : `MUSIC_ENABLED=false` et redémarrer le bot ; Lavalink peut rester arrêté
(`sudo systemctl disable --now lavalink`). L'historique et les favoris restent en base.
