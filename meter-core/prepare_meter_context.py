from pathlib import Path
import sys,json
script_directory = Path(__file__).resolve().parent
sys.path[:0] = [str(script_directory), str(script_directory.parent)]
from prepare_source_times import prepare
from prepare_source_barlines import prepare_bars
from prepare_time_conflicts import prepare_conflicts

def build(source,out):
 plan=prepare(source,out);bars=prepare_bars(source,out);count,pending=prepare_conflicts(source,plan,out)
 return dict(times=plan,bars=bars,conflicts=count,conflict_pending=pending)
if __name__=='__main__':
 source=Path(sys.argv[1]);out=Path(sys.argv[2]);validate='--validate' in sys.argv[3:]
 if validate:
  recorded=json.loads((out/'combined-plan.json').read_text(encoding='utf8'));fresh=build(source,out/'revalidated')
  if json.loads(json.dumps(fresh))!=recorded:raise ValueError('source meter plan changed during independent validation')
  for name in ['times.tsv','barlines.tsv','conflicts.tsv']:
   if (out/name).read_bytes()!=(out/'revalidated'/name).read_bytes():raise ValueError('source meter native plan rows changed')
  print('SOURCE_METER_PLAN_INDEPENDENTLY_VALIDATED',flush=True)
 else:
  plan=build(source,out);(out/'combined-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8');print('SOURCE_METER_PLAN_PREPARED',len(plan['times']['rows']),len(plan['bars']),plan['conflicts'],flush=True)
