import fs from 'node:fs/promises';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const W=1280,H=720;
const C={bg:'#FBFCFA', panel:'#FFFFFF', panel2:'#EEF6F3', white:'#123A35', muted:'#71817D', cyan:'#0A8F7C', cyan2:'#64B8A8', orange:'#E47B45', red:'#C95B55', line:'#DDE8E3'};
const FONT='Aptos';
function addText(slide,text,left,top,width,height,size,color=C.white,bold=false,opts={}){
  const s=slide.shapes.add({geometry:'textbox',position:{left,top,width,height},fill:'none',line:{style:'solid',fill:'none',width:0}});
  s.text=text; s.text.style={fontSize:size,color,bold,fontFamily:FONT, ...opts}; return s;
}
function box(slide,left,top,width,height,fill=C.panel,rad='rounded-xl',line=C.line){
  return slide.shapes.add({geometry:'roundRect',position:{left,top,width,height},fill,line:{style:'solid',fill:line,width:1},borderRadius:rad});
}
function line(slide,x1,y1,x2,y2,color=C.line,width=2){
  const left=Math.min(x1,x2), top=Math.min(y1,y2), widthBox=Math.abs(x2-x1), heightBox=Math.abs(y2-y1);
  return slide.shapes.add({geometry:'line',position:{left,top,width:widthBox,height:heightBox},line:{style:'solid',fill:color,width}});
}
function dot(slide,x,y,r,color){return slide.shapes.add({geometry:'ellipse',position:{left:x-r,top:y-r,width:r*2,height:r*2},fill:color,line:{style:'solid',fill:color,width:0}})}
function header(slide,kicker,title,sub=''){
  addText(slide,kicker.toUpperCase(),64,42,420,24,13,C.cyan,true,{letterSpacing:2});
  addText(slide,title,64,78,960,58,34,C.white,true);
  if(sub) addText(slide,sub,64,140,960,32,16,C.muted,false);
  line(slide,64,184,1216,184,C.line,1);
  addText(slide,'MAXI  /  OVERSEAS DATA COLLECTION',64,684,500,18,11,C.muted,false,{letterSpacing:1.2});
}
function footer(slide,n){addText(slide,String(n).padStart(2,'0'),1160,674,56,22,14,C.cyan,true,{align:'right'});}

