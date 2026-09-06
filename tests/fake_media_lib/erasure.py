"""Shaped exactly like stapel-cdn's erasure module.

The app label ("blobs") is not the owner name ("fakemedia"), which is the
confusion the 2026-09-07 fleet incident was made of: a host listed the app
labels ``cdn`` and ``profiles`` where the libraries declare ``media`` and
``profile``, and nothing anywhere compared the two.
"""

OWNER = "fakemedia"
SUBJECT_TYPES = ("account", "workspace", "file")
