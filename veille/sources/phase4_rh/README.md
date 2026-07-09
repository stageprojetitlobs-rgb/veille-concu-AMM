# Phase 4 — Veille RH (offres d'emploi) — NON IMPLÉMENTÉE

Signal recherché : un concurrent qui recrute sur une zone = il l'attaque
(ex. « Responsable Export Afrique », « Chef de produit élevage »).

## Contrainte de conformité (non négociable)

**Aucun contournement de blocage.** Pas de scraping forcé de LinkedIn ni d'aucune
plateforme qui l'interdit dans ses CGU / robots.txt.

## Voies officielles à cadrer avant tout code

1. **Alertes natives** des plateformes (e-mails d'alerte LinkedIn/Indeed/WTTJ) →
   ingestion via une boîte mail dédiée + parsing IMAP. Conforme car c'est un canal
   fourni par la plateforme.
2. **APIs publiques documentées** quand elles existent :
   - France Travail (ex-Pôle emploi) : API « Offres d'emploi » officielle.
   - Indeed / WTTJ : vérifier l'existence d'un programme partenaire/API.
3. **Pages carrières des concurrents** (laprovet.fr/carrieres, etc.) : souvent
   autorisées au crawl — à traiter comme une source Phase 2 (robots.txt d'abord).
4. **Prestataires de données conformes** si aucune voie gratuite ne convient.

→ À arbitrer avec l'équipe avant implémentation. Voir `SOURCES.md` ligne 5.
