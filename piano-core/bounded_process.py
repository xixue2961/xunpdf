"""Bound new diagnostic output without truncating or removing any existing file."""
from pathlib import Path
import threading

def run_bounded(runner,args,log_path,env,cancel,max_log_bytes=16*1024**2,**kwargs):
 log=Path(log_path);finished=threading.Event();exceeded=threading.Event()
 def inspect():
  if log.exists() and log.stat().st_size>max_log_bytes:exceeded.set();cancel.set();return True
  return False
 if inspect():raise RuntimeError('本次识谱日志已超出保护限额，已停止追加任务；原谱、旧结果及日志保留：'+str(log))
 def monitor():
  while not finished.wait(.1):
   if inspect():return
 thread=threading.Thread(target=monitor,daemon=True);thread.start()
 try:
  try:result=runner(args,log,env,cancel,**kwargs)
  except InterruptedError:
   if not exceeded.is_set():raise
   result=None
 finally:finished.set();thread.join(timeout=.5)
 if inspect():raise RuntimeError('识谱调试输出异常，触发日志大小保护并停止本次任务；原谱和旧结果保留：'+str(log))
 return result
