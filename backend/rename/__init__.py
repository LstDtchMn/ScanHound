"""Auto-rename + library-sort subsystem.

Identifies extracted media (reusing ScanHound's parser/TMDB/matching), scores
match confidence, builds Plex-convention names, and places files into the
library with a reversible record. Core logic ported and adapted from the Nomen
project (similarity, template engine, Plex naming, safe move) — Nomen itself is
not a dependency.
"""
