"""Conservative printed shallow-arc evidence, with explicit protection for lower oval/head ink."""
import numpy as np

def inspect(im,bounds,space,baseline):
 x,y,w,h=map(int,bounds);profiles=[]
 if w<12 or h<3 or w/h<1.7:return dict(accepted=False,reason='not_shallow_rest_sized')
 for t in (96,160,208):
  mask=im[y:y+h,x:x+w]<t;top=[]
  for xx in range(w):
   ys=np.flatnonzero(mask[:,xx]);top.append(int(ys.min()) if len(ys) else None)
  xs=np.array([i for i in range(1,w-1) if top[i] is not None]);vals=np.array([top[i] for i in xs])
  if len(xs)<.8*(w-2):return dict(accepted=False,reason='insufficient_source_contour')
  fit=np.polyfit(xs,vals,2);res=float(np.mean((vals-np.polyval(fit,xs))**2));amp=float(fit[0]*w*w/4);flat=float(np.sum(vals==vals.min())/len(vals));profiles.append(dict(threshold=t,amplitude=amp,rmse=res**.5,flat_top_fraction=flat))
 curved=all(v['amplitude']>=.12*space and v['rmse']<=.075*space for v in profiles)
 # Rowwise background accounts for the staff-line JPEG halo; reference columns never use the candidate itself.
 left=im[:,max(0,x-4*w):max(0,x-2*w)];right=im[:,min(im.shape[1],x+3*w):min(im.shape[1],x+5*w)]
 if left.shape[1]<w or right.shape[1]<w:return dict(accepted=False,reason='no_independent_source_background')
 xa=x+int(np.ceil(.25*w));xb=x+int(np.floor(.75*w));ya=int(np.ceil(baseline+.22*space));yb=int(np.floor(baseline+.7*space))+1;below=im[ya:yb,xa:xb].astype(float);background=np.minimum(np.median(left[ya:yb],axis=1),np.median(right[ya:yb],axis=1));contrasts=background[:,None]-below;supported=[];required=max(4,int(np.ceil(.2*w)))
 for row in contrasts>=4:
  d=np.diff(np.r_[False,row,False].astype(int));starts=np.flatnonzero(d==1);ends=np.flatnonzero(d==-1);supported.append(bool(any(e-s>=required for s,e in zip(starts,ends))))
 lower_head=any(supported)
 return dict(accepted=bool(curved and not lower_head),reason='stable_shallow_arc_without_lower_head_body' if curved and not lower_head else 'potential_real_head_ink_below' if lower_head else 'not_stably_curved',profiles=profiles,lower_head_like_ink=lower_head,below_source_bounds=[xa,ya,xb-xa,yb-ya],below_background=background.tolist(),below_support_rows=supported)
