"""Windows commit-budget checks; no system configuration or process termination."""
import ctypes,os,json,time
from pathlib import Path
GIB=1024**3
class MemoryStatus(ctypes.Structure):
 _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in ('total_phys','avail_phys','total_page','avail_page','total_virtual','avail_virtual','extended')]
def snapshot():
 if os.name!='nt':return {'state':'unavailable'}
 m=MemoryStatus();m.length=ctypes.sizeof(m)
 if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):raise OSError('无法读取系统内存状态')
 return dict(state='measured',physical_total=m.total_phys,physical_available=m.avail_phys,commit_limit=m.total_page,commit_available=m.avail_page)
class MemoryBudgetError(RuntimeError):
 pass

def check_memory(job,phase,reader=snapshot):
 m=reader();p=Path(job)/'memory-budget.json';history=json.loads(p.read_text(encoding='utf8')) if p.exists() else [];history.append(dict(phase=phase,**m));p.write_text(json.dumps(history,ensure_ascii=False,indent=2),encoding='utf8')
 if m.get('state')=='measured' and m['commit_available']<3*GIB:
  raise MemoryBudgetError('系统可用内存额度不足（剩余%.2fGB，识别启动至少需要3GB余量）。请先保存并关闭部分占用内存的软件，再点“继续上次任务”。原PDF和旧结果已保留。'% (m['commit_available']/GIB))
 return m
MEMORY_ERRORS=('java.lang.OutOfMemoryError','insufficient memory for the Java Runtime','Native memory allocation','paging file is too small','页面文件太小')
def is_memory_failure(log):
 return ('MemoryBudgetError:' in log or '系统可用内存额度不足（' in log
         or any(error in log for error in MEMORY_ERRORS))
def explain_failure(log,code):
 if is_memory_failure(log):return '识别因内存不足而中断，未生成新结果。请先保存并关闭部分占用内存的软件，再点“继续上次任务”；原PDF和旧结果已保留。详细日志：'
 return None


def wait_for_released_memory(job,cancel=None,emit=None,timeout=20,poll_interval=.5,reader=snapshot,clock=time.monotonic,*,phase="after_ocr_release"):
 """Allow bounded Windows commit deallocation after stopping this job's OCR.
 Keep the same 3-GiB threshold. Never terminate other processes or change paging.
 """
 if timeout<0 or poll_interval<=0:raise ValueError('invalid memory release wait bounds')
 started=clock();samples=0;announced=False;latest=None
 def finish(state):
  report=dict(state=state,elapsed_seconds=round(max(0,clock()-started),3),samples=samples,threshold_bytes=3*GIB,timeout_seconds=timeout,last_snapshot=latest)
  report_name='memory-release-wait.json' if phase=='after_ocr_release' else phase.replace('_','-')+'.json'
  (Path(job)/report_name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 while True:
  if cancel is not None and cancel.is_set():finish('cancelled');raise InterruptedError('已取消等待本任务内存释放')
  samples+=1
  try:
   latest=check_memory(job,phase,reader=reader)
   finish('recovered' if announced else 'immediate')
   if announced and emit:emit('本任务内存释放已完成，继续已有识别结果的复核')
   return latest
  except MemoryBudgetError:
   # Preserve the authoritative snapshot already written by check_memory.
   history=json.loads((Path(job)/'memory-budget.json').read_text(encoding='utf8'));latest={k:v for k,v in history[-1].items() if k!='phase'}
   remaining=timeout-(clock()-started)
   if remaining<=0:finish('timed_out');raise
   if not announced:
    announced=True
    if emit:emit('等待本任务释放内存，最多'+str(timeout)+'秒；已有识别结果已保留')
   delay=min(poll_interval,remaining)
   if cancel is not None:cancel.wait(delay)
   else:time.sleep(delay)
