"""Studio: generative code and GUI for ABP itself, with real-time preview.

A variant is an overlay of ABP's UI: a folder holding only the files it changes. ABP serves its pages with a variant's
files swapped in (same origin, real data), and the preview reloads the moment one of them changes, so a change is seen
live, several variants side by side, and one is applied (with a backup, one click to revert) or discarded.

    variants   create, edit, diff, apply, revert, discard (data/studio/variants/<id>)
    edits      the edit blocks models answer with (search/replace, new files), applied exactly or refused
    generate   layer 2: local and API models fill several variants from one instruction
    tokens     layer 0: the pages' theme tokens (CSS custom properties), read and overridden per variant
    shots      screenshots of a preview (a real browser) and what visibly changed (bot/vision's compare)
    log        every step logged (data/studio/log.jsonl), and datasets built from it for ABP's own models

Layer 0 is the person editing (files, theme, components) with the preview live; layer 2 is models proposing variants;
layer 1 (ABP's own KotMoE / BrainBuilder models trained on this log) is roadmap item GEN-1.
"""
