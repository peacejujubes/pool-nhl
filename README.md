# Pool de hockey 2026-27 — Les chums

Dashboard maison pour le pool de hockey entre chums : suit les points de
chacun des 6 alignements, les blessures/statuts des joueurs, et remplace
Pool Expert. Construit avec Starlette (Python) + SQLite, pensé pour tourner
sur ton Unraid et être consulté depuis Chrome sur ordi.

Les 6 alignements réels du repêchage (fichier `Pool de hockey 2627 Les
chums.xlsx` que tu as fourni) sont déjà chargés au premier démarrage — pas
besoin de tout ressaisir. Tu peux corriger un alignement en tout temps dans
**Alignements**.

## Démarrer

```bash
docker compose up -d --build
```

Ouvre `http://<ton-unraid>:8420`. La base SQLite vit dans `./data/pool.db`
(monté en volume) — elle survit aux rebuilds. Branche ton reverse proxy
existant par-dessus le port 8420 si tu veux un domaine/HTTPS.

Pas de config à faire : les 6 alignements, les 419 joueurs/équipes
repêchables et les statuts (blessures, transactions) connus au 10 septembre
sont déjà en place.

## Ce que ça fait, au-delà du simple classement

- **Tendances** : une mini-courbe (sparkline) à côté de chaque pooler, plus
  le mouvement au classement et les points gagnés/perdus depuis la dernière
  actualisation. Basé sur un historique (`pooler_snapshots` /
  `player_snapshots`) enregistré automatiquement à chaque actualisation LNH
  et à chaque correction manuelle dans `/admin/stats` ou `/ajustements`.
- **En chaleur** : les joueurs (tous alignements confondus) qui ont le plus
  gagné de points depuis la dernière actualisation.
- **Meneurs de la ligue** : top patineurs / gardiens / équipes across les 6
  alignements, avec qui les possède.
- **Profil offensif** : buts, passes, points en avantage numérique,
  victoires de gardien, etc. par équipe — pas juste le total de points.
- **Ticker de statuts** en haut du tableau de bord : tous les joueurs
  repêchés actuellement signalés blessés/transigés/recrue, peu importe qui
  les a.
- Codes de couleur par position (bleu attaquant, vert défenseur, orange
  gardien, violet équipe) et en-tête de chaque équipe teinté avec la couleur
  de l'équipe LNH qu'elle a repêchée.

## Pages

- **Tableau de bord** (`/`) — classement, tendances, joueurs en chaleur,
  meneurs de la ligue, profil offensif, et le détail des points par joueur
  pour chacun des 6 alignements.
