# Accès kepro.nl — Portail B2B

## Situation actuelle

Le catalogue kepro.nl (pages produits : molécules, notices PDF, fiches techniques)
est **réservé aux professionnels vétérinaires et aux distributeurs** via un compte
enregistré. Notre source 3a extrait déjà les 61 noms de produits depuis le sitemap
public. Avec un compte, on accède aux **détails complets**.

## Démarche d'inscription (5 minutes)

1. Aller sur **https://www.kepro.nl** → cliquer sur "Register" (lien visible
   sur la page de login, ou directement : https://www.kepro.nl/register)
2. Remplir le formulaire avec les coordonnées professionnelles de Lobs :
   - Nom de l'entreprise : Lobs
   - Email professionnel (ex. veille@lobs.fr ou ton email)
   - Pays : France
   - Type de compte : distributeur / laboratoire pharmaceutique vétérinaire
3. Kepro valide manuellement (généralement **1 à 3 jours ouvrés**)
4. À réception de la confirmation par email, copier le login et mot de passe

## Activation dans la veille

Une fois le compte validé, renseigner dans `.env` :

```dotenv
KEPRO_USERNAME=votre.email@lobs.fr
KEPRO_PASSWORD=motdepasse_kepro
```

Puis re-signaler : le spider kepro sera mis à jour pour s'authentifier
(session cookie WP) et accéder aux pages produits complètes, incluant :
- Substances actives (molécules)
- Espèces cibles
- Liens vers notices PDF → extraction pdfplumber
- Fiches techniques détaillées

## Ce que ça débloque

| Avant (sitemap) | Après (compte) |
|-----------------|----------------|
| Nom produit (slug URL) | Nom + molécule(s) + espèces |
| Pas de PDF | Notices PDF complètes → mots-clés |
| Pas de date | Date de mise à jour |
| 61 produits | 61 produits enrichis |
