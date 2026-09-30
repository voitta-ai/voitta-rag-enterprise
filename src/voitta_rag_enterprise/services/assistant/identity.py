"""Who is talking to the assistant, and on whose behalf.

Two identities are in play and they are deliberately kept apart:

* ``real`` — the person at the keyboard (``api.deps.real_user``). They are
  the author of every message they type, and the owner of the credential
  that pays for it: a personal API key is only ever the *real* person's,
  and the Claude subscription requires the *real* person to be a
  super-admin. Impersonation never borrows the target's credentials or
  privileges.
* ``effective`` — the account whose view is active (``current_user``):
  the real account, or the target of an admin "view as". Tools always run
  with the effective account's folder access, exactly like every other
  surface of the app during impersonation.

Conversation lists follow the same split. ``mine`` is the real account's
list; ``theirs`` (only while impersonating) is the effective account's. An
admin can read and continue either; nobody else can touch a conversation
they own neither of.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...config import get_settings
from ..acl import CurrentUser
from ..admin_store import is_super_admin

ConversationView = Literal["mine", "theirs"]


def is_assistant_admin(user: CurrentUser) -> bool:
    """May manage deployment-wide assistant settings and use the shared
    Claude subscription. Super-admins; in single-user (desktop) mode the one
    local user, who owns the whole installation."""
    return get_settings().single_user or is_super_admin(user.email)


@dataclass(frozen=True)
class AssistantIdentity:
    real: CurrentUser
    effective: CurrentUser

    @property
    def impersonating(self) -> bool:
        return self.real.id != self.effective.id

    @property
    def is_admin(self) -> bool:
        return is_assistant_admin(self.real)

    @property
    def can_use_subscription(self) -> bool:
        return self.is_admin

    def owner_for(self, view: ConversationView) -> int:
        """Account id whose conversation list ``view`` addresses.

        ``theirs`` outside impersonation is the caller's own list — there
        is no one else to be.
        """
        return self.effective.id if view == "theirs" else self.real.id

    def can_access_owner(self, owner_user_id: int) -> bool:
        return owner_user_id in (self.real.id, self.effective.id)
