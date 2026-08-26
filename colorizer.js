(function(){
var pEls=document.querySelectorAll('.pinyin');
if(!pEls.length)return;
var chEl=document.querySelector('.chinese');

/* Tone detection: char → tone number */
var TM={};
'āēīōūǖ'.split('').forEach(function(c){TM[c]=1;});
'áéíóúǘ'.split('').forEach(function(c){TM[c]=2;});
'ǎěǐǒǔǚ'.split('').forEach(function(c){TM[c]=3;});
'àèìòùǜ'.split('').forEach(function(c){TM[c]=4;});

/* Normalize tone-marked vowels → base vowels for syllable lookup */
var NM={};
'āáǎà'.split('').forEach(function(c){NM[c]='a';});
'ēéěè'.split('').forEach(function(c){NM[c]='e';});
'īíǐì'.split('').forEach(function(c){NM[c]='i';});
'ōóǒò'.split('').forEach(function(c){NM[c]='o';});
'ūúǔù'.split('').forEach(function(c){NM[c]='u';});
'ǖǘǚǜ'.split('').forEach(function(c){NM[c]='ü';});

function norm(s){
	var r='';for(var i=0;i<s.length;i++)r+=NM[s[i]]||s[i];
	return r.toLowerCase();
}
function tone(s){
	for(var i=0;i<s.length;i++)if(TM[s[i]])return TM[s[i]];
	return 5;
}
function isHz(c){
	var x=c.charCodeAt(0);
	return(x>=0x4E00&&x<=0x9FFF)||(x>=0x3400&&x<=0x4DBF)||(x>=0xF900&&x<=0xFAFF);
}
function countHz(s){var n=0;for(var i=0;i<s.length;i++)if(isHz(s[i]))n++;return n;}
function isPy(c){
	var x=c.charCodeAt(0);
	return(x>=65&&x<=90)||(x>=97&&x<=122)||!!NM[c]||c==='ü';
}

/* Complete valid pinyin syllable table (~410 syllables) */
var SY=new Set(('a,o,e,ai,ei,ao,ou,an,en,ang,eng,er,'+
'ba,bo,bi,bu,bai,bei,bao,ban,ben,bang,beng,bian,biao,bie,bin,bing,'+
'pa,po,pi,pu,pai,pei,pao,pou,pan,pen,pang,peng,pian,piao,pie,pin,ping,'+
'ma,mo,me,mi,mu,mai,mei,mao,mou,man,men,mang,meng,mian,miao,mie,min,ming,miu,'+
'fa,fo,fu,fei,fan,fen,fang,feng,fou,'+
'da,de,di,du,dai,dei,dao,dou,dan,den,dang,deng,dong,dia,dian,diao,die,diu,ding,duan,dui,dun,duo,'+
'ta,te,ti,tu,tai,tei,tao,tou,tan,tang,teng,tong,tian,tiao,tie,ting,tuan,tui,tun,tuo,'+
'na,ne,ni,nu,nü,nai,nei,nao,nou,nan,nen,nang,neng,nong,nia,nian,niang,niao,nie,nin,ning,niu,nuan,nuo,nüe,'+
'la,le,li,lu,lü,lai,lei,lao,lou,lan,lang,leng,long,lia,lian,liang,liao,lie,lin,ling,liu,luan,lun,luo,lüe,'+
'ga,ge,gu,gai,gei,gao,gou,gan,gen,gang,geng,gong,gua,guai,guan,guang,gui,gun,guo,'+
'ka,ke,ku,kai,kei,kao,kou,kan,ken,kang,keng,kong,kua,kuai,kuan,kuang,kui,kun,kuo,'+
'ha,he,hu,hai,hei,hao,hou,han,hen,hang,heng,hong,hua,huai,huan,huang,hui,hun,huo,'+
'ji,ju,jia,jian,jiang,jiao,jie,jin,jing,jiong,jiu,juan,jue,jun,'+
'qi,qu,qia,qian,qiang,qiao,qie,qin,qing,qiong,qiu,quan,que,qun,'+
'xi,xu,xia,xian,xiang,xiao,xie,xin,xing,xiong,xiu,xuan,xue,xun,'+
'zha,zhe,zhi,zhu,zhai,zhei,zhao,zhou,zhan,zhen,zhang,zheng,zhong,zhua,zhuai,zhuan,zhuang,zhui,zhun,zhuo,'+
'cha,che,chi,chu,chai,chao,chou,chan,chen,chang,cheng,chong,chuai,chuan,chuang,chui,chun,chuo,'+
'sha,she,shi,shu,shai,shei,shao,shou,shan,shen,shang,sheng,shua,shuai,shuan,shuang,shui,shun,shuo,'+
'ri,ru,re,rao,rou,ran,ren,rang,reng,rong,ruan,rui,run,ruo,'+
'za,ze,zi,zu,zai,zei,zao,zou,zan,zen,zang,zeng,zong,zuan,zui,zun,zuo,'+
'ca,ce,ci,cu,cai,cao,cou,can,cen,cang,ceng,cong,cuan,cui,cun,cuo,'+
'sa,se,si,su,sai,sao,sou,san,sen,sang,seng,song,suan,sui,sun,suo,'+
'ya,yo,ye,yi,yu,yao,you,yan,yin,yang,ying,yong,yuan,yue,yun,'+
'wa,wo,wu,wai,wei,wan,wen,wang,weng').split(','));

/*
 * Segment normalized pinyin into exactly n syllables via backtracking.
 * Scores by fewest vowel-initial syllables (consonant-initial preferred).
 * Returns best split positions [[start,end],...] or null.
 */
function seg(s,n){
	var best=null;
	(function bt(pos,sp){
		if(sp.length===n){
			if(pos===s.length){
				var vi=0;
				for(var i=0;i<sp.length;i++)
					if('aeiouü'.indexOf(s.charAt(sp[i][0]))>=0)vi++;
				if(!best||vi<best.v)best={s:sp.map(function(x){return x.slice();}),v:vi};
			}
			return;
		}
		var rem=n-sp.length,left=s.length-pos;
		if(left<rem||left>rem*6)return;
		for(var len=Math.min(6,left);len>=1;len--){
			if(SY.has(s.substring(pos,pos+len))){
				sp.push([pos,pos+len]);
				bt(pos+len,sp);
				sp.pop();
				if(best&&best.v===0)return;
			}
		}
	})(0,[]);
	return best?best.s:null;
}

var hzN=chEl?countHz(chEl.textContent):0;

pEls.forEach(function(el){
	if(el.dataset.colorized)return;
	var raw=el.textContent;
	if(!raw.trim()){el.dataset.colorized='true';return;}

	/* Strip non-pinyin chars, build original-position map */
	var stripped='',posMap=[];
	for(var i=0;i<raw.length;i++){
		if(isPy(raw[i])){posMap.push(i);stripped+=raw[i];}
	}

	var normed=norm(stripped);
	var splits=hzN>0?seg(normed,hzN):null;

	/* Handle erhua (e.g. yidianr) when standard segmentation fails */
	if(!splits && hzN>1 && normed.charAt(normed.length-1)==='r'){
		var subSplits=seg(normed.slice(0,-1),hzN-1);
		if(subSplits){
			subSplits[subSplits.length-1][1]++; // extend the last syllable to include 'r'
			splits=subSplits;
		}
	}

	if(splits){
		var html='',last=0;
		for(var i=0;i<splits.length;i++){
			var oS=posMap[splits[i][0]];
			var oE=posMap[splits[i][1]-1]+1;
			if(oS>last)html+=raw.substring(last,oS);
			var syl=raw.substring(oS,oE);
			html+='<span class="tone'+tone(syl)+'">'+syl+'</span>';
			last=oE;
		}
		if(last<raw.length)html+=raw.substring(last);
		el.innerHTML=html;
	} else {
		/* Fallback: color each whitespace-separated token by its tone mark */
		el.innerHTML=raw.replace(/\S+/g,function(w){
			return '<span class="tone'+tone(w)+'">'+w+'</span>';
		});
	}
	el.dataset.colorized='true';
});
})();
