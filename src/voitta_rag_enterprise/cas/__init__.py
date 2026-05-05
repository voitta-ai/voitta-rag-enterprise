"""Content-addressable storage for extracted text and images.

Layout::

    cas/files/<file_sha>/{text.md, manifest.json}
    cas/images/<image_sha>.bin
"""
