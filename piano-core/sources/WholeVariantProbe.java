package org.audiveris.omr.score;
import java.nio.file.*;import java.util.*;import javax.imageio.ImageIO;import org.audiveris.omr.glyph.*;import org.audiveris.omr.classifier.*;import org.audiveris.omr.run.*;import ij.process.ByteProcessor;
public class WholeVariantProbe {
 public static void main(String[] a){try{run(a);System.exit(0);}catch(Throwable t){t.printStackTrace();System.exit(1);}}
 public static void run(String[] a)throws Exception {
  ((ch.qos.logback.classic.Logger)org.slf4j.LoggerFactory.getLogger(org.slf4j.Logger.ROOT_LOGGER_NAME)).setLevel(ch.qos.logback.classic.Level.WARN);
  var image=ImageIO.read(Path.of(a[0]).toFile());var classifier=ShapeClassifier.getInstance();var rows=new ArrayList<String>();rows.add("proposal\tx\ty\tw\th\tgrade\tcompetitor\tmargin\tshape");
  for(String line:Files.readAllLines(Path.of(a[1]))){var f=line.split("\t");int x=Integer.parseInt(f[1]),y=Integer.parseInt(f[2]),w=Integer.parseInt(f[3]),h=Integer.parseInt(f[4]),interline=Integer.parseInt(f[5]);double best=-1;String result="";
   for(int dx=-1;dx<=1;dx++)for(int dy=-1;dy<=1;dy++)for(int dw=-2;dw<=1;dw++)for(int dh=-1;dh<=1;dh++){
    if(x+dx<0||y+dy<0||w+dw<1||h+dh<1||x+dx+w+dw>image.getWidth()||y+dy+h+dh>image.getHeight())continue;
    var src=image.getSubimage(x+dx,y+dy,w+dw,h+dh);var crop=new java.awt.image.BufferedImage(w+dw,h+dh,java.awt.image.BufferedImage.TYPE_BYTE_GRAY);var g=crop.createGraphics();g.drawImage(src,0,0,null);g.dispose();var bp=new ByteProcessor(crop);bp.threshold(159);var rt=new RunTableFactory(Orientation.VERTICAL).createTable(bp);var glyph=new Glyph(0,0,rt);var evals=classifier.getNaturalEvaluations(glyph,interline);Arrays.sort(evals,Evaluation.byReverseGrade);
    var top=evals[0];if(top.shape!=Shape.METRO_WHOLE)continue;double competitor=evals.length>1?evals[1].grade:0;
    if(top.grade>best){best=top.grade;result=String.join("\t",f[0],""+(x+dx),""+(y+dy),""+(w+dw),""+(h+dh),""+top.grade,""+competitor,""+(top.grade-competitor),top.shape.name());}
   }
   if(best>=0){rows.add(result);System.out.println("BEST "+result);}else System.out.println("NO_WHOLE "+f[0]);
  }
  Files.write(Path.of(a[2]),rows);System.exit(0);
 }
}
