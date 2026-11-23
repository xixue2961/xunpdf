import os,sys,json
from pathlib import Path
force_cpu='--geometry-cpu' in sys.argv
if force_cpu:sys.argv.remove('--geometry-cpu')
root=Path(__file__).resolve().parent
temp_root=Path(os.environ.get('XUN_OMR_TEMP',str(root/'.codex-task-cache/temp'))).resolve()
temp_root.mkdir(parents=True,exist_ok=True)
if any(not Path(p).resolve().is_relative_to(root) for p in sys.path if p):raise RuntimeError("External Python import path detected")
for key in ('TEMP','TMP','TMPDIR'):os.environ[key]=str(temp_root)
cache_root=temp_root.parent/'homr-caches'
for key,sub in [('CUDA_CACHE_PATH','cuda'),('HF_HOME','huggingface'),('XDG_CACHE_HOME','xdg')]:
    folder=cache_root/sub;folder.mkdir(parents=True,exist_ok=True);os.environ[key]=str(folder)
import socket
network_attempts=[]
def no_network(self,address):
    network_attempts.append(str(address))
    raise RuntimeError('Offline verification: network disabled '+str(address))
socket.socket.connect=no_network
socket.socket.connect_ex=no_network
import onnxruntime as ort
if not force_cpu:ort.preload_dlls()
print('ORT',ort.__version__,ort.get_available_providers(),flush=True)
sessions=[]
OriginalSession=ort.InferenceSession
class ProfiledSession(OriginalSession):
    def __init__(self,path,sess_options=None,**kwargs):
        if force_cpu:kwargs['providers']=['CPUExecutionProvider']
        options=sess_options or ort.SessionOptions()
        options.enable_profiling=True
        options.profile_file_prefix=str(temp_root/'homr-profile')
        super().__init__(path,sess_options=options,**kwargs)
        self.xun_model_path=str(Path(path).resolve())
        sessions.append(self)
        print('MODEL_PROVIDER',Path(str(path)).name,self.get_providers(),flush=True)
ort.InferenceSession=ProfiledSession
import cv2,numpy as np
# Windows OpenCV path compatibility: keep Unicode user/project names.
def read_unicode(path, flags=cv2.IMREAD_COLOR):
    return cv2.imdecode(np.fromfile(path,dtype=np.uint8),flags)
def write_unicode(path,image,params=None):
    ok,data=cv2.imencode(Path(path).suffix,image,params or [])
    if ok:data.tofile(path)
    return ok
cv2.imread=read_unicode
cv2.imwrite=write_unicode
if force_cpu:
    from homr import onnx_providers
    onnx_providers.cuda_available=lambda:False
    onnx_providers.rocm_available=lambda:False
    onnx_providers.coreml_available=lambda:False
from homr import main,debug
# Preserve review evidence; never delete previous generated files.
debug.Debug.clean_debug_files_from_previous_runs=lambda self:None
original_detect=main.detect_single_staffs
def capture_detect(image_path,config):
    result=original_detect(image_path,config)
    notes=[{'center':list(map(float,result.to_input_image(n.center))), 'staff_position':int(n.position)} for n in result.notes]
    notes.sort(key=lambda n:n['center'][0])
    Path(image_path).with_suffix('.segmentation-positions.json').write_text(json.dumps(notes,indent=2),encoding='utf-8')
    shapes=[]
    for note in result.notes:
        left,top=result.to_input_image(note.box.top_left);right,bottom=result.to_input_image(note.box.bottom_right)
        a=result.to_input_image((0,0));b=result.to_input_image(note.box.size)
        shapes.append({'center':list(map(float,result.to_input_image(note.center))),'staff_position':int(note.position),'box':list(map(float,(left,top,right,bottom))),'ellipse_size':[float(abs(b[0]-a[0])),float(abs(b[1]-a[1]))]})
    mask=result.predictions.notehead
    mask_path=Path(image_path).with_suffix('.notehead-mask.png');cv2.imwrite(str(mask_path),mask*255)
    corners=[list(map(float,result.to_input_image(p))) for p in [(0,0),(mask.shape[1],mask.shape[0])]]
    Path(image_path).with_suffix('.segmentation-shapes.json').write_text(json.dumps({'source_sha256':__import__('hashlib').sha256(Path(image_path).read_bytes()).hexdigest(),'notes':shapes,'mask':mask_path.name,'mask_shape':list(mask.shape),'mask_corners_in_input':corners},indent=2),encoding='utf-8')
    from homr_components import export_components
    export_components(image_path,Path(image_path).with_suffix('.segmentation-shapes.json'),mask_path,shapes,corners,__file__,[s.xun_model_path for s in sessions])
    return result
main.detect_single_staffs=capture_detect
geometry_only='--geometry-only' in sys.argv
if geometry_only:sys.argv.remove('--geometry-only')
if force_cpu and not geometry_only:raise ValueError('--geometry-cpu is only supported with --geometry-only')
if len(sys.argv)!=2:raise ValueError('Expected one source image path')
source=str(Path(sys.argv[1]).resolve())
if not Path(source).is_file():raise FileNotFoundError(source)
if geometry_only:
    config=main.ProcessingConfig(False,False,False,False,-1,True,True,False,False)
    capture_detect(source,config)
else:
    sys.argv=['homr',source,'--gpu','force','--no-title']
    main.main()
from collections import Counter
profiles=[]
for session in sessions:
    path=session.end_profiling()
    events=json.loads(Path(path).read_text(encoding='utf-8'))
    counts=Counter(e.get('args',{}).get('provider') for e in events if e.get('cat')=='Node' and e.get('args',{}).get('provider'))
    profiles.append(dict(path=path,nodes=dict(counts)))
Path(source).with_suffix('.providers.json').write_text(json.dumps(profiles,indent=2),encoding='utf-8')
print('PROFILES',profiles,flush=True)

Path(source).with_suffix('.offline.json').write_text(json.dumps(dict(network_blocked=True,attempts=network_attempts)),encoding='utf-8')

module_files={name:str(Path(module.__file__).resolve()) for name,module in list(sys.modules.items()) if getattr(module,'__file__',None)}
external={name:path for name,path in module_files.items() if not Path(path).is_relative_to(root)}
Path(source).with_suffix('.runtime.json').write_text(json.dumps({'executable':sys.executable,'sys_path':sys.path,'homr_module':main.__file__,'external_module_files':external},indent=2),encoding='utf-8')
if external:raise RuntimeError('Loaded external modules: '+str(external))
