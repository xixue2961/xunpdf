from pathlib import Path
import os,sys,ctypes,traceback
root=Path(__file__).resolve().parent
cache=root/".codex-task-cache"/"temp"
cache.mkdir(parents=True,exist_ok=True)
for key in ("TEMP","TMP","TMPDIR"):os.environ[key]=str(cache)
sys.dont_write_bytecode=True
os.chdir(root)
mutex=ctypes.windll.kernel32.CreateMutexW(None,False,"Local\\XUNpdfPortable523")
if ctypes.windll.kernel32.GetLastError()==183:
 ctypes.windll.user32.MessageBoxW(0,"识谱工具已经打开，请在任务栏切换到 XUN识谱。","XUN识谱",64)
 sys.exit(0)
log=(cache/"gui-startup.log").open("a",encoding="utf8",buffering=1)
sys.stdout=sys.stderr=log
try:
 from gui import launch
 launch()
except Exception:
 traceback.print_exc()
 ctypes.windll.user32.MessageBoxW(0,"启动失败，日志位置："+str(cache/"gui-startup.log"),"XUN识谱",16)
 raise
