from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from source_void_heads import build,apply
source,out=map(Path,sys.argv[1:3]);out.mkdir(exist_ok=True);plan=build(source)
(out/'head-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
view=apply(source,plan,out/'head-source-view')
(out/'head-result.json').write_text(json.dumps(dict(source_view=str(view),retired_heads=len(plan['candidates']),pending=plan['pending'],original_book_sha256=plan['book_sha256']),ensure_ascii=False,indent=2),encoding='utf8')
print('SOURCE_HEAD_PREFLIGHT',len(plan['candidates']),'pending',len(plan['pending']),flush=True)
