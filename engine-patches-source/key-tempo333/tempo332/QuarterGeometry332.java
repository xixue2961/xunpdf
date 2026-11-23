package org.audiveris.omr.score;
import java.util.*;
public class QuarterGeometry332 {
 public static boolean accepts(ij.process.ByteProcessor bp){int w=bp.getWidth(),h=bp.getHeight();boolean[][] ink=new boolean[h][w];for(int y=0;y<h;y++)for(int x=0;x<w;x++)ink[y][x]=bp.get(x,y)<128;List<int[]> main=null;int components=0,holes=0;
 for(boolean value:new boolean[]{true,false}){boolean[][] seen=new boolean[h][w];for(int y=0;y<h;y++)for(int x=0;x<w;x++){if(ink[y][x]!=value||seen[y][x])continue;var q=new ArrayDeque<int[]>();var points=new ArrayList<int[]>();boolean edge=false;q.add(new int[]{x,y});seen[y][x]=true;while(!q.isEmpty()){var p=q.remove();points.add(p);int xx=p[0],yy=p[1];edge|=xx==0||yy==0||xx==w-1||yy==h-1;for(int dy=-1;dy<=1;dy++)for(int dx=-1;dx<=1;dx++){int nx=xx+dx,ny=yy+dy;if(nx>=0&&ny>=0&&nx<w&&ny<h&&!seen[ny][nx]&&ink[ny][nx]==value){seen[ny][nx]=true;q.add(new int[]{nx,ny});}}}if(value&&points.size()>=3){components++;main=points;}if(!value&&!edge&&points.size()>=3)holes++;}}
 if(components!=1||holes!=0)return false;int left=main.stream().mapToInt(p->p[0]).min().orElseThrow(),top=main.stream().mapToInt(p->p[1]).min().orElseThrow(),ww=main.stream().mapToInt(p->p[0]).max().orElseThrow()-left+1,hh=main.stream().mapToInt(p->p[1]).max().orElseThrow()-top+1,area=main.size();if(hh<12||(double)hh/ww<1.4||(double)hh/ww>5)return false;int upper=Math.max(1,(int)(hh*.55)),sl=ww,sr=-1;
 for(int x=0;x<ww;x++){int count=0;for(int y=0;y<upper;y++)if(ink[top+y][left+x])count++;if(count>=.75*upper){sl=Math.min(sl,x);sr=Math.max(sr,x);}}
 if(sr<0||sr-sl+1>Math.max(3,.2*ww)||(sl+sr)/2.0<.65*ww||ww-1-sr>Math.max(1,.03*ww))return false;int outside=0;for(int y=0;y<upper;y++)for(int x=0;x<ww;x++)if((x<sl-1||x>sr+1)&&ink[top+y][left+x])outside++;if((double)outside/area>.03)return false;int head=-1;
 for(int y=upper;y<hh;y++){int count=0;for(int x=0;x<ww;x++)if(ink[top+y][left+x])count++;if(count>=.6*ww){head=y;break;}}
 if(head<0)return false;int headH=hh-head,count=0;for(int y=head;y<hh;y++)for(int x=0;x<ww;x++)if(ink[top+y][left+x])count++;double fill=(double)count/(ww*headH);return (double)ww/headH>=1&&(double)ww/headH<=3.5&&fill>=.5&&fill<=.95&&(double)hh/headH>=2;
 }
}
