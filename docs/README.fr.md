# Agent IA pour les opérations hôtelières

Ce projet transforme des demandes en langage naturel en opérations contrôlées dans un logiciel de gestion hôtelière.

Le modèle de langage comprend l’objectif et propose la prochaine action. L’orchestrateur Python conserve l’état, valide la décision, exécute un outil autorisé et vérifie le résultat dans QloApps avant d’annoncer une réussite.

## Capacités principales

- rechercher, créer, modifier ou prolonger une réservation ;
- vérifier les disponibilités et comparer des offres ;
- produire un devis à partir de tarifs vérifiés ;
- consulter les arrivées, départs et séjours en cours ;
- réaliser un check-in ou un check-out ;
- ajouter un service issu du catalogue ;
- émettre et relire une facture ;
- consulter et traiter une demande client ;
- rechercher une procédure dans la couche documentaire optionnelle.

## Fonctionnement

```text
Utilisateur
    ↓
Interface web
    ↓ HTTP + JSON
FastAPI
    ↓
Orchestrateur
    ↓ contexte + outils disponibles
Modèle de langage
    ↓ appel d’outil structuré
Validation et permissions
    ↓
Outil Python
    ↓
Adaptateur QloApps
    ↓
Logiciel de gestion hôtelière
```

Après chaque résultat, l’orchestrateur redonne l’observation au modèle. Celui-ci peut répondre, demander une précision ou choisir l’étape suivante.

Pour une écriture, le système demande une validation humaine liée aux paramètres exacts. Après exécution, il relit QloApps et ne confirme la réussite que si l’état enregistré correspond à l’action validée.

## Choix d’architecture

- DeepSeek fournit la compréhension sémantique et le function calling.
- FastAPI expose le backend à l’interface.
- Pydantic valide les contrats de données.
- Un registre fermé limite les outils accessibles au modèle.
- L’adaptateur isole les particularités de QloApps.
- Docker fournit un environnement local reproductible.
- pytest couvre les règles, outils et scénarios opérationnels.

Le projet n’utilise ni LangChain ni LangGraph : la boucle agentique a été implémentée explicitement afin de garder le contrôle sur l’état, les autorisations, les validations et les relectures.

## Documentation

- [Architecture détaillée](architecture.md)
- [Sécurité et fiabilité](safety-and-reliability.md)
- [Évaluation](evaluation.md)
- [Scénarios de démonstration](demo-scenarios.md)