async function main(){
 await fs.mkdir('output',{recursive:true});
 const p=Presentation.create({slideSize:{width:W,height:H}});
 // 1 cover
 {const s=p.slides.add();s.background.fill=C.bg;
  addText(s,'MAXI',70,64,300,60,46,C.white,true,{letterSpacing:1});
  addText(s,'海外数据采集进展',70,172,760,78,50,C.white,true);
  addText(s,'Overseas Data Collection  |  Client Briefing',74,268,690,32,20,C.cyan2);
  addText(s,'东南亚 · 独联体 · 欧洲 · 拉美',74,340,660,30,18,C.muted);
  // abstract route
  line(s,785,150,1110,225,C.cyan,4); line(s,1110,225,1010,390,C.cyan,4); line(s,1010,390,1150,505,C.orange,4); line(s,785,150,1010,390,C.line,2);
  [[785,150,C.cyan],[1110,225,C.cyan],[1010,390,C.cyan],[1150,505,C.orange]].forEach(([x,y,c])=>dot(s,x,y,10,c));
  addText(s,'DATA COVERAGE\n& NEXT HORIZON',840,555,310,60,18,C.white,true,{lineSpacing:1.2});
  addText(s,'客户沟通版  |  2026',74,650,300,20,13,C.muted);
 }
 // 2 executive overview
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'01 / OVERVIEW','海外采集网络正在从区域覆盖走向规模化扩张','当前以东南亚为核心，向独联体、欧洲与拉美延伸，下一阶段进入澳新与中东');
  box(s,64,230,360,250,C.panel); addText(s,'当前覆盖',92,258,200,24,16,C.cyan,true); addText(s,'4',92,300,120,68,56,C.white,true); addText(s,'大区',216,334,110,28,18,C.muted); addText(s,'泰国、马来西亚\n独联体、欧洲、拉美',92,400,250,52,18,C.white);
  box(s,460,230,360,250,C.panel); addText(s,'数据规模',488,258,200,24,16,C.cyan,true); addText(s,'[累计公里数]',488,300,280,60,38,C.white,true); addText(s,'采集里程，待补充实际口径',488,390,280,28,15,C.muted);
  box(s,856,230,360,250,C.panel); addText(s,'下一阶段',884,258,200,24,16,C.orange,true); addText(s,'2',884,300,120,68,56,C.orange,true); addText(s,'个方向',1008,334,120,28,18,C.muted); addText(s,'澳洲与新西兰\n中东地区',884,400,250,52,18,C.white);
  addText(s,'对客户的意义',64,548,180,24,15,C.cyan,true); addText(s,'覆盖范围持续扩大，跨区域数据能力逐步成型，可支撑更多海外场景验证与交付。',64,580,980,34,22,C.white,true);footer(s,2);
 }
 // 3 map-like footprint
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'02 / CURRENT FOOTPRINT','当前海外采集版图','已形成多区域并行推进的采集结构');
  // stylized world field
  box(s,64,220,1152,390,'#EAF2F0','rounded-2xl',C.line);
  addText(s,'EUROPE',148,280,170,25,14,C.muted,true,{letterSpacing:1.5}); addText(s,'CIS',510,300,120,25,14,C.muted,true,{letterSpacing:1.5}); addText(s,'SOUTHEAST ASIA',792,470,220,25,14,C.muted,true,{letterSpacing:1.5}); addText(s,'LATAM',214,500,140,25,14,C.muted,true,{letterSpacing:1.5});
  line(s,280,315,530,320,C.cyan,3); line(s,530,320,850,470,C.cyan,3); line(s,260,515,850,470,C.cyan,3);
  [[280,315,'欧洲','已采集 / 持续扩展',C.cyan],[530,320,'独联体','已采集 / 持续扩展',C.cyan],[850,470,'东南亚','泰国 · 马来西亚',C.cyan],[260,515,'拉美','已采集 / 持续扩展',C.cyan]].forEach(([x,y,t,sub,c])=>{dot(s,x,y,12,c);addText(s,t,x+20,y-18,160,24,19,C.white,true);addText(s,sub,x+20,y+10,190,22,13,C.muted);});
  addText(s,'地图为业务区域示意，具体公里数与城市清单可在数据确认后补齐。',84,630,700,22,12,C.muted);footer(s,3);
 }
 //4 SEA detail
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'03 / SOUTHEAST ASIA','东南亚是当前海外采集的核心区域','泰国与马来西亚构成第一阶段重点，适合持续沉淀高质量道路与场景数据');
  addText(s,'THAILAND',80,242,280,28,16,C.cyan,true,{letterSpacing:2}); addText(s,'泰国',80,280,260,48,38,C.white,true); addText(s,'当前采集公里数',80,352,220,22,14,C.muted); addText(s,'[   ] km',80,382,260,54,38,C.cyan,true); line(s,80,465,560,465,C.line,2); addText(s,'重点方向',80,492,130,22,14,C.muted);addText(s,'城市道路、干线道路、典型交通场景',80,522,420,26,18,C.white);
  addText(s,'MALAYSIA',680,242,280,28,16,C.orange,true,{letterSpacing:2}); addText(s,'马来西亚',680,280,300,48,38,C.white,true); addText(s,'当前采集公里数',680,352,220,22,14,C.muted); addText(s,'[   ] km',680,382,260,54,38,C.orange,true); line(s,680,465,1160,465,C.line,2); addText(s,'重点方向',680,492,130,22,14,C.muted);addText(s,'城市道路、复杂路口、跨区域补采',680,522,420,26,18,C.white);
  addText(s,'两国累计',80,620,100,20,14,C.muted);addText(s,'[   ] km',190,606,190,36,28,C.white,true);addText(s,'数据口径：以已完成质检的有效采集里程为准',420,620,500,20,13,C.muted);footer(s,4);
 }
 //5 other regions
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'04 / REGIONAL PROGRESS','其他区域持续推进，形成多点采集能力','独联体、欧洲与拉美承担区域扩展与场景多样性补充');
  const items=[['独联体','CIS','[   ] km','寒冷气候 / 长距离道路 / 城市复杂交通',C.cyan],['欧洲','EUROPE','[   ] km','高密度城市 / 多样道路规则 / 典型场景',C.cyan2],['拉美','LATAM','[   ] km','城市道路 / 高变化交通环境 / 区域差异',C.orange]];
  items.forEach((it,i)=>{const x=80+i*380;box(s,x,240,320,300,C.panel);addText(s,it[1],x+28,270,220,22,13,it[4],true,{letterSpacing:1.5});addText(s,it[0],x+28,310,220,38,30,C.white,true);addText(s,it[2],x+28,382,240,46,34,it[4],true);line(s,x+28,455,x+292,455,C.line,2);addText(s,it[3],x+28,480,260,54,15,C.muted);});
  addText(s,'区域采集策略：按客户需求、道路类型与数据质量目标灵活组合。',80,604,840,26,19,C.white,true);footer(s,5);
 }
 //6 data value
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'05 / DATA VALUE','从“里程覆盖”到“可交付的数据资产”','采集规模只是基础，质量、可复用性与交付效率决定客户价值');
  const xs=[80,380,680,980];const ts=[['覆盖范围','多区域持续扩展','覆盖更多海外道路与场景',C.cyan],['数据质量','有效里程沉淀','经过质检，支持后续使用',C.cyan2],['场景丰富度','道路类型更完整','城市、干线与复杂路口并行',C.orange],['交付能力','按项目灵活组织','支持区域化、阶段化交付',C.white]];
  ts.forEach((it,i)=>{dot(s,xs[i]+8,270,8,it[3]);addText(s,it[0],xs[i]+28,256,210,24,16,it[3],true);addText(s,it[1],xs[i],320,240,54,24,C.white,true);addText(s,it[2],xs[i],400,230,60,16,C.muted);if(i<3)line(s,xs[i]+230,280,xs[i]+280,280,C.line,2);});
  box(s,80,520,1040,70,'#EAF2F0','rounded-xl',C.line);addText(s,'客户可获得',108,540,130,24,15,C.cyan,true);addText(s,'更广的区域覆盖 + 更丰富的道路场景 + 更明确的项目交付边界',260,536,780,30,21,C.white,true);footer(s,6);
 }
 //7 roadmap
 {const s=p.slides.add();s.background.fill=C.bg;header(s,'06 / NEXT HORIZON','下一步规划：澳新与中东','在现有区域经验基础上，向新的气候、道路与交通环境扩展');
  line(s,140,400,1110,400,C.line,4); const nodes=[{x:180,tag:'NOW',title:'东南亚',sub:'泰国 · 马来西亚',c:C.cyan},{x:450,tag:'NEXT',title:'澳洲与新西兰',sub:'规划启动',c:C.orange},{x:740,tag:'NEXT',title:'中东地区',sub:'区域评估与规划',c:C.orange},{x:1030,tag:'SCALE',title:'多区域协同',sub:'按客户需求扩展',c:C.cyan2}];nodes.forEach(n=>{dot(s,n.x,400,14,n.c);addText(s,n.tag,n.x-45,320,100,22,12,n.c,true,{align:'center',letterSpacing:1.3});addText(s,n.title,n.x-100,440,200,28,20,C.white,true,{align:'center'});addText(s,n.sub,n.x-115,480,230,24,14,C.muted,{align:'center'});});
  addText(s,'规划原则',80,575,120,22,14,C.cyan,true);addText(s,'优先覆盖客户重点市场，再根据道路类型、采集条件与质量要求分阶段推进。',220,570,880,30,19,C.white,true);footer(s,7);
 }
 //8 close
 {const s=p.slides.add();s.background.fill=C.bg;addText(s,'MAXI',70,70,220,46,36,C.white,true);addText(s,'海外数据采集能力',70,190,780,72,52,C.white,true);addText(s,'正在形成覆盖更多区域、服务更多客户的长期数据基础设施。',74,290,850,34,22,C.cyan2);line(s,74,370,1120,370,C.line,2);addText(s,'可进一步补充的信息',74,420,240,24,16,C.cyan,true);addText(s,'各区域有效公里数\n采集城市与道路类型\n客户项目与交付案例',74,470,430,120,24,C.white,false,{lineSpacing:1.5});addText(s,'THANK YOU',880,560,250,38,26,C.orange,true,{letterSpacing:2});addText(s,'MAXI  /  CLIENT BRIEFING',882,610,270,20,12,C.muted,true,{letterSpacing:1.2});footer(s,8); }
 const out='output/maxi_overseas_data_collection_editorial_v3.pptx';
 const pptx=await PresentationFile.exportPptx(p); await pptx.save(out);
 for (const [i,sl] of p.slides.items.entries()){const png=await p.export({slide:sl,format:'png',scale:1}); await fs.writeFile(`output/slide-${String(i+1).padStart(2,'0')}.png`,new Uint8Array(await png.arrayBuffer()));}
 console.log(out);
}
main().catch(e=>{console.error(e);process.exitCode=1});
