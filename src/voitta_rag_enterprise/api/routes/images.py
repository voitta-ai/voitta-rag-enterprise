"""Image-bytes endpoint. Streams raw image data from CAS."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...cas import store as cas_store
from ...db.models import File, Image
from ...services.acl import CurrentUser, user_can_see_folder
from ..deps import current_user, db_session

router = APIRouter(prefix="/images", tags=["images"])


@router.get("/{image_id}")
def get_image(
    image_id: int,
    db: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
) -> Response:
    img = db.get(Image, image_id)
    # Folder-scoped authorization via the image's owning file — an image is
    # reachable only when its file's folder is visible to the caller.
    # Without this, image ids could be enumerated to read any figure/page
    # render in the deployment. 404 (not 403) so foreign ids aren't
    # probeable — mirrors files._require_visible_file.
    if img is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    file = db.get(File, img.file_id)
    if file is None or not user_can_see_folder(db, file.folder_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    try:
        data = cas_store.read_image_blob(img.image_cas_id)
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image bytes missing") from e
    return Response(content=data, media_type=img.mime or "application/octet-stream")
