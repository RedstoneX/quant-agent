import re, sys

def main(argv):
    p = argv[1]
    L = open(p).read().split('\n')
    out, i, n = [], 0, 0
    while i < len(L):
        if L[i].startswith('<<<<<<<'):
            n += 1
            i += 1
            ours = []
            while not L[i].startswith('|||||||') and not L[i].startswith('======='):
                ours.append(L[i])
                i += 1
            base = []
            if L[i].startswith('|||||||'):
                i += 1
                while not L[i].startswith('======='):
                    base.append(L[i])
                    i += 1
            i += 1
            theirs = []
            while not L[i].startswith('>>>>>>>'):
                theirs.append(L[i])
                i += 1
            i += 1
            j = lambda rows: '\n'.join(rows)
            is_dict = ':' in j(base)
            if is_dict:
                pairs = lambda rows: dict(re.findall(r'"(\d+)":\s*(\d+)', j(rows)))
                o, b, t = pairs(ours), pairs(base), pairs(theirs)
                keep = {k: min(int(b[k]), int(o[k]), int(t[k]))
                        for k in b if k in o and k in t}
                items = sorted(keep.items(), key=lambda kv: int(kv[0]))
                out.append('    ' + ', '.join(f'"{k}": {v}' for k, v in items) + ',')
            else:
                pick = lambda rows: set(re.findall(r'"(\d+)"', j(rows)))
                o, b, t = pick(ours), pick(base), pick(theirs)
                keep = sorted(b & o & t, key=int)
                out.append('    ' + ', '.join(f'"{x}"' for x in keep) + ',')
        else:
            out.append(L[i])
            i += 1
    s = '\n'.join(out)
    open(p, 'w').write(s)
    print('resolved', n, 'region(s); markers left', s.count('<<<<<<<'))


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
