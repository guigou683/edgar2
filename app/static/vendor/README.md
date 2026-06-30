# Assets tiers vendus localement (offline, aucun CDN)

Ces fichiers sont **pré-téléchargés sur un poste connecté, figés, puis transférés**.
Ils ne sont **pas versionnés** dans Git (voir `.gitignore`) : ils sont déposés ici
lors de la préparation de l'image / du paquet hors-ligne.

Fichiers attendus (déposés par `scripts/export_offline.sh`) :

| Fichier            | Version cible | Rôle                                  |
|--------------------|---------------|---------------------------------------|
| `htmx.min.js`      | 2.0.x         | interactivité / SSE sans build        |
| `marked.min.js`    | 14.x          | rendu Markdown côté client (streaming)|
| `highlight.min.js` | 11.x          | coloration des blocs de code          |
| `dompurify.min.js` | 3.x           | assainissement HTML (anti-XSS)        |

Tant qu'ils sont absents, l'application démarre quand même (chargés en `defer`) ;
les fonctionnalités correspondantes s'activent une fois les fichiers présents.
