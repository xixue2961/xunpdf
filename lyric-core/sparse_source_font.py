"""Conservative exact binary font prefilters; final acceptance uses the original CV metric."""
import hashlib,math
from collections import OrderedDict
import cv2,numpy as np

class SparseSourceFontMatcher:
    def __init__(self,gray,thresholds=(160,208)):
        self.search={t:(gray<t).astype(np.uint8) for t in thresholds}
        self.integrals={t:cv2.integral(v,sdepth=cv2.CV_32S) for t,v in self.search.items()}
        self.count_cache=OrderedDict();self.count_bytes=0;self.match_cache=OrderedDict();self.match_bytes=0
        self.calls=0;self.cache_hits=0;self.full_metric_checks=0
    def match(self,threshold,template):
        self.calls+=1;th,tw=template.shape;search=self.search[threshold]
        if th>search.shape[0] or tw>search.shape[1]:return []
        key=(threshold,th,tw,hashlib.sha256(template.tobytes()).digest())
        if key in self.match_cache:
            self.cache_hits+=1;self.match_cache.move_to_end(key);return self.match_cache[key]
        K=int(template.sum());N=template.size
        if not 0<K<N:return []
        ck=(threshold,th,tw)
        if ck in self.count_cache:self.count_cache.move_to_end(ck);counts=self.count_cache[ck]
        else:
            integral=self.integrals[threshold];counts=integral[th:,tw:]-integral[:-th,tw:]-integral[th:,:-tw]+integral[:-th,:-tw]
            while self.count_cache and self.count_bytes+counts.nbytes>32*1024**2:
                _,old=self.count_cache.popitem(last=False);self.count_bytes-=old.nbytes
            if counts.nbytes<=32*1024**2:self.count_cache[ck]=counts;self.count_bytes+=counts.nbytes
        # Both precision and recall >= .98 imply these exact foreground-count bounds.
        valid=(counts>=math.ceil(.98*K-1e-8))&(counts<=math.floor(K/.98+1e-8));ys,xs=np.where(valid)
        if not len(xs):return self._remember(key,[])
        target_counts=counts[ys,xs];black=np.argwhere(template);white=np.argwhere(template==0)
        miss_limit=math.floor(.02*K+1e-8);black_samples=black[np.linspace(0,len(black)-1,min(len(black),max(32,min(512,4*max(1,miss_limit))))).astype(int)]
        misses=np.zeros(len(xs),dtype=np.int32)
        for start in range(0,len(black_samples),8):
            batch=black_samples[start:start+8];misses+=(1-search[ys[:,None]+batch[:,0],xs[:,None]+batch[:,1]]).sum(axis=1,dtype=np.int32)
            keep=misses<=miss_limit;ys,xs,target_counts,misses=ys[keep],xs[keep],target_counts[keep],misses[keep]
            if not len(xs):return self._remember(key,[])
        white_limit=np.floor(.02*target_counts+1e-8).astype(int);sample_count=min(len(white),max(32,min(512,4*max(1,int(white_limit.max())))));white_samples=white[np.linspace(0,len(white)-1,sample_count).astype(int)];spurious=np.zeros(len(xs),dtype=np.int32)
        for start in range(0,len(white_samples),8):
            batch=white_samples[start:start+8];spurious+=search[ys[:,None]+batch[:,0],xs[:,None]+batch[:,1]].sum(axis=1,dtype=np.int32)
            keep=spurious<=white_limit;ys,xs,target_counts,spurious,white_limit=ys[keep],xs[keep],target_counts[keep],spurious[keep],white_limit[keep]
            if not len(xs):return self._remember(key,[])
        found=[]
        for start in range(0,len(xs),256):
            xx=xs[start:start+256];yy=ys[start:start+256];L=target_counts[start:start+256];inter=search[yy[:,None]+black[:,0],xx[:,None]+black[:,1]].sum(axis=1,dtype=np.int64);precision=inter/L;recall=inter/K
            for index in np.flatnonzero((precision>=.98)&(recall>=.98)):
                y=int(yy[index]);x=int(xx[index]);patch=search[y:y+th,x:x+tw];score=float(cv2.matchTemplate(patch,template,cv2.TM_CCOEFF_NORMED)[0,0]);self.full_metric_checks+=1
                if score>=.95:found.append((y,x,score,float(precision[index]),float(recall[index])))
        return self._remember(key,found)
    def _remember(self,key,value):
        size=96+len(value)*96
        while self.match_cache and (len(self.match_cache)>=512 or self.match_bytes+size>16*1024**2):
            _,old=self.match_cache.popitem(last=False);self.match_bytes-=96+len(old)*96
        if size<=16*1024**2:self.match_cache[key]=value;self.match_bytes+=size
        return value
