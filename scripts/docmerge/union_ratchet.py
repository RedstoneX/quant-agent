import sys
p=sys.argv[1]
L=open(p).read().split('\n')
out=[];i=0;n=0
while i<len(L):
    if L[i].startswith('<<<<<<<'):
        n+=1;i+=1;ours=[]
        while not L[i].startswith('|||||||') and not L[i].startswith('======='): ours.append(L[i]);i+=1
        if L[i].startswith('|||||||'):
            i+=1
            while not L[i].startswith('======='): i+=1
        i+=1;theirs=[]
        while not L[i].startswith('>>>>>>>'): theirs.append(L[i]);i+=1
        i+=1
        out.extend(theirs); out.extend(ours)
    else:
        out.append(L[i]); i+=1
s='\n'.join(out)
open(p,'w').write(s)
print('unioned',n,'markers left',s.count('<<<<<<<'))
