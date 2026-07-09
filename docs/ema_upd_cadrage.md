# Source 2 — EMA UPD : cadrage d'accès

Statut : **cadré, en attente d'accès** (intégration non codée tant que l'OAuth2 MAH
n'est pas accordé). Voir aussi `SOURCES.md` ligne 2 et le stub
`veille/sources/phase1_regulatory/ema_upd.py`.

---

## 1. Note de demande d'accès interne (à transmettre aux Affaires Réglementaires)

> **Objet : demande d'accès en lecture à la base européenne des médicaments
> vétérinaires (UPD) de l'EMA**
>
> Bonjour,
>
> Dans le cadre de notre dispositif de veille concurrentielle, nous souhaitons
> récupérer **automatiquement et en lecture seule** les informations publiques sur
> les médicaments vétérinaires autorisés en Europe (nouvelles AMM des concurrents,
> mises à jour). Ces informations sont centralisées par l'EMA dans la base **UPD**
> (Union Product Database).
>
> L'EMA met à disposition des **titulaires d'AMM** (ce que Lobs est déjà) un accès
> programmatique à cette base. Concrètement, j'ai besoin que vous obteniez pour nous
> trois choses auprès de l'EMA :
>
> 1. **Le type d'accès** : un accès **API en lecture seule réservé aux titulaires
>    d'AMM (MAH — Marketing Authorisation Holder)**. Nous ne demandons **aucun droit
>    d'écriture** ni de modification, uniquement de la consultation.
>
> 2. **Le rôle utilisateur** : l'accès est géré via le portail **IAM de l'EMA**. Il
>    faut qu'une personne de Lobs soit déclarée **« Super User »** de notre
>    organisation pour pouvoir ensuite autoriser un accès technique. Si ce rôle
>    existe déjà chez nous (souvent la personne qui gère déjà les soumissions EMA),
>    il suffit qu'elle valide la demande.
>
> 3. **Les identifiants techniques (OAuth2)** : pour que notre outil se connecte
>    automatiquement, l'EMA nous fournira un **identifiant client** et un **secret**
>    (l'équivalent d'un login/mot de passe pour un programme), ainsi que l'adresse du
>    service d'authentification. Ces éléments sont **confidentiels** ; il faudra me
>    les transmettre par un canal sécurisé (pas par e-mail en clair).
>
> Pouvez-vous initier cette demande auprès de notre contact EMA / du service
> compétent ? Je reste disponible pour tout détail technique.
>
> Merci,
> [Veille / Data — Lobs]

---

## 2. Specs techniques à récupérer

À demander/collecter en parallèle de la demande d'accès :

| Élément | Où / quoi |
|---|---|
| **Vet EU Implementation Guide (Vet EU IG)** | Document de référence EMA. **Chapitre 5** = API UPD (le cœur de notre besoin). |
| → Endpoints **produits** | URL de base de l'API, route de recherche/listing des médicaments vétérinaires, route de détail produit, pagination, filtres (par titulaire, par date d'AMM…). |
| → Modèle de données | Schéma des champs renvoyés : identifiants **UPD permanent identifier** et **product identifier (UUID)** — ce dernier est notre **clé de jointure avec l'ANSES** (`prod-id`), n° AMM, substance(s), espèces, statut, dates. |
| **Authentification OAuth2** | Flow attendu (très probablement *client credentials*), **token endpoint**, `scope`/`audience` requis, durée de vie du token, modalités de refresh. |
| **Environnements** | URLs de l'environnement de **test/sandbox** vs **production** (commencer par le sandbox). |
| **Quotas / rate limits** | Limites d'appels imposées par l'EMA, pour calibrer notre throttling. |
| **Conditions d'utilisation** | CGU de l'API (usage autorisé des données, mentions, restrictions de rediffusion). |

Sources EMA à consulter : page « Union Product Database » du site EMA, section
développeurs/IG vétérinaire, et la documentation IAM (gestion des rôles/Super User).

---

## 3. Squelette de client (stub)

Implémenté dans `veille/sources/phase1_regulatory/ema_upd.py` :

- Client `httpx` avec **OAuth2 client-credentials** (récupération + cache du token).
- **Aucun appel réel** tant que les identifiants ne sont pas configurés : le client
  refuse de partir en production et fonctionne sur **fixtures** (`tests/fixtures/`).
- Un mapper `_to_record()` déjà aligné sur le schéma pivot, prêt à brancher.

Pour activer le jour où l'accès est accordé :
1. Renseigner `EMA_UPD_CLIENT_ID`, `EMA_UPD_CLIENT_SECRET`, `EMA_UPD_TOKEN_URL` dans `.env`.
2. Renseigner les URLs réelles (base API, routes) issues du Chapitre 5 dans `config.yaml`.
3. Passer `sources.ema_upd.mode: live` et `enabled: true`.
