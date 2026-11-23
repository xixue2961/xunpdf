import cv2,numpy as np
def hole_features(pixels,space):
 contours,hierarchy=cv2.findContours((pixels<180).astype(np.uint8)*255,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
 if hierarchy is None:return None
 h,w=pixels.shape;holes=[]
 for i,c in enumerate(contours):
  if hierarchy[0,i,3]<0 or cv2.contourArea(c)<.01*space*space:continue
  x,y,ww,hh=cv2.boundingRect(c)
  if not .15*w<x+ww/2<.85*w or ww<.15*w:continue
  holes.append((c,cv2.contourArea(c),(x,y,ww,hh)))
 if not 1<=len(holes)<=2:return None
 area=sum(a for c,a,b in holes);x=min(b[0] for c,a,b in holes);y=min(b[1] for c,a,b in holes);right=max(b[0]+b[2] for c,a,b in holes);bottom=max(b[1]+b[3] for c,a,b in holes)
 if not .14<=area/(w*h)<=.4 or right-x<.35*w or bottom-y<.5*h:return None
 return np.array([area/(w*h),x/w,y/h,(right-x)/w,(bottom-y)/h]),len(holes)
