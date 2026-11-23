"""Worker-only connected component export; cv2 stays in the homr runtime."""
from pathlib import Path
import hashlib,json
import cv2,numpy as np


def export_components(image_path,shapes_path,mask_path,shapes,corners,worker_path,models):
 image_path=Path(image_path);mask_path=Path(mask_path);mask=cv2.imdecode(np.fromfile(str(mask_path),dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
 if mask is None:raise ValueError('Exported mask cannot be read')
 _,labels,stats,_=cv2.connectedComponentsWithStats((mask>128).astype('uint8'),8);a,b=corners;sx=mask.shape[1]/(b[0]-a[0]);sy=mask.shape[0]/(b[1]-a[1]);members=[];groups={}
 for index,note in enumerate(shapes):
  x,y=note['center'];xx=round((x-a[0])*sx);yy=round((y-a[1])*sy)
  if not 0<=xx<mask.shape[1] or not 0<=yy<mask.shape[0]:raise ValueError('Head outside segmentation mask')
  label=int(labels[yy,xx]);members.append({'note_index':index,'mask_point':[xx,yy],'component':label})
  if label:groups.setdefault(label,[]).append(index)
 components=[]
 for label,indices in sorted(groups.items()):
  x,y,w,h,area=map(int,stats[label]);components.append({'component':label,'note_indices':indices,'mask_box':[x,y,w,h],'area':area})
 sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest();weights=[]
 for path in sorted({str(Path(p).resolve()) for p in models}):weights.append({'path':path,'sha256':sha(path),'size':Path(path).stat().st_size})
 value={'schema':1,'input_sha256':sha(image_path),'shapes_sha256':sha(shapes_path),'mask_sha256':sha(mask_path),'worker_sha256':sha(worker_path),'exporter_sha256':sha(__file__),'models':weights,'mask_shape':list(mask.shape),'mask_corners_in_input':corners,'head_membership':members,'components':components,'scope':'Actual threshold128 eight-connected mask components; not head glyph or voice proof'}
 output=image_path.with_suffix('.components.json');output.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8');return value
