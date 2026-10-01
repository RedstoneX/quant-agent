## item 210 — A properly structured codebase, built in the right order (owner-ratified 2026-09-30)

The owner asked whether to run the big file split in parallel with everything else, or shut the desk down and rebuild it properly. He ratified the answer on 2026-09-30: neither.

The reasoning he accepted: the desk's behaviour is not what is broken. Two oversized files and too little recorded evidence are. A full rebuild would spend his remaining time and money to arrive at behaviour he already has. A parallel split would collide with every open pull request, because nearly all of them edit the two files being moved.

The order matters and is the completion criteria:
1. Drain the open pull-request queue to zero first.
2. Then do the split as the only work in flight, from `docs/PIPELINE_SPLIT_PLAN.md` (copied here from a scratchpad so it cannot be lost). The plan's line numbers go stale the moment any pull request merges, so rerun its measurement before step 1 of the split; the plan names an AST script that lived in the scratchpad and is not in the repo, so regenerate it.
3. Rebuild the test suite in the same pass. The silent risk is measured: 42 tests patch `pipeline.compute_indicators` and 20 patch `pipeline._get_sector` on the module, so when that code moves they stop patching anything, run the real code, and still pass. Every such patch must be re-pointed at the new home, and a check added so a patch on a name that does not exist fails loudly.
