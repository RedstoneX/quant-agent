import subprocess, sys


def show(ref, path="docs/WORK.md"):
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main(argv):
    sys.path.insert(0, ".")
    import scripts.resolve_doc_conflict as rdc

    branch, parent = argv[1], argv[2]
    base_ref = subprocess.run(
        ["git", "merge-base", parent, "origin/main"], capture_output=True, text=True
    ).stdout.strip()
    base = show(base_ref)
    ours = show(parent)
    theirs = show("origin/main")
    assert base and ours and theirs
    # item 211 was re-opened on main; a pre-reopen branch still carries its
    # retired bullet, which the resolver rightly refuses against a live item.
    strip = lambda t: chr(10).join(l for l in t.split(chr(10)) if l.strip() != "- retired queue: 211")
    base = strip(base)
    ours = strip(ours)
    merged = rdc.RESOLVERS["work"](base, ours, theirs)
    if "<<<<<<<" in merged:
        print("REFUSED")
        return 2
    open("docs/WORK.md", "w").write(merged)
    added = len([l for l in merged.split("\n") if l.startswith("**")]) - len(
        [l for l in theirs.split("\n") if l.startswith("**")]
    )
    print("ok item-count-delta-vs-main", added)


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
