# Plan d'implémentation - DEV Sweden Central

**Issue :** [#194](https://github.com/bmoussaud/fantasy-cards-generator/issues/194)  
**Responsable :** @bmoussaud  
**Date :** 26 novembre 2026  
**Révision étudiée :** `6b67f2acccfad043cf305506008fdd62080cd3c8`  
**Statut :** implémentation demandée ; qualification et revues indépendantes en
attente. Aucun preview ni déploiement n'est autorisé par cette demande.

L'option A et les arbitrages consignés dans l'issue sont les entrées de ce plan.
@bmoussaud a demandé l'implémentation et choisi `DataZoneStandard` pour le modèle
texte du nouveau DEV en conversation le 26 novembre 2026, sur la base de la
révision `3714d330c1342d554709428aacb695a7204841a0`. Cette demande n'autorise ni
preview, ni provisionnement, ni changement d'identité Azure, ni bascule, ni
suppression. Les revues spécialisées restent en attente.

Le RPI Agent n'est pas exposé dans cette session et le chargement du skill
`microsoft-foundry` a échoué. Ce document repose sur la lecture du dépôt, des
issues et de Microsoft Learn ; ce n'est pas une qualification HVE/RPI validée.

## Cible et limites

- Recréer le DEV dans un groupe distinct de l'abonnement `bmoussaudms`, avec
  `AZURE_LOCATION=swedencentral`. L'environnement logique reste `dev`.
- Conserver un seul compte Foundry cible et un seul projet : texte,
  `MAI-Image-2.7` par défaut, `gpt-image-2` sélectionnable par l'opérateur et
  `card-orchestrator`. Ajouter les embeddings uniquement si leur usage est confirmé.
- Démarrer avec de nouveaux stockages métier vides, sans copier les données
  Cosmos ou Blob historiques. Préserver intégralement l'ancien DEV.
- Conserver l'application et ses contrats existants : pas de sélecteur UI,
  pas de nouvelle migration applicative MAI, pas de fallback automatique.
- Exclure la production. Documenter les dépendances globales/externes et conserver
  les ressources dont l'usage reste inconnu.

## Constats qui déterminent les modifications

| Constat dans le dépôt | Conséquence pour le plan |
|---|---|
| `infra/main.parameters.json` transmet déjà `AZURE_LOCATION`. Les modules régionaux reçoivent `location`. | Réutiliser l'IaC existante ; un changement de région recrée une cible, il ne déplace pas l'ancien DEV. |
| `infra/main.bicep` calcule les noms globalement uniques à partir de l'abonnement et du groupe. | Un groupe distinct permet la coexistence sans refondre tous les noms. Vérifier néanmoins chaque portée d'unicité. |
| `infra/modules/ai-foundry.bicep` décrit seulement le texte et GPT Image, avec un format de modèle `OpenAI`. | Ajouter MAI avec son contrat ARM vérifié ; ne pas supposer que le format OpenAI convient à MAI. |
| Le garde `validatedFoundryImageEndpoint` exige un endpoint explicite pour MAI. | Rendre la saisie opérateur optionnelle en calculant l'endpoint du compte cible dans Bicep, puis en injectant une valeur explicite dans WEB. |
| Le défaut MAI est `1104x928`, alors que l'issue exige `1024x1536`. `imageSize` existe mais n'est pas raccordé à une variable dans `main.parameters.json`. | Exposer le paramètre et fixer explicitement `1024x1536` pour cette cible, sans modifier les défauts des autres déploiements. |
| L'application Entra utilise `uniqueName = fantasy-cards-generator-dev`, indépendant du groupe Azure. Le module fournit une liste de redirections. | Un deuxième DEV peut viser la même application Entra et remplacer ses redirections. Le choix d'isolation est bloquant. |
| `azure.yaml` impose provision, agent, provision, package WEB, déploiement WEB. Des hooks créent des secrets et épinglent l'endpoint de l'agent. | Garder cet ordre, isoler l'état azd et autoriser explicitement les effets des hooks. |

`infra/main.json` est suivi dans Git : il devra être régénéré par Bicep avec
les modifications de source, jamais édité manuellement.

## Lot 0 - Qualification et décisions bloquantes

**Prérequis :** aucun changement d'infrastructure. Ce lot prépare l'approbation.

| Sujet | Travail à effectuer | Condition de sortie |
|---|---|---|
| Disponibilité | Vérifier les fournisseurs, SKU, politiques et capacités dans l'abonnement cible, dont ACA, ACR, Cosmos, Storage, Key Vault, réseau et monitoring. | Matrice de compatibilité datée, sans supposer qu'une présence au catalogue suffit. |
| Foundry Hosted | Confirmer le runtime régional avec les versions du dépôt, le protocole Responses et la configuration de conteneur existante. | Support et capacité confirmés pour ce déploiement précis. |
| Modèles | Vérifier nom, version, format ARM, SKU, quota disponible et consommation pendant la coexistence pour texte, MAI et GPT Image. | Paramètres documentés et capacité suffisante ; aucun quota libéré en supprimant l'ancien DEV. |
| Modération | Vérifier Content Safety image et les politiques des modèles, avec les endpoints/API existants et l'identité prévue. | Chemin de modération compatible ; pas de substitution ou de désactivation silencieuse. |
| État azd | Définir le groupe cible et deux contextes de travail distincts, chacun pouvant garder le nom logique `dev`. | L'état de l'ancien DEV reste intact ; la cible ne reprend pas ses sorties, identifiants d'agent ou images par accident. |
| Identité Entra | Faire choisir une application distincte ou une application partagée avec conservation explicite des deux callbacks. | Décision du responsable et revue identité avant toute modification. |
| Exploitation | Confirmer coûts de coexistence, destinataires d'alertes, emplacements des sondes et point d'entrée à basculer. | Procédure opérateur et critères de retour arrière approuvables. |

**Recommandation à arbitrer pour Entra :** une application distincte pour le
DEV cible, avec un `uniqueName` paramétrable et distinct. Garder le défaut actuel
pour les déploiements existants. Si le partage est retenu, maintenir explicitement
l'union des URI nécessaires sans remplacer les credentials existants.

Qualifier nominativement les ressources hors IaC : `gpt-6-sol`,
`gpt-image-2.5-*`, `text-embedding-3-small`, `fcaigateway`, WebIQ, les workspaces
Monitor, `bmoussaud-2-resource`, ses déploiements et son monitoring. Pour chacune,
consigner propriétaire, consommateurs, dépendances, portée DEV/externe,
preuve disponible et décision : intégrer, conserver comme dépendance ou laisser
inchangée en attente. Une absence de télémétrie ne prouve pas l'inutilité.

La documentation Microsoft consultée annonce Sweden Central pour les
[Hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
et la [modération Content Safety image](https://learn.microsoft.com/azure/ai-services/content-safety/region-availability).
Ce sont des indications de disponibilité, pas des preuves de quota, de permissions
ou de fonctionnement du compte cible. La clôture de
[#96](https://github.com/bmoussaud/fantasy-cards-generator/issues/96)
ne dispense pas de cette vérification.

Obtenir et consigner les contributions ou évaluations d'absence d'impact requises :
backend, UI/accessibilité, tests, infrastructure/réseau, architecture,
identité/authentification, sécurité/confidentialité, IA responsable et vérification
des preuves. Aucun de ces avis n'est acquis par ce plan. Les revues indépendantes
infrastructure, identité et données restent notamment en attente.

### Vérifications de faisabilité du 26 novembre 2026

Sources : Microsoft Learn, #194 et sorties de commandes Azure en lecture seule
communiquées par @bmoussaud le 26/11/2026. L'abonnement actif n'apparaît pas dans
les sorties communiquées : confirmer qu'il s'agit de `bmoussaudms` avant usage.
L'environnement de l'agent ne résout ni `login.microsoftonline.com`, ni
`management.azure.com` ; ces observations ne sont donc pas une vérification
directe par l'agent.

| Dépendance | Sweden Central | Preuve | Reste à vérifier en live |
|---|---|---|---|
| `MAI-Image-2.7` `2026-11-02` (préversion) | Catalogue | La sortie CLI indique format `Microsoft`, SKU `GlobalStandard` et quota 7/12, soit 5 unités disponibles. | Confirmer l'abonnement et le quota au déploiement. La date de dépréciation d'inférence annoncée est le 17/02/2027. |
| `gpt-5.5` `2026-04-24` | Catalogue | La sortie CLI indique `GlobalStandard` 2000/2000 et `DataZoneStandard` 2/667, soit 665 unités disponibles pour ce dernier SKU. | Confirmer l'abonnement, le quota et la capacité effective : le nouveau DEV demande 10 unités `DataZoneStandard`. |
| `gpt-image-2` `2026-04-21` | Catalogue | La sortie CLI indique `GlobalStandard` 0/4, soit 4 unités disponibles. | Confirmer l'abonnement et le quota au déploiement ; la cible demande 1 unité. |
| Hosted agents | Oui | [Liste Learn des régions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents). | Quota de sessions régional. |
| Content Safety texte/image | Oui | [Disponibilité Learn](https://learn.microsoft.com/azure/ai-services/content-safety/region-availability). Le défaut Bicep reste `MODERATION_SERVICE=heuristic`. | Confirmer le chemin de modération requis et sa compatibilité. |
| ACA, Cosmos, ACR | Oui | Fournisseurs ARM vérifiés dans #194 ; les sorties CLI indiquent les fournisseurs enregistrés. | Restrictions régionales et politiques de l'abonnement. |
| Key Vault, Storage, NAT, Log Analytics, App Insights | À confirmer | Fournisseurs enregistrés dans le contexte CLI communiqué ; un App Insights existe déjà en Suède (#194). | Disponibilité des SKU, restrictions et politiques de l'abonnement. |
| Application Entra distincte | n/a | La requête Graph sur `fantasy-cards-generator-dev-swedencentral` retourne une liste vide. | Confirmer le tenant et les permissions Graph du contexte cible. |

Le SKU texte choisi est `DataZoneStandard`, via
`AI_FOUNDRY_TEXT_DEPLOYMENT_SKU_NAME` dans le contexte azd du nouveau DEV.
Les autres environnements conservent `GlobalStandard` par défaut. Les deux
modèles image restent en `GlobalStandard` ; le choix du SKU texte ne garantit
pas la résidence de toutes les inférences dans l'UE.

Requêtes en lecture seule à exécuter par @bmoussaud, connecté à l'abonnement
`bmoussaudms` :

```bash
az account set --subscription b8ff3e15-7e2d-4fac-a773-992fb59ccedd
az cognitiveservices model list -l swedencentral \
  --query "[?model.name=='MAI-Image-2.7'||model.name=='gpt-5.5'||model.name=='gpt-image-2'].{n:model.name,v:model.version,f:model.format,s:join(',',model.skus[].name)}" -o table
az cognitiveservices usage list -l swedencentral \
  --query "[?contains(name.value,'MAI-Image-2.7')||contains(name.value,'gpt-5.5')||contains(name.value,'gpt-image-2')].{q:name.value,cur:currentValue,lim:limit}" -o table
az cognitiveservices account deployment list -n bmoussaud-2-resource -g <rg> \
  --query "[].{d:name,m:properties.model.name,c:sku.capacity}" -o table
az cosmosdb locations show --location swedencentral -o json
az policy assignment list --query "[].{n:displayName,e:enforcementMode}" -o table
az rest --method get --url "https://graph.microsoft.com/beta/applications?\$filter=uniqueName eq 'fantasy-cards-generator-dev-swedencentral'&\$select=appId"
```

Condition de sortie : pour les SKU sélectionnés, `limit - current` vaut au moins
1 pour MAI-Image-2.7 et gpt-image-2, et au moins 10 pour gpt-5.5. Le quota peut
changer avant le déploiement. À défaut, demander un quota supplémentaire sans
supprimer l'ancien DEV.

**Point d'arrêt :** compléter la qualification et les revues en attente ;
l'approbation de l'implémentation ne les remplace pas. Le preview et le
déploiement nécessitent des autorisations distinctes.

## Lot 1 - Préparer l'IaC de coexistence

**Dépendance :** lot 0 et approbation d'implémentation.

- Réutiliser les modules Bicep/AVM pour recréer réseau, NAT, ACA, ACR, données,
  Key Vault et monitoring dans la cible. Ne pas ajouter une seconde pile parallèle.
- Documenter les paramètres non secrets du nouveau contexte : abonnement,
  groupe distinct, région, environnement `dev` et choix Entra. Ne pas copier les
  fichiers d'environnement de l'ancien DEV.
- Appliquer le choix Entra approuvé dans `infra/main.bicep`,
  `infra/main.parameters.json` et, si nécessaire,
  `infra/modules/app-registration.bicep`. Préserver callbacks et credentials
  de l'ancien DEV.
- Préserver les identités système du compte et du projet Foundry, les permissions
  de la MI WEB, de la MI projet et de l'identité de pull ACR. Réutiliser les rôles
  existants ; ne pas élargir au niveau abonnement pour contourner un refus.
- Garder Cosmos, Blob et Key Vault accessibles uniquement par private endpoints.
  Vérifier DNS et chemin réseau de l'exécuteur pour les opérations qui le requièrent,
  sans ouvrir temporairement les données au réseau public.

**Sortie attendue :** configuration reproductible du DEV cible sans modification
des ressources de l'ancien DEV ni des paramètres effectifs de production.

## Lot 2 - Décrire MAI dans le compte Foundry cible

**Dépendance :** lot 1 et paramètres de modèles vérifiés au lot 0.

- Dans `infra/modules/ai-foundry.bicep`, ajouter le déploiement MAI à côté du texte
  et de GPT Image. Paramétrer nom, version, format, SKU et capacité ; conserver
  l'ordre de déploiement nécessaire pour éviter les conflits sur le compte.
- Raccorder ces paramètres dans `infra/main.bicep` et `infra/main.parameters.json`.
  Activer le déploiement MAI pour la cible approuvée, sans imposer sa création aux
  autres environnements ou régions.
- Décrire en IaC le droit d'inférence MAI de la MI WEB, après vérification des
  opérations réellement requises et de la portée minimale. Ne pas recopier
  aveuglément une attribution manuelle.
- Définir MAI comme sélection du DEV cible. Conserver GPT Image déployé et
  sélectionnable explicitement, sans réaction automatique aux erreurs.
- Si l'override d'endpoint est vide, calculer
  `https://<compte-cible>.services.ai.azure.com` et l'injecter dans
  `FOUNDRY_IMAGE_ENDPOINT`. Conserver la validation HTTPS et la compatibilité
  d'un override explicite pour les déploiements existants.
- Raccorder `IMAGE_SIZE` à `imageSize` et configurer `1024x1536` pour cette cible.
  Il n'est pas nécessaire de changer les clients de génération ni leurs défauts
  locaux pour satisfaire ce paramétrage de déploiement.
- Recréer les connexions de monitoring et d'ACR vers les ressources cibles ;
  activer les alertes et configurer leurs destinataires selon la décision approuvée.

**Sortie attendue :** un compte/projet cible autonome pour les dépendances DEV
confirmées, sans endpoint d'images externe nécessaire.

## Lot 3 - Vérifications hors ligne et revue du preview

**Dépendance :** lots 1 et 2.

Compléter les tests existants, principalement `tests/test_deployment_config.py`,
et utiliser `tests/test_gen_client_secret_hook.py` pour les invariants d'identité
et de non-rotation. Vérifier les configurations ancien DEV, DEV Sweden Central
et production inchangée, en particulier :

- MAI et GPT Image coexistent ; la sélection et l'endpoint effectifs restent cohérents.
- Un override vide produit l'endpoint cible ; un override invalide est refusé.
- `1024x1536` atteint le runtime et reste conforme aux limites MAI.
- Les identifiants Entra et callbacks respectent le choix approuvé.
- Les références et rôles pointent vers la cible, les stockages restent privés,
  les prérequis agent et le triplet de version exact restent obligatoires.
- Un second provisionnement conserve l'image WEB et ne recrée pas de credential
  existant. Les tests MAI existants préservent refus, erreurs et absence de fallback.

Restaurer et compiler `infra/main.bicep`, régénérer `infra/main.json`, puis exécuter
les tests ciblés concernés. Les tests de configuration et mocks ne prouvent ni
les permissions Azure, ni les quotas, ni la disponibilité.

**Point d'arrêt distinct :** demander l'autorisation d'exécuter
`azd provision --preview` dans le contexte cible. Examiner auparavant les hooks
et leurs effets avec la version d'azd utilisée : un preview ne doit pas être
présumé sans effet sur l'état local ou les secrets.

Faire revoir le preview : uniquement les créations et modifications cibles
attendues, aucune suppression de l'ancien DEV, aucune mutation de production.
Vérifier séparément les changements Graph/Entra si le preview ARM ne les couvre
pas. Toute expression ARM modifiée doit être vérifiée par ce preview autorisé ;
une compilation Bicep seule ne suffit pas.

**Sortie attendue :** revue indépendante des changements et du preview, avec
écarts résolus avant demande d'autorisation de provisionnement.

## Lot 4 - Recréer et déployer le DEV cible

**Dépendance :** lot 3 et autorisation explicite de provisionnement, déploiement
et opérations d'identité/secrets nécessaires. Ce plan n'est pas cette autorisation.

Dans le contexte cible vérifié, utiliser l'ordre de `azure.yaml` :

1. Provisionner les ressources partagées, les stockages métier vides et les
   prérequis de l'agent. Autoriser la création des secrets nécessaires par les
   mécanismes existants, sans afficher leurs valeurs ni tourner ceux de l'ancien DEV.
2. Déployer `card-orchestrator` avec un artefact précisément identifié dans le
   nouveau projet. Laisser le hook épingler l'endpoint à sa version exacte.
3. Reprovisionner pour injecter le triplet nom/version/version d'artefact et
   les références nécessaires dans WEB.
4. Packager puis déployer `web-nat` ; vérifier que le placeholder initial a
   été remplacé par l'artefact applicatif attendu.

`azd up` réalise cette séquence si elle est autorisée intégralement. Sinon,
utiliser les étapes ciblées avec des points d'arrêt. Ne pas utiliser un
`azd deploy` sans nom de service, ni les anciens launchers imbriqués.

## Lot 5 - Recette, bascule et retour arrière

**Dépendance :** lot 4. Les essais réels et leurs coûts doivent être autorisés.

| Critère de l'issue | Preuve à recueillir |
|---|---|
| Placement régional | Inventaire ARM cible : ressources régionales en Sweden Central, exceptions globales/externes documentées. |
| Foundry unifié | Compte et projet uniques ; texte, MAI, GPT Image et agent visibles ; embeddings seulement si retenus. |
| Nouveau DEV vide | Vérification de l'absence de données métier historiques avant recette, puis création, persistance et lecture de nouveaux profils, cartes et images. |
| Santé et identité | `/livez` et `/healthz`, version exacte de l'agent, connexion Entra en HTTPS et contrôle d'isolement entre propriétaires. |
| Génération réelle | Anglais, recette `cadre-classique@1`, expertises verbatim et dimensions réelles du PNG `1024x1536`, y compris le parcours de photo de référence retenu. |
| Routage modèle | Fenêtre d'essai corrélée aux `ModelRequests` du compte cible ; attribution à MAI. Essai GPT après sélection opérateur explicite, puis retour explicite à MAI. |
| Sécurité fonctionnelle | Refus sans nouvelle persistance interdite, erreurs/timeout sans bascule de modèle, reprise d'artwork sans répétition du texte réussi. |
| Exploitation | Alertes et sondes cibles opérationnelles ; ancien DEV encore utilisable avec ses données et son authentification. |

Les métriques prouvent le déploiement appelé, pas la localisation physique de
l'inférence. [GlobalStandard peut traiter les données hors de Suède ou de l'UE](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types).
Ne pas présenter cette migration comme une garantie de résidence d'inférence.

Après acceptation et autorisation de bascule, remplacer le point d'entrée utilisé
par les utilisateurs et consommateurs identifiés : URL documentée, intégrations
ou DNS si un tel mécanisme existe. Ne pas supposer une bascule DNS inexistante.
Conserver l'ancienne URL et les redirections nécessaires au retour arrière.

En cas d'échec de connexion, readiness, génération, persistance ou routage :
suspendre la bascule et revenir au point d'entrée de l'ancien DEV. Préserver aussi
les données déjà créées en Sweden Central. Elles n'existeront pas dans l'ancien
DEV : documenter cette limite et décider de leur traitement, sans synchronisation
ni suppression automatique. Le retour arrière n'est pas une migration de données.

## Lot 6 - Documentation et retrait séparé

Préparer dès les lots 1 et 2 les mises à jour de `README.md`,
`infra/README.md`, `docs/architecture.md`, `docs/card-generation-api.md` et
`docs/foundry-agent-operations.md` : bootstrap cible, modèles, endpoints,
configuration d'image, identités, exceptions et retour arrière. Après recette,
y consigner séparément l'état réellement déployé et les preuves.

La suppression de l'ancien DEV intervient uniquement après acceptation et
autorisation spécifique portant sur une liste nominative de ressources et données.
Ne pas supprimer en bloc l'ancien groupe : il contient des ressources hors IaC
et potentiellement partagées. Les usages inconnus restent préservés.

Le critère de décommissionnement de l'issue demeure non satisfait tant que cette
étape distincte n'a pas été approuvée puis exécutée. Ce plan n'autorise pas non plus
de commit, push, PR, merge ou modification de l'issue.

## Ordre et permissions

`Qualification -> approbation d'implémentation -> IaC -> tests et revue ->
preview autorisé et approuvé -> déploiement autorisé -> recette ->
bascule autorisée -> décommissionnement séparément autorisé`.

Un blocage de quota, de runtime, d'identité ou de propriété d'une dépendance
renvoie à la qualification ; il ne justifie ni un contournement, ni un changement
de région, ni une réduction des garde-fous sans nouvel arbitrage.
