import json
from pathlib import Path
import numpy as np
from PIL import Image

T = {k: [np.array(x, dtype=bool) for x in v] for k, v in
     json.loads((Path(__file__).parent / "digit_templates.json").read_text(encoding="utf-8")).items()}
def getmask(img):
 a=np.asarray(img.convert('RGB')).astype(float)
 return (a[:,:,0]>115)&(a[:,:,0]>a[:,:,1]*1.3)&(a[:,:,1]>a[:,:,2]*1.3)&(a[:,:,1]>45)
def score(mask,x,t):
 w=t.shape[1]
 if x<0 or x+w>mask.shape[1]:return 1000
 return np.count_nonzero(mask[:,x:x+w]!=t)
def decode_digits(mask, start, end):
 # Dynamic programming keeps alternative digit lengths alive instead of
 # discarding them when a narrow "1" looks deceptively inexpensive.
 # Keep the best few candidates for each (digit count, end pixel).
 states = {start: [(0.0, "")]}
 finals = []
 score_cache = {}
 for digit_count in range(1, 6):
  new = {}
  for x, candidates in states.items():
   for ch in "0123456789":
    if digit_count == 1 and ch == "0":
     continue
    for templ in T[ch]:
     w = templ.shape[1]
     if x + w > end + 2:
      continue
     key = (x, ch, w, templ.tobytes())
     sc = score_cache.get(key)
     if sc is None:
      sc = score(mask, x, templ)
      score_cache[key] = sc
     for gap in (0, 1, 2):
      nxt = x + w + gap
      if nxt > end + 3:
       continue
      skipped = int(np.count_nonzero(mask[:, x+w:min(nxt, mask.shape[1])]))
      for cost, text in candidates:
       new.setdefault(nxt, []).append((cost + sc + skipped + gap * .08, text + ch))
  states = {}
  for x, entries in new.items():
   best = {}
   for cost, text in entries:
    if text not in best or cost < best[text]:
     best[text] = cost
   states[x] = sorted(((cost, text) for text, cost in best.items()))[:8]
   if abs(x - (end + 1)) <= 2:
    for cost, text in states[x]:
     finals.append((cost + abs(x - (end + 1)) * .8, text))
 if not finals:
  return None
 best = {}
 for cost, text in finals:
  if text not in best or cost < best[text]:
   best[text] = cost
 return sorted(((cost, text) for text, cost in best.items()))[:6]

def read(img):
 mask=getmask(img)
 rows=np.where(mask.any(axis=1))[0];runs=[]
 for y in rows:
  if not runs or y>runs[-1][-1]+1:runs.append([y])
  else:runs[-1].append(y)
 result=[]
 for r in runs:
  if len(r)!=7:continue
  m=mask[r[0]:r[-1]+1];xs=np.where(m.any(axis=0))[0];start=xs.min();end=xs.max()
  # hash from start; slash search around mid
  slashes=sorted([(min(score(m,x,t) for t in T['/']),x) for x in range(start+13,end-9)],key=lambda a:a[0])[:8]
  opts=[]
  for sc,x in slashes:
   left=sorted((decode_digits(m,start+off,x-3) or [] for off in (7,8,9)),key=lambda z:z[0][0] if z else 1e9)[0]
   right=decode_digits(m,x+7,end)
   if left and right:
    for l in left[:2]:
     for rr in right[:2]:
      try:
       p=int(l[1]);n=int(rr[1]);
       if 0<p<=n<100000:opts.append((sc+l[0]+rr[0],p,n,x))
      except:pass
  result.append(sorted(opts)[:4])
 return result
