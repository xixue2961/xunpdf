from collections import deque

def analyze(ink):
 h=len(ink);w=len(ink[0]) if h else 0;seen=set();black=[];holes=0
 for value in (True,False):
  seen=set()
  for y in range(h):
   for x in range(w):
    if bool(ink[y][x])!=value or (x,y) in seen:continue
    q=deque([(x,y)]);seen.add((x,y));points=[];edge=False
    while q:
     xx,yy=q.popleft();points.append((xx,yy));edge|=xx==0 or yy==0 or xx==w-1 or yy==h-1
     for dy in (-1,0,1):
      for dx in (-1,0,1):
       nx,ny=xx+dx,yy+dy
       if 0<=nx<w and 0<=ny<h and (nx,ny) not in seen and bool(ink[ny][nx])==value:seen.add((nx,ny));q.append((nx,ny))
    if value and len(points)>=3:black.append(points)
    if not value and not edge and len(points)>=3:holes+=1
 result=dict(accepted=False,components=len(black),holes=holes)
 if len(black)!=1:return result
 points=black[0];left=min(p[0] for p in points);top=min(p[1] for p in points);ww=max(p[0] for p in points)-left+1;hh=max(p[1] for p in points)-top+1;area=len(points);result.update(width=ww,height=hh,area=area)
 if hh<12 or not 1.4<=hh/ww<=5 or holes:return result
 a=[[bool(ink[top+y][left+x]) for x in range(ww)] for y in range(hh)];upper=max(1,int(hh*.55));cols=[x for x in range(ww) if sum(a[y][x] for y in range(upper))>=.75*upper]
 if not cols:return result
 sl,sr=min(cols),max(cols);outside=sum(a[y][x] for y in range(upper) for x in range(ww) if x<sl-1 or x>sr+1);result.update(stem_left=sl,stem_right=sr,upper_outside_ratio=outside/area)
 if sr-sl+1>max(3,.2*ww) or (sl+sr)/2<.65*ww or outside/area>.03 or ww-1-sr>max(1,.03*ww):return result
 wide=[y for y in range(upper,hh) if sum(a[y])>=.6*ww]
 if not wide:return result
 head_y=min(wide);head_h=hh-head_y;fill=sum(sum(row) for row in a[head_y:])/(ww*head_h);result.update(head_y=head_y,head_height=head_h,head_fill=fill)
 if 1<=ww/head_h<=3.5 and .5<=fill<=.95 and hh/head_h>=2:result['accepted']=True
 return result

def inspect_png(path):
 from PIL import Image
 im=Image.open(path).convert('L');w,h=im.size;pixels=list(im.getdata());return analyze([[pixels[y*w+x]<128 for x in range(w)] for y in range(h)])