- **Alignements** (`/setup`) — modifier qui est où ; un sélecteur par poste,
  les joueurs déjà pris par un autre pooler apparaissent grisés (impossible
  de les sélectionner tant qu'ils ne sont pas libérés).
- **Ajustements** (`/ajustements`) — points manuels (but de gardien, bonus
  de blanchissage d'équipe, correction). S'additionnent au total.
- **Stats LNH** (`/stats-lnh`) — les stats brutes de tous les joueurs et
  équipes repêchables (pas juste ceux dans un alignement) : filtre
  « disponibles seulement » pour voir qui n'a pas encore été pris, colonnes
  triables, recherche, et une colonne « Pts pool » qui applique nos règles
  de pointage à n'importe quel joueur. C'est l'équivalent du « joueurs
  libres » de Pool Expert.
- **Calendrier** (`/calendrier`) — en haut, une vraie grille de calendrier
  compacte (une colonne par jour) qui montre juste les affrontements de la
  semaine, équipe contre équipe (ex. TOR @ MTL), pour un coup d'œil rapide
  sans détails ; en dessous, la liste détaillée habituelle des matchs des 7
  prochains jours pour toute la LNH, groupés par jour, avec l'heure,
  l'aréna, un badge « B2B » quand une équipe joue deux soirs de suite et un
  badge par pooler concerné (qui a un joueur ou l'équipe elle-même dans ce
  match).
- **Comparer** (`/comparaison`) — deux alignements côte à côte (menus
  déroulants pour choisir lesquels) : total et écart, profil offensif
  comparé catégorie par catégorie, et chaque poste aligné avec les points
  des deux côtés — pratique avant de proposer un échange.
- **Statuts** (`/statuts`) — le fil de blessures/transactions pour tous les
  joueurs repêchables, pas juste ceux dans un alignement (contrairement au
  ticker du tableau de bord).
- **Stats (admin)** (`/admin/stats`) — les chiffres bruts par joueur/équipe
  (parties jouées, buts, victoires, etc.), modifiables à la main, plus le
  statut/note de chaque joueur (blessure, transaction...).
- **Alertes** (`/admin/alertes`) — coller l'URL d'un webhook Discord (créé
  dans les paramètres d'un canal : icône d'engrenage → Intégrations →
  Webhooks → Nouveau webhook) pour recevoir un résumé hebdo automatique
  (chaque lundi 8h) directement dans Discord : classement, mouvement de la
  semaine, joueur le plus chaud, et un aperçu des matchs à venir (dont les
  back-to-backs), présenté comme un vrai embed Discord (barre de couleur —
  celle de l'équipe LNH du meneur du moment — titre, sections, émojis) plutôt
  qu'un mur de texte plat. Un bouton "Envoyer un message de test" confirme
  que ça fonctionne avant d'attendre le lundi suivant, et un bouton "Envoyer
  le résumé maintenant" poste le vrai résumé tout de suite sans attendre
  lundi. On peut associer l'ID Discord de chaque pooler — Discord a besoin
  de l'ID numérique du membre, pas juste son pseudo, expliqué sur la page —
  et un bouton "🔔 Tester" à côté de chaque prénom envoie une petite mention
  juste pour cette personne, pour confirmer que ça la notifie vraiment sans
  attendre le résumé complet du lundi. Chaque pooler choisit **par
  catégorie** quelles notifications il reçoit (pas juste
  "activé/désactivé" en bloc) : 📊 Résumé hebdo (mentionné sur sa ligne du
  lundi) et 🩹 Blessures (pingé dès qu'un de ses joueurs est signalé
  blessé) — une case à cocher par catégorie, indépendante de l'ID lui-même,
  activée par défaut. L'alerte blessure part au moment où le statut d'un
  joueur est mis à `blessure` sur cette page ou dans **Stats (admin)** (pas
  via l'actualisation automatique LNH, puisque le statut est toujours saisi
  à la main — voir plus bas) : dès que le statut d'un joueur passe à
  blessé, tous les poolers qui l'ont dans leur alignement et qui ont coché
  🩹 Blessures reçoivent une mention immédiatement, sans attendre le résumé
  du lundi. Modifier juste le texte d'une note déjà "blessure" (préciser la
  durée, etc.) ne repigne personne — seule la transition vers "blessure"
  déclenche l'alerte. Pas de bot à héberger — un simple webhook.

## Comment les points se calculent

- Patineur : but ou passe = 1 pt, +1 si en avantage numérique, +1 en
  désavantage, +1 en prolongation (additionnés — un joueur qui marque 20
  fois dont 5 en AN, 1 en DN et 2 en prolongation vaut 20+5+1+2 = 28 pts).
- Gardien : victoire = 2 pts, défaite en prolongation = 1 pt, blanchissage
  = +1 pt bonus, but marqué = 10 pts.
- Équipe : victoire = 2 pts, défaite en prolongation = 1 pt.
- Un pooler qui a et l'équipe et son gardien empoche naturellement les deux
  (2+2 = 4 pts sur une victoire) — pas de logique spéciale nécessaire, la
  somme le fait déjà.

## Actualisation des stats LNH

L'app va chercher les stats sur `api-web.nhle.com` toutes les 2h
automatiquement, plus un bouton **Actualiser** sur le tableau de bord et la
page admin. Le petit point de couleur (vert/jaune/rouge) à côté indique si
la dernière actualisation a fonctionné.

**Important — à vérifier après le premier déploiement :** l'environnement où
cette app a été construite n'avait pas accès à internet pour tester contre
la vraie API de la LNH, donc les noms de champs utilisés dans
`app/nhl.py` (`pick(...)` avec plusieurs noms possibles) sont du
"best-effort" basé sur la doc communautaire de l'API. Si après un
déploiement le point reste rouge/jaune, ou que les stats restent à 0 :
1. Regarde `/admin/stats` — la liste des équipes en échec avec le message
   d'erreur s'affiche en haut de la page.
2. Si l'erreur est un vrai problème réseau (Unraid n'a pas de sortie
   internet, DNS, etc.), c'est réglé côté réseau, pas côté app.
3. Si ça répond mais que les chiffres restent à 0, les noms de champs dans
   `app/nhl.py` (fonction `refresh_all`, appels à `pick(...)`) ne
   correspondent probablement plus au JSON réel — compare avec une requête
   `curl https://api-web.nhle.com/v1/club-stats/TOR/now` et ajuste les noms.
4. En attendant, **Stats (admin)** permet d'entrer les chiffres à la main —
   l'app reste utilisable même si l'auto-refresh ne fonctionne pas.

## Structure

```
app/            code Starlette (routes, scoring, accès SQLite, client LNH)
templates/      pages Jinja2
static/         CSS (aucun JS de build, pas de framework front)
data_seed/      joueurs repêchables + les 6 alignements réels du repêchage
                (chargés une seule fois, au tout premier démarrage)
data/           base SQLite (créée au démarrage, montée en volume Docker)
```
