"""Read-only spacing normalization for widely spaced lyric glyphs; preserve column mapping."""
import numpy as np
from PIL import Image

def pack_lyric_columns(image,height,start=0,end=None):
    gray=np.asarray(image.convert('L'));end=gray.shape[1] if end is None else min(gray.shape[1],end);start=max(0,start);occupied=(gray[:,start:end]<160).any(axis=0);keep=[];i=0
    while i<len(occupied):
        if occupied[i]:keep.append(i+start);i+=1;continue
        j=i
        while j<len(occupied) and not occupied[j]:j+=1
        count=j-i
        if count>.5*height:
            pad=min(count,max(4,round(.22*height)))
            left=pad//2;right=pad-left
            keep.extend(range(i+start,i+start+left));keep.extend(range(j+start-right,j+start))
        else:keep.extend(range(i+start,j+start))
        i=j
    if not keep:raise ValueError('no source lyric columns')
    packed=Image.fromarray(gray[:,keep]);assert np.array_equal(np.asarray(packed),gray[:,keep]);return packed,list(map(int,keep))
