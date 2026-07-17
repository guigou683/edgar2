# Assets tiers vendus localement (offline, aucun CDN)

Ces fichiers sont **figés et versionnés dans le dépôt** : aucune dépendance à un CDN,
aucun accès réseau à l'exécution, et un `git clone` produit une application complète.

Fichiers vendus :

| Fichier            | Version cible | Rôle                                  |
|--------------------|---------------|---------------------------------------|
| `htmx.min.js`      | 2.0.x         | interactivité / SSE sans build        |
| `marked.min.js`    | 14.x          | rendu Markdown côté client (streaming)|
| `highlight.min.js` | 11.x          | coloration des blocs de code          |
| `dompurify.min.js` | 3.x           | assainissement HTML (anti-XSS)        |

Tant qu'ils sont absents, l'application démarre quand même (chargés en `defer`) ;
les fonctionnalités correspondantes s'activent une fois les fichiers présents.
