"""Content-addressable storage for extracted text and images.

Layout (see ARCHITECTURE.md §3.1)::

    cas/files/<file_sha>/{text.md, manifest.json}
    cas/images/<image_sha>.bin

Implementation lands in Stage 2.
"""
