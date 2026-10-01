# The board, one file per item

`docs/WORK.md` is the board. It is assembled, not stored: the file in this
repository holds every heading, every standing instruction and the ORDER of
the items, with one marker line — `<!-- item 55 -->` — where item 55's block
sits. The block itself lives in `items/item-55.md`.

**Why.** Every change has to edit the board: `scripts/definition_of_done.py`
reads what a change filed and what it closed out of the board's own diff, and
`scripts/board_numbers.py` allocates the next item number from the same text.
That requirement is right and is unchanged. What was wrong is that it made one
physical file the collision surface for every branch at once — 67% of the last
300 commits touched it, and 28% of parallel branch pairs replayed off real
history collided inside an item block. Two changes to two different items now
write to two different files.

**Reading it.** Never read `docs/WORK.md` off disk. Call
`scripts/board_source.py`: `work_md_text()` for a tree, `work_md_text_at_ref()`
for a git ref, `work_md_path()` when you need a path. All three return the
whole assembled board, byte-identically to the single file they replaced.

**Editing it.** Edit `items/item-<N>.md` for an item's own text. Edit
`docs/WORK.md` to file a new item (add its marker line where it belongs, and
its file), to retire one (the retired-numbers line), or to change anything
that is not inside an item block.

**Filing a new item.** Unchanged: `scripts/next_board_number.py`. The
open-pull-request half of that check reads marker lines as well as headings,
so a number claimed on a branch is still seen before it lands.
