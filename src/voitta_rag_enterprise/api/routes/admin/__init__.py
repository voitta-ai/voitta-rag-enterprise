"""Admin-only REST surface.

All routes here are guarded by ``admin_user`` — only the *real* user's
``is_admin`` flag matters; impersonation does NOT confer admin rights.
The one exception is ``GET /auth-providers``, which is read-only and open
to any authenticated user so admin-defined OAuth apps work as shared
sign-in/sync shortcuts; its mutating siblings remain admin-only.

Three concerns covered:

1. **Allowlist / blocklist editing** — the live sign-in gate's source of
   truth lives in plain text files on the data PD; see
   ``services.admin_store``.

2. **User admin status** — flip ``users.is_admin`` for any address that
   has signed in at least once. Bootstrap admins (env-listed
   ``VOITTA_SUPER_ADMINS``) cannot be demoted via this API: their flag
   gets re-stamped on every sign-in.

3. **Impersonation** — set/clear ``session["acting_as_user_id"]``. The
   ``current_user`` dependency reads that and routes the rest of the
   app at the impersonated user's permissions.

Package layout — the endpoints are split across concern modules, all
attaching to the single shared ``router`` in ``base``:

- ``base``            shared router + the WS-state publisher
- ``allowlist``       allowlist / blocklist editing
- ``users``           User rows + admin flag
- ``impersonation``   session-scoped view-as
- ``auth_providers``  OAuth credentials catalog
- ``caps``            indexing caps
- ``settings``        typed admin settings + Clerk directory proxy
- ``groups``          organizational groups
- ``state``           full-state builder for the WS snapshot + push

Importing this package pulls in every endpoint module, which attaches its
routes to ``router`` as a side effect.
"""

from . import (  # noqa: F401
    allowlist,
    auth_providers,
    caps,
    groups,
    impersonation,
    settings,
    users,
)
from .base import publish_admin_state, router
from .state import build_admin_state

__all__ = ["build_admin_state", "publish_admin_state", "router"]
