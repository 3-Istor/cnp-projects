# cnp-projects

## Rôle

Registre Git des projets CNP : chaque ProjectRecord décrit le placement cloud, les environnements, fonctionnalités et applications d’un projet. Le répertoire `projects/` racine est distinct et destiné aux manifests de l’Application Argo CD historique.

## Technologies

YAML, JSON Schema, validateur Python, Make et GitHub Actions ; données consommées par Argo CD.

## Entrées

| Origine / destinataire | Contenu et transmission |
| --- | --- |
| CMP | Écritures des projets et des applications via la GitHub App dans `registry/projects/<nom>.yaml`. |
| Opérateurs | Modifications Git du registre, soumises au schéma et aux contraintes de placement. |

## Sorties et consommateurs

| Origine / destinataire | Contenu et transmission |
| --- | --- |
| K3s / Argo CD | ProjectRecord lus par les ApplicationSet pour générer AppProject, services et applications ; `spec.targetCloud` choisit la destination. |
| cnp-project-base (absent localement) | Valeurs de projet, fonctionnalités et hostnames transmises au chart par les ApplicationSet. |
| CMP / contributeurs | Registre lisible par API GitHub, schéma et validation locale/CI. CMP conserve un miroir du placement en base. |

## Documentation CNP

[Fiche `cnp-projects` et workflows inter-repo](https://github.com/3-Istor/cnp-docs/blob/main/docs/04-templates/00-github-repositories-landscape.md#cnp-projects).

## Registre et validation

Lire [le contrat du registre](registry/README.md) et [son schéma](registry/schema.json) avant de modifier un ProjectRecord.

```bash
make validate
```

Ne pas placer les ProjectRecord de `registry/projects/` dans `projects/` : ce dernier est lu comme des manifests Kubernetes par l’Application Argo CD historique.

Le cloud cible doit correspondre à un cluster enregistré dans Argo CD. CMP interdit le changement en place de `targetCloud` ; une édition Git ne constitue pas une migration des ressources.
