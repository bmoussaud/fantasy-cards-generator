---
updated_at: 2026-09-16T13:45:55.855+00:00
focus_area: Décisions communes des issues #143 et #146 approuvées, implémentation non autorisée
active_issues:
  - number: 143
    title: "feat(ui): show persisted agent inputs and outputs after generation and in My Cards"
    status: DECISIONS_APPROVED_IMPLEMENTATION_NOT_AUTHORIZED
    execution_authorized: false
  - number: 146
    title: "feat(generation): make Agent mode the default and remove legacy generation"
    status: DECISIONS_APPROVED_IMPLEMENTATION_NOT_AUTHORIZED
    execution_authorized: false
---

# What We're Focused On

Le demandeur a explicitement approuvé les décisions communes de #143 et #146 : production Agent-only, mock limité aux tests automatisés, règles de trace et deny-wins, conservation de la carte avec `trace_unavailable`, préflight/rollback sans fallback, et ordre #143 schéma/persistance → #146 Agent-only → #143 interface finale.

Cette approbation **n’autorise pas le démarrage de l’implémentation**. Aucun code, aucune assignation et aucun label d’exécution ne doivent être lancés sans demande explicite ultérieure.
