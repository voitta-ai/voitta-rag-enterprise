"""ACL: user resolution + folder/file authorisation.

Modes:

- ``VOITTA_SINGLE_USER=true`` → every request maps to ``root@localhost``.
- ``VOITTA_DEV_USER=<email>`` → every request maps to that email.
- multi-user (default) → REST identity comes from the signed session cookie
  set by Google OAuth; MCP identity comes from the verified ``Authorization:
  Bearer vk_…`` token. No header-based fallbacks.

Authorisation model (v2):

A folder has *one owner* (``folders.owner_id``) and an optional set of
explicit grants (``folder_acl``). It can also be marked ``shared=true``,
which makes it visible to every account in the owner's sharing community.

Visibility (who sees the folder in their listing / can read its files):

    visible(user) = owned(user) | granted(user)
                    | {f for f in folders if f.shared and same_community}

Mutation (delete, rename, share-toggle, sync configure, reindex, upload,
mkdir, grant, revoke): owner only.

MCP search visibility further intersects with the user's per-folder
``active`` flag (``folder_user_settings.active``); default-on means
"missing row = active". This lets a user mute folders they can see but
don't want polluting LLM search results.

Indexers still stamp ``allowed_users`` on every Qdrant chunk point at
index time, but the runtime search filter has moved to ``folder_id`` (built
from ``visible_folder_ids``) — that way shared-folder visibility doesn't
require re-indexing.

Package layout — one module per concern:

- ``identity``    who is calling (CurrentUser, resolve_user_email)
- ``accounts``    the (email, company_id) account model + admin flags
- ``community``   account → sharing community (THE org-scoping seam)
- ``folder_acl``  folder/file visibility, ownership, grants, opt-outs
"""

from .accounts import (
    accounts_for_email,
    default_account_for_email,
    get_or_create_user,
    offered_accounts_for_email,
    person_is_admin,
    public_user_ids,
    seed_users_from_file,
    stamp_person_admin,
)
from .community import _owner_community, account_community
from .folder_acl import (
    active_folder_ids,
    allowed_user_ids_for_file,
    folder_active_for_user,
    folder_user_id_email,
    folder_user_ids,
    grant_folder,
    is_folder_owner,
    mcp_visible_folder_ids,
    revoke_folder,
    set_folder_active,
    user_can_see_file,
    user_can_see_folder,
    visible_folder_ids,
)
from .identity import ROOT_EMAIL, CurrentUser, resolve_user_email

__all__ = [
    "ROOT_EMAIL",
    "CurrentUser",
    "_owner_community",
    "account_community",
    "accounts_for_email",
    "active_folder_ids",
    "allowed_user_ids_for_file",
    "default_account_for_email",
    "folder_active_for_user",
    "folder_user_id_email",
    "folder_user_ids",
    "get_or_create_user",
    "grant_folder",
    "is_folder_owner",
    "mcp_visible_folder_ids",
    "offered_accounts_for_email",
    "person_is_admin",
    "public_user_ids",
    "resolve_user_email",
    "revoke_folder",
    "seed_users_from_file",
    "set_folder_active",
    "stamp_person_admin",
    "user_can_see_file",
    "user_can_see_folder",
    "visible_folder_ids",
]
